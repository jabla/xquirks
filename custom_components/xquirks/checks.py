"""The device check and the self-check after an install: what a device that runs an xquirks file sends.

Listeners (watch.py) sit on the devices that run a file of ours and are not left out, while ZHA is
loaded. They only note which attribute got a fresh value and which Tuya datapoints came: nothing is
polled, nothing is written, nothing leaves Home Assistant by itself.

- The device check (the options flow's "Check a device") asks one device for its current values (a
  Tuya data query, or a read of the attributes behind the file's entities), waits briefly and returns
  what it shows. The user marks what matches the device. The report it makes names no address, no
  device or area name and no entity id: it may go into a public issue.
- The self-check starts when ZHA loaded a file the change list installed or updated. For 24 hours of
  observation (only while ZHA runs, kept across restarts) it notes implausible values, entities that
  get no value, datapoints the quirk does not handle and those its Tuya cluster refused (a finding: the
  value never reached the entity). Then one notice per file and revision says
  whether the values look off, look fine (a value of the file came from the device and looked right)
  or did not come, and invites the device check.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import importlib.metadata
import logging
import os
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, State, callback
from homeassistant.core_config import DATA_CUSTOMIZE
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import text
from .const import CONF_EXCLUDED, DOMAIN, TUYA_QUERY_DATA, ZHA_DOMAIN
from .plausible import NO_VALUE, implausible
from .plan import stem
from .watch import MAX_DATAPOINTS, MAX_KEYS, Watch, cluster_key, entity_sources, key_of, local, log_heard, mapped_keys, wall_clock

if TYPE_CHECKING:
    from .manager import QuirkManager

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = f"{DOMAIN}.checks"
STORAGE_VERSION = 1
# Observation time a self-check needs, and the age at which an unfinished one ends without a notice.
WINDOW = timedelta(hours=24)
MAX_AGE = timedelta(days=7)
# How often the observation time is counted, the listeners checked and a finished self-check concluded.
TICK = timedelta(minutes=10)
# Datapoints within this many seconds after a delivered data query count as its answer (a device answers
# within a few seconds, a report of its own in that time is unlikely). Counted from the delivery: zigpy may
# need a retry of seconds before a sleepy device gets it.
ANSWER = 10
# The device check waits at most CHECK_ASK seconds for its query or reads (zigpy itself waits up to 28 s
# per try for a sleeping device), then until no new value came for CHECK_QUIET seconds, at most
# CHECK_WAIT seconds: half a minute in all, as its dialog says. No wait when the query or every read
# failed: no value can be its answer.
CHECK_ASK = 20.0
CHECK_WAIT = 10.0
CHECK_QUIET = 3.0
CHECK_STEP = 0.5
# Seconds after a fresh value until its entities are checked (Home Assistant has their new state then).
VALUE_DELAY = 1
# Bounds: findings per device, devices per self-check, reports kept for the diagnostics.
MAX_FINDINGS = 20
MAX_DEVICES = 10
MAX_REPORTS = 5
# Notices created, remembered so a file installed again at the same revision gets none.
MAX_NOTIFIED = 200
SAVE_DELAY = 30

# The notices a self-check ends with: something looked off, a value of the file came and looked right
# (the evidence that the file works), no value of the file came.
FINE = "self_check_fine"
OFF = "self_check_off"
UNPROVEN = "self_check_unproven"
KINDS = (FINE, OFF, UNPROVEN)

# What the device check asked when the file's values come only when the device sends them.
NOTHING_ASKED = "nothing (the file's values come only when the device sends them)"
# What an entity that reads one attribute shows right after a value of it came: no value. A select of the file that
# does not know the option the device sent shows unknown (Home Assistant's SelectEntity.state).
SHOWS_UNKNOWN = "shows unknown, although a value came from the device"
# The ZCL status of a read for an attribute the device does not have (zigpy's foundation.Status), and the remark of its row.
UNSUPPORTED_ATTRIBUTE = 0x86
UNSUPPORTED = "the device does not support this attribute"
# While zigpy's cluster log is set above warnings a self-check of a Tuya device cannot rule out a refusal behind an entity
# that got no value (watch.py sees the ones the frame shows): its notice says so (Checks._conclude).
QUIET_LOG = "refused values may not show here: the log level of zigpy.zcl is above warning"

# How fresh a value is in the device check.
NOW = "now"
SINCE_LOAD = "since load"
STORED = "stored"


@dataclasses.dataclass(frozen=True)
class Spec:
    """An entity of a watched device that reads one attribute."""

    entity_id: str  # for the state machine only: it carries names the user gave, it never leaves
    suffix: str  # its unique id after the IEEE address
    name: str  # platform: name, as the integration named it
    domain: str
    category: str | None
    from_file: bool
    key: str
    cluster: Any = dataclasses.field(default=None, compare=False)
    attribute: str = ""


@dataclasses.dataclass
class Row:
    """An entity of the file as the device check shows it."""

    suffix: str
    name: str
    category: str
    state: str
    unit: str
    fresh: str
    remark: str = ""

    @property
    def value(self) -> str:
        return f"{self.state} {self.unit}".strip()


@dataclasses.dataclass
class Reads:
    """What the reads of a device check got back: whether the device answered one, the attribute keys (watch.key_of) it
    sent a value for and those it does not support (UNSUPPORTED_ATTRIBUTE)."""

    answered: bool = False
    came: set[str] = dataclasses.field(default_factory=set)
    unsupported: set[str] = dataclasses.field(default_factory=set)


@dataclasses.dataclass
class CheckResult:
    """What a device check found. name is the device's name in Home Assistant: for the dialog only."""

    device_id: str
    name: str
    manufacturer: str
    model: str
    firmware: str | None
    file: str
    revision: str | None
    changed: bool
    quirk: str
    asked: str
    versions: str
    answer: str  # empty when nothing was asked
    answered: bool | None  # None: nothing was asked
    rows: list[Row]
    others: list[str]
    left_over: list[str]
    unmapped: list[int]
    content: str | None
    time: str
    # Runs on mains as ZHA shows its power source; None when unknown (for the dialog only).
    mains: bool | None = None
    # The file's entities that are disabled in Home Assistant: no state, nothing to compare.
    disabled: list[str] = dataclasses.field(default_factory=list)
    # The Tuya datapoints its Tuya cluster refused during the check: (datapoint, why).
    refused: list[tuple[int, str]] = dataclasses.field(default_factory=list)
    # A change xquirks made to the file waits for a ZHA reload: the device runs the version ZHA loaded before, whose
    # revision is not kept (revision is None then).
    waiting: bool = False


def mains_powered(zha_device: Any) -> bool | None:
    """Whether a ZHA device runs on mains: ZHA's power source, else zigpy's node descriptor. None when
    neither says (ZHA shows "Battery or Unknown" then)."""
    for source in (zha_device, getattr(getattr(zha_device, "device", None), "node_desc", None)):
        try:
            value = getattr(source, "is_mains_powered", None)
        except Exception:  # noqa: BLE001 - a device whose objects look otherwise has no known power source
            value = None
        if isinstance(value, bool):
            return value
    return None


def public_name(entry: er.RegistryEntry, device_class: str | None = None) -> str:
    """How a report names an entity: platform and the name the integration gave it, else its
    translation key, the device class the integration gave it (Checks.device_class) or its unique id
    suffix. Never a name the user gave, never its entity id."""
    suffix = entry.unique_id.split("-", 1)[-1]
    return f"{entry.domain}: {entry.original_name or entry.translation_key or device_class or suffix}"


@functools.cache
def _versions() -> tuple[str, str]:
    """The versions of zha and zha-quirks (reads package metadata: call it in the executor)."""
    found = []
    for package in ("zha", "zha-quirks"):
        try:
            found.append(importlib.metadata.version(package))
        except importlib.metadata.PackageNotFoundError:
            found.append("unknown")
    return found[0], found[1]


class Checks:
    def __init__(self, manager: QuirkManager, clock: Callable[[], float] | None = None) -> None:
        self.manager = manager
        # The one wall clock of the checks, the listeners and the data query stamps (seconds).
        self.now: Callable[[], float] = clock or wall_clock
        self.hass: HomeAssistant = manager.hass
        self._store: Store[dict[str, Any]] = Store(self.hass, STORAGE_VERSION, STORAGE_KEY)
        # file name -> its self-check, as stored (_start, _new_device)
        self.observations: dict[str, dict[str, Any]] = {}
        self.reports: list[dict[str, str]] = []
        # device id -> its listeners and its entities by attribute key, while ZHA is loaded
        self.watches: dict[str, Watch] = {}
        self._specs: dict[str, dict[str, list[Spec]]] = {}
        # device id -> when the last data query to it was sent and when it was delivered (this run)
        self._queried: dict[str, tuple[float, float]] = {}
        self._pending: set[tuple[str, str]] = set()
        self._value_timer: CALLBACK_TYPE | None = None
        self._last_tick: datetime | None = None
        # The notices created (issue ids, newest last).
        self.notified: list[str] = []
        # device id -> the file of ours it runs, for the watched devices (as the last refresh read them).
        self._files: dict[str, str] = {}
        # device id -> the device checks of it that run now.
        self._checking: dict[str, int] = {}
        self.closed = False

    # Setup, storage, unload

    async def async_setup(self) -> None:
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            observations = stored.get("observations")
            if isinstance(observations, dict):
                self.observations = {name: obs for name, obs in observations.items() if self._valid(obs)}
            notified = stored.get("notified")
            if isinstance(notified, list):
                self.notified = [issue_id for issue_id in notified if isinstance(issue_id, str)][-MAX_NOTIFIED:]
            reports = stored.get("reports")
            if isinstance(reports, list):
                self.reports = [r for r in reports if isinstance(r, dict) and all(isinstance(v, str) for v in r.values())][-MAX_REPORTS:]
        entry = self.manager.entry
        entry.async_on_unload(async_track_time_interval(self.hass, self._tick, TICK))

    @staticmethod
    def _valid(observation: object) -> bool:
        """A stored self-check in the shape _start writes (another shape is dropped)."""
        shape = {"fresh": list, "frame": bool, "answered": bool, "unmapped": list, "findings": dict}
        return (
            isinstance(observation, dict)
            and isinstance(observation.get("revision"), str)
            and isinstance(observation.get("started"), (int, float))
            and isinstance(observation.get("observed"), (int, float))
            and isinstance(observation.get("devices"), dict)
            and all(isinstance(device, dict) and all(isinstance(device.get(key), kind) for key, kind in shape.items()) for device in observation["devices"].values())
        )

    def _data(self) -> dict[str, Any]:
        return {"observations": self.observations, "notified": self.notified, "reports": self.reports}

    @callback
    def _save(self, now: bool = False) -> None:
        if self.closed:
            return
        if now:
            # Written at once (a delayed save of no delay can still wait for a later one it was given).
            self.manager.entry.async_create_task(self.hass, self._store.async_save(self._data()), "xquirks self-check store")
        else:
            self._store.async_delay_save(self._data, SAVE_DELAY)

    async def async_close(self) -> None:
        """The entry unloads: the listeners go, the observation time so far and the store are kept."""
        self._count(dt_util.utcnow())
        self.closed = True
        self._detach_all()
        if self._value_timer is not None:
            self._value_timer()
            self._value_timer = None
        await self._store.async_save(self._data())

    # The devices that run a file of ours

    def device_class(self, entry: er.RegistryEntry, state: State | None) -> str | None:
        """The device class the integration gave an entity: the registry's, else its state's while the
        user set none, in the registry or with customize (ZHA's occupancy sensor leaves the registry's
        empty)."""
        if entry.original_device_class:
            return str(entry.original_device_class)
        if state is None or entry.device_class is not None:
            return None
        customize = self.hass.data.get(DATA_CUSTOMIZE)
        if customize is not None and "device_class" in customize.get(entry.entity_id):
            return None
        value = state.attributes.get("device_class")
        return value if isinstance(value, str) and value else None

    def _name(self, entry: er.RegistryEntry, state: State | None) -> str:
        return public_name(entry, self.device_class(entry, state))

    def _ours(self) -> dict[str, str]:
        """Module -> file name of the files of ours in the folder ZHA loads."""
        folder = self.manager.folder
        return {stem(os.path.basename(path)): os.path.basename(path) for path in self.manager.data["installed"] if folder and os.path.dirname(path) == folder}

    def _file_of(self, device_id: str) -> str | None:
        """The file of ours a device runs while it is not left out; None otherwise."""
        device = self.manager.devices.get(device_id)
        excluded = frozenset(self.manager.entry.options.get(CONF_EXCLUDED, ()))
        if device is None or device.disabled or device_id in excluded or not device.quirk:
            return None
        return self._ours().get(device.quirk)

    def _zha_devices(self) -> dict[str, Any]:
        """IEEE -> ZHA's device, empty when ZHA's devices cannot be read."""
        try:
            proxies = self.hass.data[ZHA_DOMAIN].gateway_proxy.device_proxies
            return {str(proxy.device.ieee): proxy.device for proxy in proxies.values()}
        except (AttributeError, KeyError, TypeError):
            return {}

    @callback
    def sync(self, started: Iterable[str] = ()) -> None:
        """After a refresh that read what ZHA applies: listen to the devices that run a file of ours
        (anew where ZHA made new objects for them), drop what no longer runs, start the self-checks of
        the files ZHA loaded new or updated (started: their names)."""
        if self.closed:
            return
        zha_devices = self._zha_devices()
        wanted: dict[str, tuple[str, Any]] = {}
        for device_id in self.manager.devices:
            name = self._file_of(device_id)
            zha_device = zha_devices.get(self.manager.ieees.get(device_id, ""))
            if name and zha_device is not None and getattr(zha_device, "device", None) is not None:
                wanted[device_id] = (name, zha_device)
        self._files = {device_id: name for device_id, (name, _zha) in wanted.items()}
        for device_id in list(self.watches):
            watch = self.watches[device_id]
            if device_id not in wanted or watch.device is not wanted[device_id][1].device:
                watch.detach()
                del self.watches[device_id]
                self._specs.pop(device_id, None)
        for device_id, (_name, zha_device) in wanted.items():
            if device_id not in self.watches:
                self._attach(device_id, zha_device)
            self._specs[device_id] = self._entity_specs(device_id, zha_device)
        if self._last_tick is None and self.watches:
            self._last_tick = dt_util.utcnow()
        for name in started:
            self._start(name, wanted)
        self._prune(wanted)
        self._listen_refused()

    def _listen_refused(self) -> None:
        """Each watch hears the datapoints its Tuya cluster refuses only while a self-check or a device check of its device
        runs: the handler on zigpy's cluster log sees every warning of every cluster. A self-check of a Tuya device notes
        when that log is set above warnings (quiet_log): the reasons cannot come then."""
        heard = log_heard()
        for device_id, watch in self.watches.items():
            state = self._running(device_id)
            watch.listen_refused(state is not None or bool(self._checking.get(device_id)))
            if state is not None and watch.tuya is not None and not heard and not state.get("quiet_log"):
                state["quiet_log"] = True
                self._save()

    def _attach(self, device_id: str, zha_device: Any) -> None:
        from .manager import query_cluster

        watch = Watch(
            zha_device.device,
            query_cluster(zha_device),
            clock=self.now,
            on_fresh=lambda _watch, key, device_id=device_id: self._fresh(device_id, key),
            on_datapoints=lambda _watch, dps, device_id=device_id: self._datapoints(device_id),
            on_refused=lambda _watch, dp, device_id=device_id: self._refused(device_id, dp),
        )
        try:
            watch.attach()
        except Exception:  # noqa: BLE001 - a device whose clusters look otherwise is not watched
            _LOGGER.debug("Cannot listen to %s", device_id, exc_info=True)
            watch.detach()
            return
        self.watches[device_id] = watch

    def _entity_specs(self, device_id: str, zha_device: Any) -> dict[str, list[Spec]]:
        """attribute key -> the entities of the device that read it (enabled, set up in this run)."""
        sources = entity_sources(zha_device)
        clusters = {uid: (getattr(entity, "_cluster", None) or getattr(entity, "cluster", None), getattr(entity, "_attribute_name", ""))
                    for (_platform, uid), entity in (getattr(zha_device, "platform_entities", None) or {}).items()}
        specs: dict[str, list[Spec]] = {}
        # Disabled entities stay out (no state to judge). One enabled later comes in at the refresh
        # after the reload of ZHA that Home Assistant makes for it.
        for entry in er.async_entries_for_device(er.async_get(self.hass), device_id, include_disabled_entities=False):
            if entry.platform != ZHA_DOMAIN or entry.unique_id not in sources:
                continue
            key, from_file = sources[entry.unique_id]
            if key is None:
                continue
            cluster, attribute = clusters.get(entry.unique_id, (None, ""))
            name = self._name(entry, self.hass.states.get(entry.entity_id))
            spec = Spec(entry.entity_id, entry.unique_id.split("-", 1)[-1], name, entry.domain, entry.entity_category, from_file, key, cluster, attribute or "")
            specs.setdefault(key, []).append(spec)
        return specs

    @callback
    def zha_down(self) -> None:
        """ZHA is not loaded: its objects go, so do the listeners. The observation time stops."""
        self._count(dt_util.utcnow())
        self._last_tick = None
        self._detach_all()

    def _detach_all(self) -> None:
        for watch in self.watches.values():
            watch.detach()
        self.watches.clear()
        self._specs.clear()
        self._files.clear()

    @callback
    def queried(self, ieee: str, sent: float, delivered: float) -> None:
        """A data query sent at this time reached this device when zigpy's sending ended (delivered). Datapoints
        that came while zigpy was still sending it count as its answer too."""
        device_id = next((device_id for device_id, value in self.manager.ieees.items() if value == ieee), None)
        if device_id is None:
            return
        self._queried[device_id] = (sent, delivered)
        watch = self.watches.get(device_id)
        if watch is not None and (watch.last_datapoints or 0) >= sent:
            self._datapoints(device_id)

    # The self-check

    def _revision(self, name: str) -> str | None:
        folder = self.manager.folder
        record = self.manager.data["installed"].get(os.path.join(folder, name)) if folder else None
        return record.get("revision") if isinstance(record, dict) else None

    def _start(self, name: str, wanted: dict[str, tuple[str, Any]]) -> None:
        revision = self._revision(name)
        devices = sorted(device_id for device_id, (file, _zha) in wanted.items() if file == name)[:MAX_DEVICES]
        if revision is None or not devices:
            return
        old = self.observations.get(name)
        if old is not None:
            self._drop_notice(name, old)
        self.observations[name] = {
            "revision": revision,
            "started": self.now(),
            "observed": 0.0,
            "devices": {device_id: self._new_device() for device_id in devices},
        }
        # On disk at once: the records forgot the change that started it, a hard stop now would lose it.
        self._save(now=True)

    @staticmethod
    def _new_device() -> dict[str, Any]:
        # refused: datapoint (as text) -> [why its Tuya cluster refused it, the attribute keys it goes to]. shown: the
        # entities (unique id suffixes) that showed a value after one came, nothing looked off with it. A device stored
        # by an earlier release has neither (refused_of, shown_of).
        return {"fresh": [], "frame": False, "answered": False, "unmapped": [], "findings": {}, "refused": {}, "shown": []}

    @staticmethod
    def shown_of(state: dict[str, Any]) -> list[str]:
        """The entities of a device's stored state that showed a value (_new_device), a list of suffixes (anything else: none)."""
        shown = state.get("shown")
        return [suffix for suffix in shown if isinstance(suffix, str)] if isinstance(shown, list) else []

    @staticmethod
    def refused_of(state: dict[str, Any]) -> dict[str, list]:
        """The refused datapoints of a device's stored state, in the shape _new_device gives (anything else: none)."""
        refused = state.get("refused")
        if not isinstance(refused, dict):
            return {}
        return {dp: value for dp, value in refused.items() if isinstance(dp, str) and dp.isdigit() and isinstance(value, list) and len(value) == 2
                and isinstance(value[0], str) and isinstance(value[1], list)}

    def _prune(self, wanted: dict[str, tuple[str, Any]]) -> None:
        """Self-checks of files no longer in the folder at their revision go (and their notices), so
        do the devices that no longer run their file."""
        if self.manager.folder is None:
            return
        changed = False
        for name, observation in list(self.observations.items()):
            if self._revision(name) != observation["revision"]:
                self._drop_notice(name, observation)
                del self.observations[name]
                changed = True
                continue
            if observation.get("done"):
                continue
            for device_id in list(observation["devices"]):
                device = self.manager.devices.get(device_id)
                # A device whose quirk ZHA does not report (yet) stays.
                if device is not None and device.quirk is not None and self._file_of(device_id) != name:
                    del observation["devices"][device_id]
                    changed = True
            if not observation["devices"]:
                del self.observations[name]
                changed = True
        if changed:
            self._save()

    def _running(self, device_id: str) -> dict[str, Any] | None:
        """The stored state of the device in the self-check that runs for its file; None without one."""
        name = self._files.get(device_id)
        observation = self.observations.get(name) if name else None
        if observation is None or observation.get("done"):
            return None
        return observation["devices"].get(device_id)

    @callback
    def _fresh(self, device_id: str, key: str) -> None:
        state = self._running(device_id)
        if state is None:
            return
        if not state["frame"]:
            state["frame"] = True
            self._save()
        if key not in state["fresh"] and len(state["fresh"]) < MAX_KEYS:
            state["fresh"].append(key)
            self._save()
        if any(spec.from_file for spec in self._specs.get(device_id, {}).get(key, ())):
            self._pending.add((device_id, key))
            if self._value_timer is None:
                self._value_timer = async_call_later(self.hass, VALUE_DELAY, self._check_values)

    @callback
    def _datapoints(self, device_id: str) -> None:
        state = self._running(device_id)
        if state is None:
            return
        before = (state["frame"], state["answered"], state["unmapped"])
        state["frame"] = True
        queried, watch = self._queried.get(device_id), self.watches.get(device_id)
        last = watch.last_datapoints if watch is not None else None
        if queried is not None and last is not None and queried[0] <= last <= queried[1] + ANSWER:
            state["answered"] = True
        state["unmapped"] = sorted(set(state["unmapped"]) | set(watch.unmapped if watch else ()))[:MAX_DATAPOINTS]
        if (state["frame"], state["answered"], state["unmapped"]) != before:
            self._save()

    @callback
    def _refused(self, device_id: str, dp: int) -> None:
        """The device's Tuya cluster refused a datapoint (zhaquirks' warning, watch.py): a finding of the self-check."""
        state = self._running(device_id)
        watch = self.watches.get(device_id)
        if state is None or watch is None or dp not in watch.refused:
            return
        refused = state["refused"] = self.refused_of(state)
        # A refusal without a reason (the frame showed it, the log did not say) keeps the reason the log gave before, also
        # one an earlier watch heard (the self-check goes on across a reload of ZHA and a restart, the watch does not).
        entry = [watch.refused[dp][1] or refused.get(str(dp), [""])[0], mapped_keys(watch.tuya, dp)]
        state["frame"] = True
        if refused.get(str(dp)) != entry and (str(dp) in refused or len(refused) < MAX_DATAPOINTS):
            refused[str(dp)] = entry
            self._save()

    @callback
    def _check_values(self, _now: datetime | None = None) -> None:
        self._value_timer = None
        pending, self._pending = self._pending, set()
        for device_id, key in sorted(pending):
            state = self._running(device_id)
            if state is None:
                continue
            for spec in self._specs.get(device_id, {}).get(key, ()):
                self._judge(state, spec, after_value=True)

    def _judge(self, state: dict[str, Any], spec: Spec, *, after_value: bool = False) -> None:
        """A finding when an entity of the file shows an implausible value now (one per entity), or no value right after
        a value of the one attribute it reads came (after_value): Home Assistant showed none of it. An entity that shows a
        value with nothing off is noted as shown: only that is evidence that the file works."""
        current = self.hass.states.get(spec.entity_id)
        if not spec.from_file or current is None:
            return
        reason = implausible(spec.domain, current.state, current.attributes)
        if reason is None and after_value and spec.attribute and current.state == "unknown":
            reason = SHOWS_UNKNOWN
        findings = state["findings"]
        if reason:
            if spec.suffix not in findings and len(findings) < MAX_FINDINGS:
                findings[spec.suffix] = f"{spec.name}: {reason}"
                self._save()
        elif current.state not in NO_VALUE:
            shown = state["shown"] = self.shown_of(state)
            if spec.suffix not in shown and len(shown) < MAX_KEYS:
                shown.append(spec.suffix)
                self._save()

    def _count(self, now: datetime) -> None:
        """Add the time since the last count to the running self-checks (only while listening)."""
        if self._last_tick is None or not self.watches:
            return
        seconds = min(max((now - self._last_tick).total_seconds(), 0.0), 2 * TICK.total_seconds())
        self._last_tick = now
        for observation in self.observations.values():
            if not observation.get("done"):
                observation["observed"] = float(observation["observed"]) + seconds

    @callback
    def _tick(self, now: datetime) -> None:
        if self.closed:
            return
        running = any(not observation.get("done") for observation in self.observations.values())
        self._count(now)
        # A device ZHA swapped (a re-interview) or dropped: the next refresh listens to the new object.
        zha_devices = self._zha_devices()
        if any(zha_devices.get(self.manager.ieees.get(device_id, ""), None) is None or zha_devices[self.manager.ieees[device_id]].device is not watch.device
               for device_id, watch in self.watches.items()):
            self.manager.schedule_refresh()
        for name, observation in list(self.observations.items()):
            if observation.get("done"):
                continue
            if self.now() - observation["started"] > MAX_AGE.total_seconds():
                observation["done"] = "expired"
            elif observation["observed"] >= WINDOW.total_seconds() and self.watches:
                self._conclude(name, observation)
        if running:
            self._listen_refused()
            self._save()

    def _conclude(self, name: str, observation: dict[str, Any]) -> None:
        """The window is over: the findings per device, one notice for the file.

        Off: a device showed a problem. Fine needs evidence: an entity of the file got a value from the
        device that looked right, on a ZCL file as on a Tuya file. Without problems and evidence the
        notice says that no value of the file came, whatever else the devices sent."""
        devices = self.manager.devices
        lines: dict[str, list[str]] = {}
        problems: dict[str, list[str]] = {}
        evidence = False
        for device_id, state in sorted(observation["devices"].items(), key=lambda item: devices[item[0]].name if item[0] in devices else ""):
            if device_id not in devices:
                continue
            # The enabled entities of the file (a disabled one has no state: it is not judged).
            ours = [spec for specs in self._specs.get(device_id, {}).values() for spec in specs if spec.from_file]
            # What they show at the end is judged too: an entity enabled after its value came (ZHA kept
            # the value) had no state to judge when it came.
            for spec in ours:
                if spec.key in state["fresh"]:
                    self._judge(state, spec)
            found, notes = list(state["findings"].values()), []
            # A datapoint the device sent and its Tuya cluster refused: the value never reached the entity, which is no
            # silent one then: the notice does not say "no value from the device".
            refused = self.refused_of(state)
            found += [f"sent Tuya datapoint {dp}, " + (f"refused: {reason}" if reason else f"refused ({text.NO_REASON})")
                      for dp, (reason, _keys) in sorted(refused.items(), key=lambda item: int(item[0]))]
            # The log of zigpy's clusters was set above warnings while it watched a Tuya device: a refusal the frame does
            # not show stays hidden, so an entity without a value is one no value reached.
            watch = self.watches.get(device_id)
            quiet = state.get("quiet_log") is True or (watch is not None and watch.tuya is not None and not log_heard())
            refused_keys = {key for _reason, keys in refused.values() for key in keys if isinstance(key, str)}
            # An entity that reads the whole cluster (ZHA's climate, cover, light) misses that value too.
            refused_keys |= {whole for key in refused_keys if (whole := cluster_key(key))}
            if not state["frame"]:
                notes.append("sent nothing while xquirks watched it")
            else:
                # Evidence: an entity of the file that showed a value after one came, nothing off with it. A fresh value
                # Home Assistant showed as unknown is none.
                shown = set(self.shown_of(state))
                evidence = evidence or bool({spec.suffix for spec in ours if spec.suffix in shown} - set(state["findings"]))
                # Settings stay out: a write-only one never comes, not even after a data query.
                silent = sorted({spec.name for spec in ours if spec.key not in state["fresh"] and spec.key not in refused_keys and spec.category != "config"})
                if state["answered"]:
                    found += [f"{entity}: no value reached it, although the device answered the data query" if quiet else
                              f"{entity}: no value from the device, although it answered the data query" for entity in silent]
                elif silent:
                    notes.append(f"no value reached {', '.join(silent)}" if quiet else f"no value from the device for {', '.join(silent)}")
                if quiet and silent:
                    notes.append(QUIET_LOG)
                if state["unmapped"]:
                    notes.append(f"sent Tuya datapoints this file does not show: {', '.join(str(dp) for dp in state['unmapped'])}")
            # At most MAX_FINDINGS listed, the rest counted.
            problems[device_id] = found[:MAX_FINDINGS] + ([f"and {len(found) - MAX_FINDINGS} more"] if len(found) > MAX_FINDINGS else [])
            lines[device_id] = problems[device_id] + notes
        off = any(problems.values())
        observation["done"] = "off" if off else "fine" if evidence else "unproven"
        entry = self.manager.entry_of(name)
        placeholders = {
            "quirk": self.manager.quirk_label(name),
            "file": name,
            "revision": observation["revision"],
            "devices": text.self_check_list(lines, devices),
            "picture": self.manager.picture(name),
        }
        if off:
            found = [(devices[i].manufacturer, devices[i].model, device_problems) for i, device_problems in problems.items() if device_problems]
            placeholders["link"] = text.problem_url(entry, name, observation["revision"], found, HA_VERSION)
        issue_id = self.issue_id(name, observation)
        if issue_id in self.notified:
            # This revision had its notice already (the file was installed again since).
            return
        self.notified = [*self.notified, issue_id][-MAX_NOTIFIED:]
        kind = OFF if off else FINE if evidence else UNPROVEN
        observation["notice"] = kind
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            data={"kind": kind, "file": name},
            is_fixable=False,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=kind,
            translation_placeholders=placeholders,
        )

    @staticmethod
    def issue_id(name: str, observation: dict[str, Any]) -> str:
        return f"self_check_{stem(name)}_{observation['revision']}"

    def keep_ids(self) -> frozenset[str]:
        """The self-check notices that stay: their file is still in the folder at their revision."""
        return frozenset(self.issue_id(name, obs) for name, obs in self.observations.items() if obs.get("notice") and not obs.get("closed"))

    def _drop_notice(self, name: str, observation: dict[str, Any]) -> None:
        if observation.get("notice"):
            ir.async_delete_issue(self.hass, DOMAIN, self.issue_id(name, observation))

    # The device check

    def candidates(self) -> list[tuple[str, str]]:
        """(device id, label) of the devices a check can ask: they run a file of ours, are not left
        out and ZHA has them. The label holds the device's name: for the dialog only."""
        found = []
        for device_id in self.watches:
            device, name = self.manager.devices.get(device_id), self._file_of(device_id)
            if device is not None and name:
                found.append((device_id, f"{device.name} ({name})"))
        return sorted(found, key=lambda item: item[1].lower())

    async def async_run_check(self, device_id: str) -> CheckResult | str:
        """Ask the device for its current values and read what it shows; the abort reason when it
        cannot be asked ("zha_not_loaded", "device_gone"). While it runs the watch of the device hears what its Tuya cluster
        refuses."""
        self._checking[device_id] = self._checking.get(device_id, 0) + 1
        self._listen_refused()
        try:
            return await self._async_run_check(device_id)
        except Exception:  # noqa: BLE001 - whatever went wrong, the dialog ends with a text
            _LOGGER.warning("The device check of %s failed", device_id, exc_info=True)
            return "check_failed"
        finally:
            if (left := self._checking.pop(device_id, 1) - 1) > 0:
                self._checking[device_id] = left
            self._listen_refused()

    async def _async_run_check(self, device_id: str) -> CheckResult | str:
        from .manager import query_cluster

        if not self.hass.config_entries.async_loaded_entries(ZHA_DOMAIN):
            return "zha_not_loaded"
        name, watch = self._file_of(device_id), self.watches.get(device_id)
        zha_device = self._zha_devices().get(self.manager.ieees.get(device_id, ""))
        if not name or watch is None or zha_device is None:
            return "device_gone"
        zha_version, quirks_version = await self.hass.async_add_executor_job(_versions)
        start = self.now()
        cluster = query_cluster(zha_device)
        read = Reads()
        asked = "Tuya data query" if cluster is not None else self._reads(device_id)[0]
        # failed: the query or every read failed, so nothing the device sends can be its answer.
        answer, failed = "", False
        try:
            async with asyncio.timeout(CHECK_ASK):
                if cluster is not None:
                    try:
                        await cluster.command(TUYA_QUERY_DATA, expect_reply=False)
                    except Exception as err:  # noqa: BLE001 - the dialog says so
                        answer, failed = f"not delivered ({str(err) or type(err).__name__})", True
                    else:
                        # Its answer counts for a self-check that runs, like the one after an install.
                        if (ieee := self.manager.ieees.get(device_id)) is not None:
                            self.queried(ieee, start, self.now())
                else:
                    answer = await self._read(device_id, read)
                    failed = bool(answer)
        except TimeoutError:
            # zigpy was still trying: the request may have reached the device, its values may come.
            answer = f"stopped waiting after {CHECK_ASK:g} s"
        answered: bool | None = None
        if asked != NOTHING_ASKED and not failed:
            await self._settle(device_id, watch, start)
        # The watch read again: a sync while the check waited may have replaced it.
        watches = self._watches_of(device_id, watch)
        # What the reads brought counts as it came, also a value a quirk changed on its way (no read event, watch.py).
        fresh_now = set().union(*(heard.fresh_since(start) for heard in watches), read.came, {cluster_key(key) for key in read.came})
        if asked != NOTHING_ASKED:
            answered = bool(fresh_now) or read.answered or max(heard.last_datapoints or 0 for heard in watches) >= start
            if not answer:
                answer = "it answered" if answered else "no answer"
        device = self.manager.devices.get(device_id)
        if device is None:
            # Gone from the device registry while the check waited.
            return "device_gone"
        registry = er.async_get(self.hass)
        sources = entity_sources(zha_device)
        rows, others, left_over, disabled = [], [], [], []
        for entry in sorted(er.async_entries_for_device(registry, device_id, include_disabled_entities=True), key=lambda e: e.unique_id.split("-", 1)[-1]):
            if entry.platform != ZHA_DOMAIN:
                continue
            key, from_file = sources.get(entry.unique_id, (None, False))
            if entry.disabled_by is not None:
                # No state, nothing to compare: not offered. The report names those of the file, so a
                # missing row is no puzzle. Other disabled entities stay out as before.
                if from_file:
                    disabled.append(self._name(entry, None))
                continue
            state = self.hass.states.get(entry.entity_id)
            if state is not None and state.attributes.get("restored"):
                left_over.append(self._name(entry, state))
                continue
            if not from_file:
                others.append(self._name(entry, state))
                continue
            if key in fresh_now:
                fresh = NOW
            elif key is not None and any(key in heard.fresh for heard in watches):
                fresh = SINCE_LOAD
            else:
                fresh = STORED
            value = state.state if state is not None else "unknown"
            remark = (implausible(entry.domain, value, state.attributes) or "") if state is not None and fresh != STORED else ""
            if not remark and fresh == NOW and state is not None and value == "unknown" and key is not None and cluster_key(key) != key:
                # Its one attribute got a value during the check, Home Assistant shows none of it (as the self-check notes).
                remark = SHOWS_UNKNOWN
            if key in read.unsupported:
                remark = UNSUPPORTED
            unit = str(state.attributes.get("unit_of_measurement") or "") if state is not None else ""
            rows.append(Row(entry.unique_id.split("-", 1)[-1], self._name(entry, state), entry.entity_category or "", value, unit, fresh, remark))
        device_entry = self._device_entry(device_id)
        # The record names what xquirks wrote, ZHA runs what it loaded: they differ while a change of the file waits.
        waiting = any(change.get("file") == name for change in self.manager.data["pending"]["changes"])
        return CheckResult(
            device_id=device_id,
            name=device.name,
            manufacturer=device.manufacturer,
            model=device.model,
            firmware=device_entry.sw_version if device_entry is not None else None,
            file=name,
            revision=None if waiting else self._revision(name),
            changed=self.manager.changed_on_disk(name),
            waiting=waiting,
            quirk=device.quirk or "",
            asked=asked,
            versions=f"Home Assistant {HA_VERSION}, zha {zha_version}, zha-quirks {quirks_version}",
            answer=answer,
            answered=answered,
            rows=rows,
            others=sorted(set(others)),
            left_over=sorted(set(left_over)),
            unmapped=sorted(set().union(*(heard.unmapped_since(start) for heard in watches))),
            content=self.manager.state.tag if self.manager.state else None,
            time=dt_util.utcnow().isoformat(timespec="seconds"),
            mains=mains_powered(zha_device),
            disabled=sorted(set(disabled)),
            # The later watch's reason for a datapoint both heard: it is the newer one.
            refused=sorted({dp: reason for heard in watches for dp, reason in heard.refused_since(start)}.items()),
        )

    def _device_entry(self, device_id: str) -> Any:
        from homeassistant.helpers import device_registry as dr

        return dr.async_get(self.hass).async_get(device_id)

    def _reads(self, device_id: str) -> tuple[str, list[tuple[Any, list[str]]]]:
        """What a check of a device without a Tuya data query asks: (what it says, the attribute names
        behind the file's entities per cluster, LocalDataClusters left out: they never reach the device)."""
        by_cluster: dict[int, tuple[Any, list[str]]] = {}
        for specs in self._specs.get(device_id, {}).values():
            for spec in specs:
                cluster = spec.cluster
                if not spec.from_file or cluster is None or not spec.attribute or not callable(getattr(cluster, "read_attributes", None)):
                    continue
                if local(cluster):
                    continue
                names = by_cluster.setdefault(id(cluster), (cluster, []))[1]
                if spec.attribute not in names:
                    names.append(spec.attribute)
        count = sum(len(names) for _cluster, names in by_cluster.values())
        return (f"read of {count} attributes" if count else NOTHING_ASKED), list(by_cluster.values())

    async def _read(self, device_id: str, found: Reads) -> str:
        """Read the attributes behind the file's entities from the device (not from ZHA's cache): why nothing
        answered (else ""). What the answers held goes into found as they come: a check stopped by its
        timeout keeps what came. Without the cache every attribute is asked, so a read that returns is the
        device's answer, values or statuses."""
        reads = self._reads(device_id)[1]
        errors = []
        for cluster, names in reads:
            try:
                result = await cluster.read_attributes(names, allow_cache=False)
            except Exception as err:  # noqa: BLE001 - the next cluster is read all the same
                errors.append(str(err) or type(err).__name__)
                continue
            found.answered = True
            success, failure = result if isinstance(result, (list, tuple)) and len(result) == 2 and all(isinstance(part, dict) for part in result) else ({}, {})
            endpoint = cluster.endpoint.endpoint_id
            found.came |= {key_of(endpoint, cluster.cluster_id, name) for name in names if name in success}
            found.unsupported |= {key_of(endpoint, cluster.cluster_id, name) for name in names if failure.get(name) == UNSUPPORTED_ATTRIBUTE}
        return f"not answered ({errors[0]})" if reads and len(errors) == len(reads) else ""

    def _watches_of(self, device_id: str, first: Watch) -> list[Watch]:
        """The watches a device check reads: the one it started with and the one that listens now, when a sync replaced it while
        the check ran (a reload of ZHA, a re-interview). What came since the start sits in either."""
        current = self.watches.get(device_id)
        return [first] if current is None or current is first else [first, current]

    async def _settle(self, device_id: str, watch: Watch, start: float) -> None:
        """Wait until CHECK_QUIET seconds pass without a new value after one came, at most CHECK_WAIT (on the watches of the
        check, _watches_of)."""
        waited = 0.0
        while waited < CHECK_WAIT:
            watches = self._watches_of(device_id, watch)
            last = max([at for heard in watches for at in heard.fresh.values() if at >= start] + [heard.last_datapoints or 0 for heard in watches])
            if last >= start and self.now() - last >= CHECK_QUIET:
                return
            await asyncio.sleep(CHECK_STEP)
            waited += CHECK_STEP

    @callback
    def finish_check(self, result: CheckResult, matches: set[str], wrong: set[str]) -> tuple[str, str]:
        """The user's answers: the report (kept for the diagnostics) and the link to the form it goes to.

        A finished check takes the self-check notice of its file away: it did what the notice asked.
        A self-check that still watches goes on and ends with its notice: the check compared one
        moment, the self-check sees the rest of the day (values that come later, or never)."""
        answers = {row.suffix: ("no" if row.suffix in wrong else "yes" if row.suffix in matches else "not checked") for row in result.rows}
        report = text.check_report(result, answers)
        url = text.check_url(self.manager.entry_of(result.file), result, answers, HA_VERSION)
        self.reports = [*self.reports, {"time": result.time, "file": result.file, "revision": result.revision or "", "device": f"{result.manufacturer} {result.model}",
                                        "report": report}][-MAX_REPORTS:]
        observation = self.observations.get(result.file)
        if observation is not None and observation["revision"] == result.revision:
            observation["checked"] = True
            if observation.get("done"):
                # Closed: its notice goes and no refresh keeps it (keep_ids).
                self._drop_notice(result.file, observation)
                observation["closed"] = True
        self._save()
        return report, url

    # Diagnostics

    def summary(self) -> dict[str, Any]:
        """The self-checks and the device check reports, by file and by manufacturer and model only."""
        devices = self.manager.devices
        checks = {}
        for name, observation in sorted(self.observations.items()):
            checks[name] = {
                "revision": observation["revision"],
                "started": dt_util.utc_from_timestamp(observation["started"]).isoformat(timespec="seconds"),
                "observed_hours": round(observation["observed"] / 3600, 1),
                "result": observation.get("done") or "watching",
                "device_check_done": bool(observation.get("checked") or observation.get("closed")),
                "devices": [
                    {
                        "device": f"{devices[i].manufacturer} {devices[i].model}" if i in devices else "a device no longer in ZHA",
                        "sent_values": state["frame"],
                        "answered_the_data_query": state["answered"],
                        "fresh_values": len(state["fresh"]),
                        "findings": sorted(state["findings"].values()),
                        "unmapped_datapoints": state["unmapped"],
                        "refused_datapoints": {dp: reason or text.NO_REASON for dp, (reason, _keys) in sorted(self.refused_of(state).items(), key=lambda item: int(item[0]))},
                    }
                    for i, state in sorted(observation["devices"].items(), key=lambda item: (devices[item[0]].manufacturer, devices[item[0]].model) if item[0] in devices else ("", ""))
                ],
            }
        return {"self_check": checks, "watched_devices": len(self.watches), "device_checks": list(self.reports)}
