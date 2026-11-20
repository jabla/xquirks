"""The device check (the integration page's "Check a device") and the self-check after an install.

The devices here have clusters that send values the way zigpy does (cluster events, the Tuya MCU
cluster's commands) and ZHA entities that read them: what the listeners see. A test at the end does the
same with a real quirk file on a zigpy device and a ZHA device built from it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.entity import EntityCategory
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import async_fire_time_changed, flush_store

from . import integration
from .conftest import REPOS, STRINGS, Zha, apply_list, check_device_check_texts, continue_check, issues_of, manager_of, setup_entry, write_repo
from .integration import PACKAGE, ROOT, const, diagnostics
from .test_content import restart_with

checks = __import__(f"{PACKAGE}.checks", fromlist=["checks"])
plausible = __import__(f"{PACKAGE}.plausible", fromlist=["plausible"])
watch_module = __import__(f"{PACKAGE}.watch", fromlist=["watch"])
text = integration.text

IEEE_A = "00:11:22:33:44:55:66:01"
IEEE_C = "00:11:22:33:44:55:66:03"
TUYA_A = "z2m_tuya_a:TuyaA"
ACME_C = "z2m_acme_c:AcmeC"
FROZEN = Path(__file__).resolve().parent / "fixtures"
FORMS = ROOT / ".github" / "ISSUE_TEMPLATE"
# The device's name in Home Assistant and a name the user gave an entity: neither may leave Home Assistant.
NAME = "Radar in the kitchen"


class ZigpyDevice:
    """A zigpy device as the listeners see it: its endpoints, and the listeners zigpy calls with
    device_last_seen_updated first thing for each frame from it (zigpy.device.Device.packet_received),
    before the frame reaches a cluster."""

    def __init__(self, endpoints: dict) -> None:
        self.endpoints = endpoints
        self.listeners: list = []
        for endpoint in endpoints.values():
            for cluster in (getattr(endpoint, "in_clusters", None) or {}).values():
                cluster.device = self

    def add_listener(self, listener) -> None:
        self.listeners.append(listener)

    def remove_listener(self, listener) -> None:
        self.listeners.remove(listener)

    def frame_comes(self) -> None:
        for listener in list(self.listeners):
            listener.device_last_seen_updated(None)


def zigpy_device(*clusters: "Cluster") -> ZigpyDevice:
    """A zigpy device with these clusters on their endpoints."""
    endpoints: dict = {0: SimpleNamespace()}
    for cluster in clusters:
        endpoints.setdefault(cluster.endpoint.endpoint_id, SimpleNamespace(in_clusters={})).in_clusters[cluster.cluster_id] = cluster
    return ZigpyDevice(endpoints)


class Cluster:
    """A cluster of a zigpy device as the listeners see it: it sends attribute events and, as a Tuya
    MCU cluster (handled: the datapoints its quirk handles), the datapoints of its commands. A value or
    a frame from the device comes after the device's last_seen update, as zigpy hands it on."""

    # The ZigpyDevice it is on.
    device: ZigpyDevice | None = None

    def __init__(self, cluster_id: int, endpoint: int = 1, handled: tuple[int, ...] | None = None) -> None:
        self.cluster_id = cluster_id
        self.endpoint = SimpleNamespace(endpoint_id=endpoint)
        self.events: dict[str, list] = {}
        self.listeners: list = []
        self.sent: list[int] = []
        self.reads: list[tuple[list[str], bool]] = []
        # What the device sends when it gets a data query (a callable).
        self.answer = None
        if handled is not None:
            from zhaquirks.tuya.mcu import TuyaMCUCluster

            self.server_commands = TuyaMCUCluster.server_commands
            self.data_point_handlers = dict.fromkeys(handled, "_dp_2_attr_update")

    def on_event(self, name: str, callback):
        self.events.setdefault(name, []).append(callback)
        return lambda: self.events[name].remove(callback)

    def add_listener(self, listener) -> None:
        self.listeners.append(listener)

    def remove_listener(self, listener) -> None:
        self.listeners.remove(listener)

    def send(self, attribute: str, event: str = "attribute_updated", *, by_home_assistant: bool = False, in_frame: bool = False) -> None:
        """A value of the attribute from the device, or (by_home_assistant) one Home Assistant set itself, such as a
        write to a LocalDataCluster: zigpy sends the same event then, without a frame from the device. in_frame: a
        value of the frame that came last (frame)."""
        if not by_home_assistant and not in_frame and self.device is not None:
            self.device.frame_comes()
        for callback in list(self.events.get(event, [])):
            callback(SimpleNamespace(endpoint_id=self.endpoint.endpoint_id, cluster_id=self.cluster_id, attribute_name=attribute, attribute_id=0))

    def frame(self, *dps: int, updates: tuple[str, ...] = ()) -> None:
        """A Tuya frame with these datapoints: zigpy hands it to the cluster, whose quirk updates these attributes, then
        tells the listeners about the command."""
        if self.device is not None:
            self.device.frame_comes()
        for attribute in updates:
            self.send(attribute, in_frame=True)
        args = SimpleNamespace(data=SimpleNamespace(datapoints=[SimpleNamespace(dp=dp) for dp in dps]))
        for listener in list(self.listeners):
            listener.cluster_command(1, 2, args)

    def refuse(self, dp: int, reason: str, device: str = "0x1234") -> None:
        """zhaquirks skips a datapoint of this cluster: its warning, with zigpy's cluster prefix (zigpy.zcl Cluster.log)."""
        logging.getLogger("zigpy.zcl").warning("[%s:%s:0x%04x] Ignoring invalid datapoint %s (%s, raw %s): %s", device, self.endpoint.endpoint_id,
                                               self.cluster_id, dp, "TuyaDPType.STRING", "78", ValueError(reason))

    async def command(self, command_id: int, *args, **kwargs) -> None:
        self.sent.append(command_id)
        if self.answer is not None:
            self.answer()

    async def read_attributes(self, names, allow_cache: bool = False, **kwargs):
        self.reads.append((list(names), allow_cache))
        for name in names:
            self.send(name, "attribute_read")
        return {}, {}

    @property
    def listening(self) -> int:
        return sum(len(callbacks) for callbacks in self.events.values()) + len(self.listeners)


# zhaquirks' cluster classes as the listeners know them: by name.
class CustomCluster(Cluster):
    pass


class LocalDataCluster(CustomCluster):
    pass


class LiveDevice:
    """A ZHA device with a zigpy device whose clusters send values, ZHA entities that read them and
    the matching Home Assistant entities (registry and states).

    entities: (domain, unique id suffix, cluster, attribute, category, declared by the quirk's metadata). An
    entity without an attribute (None) reads its whole cluster, as ZHA's climate, cover and light do."""

    def __init__(self, hass: HomeAssistant, zha: Zha, ieee: str, manufacturer: str, model: str, entities: list[tuple], name: str = NAME) -> None:
        self.hass, self.zha, self.ieee = hass, zha, ieee
        self.clusters = {cluster.cluster_id: cluster for _domain, _suffix, cluster, *_rest in entities}
        self.zigpy = self._zigpy()
        self.entities = {}
        declared = []
        for domain, suffix, cluster, attribute, _category, from_metadata in entities:
            unique_id = f"{ieee}-{cluster.endpoint.endpoint_id}-{suffix}"
            entity = SimpleNamespace(unique_id=unique_id, PLATFORM=domain, _cluster=cluster)
            if attribute is not None:
                entity._attribute_name = attribute
            self.entities[(domain, unique_id)] = entity
            if from_metadata:
                declared.append(SimpleNamespace(endpoint_id=cluster.endpoint.endpoint_id, resolved_unique_id_suffix=suffix))
        self.metadata = SimpleNamespace(entity_metadata=declared)
        zha.live[ieee] = self
        self.device_id = zha.add_device(ieee, manufacturer, model, name)
        registry = er.async_get(hass)
        self.entity_ids = {}
        for domain, suffix, _cluster, attribute, category, _declared in entities:
            entry = registry.async_get_or_create(
                domain, "zha", f"{ieee}-1-{suffix}", device_id=self.device_id, original_name=(attribute or suffix).replace("_", " ").capitalize(),
                entity_category=EntityCategory.CONFIG if category == "config" else None, suggested_object_id=f"kitchen_{suffix}",
            )
            self.entity_ids[suffix] = entry.entity_id

    def _zigpy(self) -> ZigpyDevice:
        return zigpy_device(*self.clusters.values())

    def renew(self) -> None:
        """ZHA made a new zigpy device for it (a re-interview): new objects, same clusters."""
        self.zigpy = self._zigpy()
        self.zha._publish()

    def state(self, suffix: str, value: str, **attributes) -> None:
        self.hass.states.async_set(self.entity_ids[suffix], value, attributes)

    def value(self, suffix: str, value: str, **attributes) -> None:
        """The device sends a value: Home Assistant shows it, the cluster reports it."""
        self.state(suffix, value, **attributes)
        cluster, attribute = next((e._cluster, e._attribute_name) for e in self.entities.values() if e.unique_id.endswith(f"-{suffix}"))
        cluster.send(attribute)


def radar(hass: HomeAssistant, zha: Zha, handled=(1, 2, 3)) -> LiveDevice:
    """A Tuya device z2m_tuya_a.py is for: three entities on its Tuya MCU cluster, a setting among them."""
    tuya = CustomCluster(0xEF00, handled=handled)
    return LiveDevice(hass, zha, IEEE_A, "_TZE200_aaaa", "TS0601", [
        ("sensor", "temperature", tuya, "temperature", None, True),
        ("sensor", "humidity", tuya, "humidity", None, True),
        ("number", "max_temp", tuya, "max_temp", "config", True),
    ])


def acme(hass: HomeAssistant, zha: Zha) -> LiveDevice:
    """A ZCL device z2m_acme_c.py is for: a value on a cluster the quirk adds, one on a local cluster, one of ZHA's own."""
    added, local, basic = CustomCluster(0x0402), LocalDataCluster(0xFC00), Cluster(0x0000)
    return LiveDevice(hass, zha, IEEE_C, "Acme", "C-1", [
        ("sensor", "temperature", added, "measured_value", None, False),
        ("select", "mode", local, "mode", "config", True),
        ("sensor", "rssi", basic, "rssi", None, False),
    ])


async def settle(hass: HomeAssistant) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=const.REFRESH_COOLDOWN + 1))
    await hass.async_block_till_done(wait_background_tasks=True)


async def installed(hass: HomeAssistant, zha: Zha, repo_dir: Path, quirks: dict[str, str]) -> None:
    """The change list installed the files, ZHA loaded them: the self-checks start."""
    await setup_entry(hass, repo_dir)
    await apply_list(hass)
    zha.reload(quirks)
    await settle(hass)


async def watch_for(hass: HomeAssistant, freezer, hours: float) -> None:
    """Time passes with ZHA running: the self-check counts it."""
    steps = int(hours * 3600 / checks.TICK.total_seconds())
    for _ in range(steps):
        freezer.tick(checks.TICK)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=checks.VALUE_DELAY + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


def link_fields(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


def form_ids(name: str) -> set[str]:
    return {field["id"] for field in yaml.safe_load((FORMS / name).read_text())["body"] if "id" in field}


# The self-check


async def test_a_tuya_file_that_reads_wrong_gets_a_looks_off_notice(hass: HomeAssistant, zha: Zha, repo_dir: Path, freezer):
    device = radar(hass, zha)
    tuya = device.clusters[0xEF00]
    # The device answers the data query after the install with its datapoints, one the file does not handle.
    tuya.answer = lambda: tuya.frame(1, 2, 3, 101)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    assert tuya.sent == [const.TUYA_QUERY_DATA]
    # Temperature far too high (a wrong scale), the setting in range, humidity never comes.
    device.value("temperature", "3276.7", device_class="temperature", unit_of_measurement="°C")
    device.value("max_temp", "35", min=5, max=45)
    await watch_for(hass, freezer, 25)
    (issue,) = issues_of(hass, "self_check_off")
    placeholders = issue.translation_placeholders
    assert placeholders["file"] == "z2m_tuya_a.py" and placeholders["revision"] == manager_of(hass).data["installed"][next(iter(manager_of(hass).data["installed"]))]["revision"]
    assert placeholders["devices"].splitlines() == [
        f"- {NAME} (\\_TZE200\\_aaaa TS0601)",
        "    - sensor: Temperature: 3276.7 °C is outside -50 to 150 °C",
        "    - sensor: Humidity: no value from the device, although it answered the data query",
        "    - sent Tuya datapoints this file does not show: 101",
    ]
    assert issue.is_persistent and issue.is_fixable
    fields = link_fields(placeholders["link"])
    assert fields["template"] == text.PROBLEM_FORM and set(fields) - {"template", "title"} <= form_ids(text.PROBLEM_FORM)
    assert fields["title"] == "Tuya A: " and fields["file"] == "quirks/z2m_tuya_a.py"
    assert "_TZE200_aaaa TS0601: sensor: Temperature: 3276.7 °C is outside -50 to 150 °C" in fields["problem"].replace("\\", "")
    assert "kitchen" not in placeholders["link"].lower() and IEEE_A not in placeholders["link"]
    result = await diagnostics.async_get_config_entry_diagnostics(hass, hass.config_entries.async_entries(const.DOMAIN)[0])
    found = result["self_check"]["z2m_tuya_a.py"]
    assert found["result"] == "off" and found["devices"][0]["device"] == "_TZE200_aaaa TS0601" and found["devices"][0]["unmapped_datapoints"] == [101]
    assert "kitchen" not in str(result).lower() and IEEE_A not in str(result)


REFUSAL = 'TypeError("CharacterString \'x\' for humidity (uint16_t)")'


async def test_a_datapoint_the_tuya_cluster_refused_is_a_finding_not_a_missing_value(hass: HomeAssistant, zha: Zha, repo_dir: Path, freezer):
    """zhaquirks skips a datapoint the typed Tuya cluster refuses, with a warning. The self-check records it for
    the device's Tuya cluster and reports it as a finding: the notice looks off and does not blame the device for a value
    it sent. Another device's warning is not this one's."""
    device = radar(hass, zha)
    tuya = device.clusters[0xEF00]
    tuya.endpoint.device = SimpleNamespace(name="0x1234")
    tuya.endpoint.tuya_manufacturer = tuya
    tuya._dp_to_attributes = {2: [SimpleNamespace(endpoint_id=None, ep_attribute="tuya_manufacturer", attribute_name="humidity")]}
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    device.value("temperature", "21.5", device_class="temperature", unit_of_measurement="°C")
    tuya.refuse(2, REFUSAL)
    tuya.refuse(3, "ValueError('x')", device="0x9999")
    await watch_for(hass, freezer, 25)
    (issue,) = issues_of(hass, "self_check_off")
    assert issue.translation_placeholders["devices"].splitlines() == [
        f"- {NAME} (\\_TZE200\\_aaaa TS0601)",
        f"    - sent Tuya datapoint 2, refused: {integration.text.plain(REFUSAL)}",
    ]
    result = await diagnostics.async_get_config_entry_diagnostics(hass, hass.config_entries.async_entries(const.DOMAIN)[0])
    assert result["self_check"]["z2m_tuya_a.py"]["devices"][0]["refused_datapoints"] == {"2": REFUSAL}
    # The listener on zigpy's cluster log goes with the last watch.
    await hass.config_entries.async_unload(hass.config_entries.async_entries(const.DOMAIN)[0].entry_id)
    assert integration.watch.REFUSALS not in logging.getLogger("zigpy.zcl").handlers


async def test_an_entity_that_reads_the_cluster_of_a_refused_datapoint_is_not_called_silent(hass: HomeAssistant, zha: Zha, repo_dir: Path, freezer):
    """A datapoint refused for the local temperature of the thermostat cluster. ZHA's climate entity reads that
    cluster as a whole: the refusal is the finding, the entity is not listed as getting no value from the device."""
    tuya, thermostat = CustomCluster(0xEF00, handled=(1, 24)), CustomCluster(0x0201)
    device = LiveDevice(hass, zha, IEEE_A, "_TZE200_aaaa", "TS0601", [("sensor", "temperature", tuya, "temperature", None, True),
                                                                       ("climate", "thermostat", thermostat, None, None, False)])
    tuya.endpoint.device = SimpleNamespace(name="0x1234")
    tuya.endpoint.thermostat = thermostat
    tuya._dp_to_attributes = {24: [SimpleNamespace(endpoint_id=None, ep_attribute="thermostat", attribute_name="local_temperature")]}
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    device.value("temperature", "21.5", device_class="temperature", unit_of_measurement="°C")
    device.state("thermostat", "heat")
    tuya.refuse(24, REFUSAL)
    await watch_for(hass, freezer, 25)
    (issue,) = issues_of(hass, "self_check_off")
    assert issue.translation_placeholders["devices"].splitlines() == [
        f"- {NAME} (\\_TZE200\\_aaaa TS0601)",
        f"    - sent Tuya datapoint 24, refused: {integration.text.plain(REFUSAL)}",
    ]


async def test_values_that_fit_get_a_looks_fine_notice_that_stays_ignored(hass: HomeAssistant, zha: Zha, repo_dir: Path, freezer):
    device = radar(hass, zha)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    device.value("temperature", "21.5", device_class="temperature", unit_of_measurement="°C")
    device.value("humidity", "48", device_class="humidity", unit_of_measurement="%")
    await watch_for(hass, freezer, 25)
    (issue,) = issues_of(hass, "self_check_fine")
    assert issue.translation_placeholders["devices"] == f"- {NAME} (\\_TZE200\\_aaaa TS0601)"
    assert "link" not in issue.translation_placeholders
    ir.async_ignore_issue(hass, const.DOMAIN, issue.issue_id, True)
    # Neither a refresh nor a restart brings it back.
    await manager_of(hass).async_refresh()
    await restart_with(hass, entry, repo_dir)
    await watch_for(hass, freezer, 25)
    assert issues_of(hass, "self_check_fine") == []
    assert ir.async_get(hass).async_get_issue(const.DOMAIN, issue.issue_id).dismissed_version is not None
    # An update of the file: the notice of the old revision goes, a new self-check starts.
    write_repo(repo_dir, version=2)
    REPOS[repo_dir].publish()
    await manager_of(hass).async_check()
    await apply_list(hass)
    zha.reload()
    await settle(hass)
    assert ir.async_get(hass).async_get_issue(const.DOMAIN, issue.issue_id) is None
    assert manager_of(hass).summary()["self_check"]["z2m_tuya_a.py"]["result"] == "watching"


@pytest.mark.parametrize("shown", ["unknown", "fast"])
async def test_a_value_that_shows_as_unknown_is_no_evidence(hass: HomeAssistant, zha: Zha, repo_dir: Path, freezer, shown: str):
    """The device sends a mode the file's select does not know: Home Assistant shows unknown (SelectEntity.state). That is
    no value that looked right but something off. A mode it knows is the evidence."""
    device = acme(hass, zha)
    await installed(hass, zha, repo_dir, {IEEE_C: ACME_C})
    device.value("mode", shown, options=["slow", "fast"])
    await watch_for(hass, freezer, 25)
    silent = "    - no value from the device for sensor: Measured value"
    if shown == "unknown":
        assert issues_of(hass, "self_check_fine") == []
        (issue,) = issues_of(hass, "self_check_off")
        assert issue.translation_placeholders["devices"].splitlines()[1:] == [f"    - select: Mode: {checks.SHOWS_UNKNOWN}", silent]
    else:
        assert issues_of(hass, "self_check_off") == []
        (issue,) = issues_of(hass, "self_check_fine")
        assert issue.translation_placeholders["devices"].splitlines()[1:] == [silent]
        assert self_check(hass, "z2m_acme_c.py")["devices"][0]["findings"] == []


def valve(hass: HomeAssistant, zha: Zha) -> LiveDevice:
    """A ZCL device z2m_acme_c.py is for: a temperature and a write button on a cluster the quirk adds. The button's
    attribute is one it writes (ZHA's WriteAttributeButton names it as an entity that reads one does)."""
    added = CustomCluster(0x0402)
    return LiveDevice(hass, zha, IEEE_C, "Acme", "C-1", [
        ("sensor", "temperature", added, "measured_value", None, False),
        ("button", "self_test", added, "self_test", "config", True),
    ])


@pytest.mark.parametrize("pressed", [False, True])
async def test_a_button_is_neither_a_finding_nor_evidence(hass: HomeAssistant, zha: Zha, repo_dir: Path, freezer, pressed: bool):
    """A button's state is when it was last pressed, unknown until then: it shows no value of the device. The device
    reports the attribute the button writes. A button nobody pressed is nothing off, and one somebody pressed is no
    value that looked right: without a value of the temperature the file stays unproven."""
    device = valve(hass, zha)
    await installed(hass, zha, repo_dir, {IEEE_C: ACME_C})
    device.value("self_test", "2026-11-17T10:00:00+00:00" if pressed else "unknown")
    await watch_for(hass, freezer, 25)
    assert issues_of(hass, "self_check_off") == issues_of(hass, "self_check_fine") == []
    (issue,) = issues_of(hass, "self_check_unproven")
    assert issue.translation_placeholders["devices"].splitlines()[1:] == ["    - no value from the device for sensor: Measured value"]
    assert self_check(hass, "z2m_acme_c.py")["devices"][0]["findings"] == []


async def test_datapoints_count_as_the_answer_only_within_seconds_of_the_query(hass: HomeAssistant, zha: Zha, repo_dir: Path, freezer):
    """The device missed the data query and reports on its own five minutes later: no answer, so the
    humidity that never came is a note, not a problem. The watch and the query read the same clock."""
    device = radar(hass, zha)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    assert device.clusters[0xEF00].sent == [const.TUYA_QUERY_DATA]
    freezer.tick(timedelta(minutes=5))
    device.clusters[0xEF00].frame(1)
    device.value("temperature", "21.5", device_class="temperature", unit_of_measurement="°C")
    await watch_for(hass, freezer, 25)
    assert issues_of(hass, "self_check_off") == []
    (issue,) = issues_of(hass, "self_check_fine")
    assert issue.translation_placeholders["devices"].splitlines()[1:] == ["    - no value from the device for sensor: Humidity"]
    assert manager_of(hass).summary()["self_check"]["z2m_tuya_a.py"]["devices"][0]["answered_the_data_query"] is False


async def test_the_answer_to_a_query_delivered_after_a_retry_counts(hass: HomeAssistant, zha: Zha, repo_dir: Path, freezer):
    """zigpy needs a retry of seconds before a sleepy device gets the data query: its answer right after the delivery
    counts, so a value that still does not come is a finding, not a note."""
    device = radar(hass, zha)
    tuya = device.clusters[0xEF00]

    async def delivered_after_a_retry(command_id: int, *args, **kwargs):
        tuya.sent.append(command_id)
        freezer.tick(timedelta(seconds=12))

    tuya.command = delivered_after_a_retry
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    assert tuya.sent == [const.TUYA_QUERY_DATA]
    freezer.tick(timedelta(seconds=0.3))
    tuya.frame(1)
    device.value("temperature", "21.5", device_class="temperature", unit_of_measurement="°C")
    await watch_for(hass, freezer, 25)
    assert manager_of(hass).summary()["self_check"]["z2m_tuya_a.py"]["devices"][0]["answered_the_data_query"] is True
    (issue,) = issues_of(hass, "self_check_off")
    assert issue.translation_placeholders["devices"].splitlines()[1:] == ["    - sensor: Humidity: no value from the device, although it answered the data query"]


def map_datapoints(tuya: Cluster, mapping: dict[int, str]) -> None:
    """The Tuya cluster's datapoint table as zhaquirks keeps it (_dp_to_attributes): datapoint -> an attribute of itself."""
    tuya.endpoint.tuya_cluster = tuya
    tuya._dp_to_attributes = {dp: [SimpleNamespace(endpoint_id=None, ep_attribute="tuya_cluster", attribute_name=name)] for dp, name in mapping.items()}


@pytest.fixture
def quiet_cluster_log():
    """Home Assistant's logger settings put zigpy's cluster log above warnings: zhaquirks' refusals are not logged."""
    logger = logging.getLogger("zigpy.zcl")
    level = logger.level
    logger.setLevel(logging.ERROR)
    yield
    logger.setLevel(level)


async def test_a_refusal_the_log_does_not_tell_shows_in_the_frame(hass: HomeAssistant, zha: Zha, repo_dir: Path, freezer, quiet_cluster_log):
    """The typed Tuya cluster refuses the humidity while the log is quiet: the frame brought datapoint 2, whose attribute it
    did not reach. A refusal without a reason, not an entity that got no value."""
    device = radar(hass, zha)
    tuya = device.clusters[0xEF00]
    map_datapoints(tuya, {1: "temperature", 2: "humidity", 3: "max_temp"})
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    freezer.tick(timedelta(minutes=5))
    device.state("temperature", "21.5", device_class="temperature", unit_of_measurement="°C")
    tuya.frame(1, 2, updates=("temperature",))
    await watch_for(hass, freezer, 25)
    (issue,) = issues_of(hass, "self_check_off")
    assert issue.translation_placeholders["devices"].splitlines()[1:] == [f"    - sent Tuya datapoint 2, refused ({text.NO_REASON})"]
    assert manager_of(hass).summary()["self_check"]["z2m_tuya_a.py"]["devices"][0]["refused_datapoints"] == {"2": text.NO_REASON}


async def test_a_refusal_the_frame_shows_keeps_the_reason_the_log_gave_before(hass: HomeAssistant, zha: Zha, repo_dir: Path, freezer):
    """The log names why datapoint 2 was refused. Later the log is set above warnings and the device sends the datapoint
    again: the frame shows that refusal without a reason. The watch keeps the reason it heard (a device check reads it
    there), and so do the notice and the diagnostics, also after ZHA made a new device whose watch never heard it."""
    device = radar(hass, zha)
    tuya = device.clusters[0xEF00]
    tuya.endpoint.device = SimpleNamespace(name="0x1234")
    map_datapoints(tuya, {1: "temperature", 2: "humidity", 3: "max_temp"})
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    device.state("temperature", "21.5", device_class="temperature", unit_of_measurement="°C")
    tuya.frame(1, updates=("temperature",))
    tuya.refuse(2, REFUSAL)
    zcl = logging.getLogger("zigpy.zcl")
    level = zcl.level
    zcl.setLevel(logging.ERROR)
    checker = manager_of(hass).checks
    heard = checker.watches[device.device_id]
    try:
        tuya.frame(1, 2, updates=("temperature",))
        assert heard.refused_since(0) == [(2, REFUSAL)]
        assert self_check(hass)["devices"][0]["refused_datapoints"] == {"2": REFUSAL}
        device.renew()
        checker.sync()
        tuya.frame(1, 2, updates=("temperature",))
    finally:
        zcl.setLevel(level)
    renewed = checker.watches[device.device_id]
    assert renewed is not heard and renewed.refused_since(0) == [(2, "")]
    assert self_check(hass)["devices"][0]["refused_datapoints"] == {"2": REFUSAL}
    await watch_for(hass, freezer, 25)
    (issue,) = issues_of(hass, "self_check_off")
    assert issue.translation_placeholders["devices"].splitlines()[1:] == [f"    - sent Tuya datapoint 2, refused: {text.plain(REFUSAL)}"]


async def test_with_the_log_quiet_an_entity_without_a_value_is_one_no_value_reached(hass: HomeAssistant, zha: Zha, repo_dir: Path, freezer, quiet_cluster_log):
    """The device answers the data query without the humidity, the log is quiet: a refusal the frame cannot show could be
    behind it, so the notice says no value reached the entity, not that the device sent none, and why."""
    device = radar(hass, zha)
    tuya = device.clusters[0xEF00]
    tuya.answer = lambda: tuya.frame(1, updates=("temperature",))
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    device.state("temperature", "21.5", device_class="temperature", unit_of_measurement="°C")
    await watch_for(hass, freezer, 25)
    (issue,) = issues_of(hass, "self_check_off")
    assert issue.translation_placeholders["devices"].splitlines()[1:] == [
        "    - sensor: Humidity: no value reached it, although the device answered the data query",
        f"    - {checks.QUIET_LOG}",
    ]


async def test_a_setting_changed_in_home_assistant_is_no_value_from_the_device(hass: HomeAssistant, zha: Zha, repo_dir: Path, freezer):
    """The device sends nothing all day. The user changes a setting of the file once: zigpy sends the same attribute event
    for that write, but the device sent nothing, and the notice says so instead of calling the values fine."""
    device = radar(hass, zha)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    freezer.tick(timedelta(minutes=5))
    device.state("max_temp", "40", min=5, max=45)
    device.clusters[0xEF00].send("max_temp", by_home_assistant=True)
    await watch_for(hass, freezer, 25)
    assert issues_of(hass, "self_check_fine") == []
    (issue,) = issues_of(hass, "self_check_unproven")
    assert issue.translation_placeholders["devices"].splitlines()[1:] == ["    - sent nothing while xquirks watched it"]


async def test_the_watch_reads_the_clock_when_a_value_comes():
    """No clock bound at import: a watch without one reads the time module when a value arrives."""
    cluster = Cluster(0x0402)
    watch = watch_module.Watch(zigpy_device(cluster))
    watch.attach()
    with patch.object(watch_module.time, "time", return_value=1234.0):
        cluster.send("measured_value")
    assert watch.fresh == {"1:1026:measured_value": 1234.0, "1:1026:*": 1234.0}


async def test_a_notice_counts_the_problems_it_does_not_list(hass: HomeAssistant, zha: Zha, repo_dir: Path, freezer):
    """The device answers the data query, 25 measurements of the file get no value: the notice lists 20
    and says how many more there are."""
    tuya = CustomCluster(0xEF00, handled=(1,))
    entities = [("sensor", f"value_{i:02}", tuya, f"value_{i:02}", None, True) for i in range(25)]
    LiveDevice(hass, zha, IEEE_A, "_TZE200_aaaa", "TS0601", entities)
    tuya.answer = lambda: tuya.frame(1)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    await watch_for(hass, freezer, 25)
    (issue,) = issues_of(hass, "self_check_off")
    lines = issue.translation_placeholders["devices"].splitlines()
    assert len(lines) == 1 + checks.MAX_FINDINGS + 1
    assert lines[1] == "    - sensor: Value 00: no value from the device, although it answered the data query"
    assert lines[-1] == "    - and 5 more"


async def test_a_file_installed_again_at_the_same_revision_gets_no_second_notice(hass: HomeAssistant, zha: Zha, repo_dir: Path, freezer):
    device = radar(hass, zha)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    device.value("temperature", "21.5", device_class="temperature", unit_of_measurement="°C")
    await watch_for(hass, freezer, 25)
    (issue,) = issues_of(hass, "self_check_fine")
    ir.async_ignore_issue(hass, const.DOMAIN, issue.issue_id, True)
    # Left out, the file goes. Taken back in, the same revision comes back and is watched again.
    for excluded, quirk in (([device.device_id], "zigpy.device.Device"), ([], TUYA_A)):
        hass.config_entries.async_update_entry(entry, options={const.CONF_EXCLUDED: excluded})
        await hass.async_block_till_done(wait_background_tasks=True)
        await apply_list(hass)
        zha.reload({IEEE_A: quirk})
        await settle(hass)
    assert manager_of(hass).summary()["self_check"]["z2m_tuya_a.py"]["result"] == "watching"
    device.value("temperature", "21.6", device_class="temperature", unit_of_measurement="°C")
    await watch_for(hass, freezer, 25)
    assert manager_of(hass).summary()["self_check"]["z2m_tuya_a.py"]["result"] == "fine"
    assert issues_of(hass, "self_check_fine") == []


async def test_the_self_check_counts_only_time_with_zha_running_and_survives_a_restart(hass: HomeAssistant, zha: Zha, repo_dir: Path, freezer):
    device = radar(hass, zha)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    device.value("temperature", "3276.7", device_class="temperature", unit_of_measurement="°C")
    await watch_for(hass, freezer, 10)
    # Home Assistant restarts: what was seen stays, the time down does not count.
    await restart_with(hass, entry, repo_dir)
    freezer.tick(timedelta(hours=30))
    await watch_for(hass, freezer, 10)
    summary = manager_of(hass).summary()["self_check"]["z2m_tuya_a.py"]
    assert summary["result"] == "watching" and 19 <= summary["observed_hours"] <= 21
    assert summary["devices"][0]["findings"] == ["sensor: Temperature: 3276.7 °C is outside -50 to 150 °C"]
    # ZHA unloads: the listeners go, time stops.
    zha.start_reload()
    await settle(hass)
    assert device.clusters[0xEF00].listening == 0
    await watch_for(hass, freezer, 10)
    assert manager_of(hass).summary()["self_check"]["z2m_tuya_a.py"]["observed_hours"] == summary["observed_hours"]
    zha.finish_reload()
    await settle(hass)
    await watch_for(hass, freezer, 5)
    assert issues_of(hass, "self_check_off")


@pytest.mark.parametrize("sent", ["nothing", "tuya", "zcl"])
async def test_a_file_none_of_whose_values_came_is_not_called_plausible(hass: HomeAssistant, zha: Zha, repo_dir: Path, freezer, sent: str):
    """Plausible needs evidence: a value of the file from the device that looked right. Without one the
    notice says that no value came, whatever else the device sent: nothing at all, only a Tuya
    datapoint the file does not map (it missed the data query) or, for a ZCL file, only reports of
    clusters that are ZHA's own."""
    if sent == "zcl":
        device = acme(hass, zha)
        await installed(hass, zha, repo_dir, {IEEE_C: ACME_C})
        device.clusters[0x0000].send("rssi")
        expected = ["    - no value from the device for sensor: Measured value"]
    else:
        device = radar(hass, zha)
        await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
        freezer.tick(timedelta(minutes=5))
        if sent == "tuya":
            device.clusters[0xEF00].frame(101)
            expected = ["    - no value from the device for sensor: Humidity, sensor: Temperature", "    - sent Tuya datapoints this file does not show: 101"]
        else:
            expected = ["    - sent nothing while xquirks watched it"]
    await watch_for(hass, freezer, 25)
    assert issues_of(hass, "self_check_off") == issues_of(hass, "self_check_fine") == []
    (issue,) = issues_of(hass, "self_check_unproven")
    assert issue.translation_placeholders["devices"].splitlines()[1:] == expected
    assert "link" not in issue.translation_placeholders and issue.is_persistent and issue.is_fixable
    assert list(manager_of(hass).summary()["self_check"].values())[0]["result"] == "unproven"


async def test_without_an_answered_query_a_missing_measurement_is_a_note(hass: HomeAssistant, zha: Zha, repo_dir: Path, freezer):
    """A device that missed the data query: a measurement that did not come is noted, a setting is not
    (a setting comes only after a query), neither makes the file look off."""
    device = radar(hass, zha)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    freezer.tick(timedelta(minutes=5))
    device.value("temperature", "21.5", device_class="temperature", unit_of_measurement="°C")
    await watch_for(hass, freezer, 25)
    (issue,) = issues_of(hass, "self_check_fine")
    assert issue.translation_placeholders["devices"].splitlines()[1:] == ["    - no value from the device for sensor: Humidity"]


async def test_a_device_left_out_ends_the_self_check_of_its_file(hass: HomeAssistant, zha: Zha, repo_dir: Path):
    device = radar(hass, zha)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    assert "z2m_tuya_a.py" in manager_of(hass).summary()["self_check"]
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    hass.config_entries.async_update_entry(entry, options={const.CONF_EXCLUDED: [device.device_id]})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert manager_of(hass).summary()["self_check"] == {} and manager_of(hass).checks.watches == {}
    assert device.clusters[0xEF00].listening == 0


async def test_listeners_follow_zha_and_go_at_unload(hass: HomeAssistant, zha: Zha, repo_dir: Path, freezer):
    device = radar(hass, zha)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    tuya = device.clusters[0xEF00]
    # Three attribute events, the cleared one (a Tuya device) and the command listener; one on the device for its frames.
    assert tuya.listening == 5 and len(device.zigpy.listeners) == 1
    old = manager_of(hass).checks.watches[device.device_id]
    # A re-interview: ZHA has a new zigpy device, the periodic look finds it, the refresh listens to it.
    device.renew()
    await watch_for(hass, freezer, 0.2)
    await settle(hass)
    new = manager_of(hass).checks.watches[device.device_id]
    assert new is not old and new.device is device.zigpy and tuya.listening == 5
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    await hass.config_entries.async_unload(entry.entry_id)
    assert tuya.listening == 0 and device.zigpy.listeners == []


# The device check


async def open_check(hass: HomeAssistant, entry) -> dict:
    """The integration page's "Check a device" (its button or the three-dot menu): the device check, a subentry flow of
    the entry with the source "user". It starts with the device picker."""
    result = await hass.config_entries.subentries.async_init((entry.entry_id, "device_check"), context={"source": "user"})
    return check_texts(result)


async def run_check(hass: HomeAssistant, entry, device_id: str, *, to_list: bool = True) -> dict:
    """Pick the device, wait for the progress step to end: the values page, with to_list its "Something does not match"
    (the list where the user marks what matches the device and what does not)."""
    result = await open_check(hass, entry)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "check"
    with patch.object(checks, "CHECK_WAIT", 0.3), patch.object(checks, "CHECK_QUIET", 0.05), patch.object(checks, "CHECK_STEP", 0.05):
        result = await continue_check(hass, result, {"device": device_id})
        if result["type"] is FlowResultType.SHOW_PROGRESS:
            assert result["progress_action"] == "asking"
            await hass.async_block_till_done(wait_background_tasks=True)
            result = await continue_check(hass, result)
    if to_list and result["type"] is FlowResultType.MENU:
        assert result["menu_options"] == ["all_match", "mismatch"], result
        result = await continue_check(hass, result, {"next_step_id": "mismatch"})
    return result


def check_texts(result: dict) -> dict:
    """The device check's texts of a step or abort render with its placeholders."""
    result.setdefault("kind", None)
    return check_device_check_texts(result)


async def report_link(hass: HomeAssistant, result: dict, step: str = "report") -> str:
    """The last page of the device check: a page without fields whose one button opens the link (the form) as an
    external step. The link."""
    assert result["type"] is FlowResultType.FORM and result["step_id"] == step and result["data_schema"] is None, result
    opened = await continue_check(hass, result, {})
    assert opened["type"] is FlowResultType.EXTERNAL_STEP and opened["step_id"] == ("open_test" if step == "thumbs_up" else "open"), opened
    return opened["url"]


async def conclude(hass: HomeAssistant) -> None:
    """The self-checks end at the next count (real time: the device check below waits in real time)."""
    with patch.object(checks, "WINDOW", timedelta(0)):
        async_fire_time_changed(hass, dt_util.utcnow() + checks.TICK)
        await hass.async_block_till_done(wait_background_tasks=True)


async def test_a_tuya_device_check_ends_with_a_filled_in_device_test(hass: HomeAssistant, zha: Zha, repo_dir: Path):
    device = radar(hass, zha)
    tuya = device.clusters[0xEF00]
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    # In the window the device sends its temperature and its setting, the humidity stays as stored.
    device.state("humidity", "48", device_class="humidity", unit_of_measurement="%")
    device.value("temperature", "21.4", device_class="temperature", unit_of_measurement="°C")
    device.value("max_temp", "35", min=5, max=45)
    await conclude(hass)
    assert issues_of(hass, "self_check_fine")

    def answer() -> None:
        tuya.frame(1, 2, 3, 120)
        device.value("temperature", "21.5", device_class="temperature", unit_of_measurement="°C")
        device.value("max_temp", "35", min=5, max=45)

    tuya.answer = answer
    sent = len(tuya.sent)
    result = await run_check(hass, entry, device.device_id)
    assert len(tuya.sent) == sent + 1
    assert result["step_id"] == "results" and not result["errors"]
    shown = result["description_placeholders"]["entities"]
    assert "| sensor: Temperature | 1-temperature |  | 21.5 | °C | now |  |" in shown.replace("\\_", "_").replace("\\-", "-")
    assert "| sensor: Humidity | 1-humidity |  | 48 | % | stored |  |" in shown.replace("\\_", "_")
    assert "Tuya datapoints this file does not show: 120." in shown
    assert IEEE_A not in shown and "kitchen" not in shown.lower()
    result = await continue_check(hass, result, {"matches": ["1-temperature", "1-max_temp"], "wrong": []})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "report"
    assert result["description_placeholders"] == {"matches": "2", "unchecked": "1"}
    # The report is kept when the last page shows. The notice stays until its button is clicked.
    assert len(manager_of(hass).checks.reports) == 1 and issues_of(hass, "self_check_fine")
    fields = link_fields(await report_link(hass, result))
    assert fields["template"] == text.VERIFY_FORM and set(fields) - {"template", "title"} <= form_ids(text.VERIFY_FORM)
    assert fields["title"] == "Verify: Tuya A" and fields["file"] == "quirks/z2m_tuya_a.py"
    report = fields["report"]
    assert report.startswith("## xquirks device check: \\_TZE200\\_aaaa TS0601")
    assert "| yes | sensor: Temperature |" in report and "| not checked | sensor: Humidity |" in report
    assert "Tuya data query, it answered" in report
    assert IEEE_A not in report and "kitchen" not in report.lower() and NAME not in report
    # The report is in the diagnostics, the self-check notice went: it asked for this check.
    result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
    assert result["device_checks"][-1]["report"] == report and result["device_checks"][-1]["device"] == "_TZE200_aaaa TS0601"
    assert issues_of(hass, "self_check_fine") == []
    await manager_of(hass).async_refresh()
    assert issues_of(hass, "self_check_fine") == []


async def test_refusals_are_heard_only_while_a_check_runs_and_the_device_check_lists_them(hass: HomeAssistant, zha: Zha, repo_dir: Path):
    """The handler on zigpy's cluster log sees every warning of every cluster. It listens while the self-check
    of a device runs and while a device check of it runs, not otherwise. The device check lists the datapoints refused
    while it ran, as it lists those the file does not show."""
    device = radar(hass, zha)
    tuya = device.clusters[0xEF00]
    tuya.endpoint.device = SimpleNamespace(name="0x1234")
    tuya.endpoint.tuya_manufacturer = tuya
    tuya._dp_to_attributes = {2: [SimpleNamespace(endpoint_id=None, ep_attribute="tuya_manufacturer", attribute_name="humidity")]}
    zcl = logging.getLogger("zigpy.zcl")
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    assert integration.watch.REFUSALS in zcl.handlers
    device.value("temperature", "21.4", device_class="temperature", unit_of_measurement="°C")
    await conclude(hass)
    assert issues_of(hass, "self_check_fine") and integration.watch.REFUSALS not in zcl.handlers
    watch = manager_of(hass).checks.watches[device.device_id]
    tuya.refuse(2, REFUSAL)
    assert watch.refused == {}
    # Nor are the frames looked at for refusals while no check runs: a frame costs the watch no walk through the
    # quirk's datapoint table then.
    with patch.object(integration.watch, "mapped_keys", wraps=integration.watch.mapped_keys) as walked:
        tuya.frame(2)
    assert not walked.called and watch.refused == {}
    heard = []

    def answer() -> None:
        heard.append(integration.watch.REFUSALS in zcl.handlers)
        tuya.frame(1, 2)
        tuya.refuse(2, REFUSAL)
        device.value("temperature", "21.5", device_class="temperature", unit_of_measurement="°C")

    tuya.answer = answer
    result = await run_check(hass, entry, device.device_id)
    listed = f"Tuya datapoints this file refused: 2 ({text.plain(REFUSAL)})"
    assert heard == [True] and f"{listed}." in result["description_placeholders"]["entities"]
    assert integration.watch.REFUSALS not in zcl.handlers
    result = await continue_check(hass, result, {"matches": ["1-temperature"], "wrong": []})
    assert f"- {listed}\n" in link_fields(await report_link(hass, result))["report"]


async def test_a_device_check_reads_the_watch_a_sync_put_on_the_device_while_it_ran(hass: HomeAssistant, zha: Zha, repo_dir: Path):
    """A sync while the device check waits (a reload of ZHA, a re-interview) puts a new watch on the device. What
    came before it sits in the old watch, what came after in the new one: the check lists both."""
    device = radar(hass, zha)
    tuya = device.clusters[0xEF00]
    tuya.endpoint.device = SimpleNamespace(name="0x1234")
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    device.value("temperature", "21.4", device_class="temperature", unit_of_measurement="°C")
    await conclude(hass)
    checker = manager_of(hass).checks
    old = checker.watches[device.device_id]

    def answer() -> None:
        tuya.frame(1, 120)
        tuya.refuse(3, REFUSAL)
        device.renew()
        checker.sync()
        tuya.refuse(2, REFUSAL)
        device.value("temperature", "21.5", device_class="temperature", unit_of_measurement="°C")

    tuya.answer = answer
    result = await run_check(hass, entry, device.device_id)
    assert checker.watches[device.device_id] is not old
    shown = result["description_placeholders"]["entities"]
    assert f"Tuya datapoints this file refused: 2 ({text.plain(REFUSAL)}), 3 ({text.plain(REFUSAL)})." in shown
    assert "Tuya datapoints this file does not show: 120." in shown
    assert "| sensor: Temperature | 1-temperature |  | 21.5 | °C | now |  |" in shown.replace("\\_", "_").replace("\\-", "-")


async def test_a_value_marked_wrong_goes_to_the_wrong_value_form(hass: HomeAssistant, zha: Zha, repo_dir: Path):
    device = radar(hass, zha)
    tuya = device.clusters[0xEF00]
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    tuya.answer = lambda: device.value("temperature", "21.5", device_class="temperature", unit_of_measurement="°C")
    result = await run_check(hass, entry, device.device_id)
    # Each value in one list only, at least one value.
    for answers, error in (({"matches": ["1-temperature"], "wrong": ["1-temperature"]}, "both"), ({"matches": [], "wrong": []}, "nothing")):
        result = await continue_check(hass, result, answers)
        assert result["type"] is FlowResultType.FORM and result["errors"] == {"base": error}
    result = await continue_check(hass, result, {"matches": ["1-max_temp"], "wrong": ["1-temperature"]})
    assert result["step_id"] == "report_wrong" and result["description_placeholders"] == {"wrong": "1", "matches": "1", "unchecked": "1"}
    fields = link_fields(await report_link(hass, result, "report_wrong"))
    assert fields["template"] == text.PROBLEM_FORM and set(fields) - {"template", "title"} <= form_ids(text.PROBLEM_FORM)
    assert fields["problem"] == "- sensor: Temperature: Home Assistant shows 21.5 °C, the device shows something else"
    assert "| no | sensor: Temperature |" in fields["diagnostics"]


@pytest.mark.parametrize(
    ("power", "step"),
    [("unknown", "values_no_answer"), ("battery", "values_no_answer"), ("zha_mains", "values_no_answer_mains"), ("node_mains", "values_no_answer_mains")],
)
async def test_a_device_that_does_not_answer_says_so_on_the_values_page(hass: HomeAssistant, zha: Zha, repo_dir: Path, power: str, step: str):
    """A battery device (or one whose power source is unknown) may sleep: wake it. A mains device that
    does not answer cannot be reached: see that it has power. The power source as ZHA shows it, else
    from zigpy's node descriptor. The list after "Something does not match" does not say it again."""
    device = radar(hass, zha)
    if power == "zha_mains":
        device.mains = True
    elif power != "unknown":
        device.zigpy.node_desc = SimpleNamespace(is_mains_powered=power == "node_mains")
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    result = await run_check(hass, entry, device.device_id, to_list=False)
    assert result["type"] is FlowResultType.MENU and result["step_id"] == step and result["menu_options"] == ["all_match", "mismatch"]
    assert "Asked the device: Tuya data query, no answer." in result["description_placeholders"]["entities"]
    result = await continue_check(hass, result, {"next_step_id": "mismatch"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "results" and not result["errors"]


async def test_a_zcl_device_check_reads_the_files_attributes_from_the_device(hass: HomeAssistant, zha: Zha, repo_dir: Path):
    device = acme(hass, zha)
    await installed(hass, zha, repo_dir, {IEEE_C: ACME_C})
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    device.state("temperature", "21.5", device_class="temperature", unit_of_measurement="°C")
    result = await run_check(hass, entry, device.device_id)
    # The cluster the quirk adds is read, not from the cache. The local cluster never reaches the device,
    # ZHA's own entity is not the file's.
    assert device.clusters[0x0402].reads == [(["measured_value"], False)]
    assert device.clusters[0xFC00].reads == device.clusters[0x0000].reads == []
    shown = result["description_placeholders"]["entities"]
    assert "Asked the device: read of 1 attribute, it answered." in shown
    assert [option["value"] for option in result["data_schema"].schema["matches"].config["options"]] == ["1-mode", "1-temperature"]
    result = await continue_check(hass, result, {"matches": ["1-temperature"]})
    report = link_fields(await report_link(hass, result))["report"]
    assert "Other entities of the device (not part of the check): sensor: Rssi" in report


async def test_a_query_that_times_out_says_how(hass: HomeAssistant, zha: Zha, repo_dir: Path):
    """zigpy raises TimeoutError() without a text for a data query it could not deliver: the dialog names the error."""
    device = radar(hass, zha)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]

    async def undelivered(command_id: int, *args, **kwargs):
        raise TimeoutError

    device.clusters[0xEF00].command = undelivered
    result = await run_check(hass, entry, device.device_id)
    assert "Asked the device: Tuya data query, not delivered (TimeoutError)." in result["description_placeholders"]["entities"]


async def test_a_check_while_an_update_waits_for_zha_names_no_revision(hass: HomeAssistant, zha: Zha, repo_dir: Path):
    """The update is written, ZHA has not loaded it yet: the device runs the revision before, which the record no longer
    names. The dialog and the report say so instead of naming the new revision for values of the old one."""
    device = radar(hass, zha)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    write_repo(repo_dir, version=2)
    REPOS[repo_dir].publish()
    await manager_of(hass).async_check()
    await apply_list(hass)
    assert manager_of(hass).data["pending"]["changes"] == [{"file": "z2m_tuya_a.py", "change": "updated"}]
    result = await run_check(hass, entry, device.device_id)
    assert result["description_placeholders"]["revision"] == "unknown"
    assert text.WAITING in result["description_placeholders"]["entities"]
    result = await continue_check(hass, result, {"matches": ["1-temperature"]})
    fields = link_fields(await report_link(hass, result))
    assert fields["revision"] == "unknown"
    assert f"- File: `z2m_tuya_a.py`, revision unknown ({text.WAITING_SHORT})\n" in fields["report"]


@pytest.mark.parametrize("answer", ["unsupported", "changed"])
async def test_the_answer_to_a_read_counts_as_it_came(hass: HomeAssistant, zha: Zha, repo_dir: Path, answer: str):
    """zigpy sends no read event for an attribute the device does not support, nor for a value the quirk changes on its way
    to the cache: the read's own answer says what came. A device that answers "unsupported attribute" did answer, and the
    row says what it answered: the file's attribute is wrong for this device."""
    device = acme(hass, zha)
    added = device.clusters[0x0402]

    async def read_attributes(names, allow_cache: bool = False, **kwargs):
        added.reads.append((list(names), allow_cache))
        if answer == "unsupported":
            return {}, dict.fromkeys(names, 0x86)
        return dict.fromkeys(names, 2150), {}

    added.read_attributes = read_attributes
    await installed(hass, zha, repo_dir, {IEEE_C: ACME_C})
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    device.state("temperature", "21.5", device_class="temperature", unit_of_measurement="°C")
    result = await run_check(hass, entry, device.device_id)
    assert added.reads == [(["measured_value"], False)]
    assert not result["errors"]
    shown = result["description_placeholders"]["entities"]
    assert "Asked the device: read of 1 attribute, it answered." in shown
    row = next(line for line in shown.splitlines() if line.startswith("| sensor: Measured value |"))
    assert row.endswith(f"| stored | {checks.UNSUPPORTED} |" if answer == "unsupported" else "| now |  |"), row


async def test_a_select_that_shows_unknown_after_its_value_came_gets_a_remark(hass: HomeAssistant, zha: Zha, repo_dir: Path):
    """The device sends a mode during the check that the file's select does not know: Home Assistant shows unknown
    (SelectEntity.state). The row says what the self-check would note."""
    device = acme(hass, zha)
    added, local = device.clusters[0x0402], device.clusters[0xFC00]

    async def read_attributes(names, allow_cache: bool = False, **kwargs):
        added.reads.append((list(names), allow_cache))
        # The device reports its mode right after the read.
        device.value("mode", "unknown", options=["slow", "fast"])
        return dict.fromkeys(names, 2150), {}

    added.read_attributes = read_attributes
    await installed(hass, zha, repo_dir, {IEEE_C: ACME_C})
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    device.state("temperature", "21.5", device_class="temperature", unit_of_measurement="°C")
    result = await run_check(hass, entry, device.device_id)
    rows = {line.split(" | ")[0]: line for line in result["description_placeholders"]["entities"].splitlines() if line.startswith("| select:") or line.startswith("| sensor:")}
    assert rows["| select: Mode"].endswith(f"| unknown |  | now | {checks.SHOWS_UNKNOWN} |"), rows
    assert rows["| sensor: Measured value"].endswith("| now |  |"), rows
    assert local.reads == []


@pytest.mark.parametrize("answer", ["unsupported", "value"])
async def test_a_device_check_asks_for_no_buttons_attribute_and_remarks_nothing_on_it(hass: HomeAssistant, zha: Zha, repo_dir: Path, answer: str):
    """The attribute a write button names is one it writes: the check does not read it, so a device that would answer
    "unsupported attribute" for it (it can only be written) gets no remark that it lacks the attribute. Nor does the
    button "show unknown although a value came" when the device reports that attribute during the check."""
    device = valve(hass, zha)
    added = device.clusters[0x0402]

    async def read_attributes(names, allow_cache: bool = False, **kwargs):
        added.reads.append((list(names), allow_cache))
        if answer == "value":
            device.value("self_test", "unknown")
        return {name: 2150 for name in names if name == "measured_value"}, {name: 0x86 for name in names if name != "measured_value"}

    added.read_attributes = read_attributes
    await installed(hass, zha, repo_dir, {IEEE_C: ACME_C})
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    device.state("temperature", "21.5", device_class="temperature", unit_of_measurement="°C")
    device.state("self_test", "unknown")
    result = await run_check(hass, entry, device.device_id)
    assert added.reads == [(["measured_value"], False)]
    shown = result["description_placeholders"]["entities"]
    assert "Asked the device: read of 1 attribute, it answered." in shown
    rows = {line.split(" | ")[0]: line for line in shown.splitlines() if line.startswith("| button:") or line.startswith("| sensor:")}
    assert rows["| button: Self test"].endswith("| unknown |  | stored |  |"), rows
    assert rows["| sensor: Measured value"].endswith("| now |  |"), rows


async def test_a_check_that_asks_nothing_does_not_claim_a_silent_device(hass: HomeAssistant, zha: Zha, repo_dir: Path):
    """Every value of the file sits on a local cluster: nothing goes to the device, so the dialog shows
    no "did not answer" banner and says that nothing was asked."""
    local, basic = LocalDataCluster(0xFC00), Cluster(0x0000)
    device = LiveDevice(hass, zha, IEEE_C, "Acme", "C-1", [("select", "mode", local, "mode", "config", True), ("sensor", "rssi", basic, "rssi", None, False)])
    await installed(hass, zha, repo_dir, {IEEE_C: ACME_C})
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    device.state("mode", "eco", options=["eco", "comfort"])
    result = await run_check(hass, entry, device.device_id, to_list=False)
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "values"
    assert result["description_placeholders"]["entities"].startswith("Asked the device: nothing (the file's values come only when the device sends them).\n\n")
    result = await continue_check(hass, result, {"next_step_id": "all_match"})
    assert "- Asked the device: nothing (the file's values come only when the device sends them)\n" in link_fields(await report_link(hass, result))["report"]


async def test_a_device_that_never_answers_a_read_ends_the_check_in_time(hass: HomeAssistant, zha: Zha, repo_dir: Path):
    """zigpy waits up to 28 s per try for a sleeping device: the check stops waiting for the request at
    its own bound, so the whole check stays within the half minute its dialog names."""
    device = acme(hass, zha)
    await installed(hass, zha, repo_dir, {IEEE_C: ACME_C})

    async def never(names, allow_cache: bool = False, **kwargs):
        await asyncio.Event().wait()

    device.clusters[0x0402].read_attributes = never
    with patch.object(checks, "CHECK_ASK", 0.1), patch.object(checks, "CHECK_WAIT", 0.2), patch.object(checks, "CHECK_STEP", 0.05):
        result = await asyncio.wait_for(manager_of(hass).checks.async_run_check(device.device_id), 3)
    assert (result.asked, result.answer, result.answered) == ("read of 1 attribute", "stopped waiting after 0.1 s", False)
    assert checks.CHECK_ASK + checks.CHECK_WAIT <= 30
    assert "half a minute" in STRINGS["config_subentries"]["device_check"]["progress"]["asking"]


async def test_the_read_names_one_attribute_in_the_singular(hass: HomeAssistant, zha: Zha, repo_dir: Path):
    """"read of 1 attribute", "read of 2 attributes": the word follows the count."""
    device = acme(hass, zha)
    await installed(hass, zha, repo_dir, {IEEE_C: ACME_C})
    checks_ = manager_of(hass).checks
    assert checks_._reads(device.device_id)[0] == "read of 1 attribute"
    specs = checks_._specs[device.device_id]
    (spec,) = [spec for found in specs.values() for spec in found if spec.attribute == "measured_value"]
    specs.setdefault("second", []).append(dataclasses.replace(spec, attribute="tolerance"))
    assert checks_._reads(device.device_id)[0] == "read of 2 attributes"


@pytest.mark.parametrize("kind", ["tuya", "zcl"])
async def test_a_query_or_read_that_was_not_delivered_waits_for_no_values(hass: HomeAssistant, zha: Zha, repo_dir: Path, kind: str):
    """Nothing reached the device (the live test: APS_NO_ACK), so nothing can come back: the check goes
    to its results at once instead of waiting for values."""
    why = "Failed to deliver packet: <TXStatus.APS_NO_ACK: 167>"

    async def fail(*args, **kwargs):
        raise RuntimeError(why)

    if kind == "tuya":
        device = radar(hass, zha)
        await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
        device.clusters[0xEF00].command = fail
        expected = f"not delivered ({why})"
    else:
        device = acme(hass, zha)
        await installed(hass, zha, repo_dir, {IEEE_C: ACME_C})
        device.clusters[0x0402].read_attributes = fail
        expected = f"not answered ({why})"
    with patch.object(checks, "CHECK_WAIT", 5.0), patch.object(checks, "CHECK_STEP", 0.05):
        result = await asyncio.wait_for(manager_of(hass).checks.async_run_check(device.device_id), 1)
    assert (result.answer, result.answered) == (expected, False)


async def test_an_entity_that_reads_a_whole_cluster_shows_when_its_cluster_reported(hass: HomeAssistant, zha: Zha, repo_dir: Path):
    """ZHA's climate, cover and light entities name no single attribute: a value on their cluster is a
    fresh value for them."""
    thermostat = CustomCluster(0x0201)
    device = LiveDevice(hass, zha, IEEE_C, "Acme", "C-1", [("climate", "thermostat", thermostat, None, None, False),
                                                         ("sensor", "local_temperature", thermostat, "local_temperature", None, True)])
    await installed(hass, zha, repo_dir, {IEEE_C: ACME_C})
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    device.state("thermostat", "heat")
    device.state("local_temperature", "21.5", device_class="temperature", unit_of_measurement="°C")
    result = await run_check(hass, entry, device.device_id)
    shown = result["description_placeholders"]["entities"].replace("\\_", "_")
    assert "| climate: Thermostat | 1-thermostat |  | heat |  | now |  |" in shown


async def test_an_entity_without_a_name_goes_by_its_translation_key_or_device_class(hass: HomeAssistant, zha: Zha, repo_dir: Path):
    """ZHA's occupancy sensor has no name of its own (the live test showed "binary_sensor: 1-1030"). An
    entity without a name goes by its translation key, else by the device class the integration gave
    it, else by its unique id suffix. Never by a name or a device class the user set (in the registry
    or with customize in configuration.yaml), never by its entity id."""
    from homeassistant.core_config import DATA_CUSTOMIZE
    from homeassistant.helpers.entity_values import EntityValues

    tuya = CustomCluster(0xEF00, handled=(1, 2, 3, 4, 5))
    device = LiveDevice(hass, zha, IEEE_A, "_TZE200_aaaa", "TS0601", [
        ("binary_sensor", "1030", tuya, "occupancy", None, True),
        ("binary_sensor", "1031", tuya, "contact", None, True),
        ("binary_sensor", "1032", tuya, "vibration", None, True),
        ("sensor", "dis_current", tuya, "dis_current", None, True),
        ("sensor", "human_motion_state", tuya, "human_motion_state", None, True),
    ])
    registry = er.async_get(hass)
    for suffix in ("1030", "1031", "1032", "dis_current", "human_motion_state"):
        registry.async_update_entity(device.entity_ids[suffix], original_name=None, name=f"Kitchen {suffix} by the user")
    # The registry's own device class, device classes the user set, a translation key.
    registry.async_update_entity(device.entity_ids["dis_current"], original_device_class="distance")
    registry.async_update_entity(device.entity_ids["1031"], device_class="garage_door")
    hass.data[DATA_CUSTOMIZE] = EntityValues({device.entity_ids["1032"]: {"device_class": "the pantry window"}})
    registry.async_update_entity(device.entity_ids["human_motion_state"], translation_key="human_motion_state")
    device.state("1030", "on", device_class="occupancy")
    device.state("1031", "off", device_class="garage_door")
    device.state("1032", "off", device_class="the pantry window")
    device.state("dis_current", "600", device_class="distance", unit_of_measurement="cm")
    device.state("human_motion_state", "small", device_class="enum", options=["none", "small", "large"])
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    result = await run_check(hass, entry, device.device_id)
    labels = [option["label"] for option in result["data_schema"].schema["matches"].config["options"]]
    assert labels == ["binary_sensor: occupancy: on", "binary_sensor: 1-1031: off", "binary_sensor: 1-1032: off", "sensor: distance: 600 cm", "sensor: human_motion_state: small"]
    result = await continue_check(hass, result, {"matches": ["1-1030"]})
    report = link_fields(await report_link(hass, result))["report"].replace("\\_", "_")
    assert "| yes | binary_sensor: occupancy | 1-1030 |" in report
    assert "by the user" not in report and "garage" not in report and "pantry" not in report and "kitchen" not in report.lower()
    # The self-check names them the same way, in the notice and in its link: the device answers the
    # check's data query with a datapoint that fills none of them.
    tuya.frame(1)
    await conclude(hass)
    (issue,) = issues_of(hass, "self_check_off")
    names = ["binary_sensor: 1-1031", "binary_sensor: 1-1032", "binary_sensor: occupancy", "sensor: distance", "sensor: human_motion_state"]
    lines = issue.translation_placeholders["devices"].replace("\\_", "_").splitlines()[1:]
    assert lines == [f"    - {name}: no value from the device, although it answered the data query" for name in names]
    problem = link_fields(issue.translation_placeholders["link"])["problem"].replace("\\_", "_")
    assert all(name in problem for name in names) and "by the user" not in problem and "garage" not in problem and "pantry" not in problem


async def test_the_check_offers_only_devices_that_run_a_file_of_ours(hass: HomeAssistant, zha: Zha, repo_dir: Path):
    device = radar(hass, zha)
    await setup_entry(hass, repo_dir)
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    result = check_texts(await open_check(hass, entry))
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "nothing_to_check"
    await apply_list(hass)
    zha.reload({IEEE_A: TUYA_A})
    await settle(hass)
    result = check_texts(await open_check(hass, entry))
    assert result["data_schema"].schema["device"].config["options"] == [{"value": device.device_id, "label": f"{NAME} (z2m_tuya_a.py)"}]
    # Left out in the options: not offered.
    hass.config_entries.async_update_entry(entry, options={const.CONF_EXCLUDED: [device.device_id]})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert check_texts(await open_check(hass, entry))["reason"] == "nothing_to_check"


async def test_without_zha_the_check_asks_nothing(hass: HomeAssistant, zha: Zha, repo_dir: Path):
    device = radar(hass, zha)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    result = await open_check(hass, entry)
    zha.start_reload()
    # The check ends before it asks anything: no progress dialog.
    result = await continue_check(hass, result, {"device": device.device_id})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "zha_not_loaded"
    assert device.clusters[0xEF00].sent == [const.TUYA_QUERY_DATA]


# One file's self-check through the events that reach it: watching, its notice shown, done


async def finish_check(hass: HomeAssistant, entry, device: LiveDevice) -> None:
    """A device check of the device to its last dialog and its button: the temperature marked as matching."""
    result = await run_check(hass, entry, device.device_id)
    result = await continue_check(hass, result, {"matches": ["1-temperature"]})
    await report_link(hass, result)


def self_check(hass: HomeAssistant, name: str = "z2m_tuya_a.py") -> dict | None:
    return manager_of(hass).summary()["self_check"].get(name)


async def values_checked(hass: HomeAssistant) -> None:
    """The values that came are checked (VALUE_DELAY after they came)."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=checks.VALUE_DELAY + 1))
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_a_check_finished_while_watching_leaves_the_self_check_running(hass: HomeAssistant, zha: Zha, repo_dir: Path):
    """The change list's done dialog invites the device check while the self-check watches. A check
    finished then does not end the watch: what the device sends later in the day still makes the
    notice. A check finished after the notice makes the notice go for good."""
    device = radar(hass, zha)
    tuya = device.clusters[0xEF00]
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    tuya.answer = lambda: device.value("temperature", "21.5", device_class="temperature", unit_of_measurement="°C")
    # Two checks while it watches.
    for _ in range(2):
        await finish_check(hass, entry, device)
        assert self_check(hass)["result"] == "watching" and self_check(hass)["device_check_done"]
    assert len(manager_of(hass).checks.reports) == 2
    # Later in the day the device sends a temperature far off: the notice says so.
    device.value("temperature", "3276.7", device_class="temperature", unit_of_measurement="°C")
    await values_checked(hass)
    await conclude(hass)
    (issue,) = issues_of(hass, "self_check_off")
    assert "sensor: Temperature: 3276.7 °C is outside -50 to 150 °C" in issue.translation_placeholders["devices"]
    # It stays through a refresh, a restart of Home Assistant and a reload of ZHA.
    await manager_of(hass).async_refresh()
    await restart_with(hass, entry, repo_dir)
    zha.reload()
    await settle(hass)
    assert [i.issue_id for i in issues_of(hass, "self_check_off")] == [issue.issue_id]
    # A check after the notice did what the notice asks: it goes, nothing brings it back.
    await finish_check(hass, entry, device)
    assert issues_of(hass, "self_check_off") == []
    # Done: a further check, a refresh and the next counts change nothing.
    await finish_check(hass, entry, device)
    await manager_of(hass).async_refresh()
    await conclude(hass)
    assert issues_of(hass, "self_check_off") == []
    assert self_check(hass)["result"] == "off" and self_check(hass)["device_check_done"]


@pytest.mark.parametrize("state", ["watching", "notice"])
@pytest.mark.parametrize("event", ["updated", "removed"])
async def test_a_file_updated_or_removed_ends_its_self_check_and_notice(hass: HomeAssistant, zha: Zha, repo_dir: Path, state: str, event: str):
    device = radar(hass, zha)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    old = self_check(hass)["revision"]
    if state == "notice":
        device.value("temperature", "21.5", device_class="temperature", unit_of_measurement="°C")
        await conclude(hass)
        assert issues_of(hass, "self_check_fine")
    if event == "updated":
        write_repo(repo_dir, version=2)
        REPOS[repo_dir].publish()
        await manager_of(hass).async_check()
        await apply_list(hass)
        zha.reload()
    else:
        # The device leaves ZHA: the change list removes the file it used.
        zha.remove_device(device.device_id)
        await manager_of(hass).async_refresh()
        await apply_list(hass)
        zha.reload()
    await settle(hass)
    assert issues_of(hass, "self_check_fine") == []
    if event == "updated":
        assert self_check(hass)["result"] == "watching" and self_check(hass)["revision"] != old
    else:
        assert self_check(hass) is None


async def test_a_left_out_device_keeps_its_notice_until_the_file_goes(hass: HomeAssistant, zha: Zha, repo_dir: Path):
    """While it watches, a device left out leaves the self-check at once (test above). Once the notice
    is out, it stays as long as the file it names is there."""
    device = radar(hass, zha)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    device.value("temperature", "21.5", device_class="temperature", unit_of_measurement="°C")
    await conclude(hass)
    (issue,) = issues_of(hass, "self_check_fine")
    hass.config_entries.async_update_entry(entry, options={const.CONF_EXCLUDED: [device.device_id]})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert [i.issue_id for i in issues_of(hass, "self_check_fine")] == [issue.issue_id]
    await apply_list(hass)
    zha.reload({IEEE_A: "zigpy.device.Device"})
    await settle(hass)
    assert issues_of(hass, "self_check_fine") == [] and self_check(hass) is None


async def test_disabling_the_entry_pauses_the_watch_and_takes_the_notice(hass: HomeAssistant, zha: Zha, repo_dir: Path, freezer):
    """Disabled, the entry watches nothing and counts no time. Enabled again, the watch goes on. A
    notice that is out goes with the disabling, the next enabling brings no second one."""
    from homeassistant.config_entries import ConfigEntryDisabler

    device = radar(hass, zha)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    device.value("temperature", "21.5", device_class="temperature", unit_of_measurement="°C")
    await watch_for(hass, freezer, 10)
    await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert device.clusters[0xEF00].listening == 0
    freezer.tick(timedelta(hours=30))
    await hass.config_entries.async_set_disabled_by(entry.entry_id, None)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert self_check(hass)["result"] == "watching" and 9.5 <= self_check(hass)["observed_hours"] <= 10.5
    await watch_for(hass, freezer, 15)
    (issue,) = issues_of(hass, "self_check_fine")
    await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert ir.async_get(hass).async_get_issue(const.DOMAIN, issue.issue_id) is None
    await hass.config_entries.async_set_disabled_by(entry.entry_id, None)
    await hass.async_block_till_done(wait_background_tasks=True)
    await watch_for(hass, freezer, 1)
    assert issues_of(hass, "self_check_fine") == [] and self_check(hass)["result"] == "fine"


async def test_a_self_check_without_its_day_in_a_week_ends_without_a_notice(hass: HomeAssistant, zha: Zha, repo_dir: Path, freezer):
    """Observation time counts only while ZHA runs: a self-check that did not get its day within a week
    ends without a notice."""
    device = radar(hass, zha)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    device.value("temperature", "21.5", device_class="temperature", unit_of_measurement="°C")
    await watch_for(hass, freezer, 2)
    zha.start_reload()
    await settle(hass)
    freezer.tick(checks.MAX_AGE)
    zha.finish_reload()
    await settle(hass)
    await watch_for(hass, freezer, 1)
    assert self_check(hass)["result"] == "expired" and self_check(hass)["observed_hours"] < 4
    assert [i for kind in checks.KINDS for i in issues_of(hass, kind)] == []


async def hard_restart(hass: HomeAssistant, entry, hass_storage, written: dict) -> None:
    """Home Assistant stops hard and starts again: the self-check store holds what it had on disk
    before (written), not what waited for the delayed save. The issue registry kept its changes (it
    saves sooner: 10 s against 30 s)."""
    await hass.config_entries.async_unload(entry.entry_id)
    hass_storage[checks.STORAGE_KEY] = written
    with patch.object(integration.manager, "_RUN", "after a hard stop"):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)


async def test_a_self_check_that_starts_is_on_disk_at_once(hass: HomeAssistant, zha: Zha, repo_dir: Path, hass_storage):
    """ZHA loaded an installed file: the records forget the pending change at once, so nothing starts
    the self-check again after a hard stop. It is on disk as soon as it starts."""
    import copy

    radar(hass, zha)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    assert manager_of(hass).data["pending"]["changes"] == []
    written = copy.deepcopy(hass_storage.get(checks.STORAGE_KEY))
    assert written is not None
    await hard_restart(hass, entry, hass_storage, written)
    assert self_check(hass)["result"] == "watching"


@pytest.mark.parametrize("event", ["check_watching", "check_notice", "window", "removed"])
async def test_an_event_lost_with_the_delayed_save_leaves_a_consistent_state(hass: HomeAssistant, zha: Zha, repo_dir: Path, hass_storage, event: str):
    """The store saves 30 s after a change. A hard stop before that: the next start reads the state
    before the event, and the self-check goes on from there."""
    import copy

    device = radar(hass, zha)
    tuya = device.clusters[0xEF00]
    tuya.answer = lambda: device.value("temperature", "21.5", device_class="temperature", unit_of_measurement="°C")
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    device.value("temperature", "21.5", device_class="temperature", unit_of_measurement="°C")
    if event == "check_notice":
        await conclude(hass)
        (issue,) = issues_of(hass, "self_check_fine")
    # Everything so far is on disk (the delayed save went out).
    await flush_store(manager_of(hass).checks._store)
    written = copy.deepcopy(hass_storage[checks.STORAGE_KEY])
    if event.startswith("check"):
        await finish_check(hass, entry, device)
    elif event == "window":
        await conclude(hass)
        (issue,) = issues_of(hass, "self_check_fine")
    else:
        zha.remove_device(device.device_id)
        await manager_of(hass).async_refresh()
        await apply_list(hass)
        zha.reload()
        await settle(hass)
    await hard_restart(hass, entry, hass_storage, written)
    if event == "check_watching":
        # The report is lost (its link was shown), the watch goes on and ends with its notice.
        assert self_check(hass)["result"] == "watching" and manager_of(hass).checks.reports == []
        await conclude(hass)
        assert issues_of(hass, "self_check_fine")
    elif event == "check_notice":
        # The notice went with the check: nothing brings it back.
        await manager_of(hass).async_refresh()
        await conclude(hass)
        assert ir.async_get(hass).async_get_issue(const.DOMAIN, issue.issue_id) is None
    elif event == "window":
        # Watching again from the last count on disk: the next count ends it with the same notice, once.
        assert self_check(hass)["result"] == "watching"
        await conclude(hass)
        assert [i.issue_id for i in issues_of(hass, "self_check_fine")] == [issue.issue_id]
        assert self_check(hass)["result"] == "fine"
    else:
        # The stored self-check of the removed file is back for a moment: the first refresh drops it.
        assert self_check(hass) is None


# Disabled entities: a quirk file can start some disabled as z2m does (ZHA: entity_registry_enabled_default
# False, so the registry says disabled_by integration). A user can disable any. A disabled entity has no
# state: neither check judges it.

DISABLERS = [er.RegistryEntryDisabler.INTEGRATION, er.RegistryEntryDisabler.USER]


def disable(hass: HomeAssistant, device: LiveDevice, suffix: str, by: er.RegistryEntryDisabler | None) -> None:
    """The entity is disabled (by) or enabled (None). Disabled, Home Assistant removes its state."""
    entity_id = device.entity_ids[suffix]
    er.async_get(hass).async_update_entity(entity_id, disabled_by=by)
    if by is not None:
        hass.states.async_remove(entity_id)


@pytest.mark.parametrize("by", DISABLERS)
@pytest.mark.parametrize("case", ["missing", "evidence"])
async def test_the_self_check_does_not_judge_a_disabled_entity(hass: HomeAssistant, zha: Zha, repo_dir: Path, freezer, by, case: str):
    """missing: the device answers the data query without the disabled humidity. That is no missing
    measurement: the temperature that came makes the file look fine. evidence: only the disabled
    humidity gets values. They are no evidence: no value of the file came that could be judged."""
    device = radar(hass, zha)
    tuya = device.clusters[0xEF00]
    disable(hass, device, "humidity", by)
    if case == "missing":
        tuya.answer = lambda: (tuya.frame(1), device.value("temperature", "21.5", device_class="temperature", unit_of_measurement="°C"))
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    if case == "evidence":
        freezer.tick(timedelta(minutes=5))
        tuya.send("humidity")
    await watch_for(hass, freezer, 25)
    if case == "missing":
        (issue,) = issues_of(hass, "self_check_fine")
        assert issue.translation_placeholders["devices"].splitlines()[1:] == []
    else:
        (issue,) = issues_of(hass, "self_check_unproven")
        assert issue.translation_placeholders["devices"].splitlines()[1:] == ["    - no value from the device for sensor: Temperature"]


async def test_an_entity_enabled_later_counts_from_then_on(hass: HomeAssistant, zha: Zha, repo_dir: Path, freezer):
    """Enabling an entity makes Home Assistant reload ZHA, the refresh after it watches the entity. What it
    shows from then on is judged, also a value that came while it was disabled (ZHA kept it)."""
    device = radar(hass, zha)
    disable(hass, device, "humidity", er.RegistryEntryDisabler.INTEGRATION)
    await installed(hass, zha, repo_dir, {IEEE_A: TUYA_A})
    device.value("temperature", "21.5", device_class="temperature", unit_of_measurement="°C")
    # A humidity far off while disabled: no state, nothing to judge yet.
    device.clusters[0xEF00].send("humidity")
    await watch_for(hass, freezer, 2)
    assert self_check(hass)["devices"][0]["findings"] == []
    # Enabled: ZHA reloads and shows the value it kept. No new value comes.
    disable(hass, device, "humidity", None)
    zha.start_reload()
    device.state("humidity", "150", device_class="humidity", unit_of_measurement="%")
    zha.finish_reload()
    await settle(hass)
    await watch_for(hass, freezer, 23)
    (issue,) = issues_of(hass, "self_check_off")
    assert issue.translation_placeholders["devices"].splitlines()[1:] == ["    - sensor: Humidity: 150 % is outside 0 to 100 %"]


@pytest.mark.parametrize("by", DISABLERS)
async def test_the_device_check_does_not_offer_a_disabled_entity(hass: HomeAssistant, zha: Zha, repo_dir: Path, by):
    """A disabled entity of the file is not read, not shown and not offered. The report names it in a
    line of its own, so a missing row does not look like a fault of the file."""
    added, local, basic = CustomCluster(0x0402), LocalDataCluster(0xFC00), Cluster(0x0000)
    device = LiveDevice(hass, zha, IEEE_C, "Acme", "C-1", [
        ("sensor", "temperature", added, "measured_value", None, False),
        ("sensor", "humidity", added, "humidity_value", None, False),
        ("select", "mode", local, "mode", "config", True),
        ("sensor", "rssi", basic, "rssi", None, False),
    ])
    disable(hass, device, "humidity", by)
    er.async_get(hass).async_update_entity(device.entity_ids["humidity"], name="Pantry humidity by the user")
    await installed(hass, zha, repo_dir, {IEEE_C: ACME_C})
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    device.state("temperature", "21.5", device_class="temperature", unit_of_measurement="°C")
    result = await run_check(hass, entry, device.device_id)
    assert device.clusters[0x0402].reads == [(["measured_value"], False)]
    assert [option["value"] for option in result["data_schema"].schema["matches"].config["options"]] == ["1-mode", "1-temperature"]
    assert "humidity" not in result["description_placeholders"]["entities"].lower()
    result = await continue_check(hass, result, {"matches": ["1-temperature"]})
    report = link_fields(await report_link(hass, result))["report"]
    assert "Disabled in Home Assistant (not part of the check): sensor: Humidity value" in report
    assert "| not checked | sensor: Humidity" not in report and "pantry" not in report.lower()


# The report and its link


def result_with(rows: int, name: str = "Acme") -> SimpleNamespace:
    row = checks.Row
    return checks.CheckResult(
        device_id="d", name=NAME, manufacturer=name, model="C-1", firmware="0x46", file="z2m_acme_c.py", revision="0123abcd", changed=False, quirk="z2m_acme_c",
        asked="read of 3 attributes", versions="Home Assistant 2026.11.0, zha 2.4.2, zha-quirks 2.4.1", answer="it answered", answered=True,
        rows=[row(f"1-value_{i}", f"sensor: Value {i}", "", "21.5", "°C", "now") for i in range(rows)], others=[], left_over=["switch: Old"], unmapped=[],
        content="content-3", time="2026-11-15T12:00:00+00:00",
    )


def test_a_long_report_drops_columns_then_points_to_the_diagnostics():
    entry = {"vendor": "Acme", "model": "C-1"}
    small = text.check_url(entry, result_with(3), {"1-value_0": "yes"}, "2026.11.0")
    assert "Unique id suffix" in link_fields(small)["report"]
    for rows, expected in ((40, "compact"), (400, "note")):
        url = text.check_url(entry, result_with(rows), {"1-value_0": "yes"}, "2026.11.0")
        assert len(url) <= text.REPORT_LIMIT
        report = link_fields(url)["report"]
        if expected == "compact":
            assert "Unique id suffix" not in report and "| yes | sensor: Value 0 | 21.5 | °C | now |  |" in report
        else:
            assert report == text.CHECK_TOO_LONG
    assert "Left over from an earlier quirk (not part of the check): switch: Old" in text.check_report(result_with(1), {})


def test_many_values_marked_wrong_still_fit_the_link():
    """The wrong values are listed in the form's problem field: past the limit the last ones are counted
    instead (the report in the diagnostics names them all)."""
    result = result_with(50)
    url = text.check_url({"vendor": "Acme", "model": "C-1"}, result, {row.suffix: "no" for row in result.rows}, "2026.11.0")
    assert len(url) <= text.REPORT_LIMIT
    fields = link_fields(url)
    problem = fields["problem"].splitlines()
    assert problem[0] == "- sensor: Value 0: Home Assistant shows 21.5 °C, the device shows something else"
    assert problem[-1] == f"- and {50 - len(problem) + 1} more (all of them in the report)" and len(problem) > 10
    assert fields["diagnostics"] == text.CHECK_TOO_LONG


def test_a_long_self_check_link_leaves_out_the_last_findings():
    found = [("Acme", "C-1", [f"sensor: Value {i}: {i * 1000} °C is outside -50 to 150 °C" for i in range(60)])]
    url = text.problem_url({"vendor": "Acme", "model": "C-1"}, "z2m_acme_c.py", "0123abcd", found, "2026.11.0")
    assert len(url) <= text.REPORT_LIMIT
    problem = link_fields(url)["problem"].splitlines()
    assert problem[0] == "Found by the self-check of the xquirks integration:" and 1 < len(problem) <= text.MAX_PROBLEM_LINES + 1
    assert problem[1] == "- Acme C-1: sensor: Value 0: 0 °C is outside -50 to 150 °C"


def test_the_forms_ask_for_what_the_links_fill_in():
    """The device check's report goes into the Device test form's report field. The link fills the form in, so its text
    says nothing about the probe: the report field points a test without the integration to it."""
    form = yaml.safe_load((FORMS / text.VERIFY_FORM).read_text())
    intro = form["body"][0]["attributes"]["value"]
    assert "probe" not in intro.lower() and "CONTRIBUTING" not in intro
    (report,) = [field for field in form["body"] if field.get("id") == "report"]
    hint = report["attributes"]["description"]
    assert "probe" in hint and "CONTRIBUTING.md#a-device-test-verified" in hint
    assert {"file", "revision", "ha", "report"} <= form_ids(text.VERIFY_FORM)
    assert {"file", "revision", "ha", "problem", "diagnostics"} <= form_ids(text.PROBLEM_FORM)


def test_every_self_check_notice_and_check_step_has_its_texts():
    folder = integration.FOLDER / "translations"
    import json

    for path in sorted(folder.glob("*.json")):
        strings = json.loads(path.read_text())
        assert set(checks.KINDS) <= set(strings["issues"]), path.name
        options = strings["options"]
        # The options keep only the settings: the device check is a subentry flow (test_self_check_flow.py checks its texts).
        assert options == {"step": {"settings": options["step"]["settings"]}}, path.name
        check = strings["config_subentries"]["device_check"]
        assert set(check["error"]) == {"both", "nothing"} and set(check["progress"]) == {"asking"}, path.name


# Plausible values


@pytest.mark.parametrize(
    ("domain", "state", "attributes", "expected"),
    [
        ("sensor", "21.5", {"device_class": "temperature", "unit_of_measurement": "°C"}, None),
        ("sensor", "3276.7", {"device_class": "temperature", "unit_of_measurement": "°C"}, "3276.7 °C is outside -50 to 150 °C"),
        ("sensor", "-60", {"device_class": "temperature", "unit_of_measurement": "°C"}, "-60 °C is outside -50 to 150 °C"),
        ("sensor", "200", {"device_class": "temperature", "unit_of_measurement": "°F"}, None),
        ("sensor", "400", {"device_class": "temperature", "unit_of_measurement": "°F"}, "400 °F is outside -50 to 150 °C"),
        ("sensor", "101", {"device_class": "humidity", "unit_of_measurement": "%"}, "101 % is outside 0 to 100 %"),
        ("sensor", "-1", {"device_class": "battery", "unit_of_measurement": "%"}, "-1 % is outside 0 to 100 %"),
        ("sensor", "1013", {"device_class": "atmospheric_pressure", "unit_of_measurement": "hPa"}, None),
        ("sensor", "101.3", {"device_class": "atmospheric_pressure", "unit_of_measurement": "kPa"}, None),
        ("sensor", "10130", {"device_class": "atmospheric_pressure", "unit_of_measurement": "hPa"}, "10130 hPa is outside 300 to 1100 hPa"),
        ("sensor", "230000", {"device_class": "voltage", "unit_of_measurement": "mV"}, None),
        ("sensor", "2300", {"device_class": "voltage", "unit_of_measurement": "V"}, "2300 V is outside 0 to 1000 V"),
        ("sensor", "0.95", {"device_class": "power_factor"}, None),
        # Home Assistant shows the micro unit with the Greek mu (U+03BC), older code wrote the micro sign (U+00B5).
        ("sensor", "5000", {"device_class": "pm25", "unit_of_measurement": "\u03bcg/m\u00b3"}, "5000 \u03bcg/m\u00b3 is outside 0 to 2000 \u03bcg/m\u00b3"),
        ("sensor", "5000", {"device_class": "pm10", "unit_of_measurement": "\u00b5g/m\u00b3"}, "5000 \u00b5g/m\u00b3 is outside 0 to 2000 \u03bcg/m\u00b3"),
        ("sensor", "12", {"device_class": "pm1", "unit_of_measurement": "\u03bcg/m\u00b3"}, None),
        # A unit the table does not know, a class without a range, no number: nothing to say.
        ("sensor", "5000", {"device_class": "temperature", "unit_of_measurement": "mK"}, None),
        ("sensor", "-5000", {"device_class": "power", "unit_of_measurement": "W"}, None),
        ("sensor", "unknown", {"device_class": "temperature", "unit_of_measurement": "°C"}, None),
        ("sensor", "nan", {"device_class": "temperature", "unit_of_measurement": "°C"}, None),
        ("number", "50", {"min": 5, "max": 45}, "50 is outside its own range 5 to 45"),
        ("number", "45", {"min": 5, "max": 45}, None),
        # A choice outside the options never reaches the state machine (a select shows unknown, an enum sensor keeps its
        # state): nothing to judge in a state that has one.
        ("select", "undefined_0x05", {"options": ["low", "high"]}, None),
        ("select", "low", {"options": ["low", "high"]}, None),
        ("sensor", "undefined_0x02", {"device_class": "enum", "options": ["idle", "heating"]}, None),
        ("switch", "on", {}, None),
    ],
)
def test_implausible_values(domain: str, state: str, attributes: dict, expected: str | None):
    assert plausible.implausible(domain, state, attributes) == expected


# The listeners


async def test_a_watch_keeps_bounded_records_and_leaves_no_listener_behind():
    tuya = Cluster(0xEF00, handled=(1,))
    other = Cluster(0x0402)
    device = zigpy_device(tuya, other)
    clock = iter(range(10_000))
    watch = watch_module.Watch(device, tuya, clock=lambda: next(clock))
    watch.attach()
    watch.attach()
    # A Tuya device: the cleared event on each cluster too (a datapoint may reach any of them).
    assert tuya.listening == 5 and other.listening == 4 and device.listeners == [watch]
    for index in range(watch_module.MAX_KEYS + 50):
        other.send(f"attribute_{index}")
    tuya.frame(*range(1, 200))
    assert len(watch.fresh) == watch_module.MAX_KEYS and len(watch.unmapped) == watch_module.MAX_DATAPOINTS and 1 not in watch.unmapped
    watch.detach()
    assert tuya.listening == other.listening == 0 and device.listeners == []


def test_refusals_of_earlier_checks_do_not_crowd_out_those_of_a_later_one():
    """A watch full of refusals heard in earlier checks still keeps one heard now: the oldest goes."""
    tuya = Cluster(0xEF00, handled=(1,))
    tuya.endpoint.device = SimpleNamespace(name="0x1234")
    now = [0.0]
    watch = watch_module.Watch(SimpleNamespace(endpoints={1: SimpleNamespace(in_clusters={0xEF00: tuya})}), tuya, clock=lambda: now[0])
    watch.attach()
    watch.listen_refused(True)
    try:
        for dp in range(1, watch_module.MAX_DATAPOINTS + 1):
            now[0] = float(dp)
            tuya.refuse(dp, "earlier")
        now[0] = 100.0
        tuya.refuse(200, "now")
    finally:
        watch.detach()
    assert watch.refused_since(100.0) == [(200, "now")]
    assert len(watch.refused) == watch_module.MAX_DATAPOINTS and 1 not in watch.refused and 2 in watch.refused


async def test_reads_of_a_local_cluster_are_not_fresh_values():
    """A LocalDataCluster answers a read from its cache: only its updates count."""
    local = LocalDataCluster(0xFC00)
    watch = watch_module.Watch(zigpy_device(local))
    watch.attach()
    assert set(local.events) == {"attribute_updated", "attribute_report"}
    local.send("mode", "attribute_updated")
    assert set(watch.fresh) == {"1:64512:mode", "1:64512:*"}


async def test_a_value_home_assistant_sets_itself_is_no_value_from_the_device():
    """zigpy sends "attribute_updated" also for a write to a LocalDataCluster (every setting of a Tuya file) and for ZHA's
    switch after an on or off command: only an update while a frame from the device is handled counts."""
    local, on_off = LocalDataCluster(0xFC00), Cluster(0x0006)
    device = zigpy_device(local, on_off)
    watch = watch_module.Watch(device)
    watch.attach()
    local.send("mode", by_home_assistant=True)
    assert watch.fresh == {}
    # The device answers the switch's command; ZHA sets the attribute when its task goes on, after the frame closed.
    device.frame_comes()
    await asyncio.sleep(0)
    on_off.send("on_off", by_home_assistant=True)
    assert watch.fresh == {}
    # A frame from another device that is handled meanwhile does not count either.
    neighbour = watch_module.Watch(zigpy_device(Cluster(0x0402)))
    neighbour.attach()
    neighbour.device.frame_comes()
    local.send("mode", by_home_assistant=True)
    assert watch.fresh == {}
    # Nor does a write Home Assistant starts while a frame from the device is handled, in a task that runs at once
    # (eagerly, as Home Assistant starts its tasks): the task has the frame in its context, but it is not the frame's.
    async def write() -> None:
        local.send("mode", in_frame=True)

    device.frame_comes()
    task = asyncio.Task(write(), loop=asyncio.get_running_loop(), eager_start=True)
    assert task.done() and watch.fresh == {}
    # The value from the device itself does.
    local.send("mode")
    assert set(watch.fresh) == {"1:64512:mode", "1:64512:*"}
    watch.detach()
    neighbour.detach()


# zigpy as pinned, no stand-in: a device with a cluster whose quirk changes the temperature on its way to the cache and
# passes it on to a local cluster and to a plain one, a second local cluster for a setting whose quirk changes its value
# too and mirrors it into the temperature cluster, and the listener (watch.py, loaded by path) on it.
READ_ANSWER = """
import asyncio, collections, importlib.util, sys, warnings
from unittest.mock import AsyncMock, MagicMock
warnings.simplefilter("ignore")
import zigpy.device, zigpy.types as t
from zha.application.platforms import button
from zhaquirks import CustomCluster, LocalDataCluster
from zigpy.zcl import foundation
from zigpy.zcl.clusters.general import AnalogOutput
from zigpy.zcl.clusters.measurement import PressureMeasurement, RelativeHumidity, TemperatureMeasurement

spec = importlib.util.spec_from_file_location("watch_under_test", sys.argv[1])
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)


class Scaled(CustomCluster, TemperatureMeasurement):
    def _update_attribute(self, attrid, value):
        super()._update_attribute(attrid, value * 10 if attrid == 0 else value)
        if attrid == 0:
            self.endpoint.humidity.update_attribute(0, value * 2)
            self.endpoint.pressure.update_attribute(0, value * 3)


class Fed(LocalDataCluster, RelativeHumidity):
    pass


class Setting(LocalDataCluster, AnalogOutput):
    def _update_attribute(self, attrid, value):
        super()._update_attribute(attrid, value + 1)
        self.endpoint.temperature.update_attribute(TemperatureMeasurement.AttributeDefs.tolerance.id, value)


async def main():
    device = zigpy.device.Device(MagicMock(), t.EUI64.convert("00:11:22:33:44:55:66:77"), 0x1234)
    endpoint = device.add_endpoint(1)
    endpoint.profile_id, endpoint.device_type = 260, 0x0302
    scaled, fed, setting = Scaled(endpoint), Fed(endpoint), Setting(endpoint)
    for cluster in (scaled, fed, setting):
        endpoint.add_input_cluster(cluster.cluster_id, cluster)
    pressure = endpoint.add_input_cluster(PressureMeasurement.cluster_id)
    device.request = AsyncMock()
    endpoint.reply = AsyncMock()
    device._application._req_listeners = collections.defaultdict(list)
    seen = watch.Watch(device)
    seen.attach()

    def values():
        return [cluster.get(name) for cluster, name in ((scaled, "measured_value"), (fed, "measured_value"), (pressure, "measured_value"), (setting, "present_value"), (scaled, "tolerance"))]

    # Home Assistant sets the temperature and the setting itself: the quirk passes both on as it does a value of the device.
    scaled.update_attribute(0, 5)
    await setting.write_attributes({"present_value": 7})
    print("set", values(), sorted(seen.fresh))

    # From here on a listener of the temperature starts a write of Home Assistant's to the setting, in a task that runs at
    # once.
    tasks = []

    async def write():
        await setting.write_attributes({"present_value": 9})

    def listener(event):
        if event.attribute_id == 0:
            tasks.append(asyncio.Task(write(), loop=asyncio.get_running_loop(), eager_start=True))

    scaled.on_event("attribute_updated", listener)

    # The device answers a read with 215: zigpy hands the answer on in the task that asked, the quirk makes 2150 of it and
    # passes 430 and 645 on.
    record = foundation.ReadAttributeRecord(attrid=0, status=foundation.Status.SUCCESS, value=foundation.TypeValue(type=t.int16s, value=t.int16s(215)))
    scaled.read_attributes_raw = AsyncMock(return_value=[[record]])
    await scaled.read_attributes(["measured_value"], allow_cache=False)
    print("read", values(), [task.done() for task in tasks], sorted(seen.fresh))
    seen.fresh.clear()

    # The setting answers a read from its cache, which its quirk changes and mirrors as well.
    await setting.read_attributes(["present_value"], allow_cache=False)
    print("local read", values(), sorted(seen.fresh))

    # The device reports 215: the listener's write runs while the report's frame is still handled.
    nwk = t.AddrModeAddress(addr_mode=t.AddrMode.NWK, address=0x1234)
    device.packet_received(t.ZigbeePacket(src=nwk, src_ep=1, dst=t.AddrModeAddress(addr_mode=t.AddrMode.NWK, address=0), dst_ep=1, profile_id=260,
                                          cluster_id=0x0402, data=t.SerializableBytes(bytes([0x18, 0x07, 0x0A, 0x00, 0x00, 0x29, 0xD7, 0x00])), lqi=255, rssi=-40))
    print("report", values(), [task.done() for task in tasks], sorted(seen.fresh))
    seen.detach()
    print("buttons", button.Button.PLATFORM == watch.BUTTON, button.WriteAttributeButton.PLATFORM == watch.BUTTON)

asyncio.run(main())
"""


def test_the_answer_to_a_read_counts_and_a_write_started_in_a_frame_does_not():
    """No stand-in: zigpy tells the value of a read's answer that the quirk changed as "attribute_updated", in the task
    that asked and after the frame closed. It is a value from the device all the same, on the cluster that was read and
    on the clusters the quirk passes it on to, as with a report. The same event for a value Home Assistant set itself is
    none, nor for a read a local cluster answers from its cache, nor for a write to another cluster that a listener of the
    value starts in a task that runs at once, while zigpy still hands the read's answer on or while the report's frame is
    still handled: not on that cluster and not on the read one its quirk mirrors the write into. ZHA's buttons are of the
    platform the watch knows them by."""
    out = subprocess.run([sys.executable, "-I", "-c", READ_ANSWER, str(integration.FOLDER / "watch.py")], capture_output=True, text=True, timeout=300, check=False)
    # The temperature cluster, the local cluster and the plain one the quirk feeds: never the setting, never the tolerance.
    keys = str([f"1:{cluster}:{attribute}" for cluster in (1026, 1027, 1029) for attribute in ("*", "measured_value")])
    assert out.stdout.strip().splitlines()[-5:] == [
        "set [50, 10, 15, 8, 7] []",
        f"read [2150, 430, 645, 10, 9] [True] {keys}",
        "local read [2150, 430, 645, 11.0, 10.0] []",
        f"report [2150, 430, 645, 10, 9] [True, True] {keys}",
        "buttons True True",
    ], out.stdout[-1500:] + out.stderr[-3000:]


def test_listeners_and_entities_on_a_real_quirk(tmp_path: Path):
    """No stand-in: the frozen MOES Tuya file applied to a zigpy device and a ZHA device built from it.
    The watch sees a Tuya frame as the attribute update the quirk makes of it and the datapoint it
    does not handle, not a value Home Assistant writes itself; each entity of the file reads an
    attribute of the Tuya MCU cluster; detach leaves no listener. The device check reads its power
    source as ZHA shows it."""
    script = f"""
import asyncio, collections, pathlib, sys, warnings
from unittest.mock import AsyncMock, MagicMock
warnings.simplefilter("ignore")
sys.path.insert(0, {str(ROOT)!r})
import zhaquirks
import zigpy.device, zigpy.types as t
from zha.quirks import DEVICE_REGISTRY

zhaquirks.setup(sys.argv[1])
from zha.zigbee.device import Device
from zigpy.zdo import types as zdo_t
from {PACKAGE} import checks, manager, watch

def joined():
    device = zigpy.device.Device(MagicMock(), t.EUI64.convert("00:11:22:33:44:55:66:77"), 0x1234)
    endpoint = device.add_endpoint(1)
    endpoint.profile_id, endpoint.device_type = 260, 0x0051
    for cluster_id in (0x0000, 0x0004, 0x0005, 0xEF00):
        endpoint.add_input_cluster(cluster_id)
    for cluster_id in (0x000A, 0x0019):
        endpoint.add_output_cluster(cluster_id)
    device.manufacturer, device.model = "_TZE284_2fnssffc", "TS0601"
    return device

async def main():
    quirked = DEVICE_REGISTRY.resolve(joined())
    zha_device = Device.new(quirked, MagicMock())
    print("power", checks.mains_powered(zha_device))
    # A mains router (0x8E: mains powered among the capability flags), as the node descriptor says.
    quirked.node_desc = zdo_t.NodeDescriptor(1, 64, 0x8E, 4476, 82, 82, 0, 82, 0)
    print("power", checks.mains_powered(Device.new(quirked, MagicMock())))
    zha_device._discover_new_entities()
    for entity in zha_device._pending_entities:
        zha_device._add_entity(entity, emit_event=False)
    sources = watch.entity_sources(zha_device)
    print("energy", sources["00:11:22:33:44:55:66:77-1-energy"])
    print("restart", sources["00:11:22:33:44:55:66:77-1-device_restart"])
    print("rssi", sources["00:11:22:33:44:55:66:77-1-0-rssi"])
    tuya = manager.query_cluster(zha_device)

    def listening():
        return sum(len(listeners) for listeners in tuya._event_listeners.values()) + len(tuya._listeners)

    before = listening(), len(quirked._listeners)
    seen = watch.Watch(quirked, tuya)
    seen.attach()
    quirked.request = AsyncMock()
    tuya.endpoint.reply = AsyncMock()
    quirked._application._req_listeners = collections.defaultdict(list)
    # A report of datapoint 1 (energy, 100) and of datapoint 200, which the file does not handle, the way zigpy hands a
    # frame from the device on.
    nwk = t.AddrModeAddress(addr_mode=t.AddrMode.NWK, address=0x1234)
    data = bytes([0x09, 0x05, 0x02, 0x00, 0x05, 0x01, 0x02, 0x00, 0x04, 0, 0, 0, 100, 200, 0x02, 0x00, 0x04, 0, 0, 0, 7])
    quirked.packet_received(t.ZigbeePacket(src=nwk, src_ep=1, dst=t.AddrModeAddress(addr_mode=t.AddrMode.NWK, address=0), dst_ep=1, profile_id=260,
                                           cluster_id=0xEF00, data=t.SerializableBytes(data), lqi=255, rssi=-40))
    await asyncio.sleep(0)
    print("fresh", sorted(seen.fresh), "unmapped", sorted(seen.unmapped))
    # Home Assistant writes the attribute itself: zigpy sends the same attribute events, the device sent nothing.
    seen.fresh.clear()
    await tuya.write_attributes({{"energy": 5}})
    await asyncio.sleep(0)
    print("written", sorted(seen.fresh))
    seen.detach()
    print("left", listening() - before[0], len(quirked._listeners) - before[1])

asyncio.run(main())
"""
    (tmp_path / "custom").mkdir()
    (tmp_path / "custom" / "z2m_moes_zm6lt1.py").write_bytes((FROZEN / "z2m_moes_zm6lt1.py.txt").read_bytes())
    out = subprocess.run([sys.executable, "-I", "-c", script, str(tmp_path / "custom")], capture_output=True, text=True, timeout=300, check=False)
    assert out.stdout.strip().splitlines()[-8:] == [
        "power None",
        "power True",
        "energy ('1:61184:energy', True)",
        "restart ('1:61184:device_restart', True)",
        "rssi (None, False)",
        "fresh ['1:61184:*', '1:61184:energy'] unmapped [200]",
        "written []",
        "left 0 0",
    ], out.stdout[-1500:] + out.stderr[-3000:]
