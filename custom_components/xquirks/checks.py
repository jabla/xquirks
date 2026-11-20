"""The device check and the self-check after an install: what a device that runs an xquirks file sends.

Listeners (watch.py) sit on the devices that run a file of ours and are not left out, while ZHA is
loaded. They only note which attribute got a fresh value and which Tuya datapoints came: nothing is
polled, nothing is written, nothing leaves Home Assistant by itself.

- The device check (the "Check a device" flow of the integration page, also reached from a self-check notice)
  asks one device for its current values (a Tuya data query, or a read of the attributes behind the
  file's entities), waits briefly and returns what it shows. The user marks what matches the device. The report it makes names no address, no
  device or area name and no entity id: it may go into a public issue.
- The self-check starts when ZHA loaded a file the change list installed or updated. For 24 hours of
  observation (only while ZHA runs, kept across restarts) it notes implausible values, entities that
  get no value, datapoints the quirk does not handle and those its Tuya cluster refused (a finding: the
  value never reached the entity). Then one notice per file and revision says
  whether the values look off, look fine (a value of the file came from the device and looked right)
  or did not come, and invites the device check. Before a fine or unproven notice, one search on GitHub
  (it names the file's key, nothing else) tells how many people confirmed the file on each device's
  variant (confirm.py): a device whose variant reached the threshold is left out of it. A file verified
  on a device gets neither notice. A notice that the values look off always comes.
- The firmware watch: a device whose firmware changed from one known version to another (the device
  registry's sw_version, which ZHA writes from the device's update entity) is watched for a day again,
  alone, with the same notices, while its file is not verified. A version after none is no update
  (many devices report theirs only after their first image query). Each version is watched once per
  device and file revision (a device that flips between two brings no notice a day).
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import hashlib
import importlib.metadata
import logging
import os
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, State, callback
from homeassistant.core_config import DATA_CUSTOMIZE
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import confirm, remote, text
from .const import CONF_EXCLUDED, DOMAIN, TUYA_QUERY_DATA, ZHA_DOMAIN
from .content import verified
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
# The firmware watch: the versions already watched per device (the oldest goes first), the devices whose versions are
# kept (past it a further device is not recorded until one goes: no churn), the length a version is kept at (a longer
# one is cut and gets a hash of the whole, so two stay apart). At most 200 x (1 + 4) versions of 64 characters.
MAX_VERSIONS = 4
MAX_FIRMWARE = 200
MAX_VERSION_LENGTH = 64

# The notices a self-check ends with: something looked off, a value of the file came and looked right
# (the evidence that the file works), no value of the file came.
FINE = "self_check_fine"
OFF = "self_check_off"
UNPROVEN = "self_check_unproven"
KINDS = (FINE, OFF, UNPROVEN)
# The line a firmware watch adds to its device in the notice (English like the other lines).
FIRMWARE_NOTE = "watched again after a firmware update"
# The end of a firmware watch whose file is verified now: no notice.
VERIFIED = "verified"

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


def firmware_version(value: object) -> str | None:
    """A firmware version as the firmware watch keeps it: None for none, an empty or blank text and anything but text.
    One longer than MAX_VERSION_LENGTH is cut and ends with a hash of the whole (it reads the same again)."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        raw = value.encode()
    except UnicodeEncodeError:
        return None
    if len(value) <= MAX_VERSION_LENGTH:
        return value
    digest = hashlib.sha256(raw).hexdigest()[:16]
    return f"{value[:MAX_VERSION_LENGTH - len(digest) - 1]}#{digest}"


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
        # device id -> its firmware as the firmware watch keeps it (_firmware): "file" and "revision" it ran when they were
        # recorded, "version" (the last known one, None before any), "watched" (the versions a watch saw, oldest first) and
        # "watch", the firmware watch of the device (a self-check of it alone, in the shape of _start plus "file", "device",
        # "firmware").
        self.firmware: dict[str, dict[str, Any]] = {}
        self.reports: list[dict[str, str]] = []
        # device id -> its listeners and its entities by attribute key, while ZHA is loaded
        self.watches: dict[str, Watch] = {}
        self._specs: dict[str, dict[str, list[Spec]]] = {}
        # device id -> when the last data query to it was sent and when it was delivered (this run)
        self._queried: dict[str, tuple[float, float]] = {}
        # device id -> the file of its firmware watch that started in this sync: the device gets one Tuya data query right
        # after it (manager._query_tuya_devices), as the devices of a file ZHA loaded new or updated. Not stored: a watch
        # that resumes after a restart is not asked again, as a file's self-check is not.
        self.query_due: dict[str, str] = {}
        self._pending: set[tuple[str, str]] = set()
        self._value_timer: CALLBACK_TYPE | None = None
        self._last_tick: datetime | None = None
        # The notices created (issue ids, newest last).
        self.notified: list[str] = []
        # device id -> the file of ours it runs, for the watched devices (as the last refresh read them).
        self._files: dict[str, str] = {}
        # device id -> the device checks of it that run now.
        self._checking: dict[str, int] = {}
        # The self-checks (by issue id) that wait for their confirmation search (_due), and the lock that runs one search at a
        # time.
        self._searching: set[str] = set()
        self._search_lock = asyncio.Lock()
        self.closed = False

    # Setup, storage, unload

    async def async_setup(self) -> None:
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            observations = stored.get("observations")
            if isinstance(observations, dict):
                # A file's self-check that looks like a firmware watch is none.
                self.observations = {name: obs for name, obs in observations.items() if self._valid(obs) and self.watch_device(obs) is None}
            notified = stored.get("notified")
            if isinstance(notified, list):
                self.notified = [issue_id for issue_id in notified if isinstance(issue_id, str)][-MAX_NOTIFIED:]
            reports = stored.get("reports")
            if isinstance(reports, list):
                self.reports = [r for r in reports if isinstance(r, dict) and all(isinstance(v, str) for v in r.values())][-MAX_REPORTS:]
            self.firmware = self._load_firmware(stored.get("firmware"))
        # A notice an earlier run (or release) made for a file the state in use calls verified.
        self.drop_verified()
        self.migrate_notices()
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

    @classmethod
    def _load_firmware(cls, stored: object) -> dict[str, dict[str, Any]]:
        """The kept firmware of the devices in the shape _firmware writes (a record of another shape goes, so does a watch
        of another shape: a device without a record is recorded anew, not watched). Cut to the bounds."""
        if not isinstance(stored, dict):
            return {}
        found: dict[str, dict[str, Any]] = {}
        for device_id, record in list(stored.items())[-MAX_FIRMWARE:]:
            if not isinstance(device_id, str) or not isinstance(record, dict) or not isinstance(record.get("file"), str):
                continue
            revision, watched = record.get("revision"), record.get("watched")
            versions = [version for version in (watched if isinstance(watched, list) else []) if firmware_version(version) == version]
            found[device_id] = {
                "file": record["file"],
                "revision": revision if isinstance(revision, str) else None,
                "version": firmware_version(record.get("version")),
                "watched": list(dict.fromkeys(versions))[-MAX_VERSIONS:],
            }
            if cls._valid_watch(device_id, record["file"], record.get("watch")):
                found[device_id]["watch"] = record["watch"]
        return found

    @classmethod
    def _valid_watch(cls, device_id: str, name: str, watch: object) -> bool:
        """A stored firmware watch of this device and file in the shape _watch_firmware writes."""
        if not isinstance(watch, dict) or not cls._valid(watch):
            return False
        firmware, done, notice = watch.get("firmware"), watch.get("done"), watch.get("notice")
        return (
            watch.get("file") == name
            and watch.get("device") == device_id
            and list(watch["devices"]) == [device_id]
            and isinstance(firmware, str)
            and firmware_version(firmware) == firmware
            and (done is None or isinstance(done, str))
            and notice in (None, *KINDS)
            and all(watch.get(flag) in (None, True) for flag in ("closed", "checked"))
        )

    def _data(self) -> dict[str, Any]:
        return {"observations": self.observations, "notified": self.notified, "reports": self.reports, "firmware": self.firmware}

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
        the files ZHA loaded new or updated (started: their names), then the firmware watches (_firmware: a refresh
        follows every change of a device's firmware in the device registry)."""
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
        self._firmware(wanted)
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
        # The file's key, kept: a later state may ship the file verified, with a new docstring (another revision) and the same key.
        if (key := self._state_key(name, revision)) is not None:
            self.observations[name]["key"] = key
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

    # The firmware watch

    def _all(self) -> list[tuple[str, dict[str, Any]]]:
        """Every self-check as (file name, observation): those of the files, then the firmware watches."""
        return [*self.observations.items(), *((record["watch"]["file"], record["watch"]) for record in self.firmware.values() if "watch" in record)]

    @staticmethod
    def watch_device(observation: dict[str, Any]) -> str | None:
        """The device of a firmware watch; None for the self-check of a file."""
        device = observation.get("device")
        return device if isinstance(device, str) and isinstance(observation.get("firmware"), str) else None

    def _current(self, name: str, observation: dict[str, Any]) -> bool:
        """The self-check is still the one kept for its file (a firmware watch: for its device)."""
        device = self.watch_device(observation)
        if device is None:
            return self.observations.get(name) is observation
        record = self.firmware.get(device)
        return record is not None and record.get("watch") is observation

    def _file_watches(self, name: str, device_id: str) -> bool:
        """The self-check of the file runs and watches the device."""
        observation = self.observations.get(name)
        return observation is not None and not observation.get("done") and device_id in observation["devices"]

    def _covers(self, name: str, device_id: str) -> bool:
        """A self-check of the file watches the device now: the file's or the device's firmware watch."""
        record = self.firmware.get(device_id)
        watch = record.get("watch") if record is not None else None
        return self._file_watches(name, device_id) or (watch is not None and watch["file"] == name and not watch.get("done"))

    def _version(self, device_id: str) -> str | None:
        """The device's firmware in the device registry, as the firmware watch keeps it."""
        entry = self._device_entry(device_id)
        return firmware_version(entry.sw_version) if entry is not None else None

    def _waiting(self, name: str) -> bool:
        """A change of the file waits for a reload of ZHA: the devices run the version ZHA loaded before."""
        return any(change.get("file") == name for change in self.manager.data["pending"]["changes"])

    def _firmware(self, wanted: dict[str, tuple[str, Any]]) -> None:
        """The firmware of the devices that run a file of ours, after the self-checks of the files started and were pruned.

        A device that left (gone from the device registry, left out, disabled, another file or none) loses its versions and
        its watch with its notice. A watch whose file was updated or removed goes, so does one whose device the file's
        self-check watches again (the file installed again). Then per device ZHA has: its version recorded (none never
        overwrites a known one), a new version after a known one watched for a day (_watch_firmware) unless it was watched
        before, the file is verified, the file's self-check watches the device already or a change of the file waits for a
        reload of ZHA. A version counts as watched while a watch of the device runs and sees it."""
        if self.manager.folder is None:
            return
        changed = False
        for device_id, record in list(self.firmware.items()):
            device = self.manager.devices.get(device_id)
            # A device whose quirk ZHA does not report (yet) stays.
            if device is None or (device.quirk is not None and self._file_of(device_id) != record["file"]):
                self._drop_watch(record)
                del self.firmware[device_id]
                changed = True
                continue
            watch = record.get("watch")
            if watch is not None and (self._revision(watch["file"]) != watch["revision"] or self._file_watches(watch["file"], device_id)):
                self._drop_watch(record)
                changed = True
        for device_id, (name, _zha_device) in wanted.items():
            version, revision = self._version(device_id), self._revision(name)
            record = self.firmware.get(device_id)
            if record is None:
                if len(self.firmware) >= MAX_FIRMWARE:
                    continue
                # First seen: recorded, nothing to compare with.
                record = self.firmware[device_id] = {"file": name, "revision": revision, "version": version, "watched": []}
                changed = True
            else:
                if record["revision"] != revision:
                    # Another revision of the file: every version is a new question.
                    record["revision"], record["watched"] = revision, []
                    changed = True
                if version is not None and version != record["version"]:
                    known, record["version"] = record["version"], version
                    changed = True
                    # Verified also for the key the file's self-check kept: the state in use may ship the file verified at a new
                    # revision (a new docstring) while the update waits.
                    if (known is not None and revision is not None and version not in record["watched"] and not self._file_watches(name, device_id)
                            and not self._waiting(name) and not self.file_verified(name, {"revision": revision, "key": self.check_key(name, revision)})):
                        self._watch_firmware(device_id, name, version)
            if version is not None and version not in record["watched"] and self._covers(name, device_id):
                record["watched"] = [*record["watched"], version][-MAX_VERSIONS:]
                changed = True
        if changed:
            self._save()

    def _watch_firmware(self, device_id: str, name: str, version: str) -> None:
        """A day of watching for one device on its new firmware: a self-check of it alone, in place of its last firmware
        watch (whose notice goes: the device runs another firmware now). On disk at once, like _start. The device is due
        for a Tuya data query (query_due)."""
        record, revision = self.firmware[device_id], self._revision(name)
        if revision is None:
            return
        # The file's key, kept (as _start does): the state's for this revision, else the one its self-check or last watch kept.
        key = self.check_key(name, revision)
        self._drop_watch(record)
        watch = {"file": name, "device": device_id, "firmware": version, "revision": revision, "started": self.now(), "observed": 0.0,
                 "devices": {device_id: self._new_device()}}
        if key is not None:
            watch["key"] = key
        record["watch"] = watch
        self.query_due[device_id] = name
        self._save(now=True)

    def _drop_watch(self, record: dict[str, Any]) -> None:
        """The firmware watch of a device goes, with its notice."""
        watch = record.pop("watch", None)
        if watch is not None:
            self._drop_notice(watch["file"], watch)

    def _running(self, device_id: str) -> dict[str, Any] | None:
        """The stored state of the device in the self-check that runs for its file, else in its firmware watch; None
        without one. Never both: a firmware watch starts only when the file's self-check does not watch the device, and
        goes when it does again (_firmware)."""
        name = self._files.get(device_id)
        if not name:
            return None
        if self._file_watches(name, device_id):
            return self.observations[name]["devices"][device_id]
        record = self.firmware.get(device_id)
        watch = record.get("watch") if record is not None else None
        if watch is None or watch["file"] != name or watch.get("done"):
            return None
        return watch["devices"].get(device_id)

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
        for _name, observation in self._all():
            if not observation.get("done"):
                observation["observed"] = float(observation["observed"]) + seconds

    @callback
    def _tick(self, now: datetime) -> None:
        if self.closed:
            return
        running = any(not observation.get("done") for _name, observation in self._all())
        self._count(now)
        # A device ZHA swapped (a re-interview) or dropped: the next refresh listens to the new object.
        zha_devices = self._zha_devices()
        if any(zha_devices.get(self.manager.ieees.get(device_id, ""), None) is None or zha_devices[self.manager.ieees[device_id]].device is not watch.device
               for device_id, watch in self.watches.items()):
            self.manager.schedule_refresh()
        for name, observation in self._all():
            if observation.get("done"):
                continue
            if self.now() - observation["started"] > MAX_AGE.total_seconds():
                observation["done"] = "expired"
            elif observation["observed"] >= WINDOW.total_seconds() and self.watches:
                self._due(name, observation)
        if running:
            self._listen_refused()
            self._save()

    def _due(self, name: str, observation: dict[str, Any]) -> None:
        """The day of a self-check is over: concluded now, or after a search for the confirmations of its file when that
        can change its notice (the file is not verified, has a key, the state in use a threshold). Not again while that
        search runs (a file's self-check and a firmware watch of it each have their own)."""
        if (searching := self.issue_id(name, observation)) in self._searching:
            return
        key = self.file_key(name, observation)
        if key is None or self.manager.source.confirm_threshold is None or self.file_verified(name, observation):
            self._conclude(name, observation)
            return
        self._searching.add(searching)
        self.manager.entry.async_create_background_task(self.hass, self._async_search(name, observation, key), f"xquirks confirmations of {name}")

    async def _async_search(self, name: str, observation: dict[str, Any], key: str) -> None:
        """The confirmation search of a self-check (one search at a time: a later one may meet the rate limit, it is not
        repeated), then its conclusion, while it still runs as it was (the same file and revision, not concluded) and ZHA
        is loaded. Otherwise a later count decides anew, also after a restart: nothing of a search is kept. Its pages ask
        for the variants of its devices."""
        try:
            wanted = self.fixed_parts(observation["devices"], key)
            async with self._search_lock:
                answer = None if self.closed else await remote.async_search(async_get_clientsession(self.hass), key, wanted)
        finally:
            self._searching.discard(self.issue_id(name, observation))
        if self.closed or not self._current(name, observation) or observation.get("done") or not self.watches:
            return
        self._conclude(name, observation, answer)
        self._listen_refused()
        self._save()

    def _state_key(self, name: str, revision: str) -> str | None:
        """The key the state in use states for a file at this revision; None for another revision or without a valid key."""
        source = self.manager.source
        return source.keys.get(name) if source.revisions.get(name) == revision else None

    def file_key(self, name: str, observation: dict[str, Any]) -> str | None:
        """The key of the file a self-check watches: the one the state in use states for its revision, else the one kept
        when it started (the state ships another revision: an update waits). None without one: its notices ask as
        without confirmations."""
        if (key := self._state_key(name, observation["revision"])) is not None:
            return key
        key = observation.get("key")
        return key if confirm.valid_key(key) else None

    def file_verified(self, name: str, observation: dict[str, Any]) -> bool:
        """The state in use calls the file a self-check watches verified on a device: its entry, for that revision or for
        the file's key (a file that became verified differs from it only in its docstring)."""
        entry = self.manager.entry_of(name)
        if entry is None or not verified(entry):
            return False
        source, key = self.manager.source, self.file_key(name, observation)
        return source.revisions.get(name) == observation["revision"] or (key is not None and source.keys.get(name) == key)

    def fixed_part(self, device_id: str, key: str) -> str | None:
        """The fixed title part of the "verify" issues of a device's variant (confirm.fixed_part): manufacturer and model
        as zigpy reports them (the device check and its form name them so: a quirk's friendly name changes only the
        device registry's), the firmware as the device registry has it. None for a device no longer known or a bad key."""
        device, entry = self.manager.devices.get(device_id), self._device_entry(device_id)
        if device is None or entry is None:
            return None
        return confirm.fixed_part(device.manufacturer, device.model, entry.sw_version, key)

    def fixed_parts(self, devices: Iterable[str], key: str) -> set[str]:
        """The fixed title parts of these devices' variants (fixed_part), those known."""
        return {fixed for device_id in devices if (fixed := self.fixed_part(device_id, key)) is not None}

    def confirmed(self, name: str, observation: dict[str, Any], answer: object, devices: Iterable[str] | None = None) -> set[str]:
        """The devices of a self-check (or those of devices: a fix flow's first page) whose variant reached the threshold in
        a search answer (confirm.confirmation: the oldest open issue with the variant's fixed title part). None of them
        without a key or a threshold, or for an answer that cannot tell (None: no search, or it failed)."""
        key, threshold = self.file_key(name, observation), self.manager.source.confirm_threshold
        if key is None or threshold is None:
            return set()
        return {device_id for device_id in (observation["devices"] if devices is None else devices)
                if (fixed := self.fixed_part(device_id, key)) is not None and confirm.reached(confirm.confirmation(answer, fixed).count, threshold)}

    async def async_search_now(self, key: str, timeout: float, wanted: Iterable[str] = ()) -> object:
        """The confirmation search of a fix flow's first page (its pages ask for the fixed parts of wanted): one at a time
        with those of the self-checks, at most timeout seconds in all (the wait for a running one and every page
        included). None when it took longer or failed (remote.async_search)."""
        try:
            async with asyncio.timeout(timeout), self._search_lock:
                return None if self.closed else await remote.async_search(async_get_clientsession(self.hass), key, set(wanted))
        except TimeoutError:
            return None

    @callback
    def drop_verified(self) -> None:
        """The fine and unproven notices of files the state in use calls verified go (at load and after a content
        change): nobody needs to test those files. A notice that the values look off stays. A firmware watch of such a
        file that still runs ends without a notice."""
        changed = False
        for name, observation in self._all():
            if self.watch_device(observation) is not None and not observation.get("done") and self.file_verified(name, observation):
                observation["done"] = VERIFIED
                changed = True
            elif observation.get("notice") in (FINE, UNPROVEN) and not observation.get("closed") and self.file_verified(name, observation):
                self._drop_notice(name, observation)
                del observation["notice"]
                changed = True
        if changed:
            self._save()

    @callback
    def migrate_notices(self) -> None:
        """The self-check notices v0.1.0 made without a fix flow (data: the kind and the file). Their texts are the fix
        flow's now (a notice has a description or a fix flow). Each becomes fixable with the data its fix flow needs
        (repairs.SelfCheckFlow) from its self-check, keeping its texts and whether the user ignored it. One goes when its
        file has no open self-check of that revision with that notice in the store, or when its texts lack a placeholder
        the fix flow shows. drop_verified ran before: a fine or unproven notice of a file verified now went."""
        for (domain, issue_id), issue in list(ir.async_get(self.hass).issues.items()):
            if domain != DOMAIN or issue.is_fixable or issue.translation_key not in KINDS:
                continue
            kind, data, placeholders = issue.translation_key, issue.data or {}, issue.translation_placeholders or {}
            name = data.get("file")
            observation = self.observations.get(name) if isinstance(name, str) else None
            shown = ("quirk", "picture", "file", "revision", "devices", *(("link",) if kind == OFF else ()))
            if (observation is None or data.get("kind") != kind or self.issue_id(name, observation) != issue_id or observation.get("notice") != kind
                    or not observation.get("done") or observation.get("closed") or not all(isinstance(placeholders.get(key), str) for key in shown)):
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
                continue
            # The same id: Home Assistant keeps when it was created and whether the user ignored it.
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                data={"kind": kind, "file": name, "revision": observation["revision"], "devices": sorted(observation["devices"])},
                is_fixable=True,
                is_persistent=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key=kind,
                translation_placeholders=dict(placeholders),
            )

    def _conclude(self, name: str, observation: dict[str, Any], answer: object = None) -> None:
        """The window is over: the findings per device, one notice for the file.

        Off: a device showed a problem. Fine needs evidence: an entity of the file got a value from the
        device that looked right, on a ZCL file as on a Tuya file. Without problems and evidence the
        notice says that no value of the file came, whatever else the devices sent. A fine or unproven
        notice asks for a device test: none for a verified file, and none names a device whose variant
        reached the threshold in the search answer (none at all when that leaves no device). Its kind comes from the
        devices it names. A firmware watch of a file verified now ends without any notice: it matters only until the file
        is verified."""
        firmware_device = self.watch_device(observation)
        if firmware_device is not None and self.file_verified(name, observation):
            observation["done"] = VERIFIED
            return
        devices = self.manager.devices
        lines: dict[str, list[str]] = {}
        problems: dict[str, list[str]] = {}
        # The devices with evidence: the kind of a notice comes from the devices it names.
        evidence: set[str] = set()
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
            found, notes = list(state["findings"].values()), ([FIRMWARE_NOTE] if device_id == firmware_device else [])
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
                if {spec.suffix for spec in ours if spec.suffix in shown} - set(state["findings"]):
                    evidence.add(device_id)
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
        if not off:
            if self.file_verified(name, observation):
                return
            if confirmed := self.confirmed(name, observation, answer):
                lines = {device_id: device_lines for device_id, device_lines in lines.items() if device_id not in confirmed}
                if not lines:
                    return
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
        kind = OFF if off else FINE if evidence & set(lines) else UNPROVEN
        observation["notice"] = kind
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            # The devices it names, for what follows the notice. A firmware watch names its device (SelfCheckFlow finds it so).
            data={"kind": kind, "file": name, "revision": observation["revision"], "devices": sorted(lines),
                  **({"device": firmware_device} if firmware_device is not None else {})},
            # Its fix flow (repairs.SelfCheckFlow) guides through the device check.
            is_fixable=True,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=kind,
            translation_placeholders=placeholders,
        )

    @classmethod
    def issue_id(cls, name: str, observation: dict[str, Any]) -> str:
        """The notice of a file's self-check: one per file revision. That of a firmware watch also names the device and a
        hash of its firmware."""
        found = f"self_check_{stem(name)}_{observation['revision']}"
        if (device := cls.watch_device(observation)) is not None:
            found += f"_{device}_{hashlib.sha256(observation['firmware'].encode(errors='replace')).hexdigest()[:8]}"
        return found

    def notice_files(self) -> set[str]:
        """The files of the self-checks that watch (a notice may end them) or whose notice is open: the first page of the
        notice shows the picture of their device (manager._named_files fetches it while they watch)."""
        return {name for name, obs in self._all() if not obs.get("done") or (obs.get("notice") and not obs.get("closed"))}

    def keep_ids(self) -> frozenset[str]:
        """The self-check notices that stay: their file is still in the folder at their revision (a firmware watch's: its
        device still runs it)."""
        return frozenset(self.issue_id(name, obs) for name, obs in self._all() if obs.get("notice") and not obs.get("closed"))

    def _drop_notice(self, name: str, observation: dict[str, Any]) -> None:
        if observation.get("notice"):
            ir.async_delete_issue(self.hass, DOMAIN, self.issue_id(name, observation))

    # The flows of a notice (repairs.SelfCheckFlow and the device check it hands over to)

    def open_notice(self, issue_id: object, data: Mapping[str, Any], answered: frozenset[tuple[object, ...]] | None = None) -> tuple[dict[str, Any] | None, str]:
        """The self-check of a notice (its issue id and data) while the notice stands as it was made. Otherwise None and the
        reason its flows end with: "file_changed" (updated or removed, the device left the firmware watch) or "gone"
        (closed in another window, or gone with a new self-check of the file). With answered (the self-checks a last page
        answered, finish_check): also "gone" when the notice now belongs to another self-check (one made again under the
        same id since the page showed)."""
        # A firmware watch's notice names its device.
        observation = self.self_check(data.get("file"), data.get("revision"), data.get("device"))
        if observation is None:
            return None, "file_changed"
        if (not observation.get("done") or not observation.get("notice") or observation.get("closed") or issue_id != self.issue_id(data["file"], observation)
                or ir.async_get(self.hass).async_get_issue(DOMAIN, issue_id) is None):
            return None, "gone"
        if answered is not None and self._which(observation) not in answered:
            return None, "gone"
        return observation, ""

    def self_check(self, name: object, revision: object, device: object = None) -> dict[str, Any] | None:
        """The self-check of a notice (with device: the firmware watch of that device): its file still in the folder at this
        revision. None otherwise (updated, removed, the device left). A later firmware watch of the device has a notice of
        another id: the flow tells it by the notice it was opened on."""
        if not isinstance(name, str):
            return None
        if device is None:
            observation = self.observations.get(name)
        else:
            record = self.firmware.get(device) if isinstance(device, str) else None
            observation = record.get("watch") if record is not None else None
            if observation is not None and observation["file"] != name:
                return None
        if observation is None or not isinstance(revision, str) or observation["revision"] != revision or self._revision(name) != revision:
            return None
        return observation

    def notice_devices(self, name: str, devices: object) -> list[str]:
        """Those of a notice's devices (its data) that run its file now and ZHA has: the device check can ask them."""
        if not isinstance(devices, list):
            return []
        return [device_id for device_id in dict.fromkeys(devices) if isinstance(device_id, str) and device_id in self.watches and self._file_of(device_id) == name]

    @callback
    def close_notice(self, name: str, observation: dict[str, Any]) -> None:
        """The notice of a concluded self-check goes for good: no refresh keeps it (keep_ids), its file revision gets no
        other (notified). After the button of a device check's last page (close_checked), a fix flow's "Do not check" and
        "already confirmed"."""
        if not observation.get("done"):
            return
        self._drop_notice(name, observation)
        observation["closed"] = True
        self._save()

    def check_key(self, name: str, revision: str | None) -> str | None:
        """The key of a file at the revision a device check ran: the state's for that revision, else the one the
        self-check of that revision kept at its start. None while a change waits for a ZHA reload (no revision) or
        without a valid key."""
        if revision is None:
            return None
        if (key := self._state_key(name, revision)) is not None:
            return key
        observation = self.observations.get(name)
        if observation is not None and observation["revision"] == revision:
            return self.file_key(name, observation)
        # A firmware watch of the file at that revision kept it too.
        for file, watch in self._all():
            if file == name and watch["revision"] == revision and confirm.valid_key(watch.get("key")):
                return watch["key"]
        return None

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
        return (f"read of {count} {'attribute' if count == 1 else 'attributes'}" if count else NOTHING_ASKED), list(by_cluster.values())

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

    def _answered(self, result: CheckResult) -> list[dict[str, Any]]:
        """The self-checks a device check answers now: the file's at the revision the device ran, and the firmware watch of
        the device checked at it (not those of the file's other devices)."""
        found = []
        observation = self.observations.get(result.file)
        if observation is not None and observation["revision"] == result.revision:
            found.append(observation)
        record = self.firmware.get(result.device_id)
        watch = record.get("watch") if record is not None else None
        if watch is not None and watch["file"] == result.file and watch["revision"] == result.revision:
            found.append(watch)
        return found

    @staticmethod
    def _which(observation: dict[str, Any]) -> tuple[object, ...]:
        """Which self-check this is: a later one of the same file and device has another start (a firmware watch of a newer
        firmware, the file installed again), also after a restart (the start is stored)."""
        return observation.get("firmware"), observation["revision"], observation["started"]

    @callback
    def finish_check(self, result: CheckResult, matches: set[str], wrong: set[str]) -> tuple[str, str, frozenset[tuple[object, ...]]]:
        """The user's answers, when the last page of the check shows: the report (kept for the diagnostics), the link to the
        form it goes to and which self-checks it answered (for close_checked).

        The self-checks it answers count it as done ("checked", the diagnostics' device_check_done) from here: the report
        is kept, whether the page's button is clicked or not. Their notices stay until the button (close_checked)."""
        answers = {row.suffix: ("no" if row.suffix in wrong else "yes" if row.suffix in matches else "not checked") for row in result.rows}
        report = text.check_report(result, answers)
        # The form's title names the variant as the confirmation search compares it.
        url = text.check_url(self.manager.entry_of(result.file), result, answers, HA_VERSION, self.check_key(result.file, result.revision))
        self.reports = [*self.reports, {"time": result.time, "file": result.file, "revision": result.revision or "", "device": f"{result.manufacturer} {result.model}",
                                        "report": report}][-MAX_REPORTS:]
        answered = self._answered(result)
        for observation in answered:
            observation["checked"] = True
        self._save()
        return report, url, frozenset(self._which(observation) for observation in answered)

    @callback
    def close_checked(self, result: CheckResult, answered: frozenset[tuple[object, ...]]) -> None:
        """The button of the check's last page (it opens the form or the device test): the notices of the self-checks the
        check answered when the page showed (finish_check) go, it did what they asked. One that started since (the device
        got a newer firmware) keeps its notice: the check never saw it. A self-check that still watches goes on and ends
        with its notice: the check compared one moment, the self-check sees the rest of the day (values that come later,
        or never)."""
        for observation in self._answered(result):
            if self._which(observation) in answered:
                self.close_notice(result.file, observation)

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
                # A check that reached its report (finish_check), whether its notice closed or not. A notice closed without a
                # check ("Do not check", already confirmed) is no check.
                "device_check_done": bool(observation.get("checked")),
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
        # The firmware watches, by manufacturer and model.
        firmware = [
            {
                "device": f"{devices[device_id].manufacturer} {devices[device_id].model}" if device_id in devices else "a device no longer in ZHA",
                "file": record["watch"]["file"],
                "revision": record["watch"]["revision"],
                "firmware": record["watch"]["firmware"],
                "observed_hours": round(record["watch"]["observed"] / 3600, 1),
                "result": record["watch"].get("done") or "watching",
                "device_check_done": bool(record["watch"].get("checked")),
            }
            for device_id, record in sorted(self.firmware.items(), key=lambda item: item[1]["file"]) if "watch" in record
        ]
        return {"self_check": checks, "firmware_checks": firmware, "watched_devices": len(self.watches), "device_checks": list(self.reports)}
