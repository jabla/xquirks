"""Listening in process to what a ZHA device sends: which attributes got a fresh value from it, which
Tuya datapoints it sent and which of them its Tuya cluster refused. No Home Assistant imports, no radio
traffic: the listeners see only what ZHA receives anyway, and they cost a dictionary write per value and a
small object per frame.

A refused datapoint is one zhaquirks skipped with its warning "Ignoring invalid datapoint" (the typed Tuya
cluster of the file refuses a value of another type than its attribute's, a converter failed, the payload
did not decode). One handler on zigpy's cluster log (REFUSED_LOGGER) hands each such warning to the watch
of that cluster, only while a watch listens for them (Watch.listen_refused: while a self-check or a device
check of its device runs). The frame shows the refusal too, without the log: zigpy hands a Tuya frame to the
cluster and only then tells its listeners about the command (cluster_command). A datapoint of the frame that
the quirk handles, none of whose attributes got an update (or was cleared) meanwhile, did not reach them. Its
reason comes from the warning when the log is heard. It stays empty when the log is set above warnings
(log_heard). The frame is looked at for refusals only while the watch listens for them, like the log.

A value restored from zigbee.db at a start counts for nothing here: zigpy keeps decoded values across
quirk changes, so only a value that arrives while the file runs says something about the file.

zigpy also sends "attribute_updated" for a value Home Assistant sets itself: a write to a LocalDataCluster
(every setting of a Tuya file), ZHA's switch after an on or off command. Such an update counts only while a
frame from the device is being handled (_Frame), as the attribute updates a quirk makes of a Tuya frame or of
a report do. Reports and reads count as they come: zigpy sends those only for values from the device. The
answer to a read is handled in the task that asked, after its frame closed, and zigpy tells a value of it that
the quirk changed on its way to the cache as "attribute_updated": such an update counts while zigpy hands that
answer on (_read_answer), on the cluster that was read and on every cluster the quirk passes the value on to. A
write that a listener of the answer starts meanwhile does not: it runs in a coroutine of its own.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import logging
import sys
import time
from collections.abc import Callable
from typing import Any

# The zigpy cluster events of a value from the device (zigpy.zcl AttributeUpdatedEvent,
# AttributeReportedEvent, AttributeReadEvent), as ZHA's own entities subscribe to them.
ATTRIBUTE_EVENTS = ("attribute_updated", "attribute_report", "attribute_read")
UPDATED = "attribute_updated"
READ = "attribute_read"
# zigpy's event when a quirk sets an attribute to no value (AttributeClearedEvent): the datapoint reached it.
CLEARED = "attribute_cleared"
# The attribute part of a cluster's own key: any value on the cluster is fresh for an entity that reads
# the whole cluster (ZHA's climate, cover and light name no single attribute).
WHOLE_CLUSTER = "*"
# Bounds per device: attribute keys and datapoints kept; the length of a reason a datapoint was refused for.
MAX_KEYS = 256
MAX_DATAPOINTS = 32
MAX_REASON = 160
# zhaquirks' warning for a datapoint its Tuya cluster skips (TuyaNewManufCluster.handle_get_data), with zigpy's
# cluster prefix before it ("[<device>:<endpoint>:0x<cluster>] ", zigpy.zcl Cluster.log): seven arguments, the
# cluster's three, then the datapoint, its type, its raw bytes and the error.
REFUSED_LOGGER = "zigpy.zcl"
REFUSED_FORMAT = "Ignoring invalid datapoint %s (%s, raw %s): %s"
# The method of zigpy's Cluster that hands the device's answer to a read of attributes on, record by record
# (zigpy.zcl), and how far up the call stack of an update it is looked for: the quirk's own calls lie between.
READ_ANSWER = "_process_read_attributes_response"
MAX_STACK = 32
# ZHA's platform (PlatformEntity.PLATFORM) of the entities that show no value of the device.
BUTTON = "button"


def log_heard() -> bool:
    """Whether zigpy's cluster log passes warnings now: the reasons of refusals reach the watches only then."""
    return logging.getLogger(REFUSED_LOGGER).isEnabledFor(logging.WARNING)


def wall_clock() -> float:
    """The time now (looked up at each call: a clock replaced after import is the one read)."""
    return time.time()


class _Frame:
    """A frame from a zigpy device while zigpy hands it to the device's clusters.

    zigpy calls the listeners of a device with device_last_seen_updated first thing for each frame from it
    (zigpy.device.Device.packet_received) and hands the frame to its clusters right after, without a pause: the
    attribute updates a quirk makes of it run in the same task and context before the event loop goes on. The
    frame is open until the loop's next round, for the task that opened it (task: None for a callback of the
    loop). Code in another context never sees it: a write of Home Assistant's runs in the task of its service
    call. A task started while the frame is handled copies its context. One the loop starts runs after the frame
    closed. One started eagerly (as Home Assistant starts its tasks) runs at once, with the frame still open: it
    is told apart as another task."""

    __slots__ = ("device", "open", "task")

    def __init__(self, device: Any) -> None:
        self.device = device
        self.open = True
        self.task = asyncio.current_task()

    def close(self) -> None:
        self.open = False


_FRAME: contextvars.ContextVar[_Frame | None] = contextvars.ContextVar("xquirks_frame", default=None)


def key_of(endpoint_id: Any, cluster_id: Any, attribute: Any) -> str:
    """The key of one attribute of a device: endpoint, cluster id and attribute name (or id)."""
    return f"{endpoint_id}:{cluster_id}:{attribute}"


def cluster_key(key: str) -> str | None:
    """The key of the whole cluster an attribute key (key_of) belongs to: what an entity that reads the whole cluster
    goes by (entity_sources). None for a key of another shape."""
    parts = key.split(":", 2)
    return key_of(parts[0], parts[1], WHOLE_CLUSTER) if len(parts) == 3 else None


def _read_answer() -> bool:
    """zigpy hands the device's answer to a read of attributes on right now and the caller is part of that: READ_ANSWER
    is on the call stack, of a cluster whose reads reach the device (a LocalDataCluster answers from its cache), with
    nothing but plain calls between it and the caller. That is the quirk at work on the answer, on the cluster that was
    read or on one it passes the value on to. A coroutine on the way is other work that was started meanwhile: a write
    always runs in one (zigpy's write_attributes), also in a task that runs at once."""
    frame = sys._getframe(1)
    for _ in range(MAX_STACK):
        if frame is None or frame.f_code.co_flags & inspect.CO_COROUTINE:
            break
        if frame.f_code.co_name == READ_ANSWER:
            return not local(frame.f_locals.get("self"))
        frame = frame.f_back
    return False


def _is(cluster: Any, name: str) -> bool:
    """The cluster is an instance of a class of this name (zhaquirks' cluster classes, by name: no import)."""
    return any(base.__name__ == name for base in type(cluster).__mro__)


def local(cluster: Any) -> bool:
    """A zhaquirks LocalDataCluster: reading it returns its cache, nothing reaches the device."""
    return _is(cluster, "LocalDataCluster")


def handled_datapoints(cluster: Any) -> frozenset[int]:
    """The Tuya datapoints the quirk's Tuya MCU cluster does something with."""
    found: set[int] = set()
    for name in ("data_point_handlers", "dp_to_attribute", "_dp_to_attributes"):
        mapping = getattr(cluster, name, None)
        if isinstance(mapping, dict):
            found |= {dp for dp in mapping if isinstance(dp, int)}
    return frozenset(found)


def datapoints_of(args: Any) -> list[int]:
    """The datapoint ids of a Tuya MCU command as zigpy parsed it (its data field holds the datapoints)."""
    data = getattr(args, "data", None)
    if data is None and isinstance(args, (list, tuple)) and args:
        data = args[0]
    points = getattr(data, "datapoints", None)
    if not isinstance(points, (list, tuple)):
        return []
    return [int(point.dp) for point in points if isinstance(getattr(point, "dp", None), int)]


def mapped_keys(tuya: Any, dp: int) -> list[str]:
    """The attribute keys the Tuya cluster's datapoint table sends datapoint dp to (zhaquirks' mappings: an endpoint
    of their own or the cluster's, the cluster by its ep_attribute, one name or a tuple of names). Empty where the
    table cannot be read."""
    found: list[str] = []
    try:
        table = getattr(tuya, "_dp_to_attributes", None)
        mappings = table.get(dp) if isinstance(table, dict) else None
        if mappings is None:
            table = getattr(tuya, "dp_to_attribute", None)
            mappings = table.get(dp) if isinstance(table, dict) else None
        if mappings is None:
            return found
        endpoint = tuya.endpoint
        for mapping in mappings if isinstance(mappings, (list, tuple)) else (mappings,):
            if getattr(mapping, "endpoint_id", None):
                endpoint = tuya.endpoint.device.endpoints[mapping.endpoint_id]
            cluster = getattr(endpoint, mapping.ep_attribute)
            names = mapping.attribute_name if isinstance(mapping.attribute_name, tuple) else (mapping.attribute_name,)
            found += [key_of(endpoint.endpoint_id, cluster.cluster_id, name) for name in names if isinstance(name, str)]
    except Exception:  # noqa: BLE001 - a table of another shape names no attribute
        pass
    return found


class _Refusals(logging.Handler):
    """The one handler on zigpy's cluster log while a watch with a Tuya cluster listens for refusals: zhaquirks' warning
    for a datapoint it skipped goes to the watch of that cluster. It never raises and drops every other record at once."""

    def __init__(self) -> None:
        super().__init__()
        self.watches: list[Watch] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if isinstance(record.msg, str) and record.msg.endswith(REFUSED_FORMAT) and isinstance(record.args, tuple) and len(record.args) == 7:
                for watch in list(self.watches):
                    watch.refused_record(record.args)
        except Exception:  # noqa: BLE001, S110 - a listener never disturbs ZHA
            pass

    def add(self, watch: Watch) -> None:
        if watch in self.watches:
            return
        if not self.watches:
            logging.getLogger(REFUSED_LOGGER).addHandler(self)
        self.watches.append(watch)

    def remove(self, watch: Watch) -> None:
        if watch in self.watches:
            self.watches.remove(watch)
            if not self.watches:
                logging.getLogger(REFUSED_LOGGER).removeHandler(self)


REFUSALS = _Refusals()


def entity_sources(zha_device: Any) -> dict[str, tuple[str | None, bool]]:
    """unique id -> (the key of the attribute the ZHA entity reads, its cluster's own key when it names
    no single attribute, None without a cluster and for a button; whether the quirk ZHA applied makes the
    entity) for each entity of a ZHA device.

    A button reads nothing: its state is when it was last pressed, and the attribute a write button names is
    the one it writes. No value of the device shows in it, none is asked for it.

    The quirk makes an entity its metadata declares (unique id <IEEE>-<endpoint>-<suffix>) and every
    entity on a cluster it put on the device (a CustomCluster: zigpy's own clusters are not)."""
    ieee = str(getattr(zha_device, "ieee", ""))
    declared = set()
    metadata = getattr(zha_device, "quirk_metadata", None)
    for meta in getattr(metadata, "entity_metadata", None) or ():
        suffix = getattr(meta, "resolved_unique_id_suffix", None)
        if suffix:
            declared.add(f"{ieee}-{getattr(meta, 'endpoint_id', 1)}-{suffix}")
    found: dict[str, tuple[str | None, bool]] = {}
    entities = getattr(zha_device, "platform_entities", None)
    for entity in (entities.values() if isinstance(entities, dict) else ()):
        unique_id = getattr(entity, "unique_id", None)
        if not isinstance(unique_id, str):
            continue
        cluster = getattr(entity, "_cluster", None) or getattr(entity, "cluster", None)
        attribute = getattr(entity, "_attribute_name", None)
        endpoint = getattr(getattr(cluster, "endpoint", None), "endpoint_id", None)
        cluster_id = getattr(cluster, "cluster_id", None)
        key = None
        if cluster_id is not None and endpoint is not None and getattr(entity, "PLATFORM", None) != BUTTON:
            key = key_of(endpoint, cluster_id, attribute if isinstance(attribute, str) else WHOLE_CLUSTER)
        found[unique_id] = (key, unique_id in declared or (cluster is not None and _is(cluster, "CustomCluster")))
    return found


class Watch:
    """The listeners on one zigpy device: the attribute keys that got a fresh value (key -> time), the
    datapoints its Tuya MCU cluster got that the quirk does not handle, those it refused (dp -> time and
    why, empty when the log did not say; the latest MAX_DATAPOINTS) and when it last sent anything.

    on_fresh(watch, key) runs for every fresh value, on_datapoints(watch, dps) for every Tuya frame,
    on_refused(watch, dp) for every refused datapoint.
    """

    def __init__(
        self,
        zigpy_device: Any,
        tuya: Any | None = None,
        *,
        on_fresh: Callable[[Watch, str], None] | None = None,
        on_datapoints: Callable[[Watch, list[int]], None] | None = None,
        on_refused: Callable[[Watch, int], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.device = zigpy_device
        self.tuya = tuya
        self.fresh: dict[str, float] = {}
        self.unmapped: dict[int, float] = {}
        self.refused: dict[int, tuple[float, str]] = {}
        self.last_datapoints: float | None = None
        self._on_fresh = on_fresh
        self._on_datapoints = on_datapoints
        self._on_refused = on_refused
        self._clock = clock or wall_clock
        self._handled = handled_datapoints(tuya) if tuya is not None else frozenset()
        self._unsubs: list[Callable[[], None]] = []
        # The frame of the device being handled: the attribute keys it updated or cleared so far and the datapoints
        # whose refusal the log reported meanwhile.
        self._frame: _Frame | None = None
        self._reached: set[str] = set()
        self._heard: set[int] = set()

    @property
    def attached(self) -> bool:
        return bool(self._unsubs)

    @property
    def _listening(self) -> bool:
        """A check wants the refusals of this watch now (listen_refused)."""
        return self in REFUSALS.watches

    def attach(self) -> None:
        """Listen to the device's frames, to every input cluster of its endpoints (and to its Tuya MCU cluster's commands)."""
        if self._unsubs:
            return
        device = self.device
        if callable(getattr(device, "add_listener", None)):
            device.add_listener(self)
            self._unsubs.append(lambda: device.remove_listener(self))
        endpoints = getattr(device, "endpoints", None)
        for ep_id, endpoint in sorted((endpoints or {}).items(), key=lambda item: item[0]) if isinstance(endpoints, dict) else ():
            if not isinstance(ep_id, int) or not ep_id:
                continue
            for cluster in list((getattr(endpoint, "in_clusters", None) or {}).values()):
                on_event = getattr(cluster, "on_event", None)
                if not callable(on_event):
                    continue
                cached = local(cluster)
                for name in ATTRIBUTE_EVENTS:
                    if cached and name == READ:
                        continue
                    self._unsubs.append(on_event(name, functools.partial(self._attribute, name)))
                if self.tuya is not None:
                    # A Tuya datapoint may go to an attribute on any cluster of the device: its refusal shows there.
                    self._unsubs.append(on_event(CLEARED, self._cleared))
        tuya = self.tuya
        if tuya is not None and callable(getattr(tuya, "add_listener", None)):
            tuya.add_listener(self)
            self._unsubs.append(lambda: tuya.remove_listener(self))

    def device_last_seen_updated(self, _last_seen: Any) -> None:
        """zigpy: a frame from the device came; its clusters handle it right after (_Frame)."""
        try:
            frame = _Frame(self.device)
            asyncio.get_running_loop().call_soon(frame.close)
            _FRAME.set(frame)
            self._frame, self._reached, self._heard = frame, set(), set()
        except Exception:  # noqa: BLE001, S110 - a listener never disturbs ZHA
            pass

    def _in_frame(self) -> bool:
        """A frame from this device is being handled in the task and context that run now (_Frame)."""
        frame = _FRAME.get()
        return frame is not None and frame.open and frame.device is self.device and frame.task is asyncio.current_task()

    def _in_own_frame(self) -> bool:
        """The frame being handled now is the one this watch keeps track of (device_last_seen_updated)."""
        return self._in_frame() and _FRAME.get() is self._frame

    def listen_refused(self, on: bool) -> None:
        """Hear (on) or no longer hear zhaquirks' warnings for the datapoints this watch's Tuya cluster refuses: the handler
        on zigpy's cluster log sees every warning of every cluster, so it listens only while a check needs it. The frames
        of the device are looked at for refusals (cluster_command) for as long."""
        if on and self.tuya is not None and self._unsubs:
            REFUSALS.add(self)
        else:
            REFUSALS.remove(self)

    def detach(self) -> None:
        REFUSALS.remove(self)
        unsubs, self._unsubs = self._unsubs, []
        for unsub in unsubs:
            try:
                unsub()
            except Exception:  # noqa: BLE001, S110 - a device ZHA dropped may have dropped its listeners too
                pass

    def _attribute(self, name: str, event: Any) -> None:
        try:
            if name == UPDATED and not self._in_frame() and not _read_answer():
                # A value Home Assistant set itself (or a quirk set by a timer): the device did not send it.
                return
            attribute = getattr(event, "attribute_name", None) or getattr(event, "attribute_id", None)
            endpoint_id, cluster_id = getattr(event, "endpoint_id", None), getattr(event, "cluster_id", None)
            now = self._clock()
            if self._in_own_frame():
                self._reached.add(key_of(endpoint_id, cluster_id, attribute))
            for key in (key_of(endpoint_id, cluster_id, attribute), key_of(endpoint_id, cluster_id, WHOLE_CLUSTER)):
                if key in self.fresh or len(self.fresh) < MAX_KEYS:
                    self.fresh[key] = now
                if self._on_fresh is not None:
                    self._on_fresh(self, key)
        except Exception:  # noqa: BLE001, S110 - a listener never disturbs ZHA
            pass

    def _cleared(self, event: Any) -> None:
        """A quirk set an attribute to no value: the datapoint reached it (no fresh value)."""
        try:
            if self._in_own_frame():
                attribute = getattr(event, "attribute_name", None) or getattr(event, "attribute_id", None)
                self._reached.add(key_of(getattr(event, "endpoint_id", None), getattr(event, "cluster_id", None), attribute))
        except Exception:  # noqa: BLE001, S110 - a listener never disturbs ZHA
            pass

    def cluster_command(self, tsn: int, command_id: int, args: Any) -> None:
        """A command the Tuya MCU cluster received (zigpy's legacy listener call)."""
        try:
            dps = datapoints_of(args)
            if not dps:
                return
            now = self._clock()
            self.last_datapoints = now
            for dp in dps:
                if dp not in self._handled and (dp in self.unmapped or len(self.unmapped) < MAX_DATAPOINTS):
                    self.unmapped[dp] = now
            if self._on_datapoints is not None:
                self._on_datapoints(self, dps)
            if self._listening and self._in_own_frame():
                # zigpy has handed the frame to the cluster: a handled datapoint none of whose attributes it reached was
                # refused. The log named those it heard already.
                for dp in dict.fromkeys(dps):
                    keys = set(mapped_keys(self.tuya, dp)) if dp in self._handled and dp not in self._heard else set()
                    if keys and not keys & self._reached:
                        self._refusal(dp, "")
        except Exception:  # noqa: BLE001, S110 - a listener never disturbs ZHA
            pass

    def refused_record(self, args: tuple) -> None:
        """zhaquirks' warning for a skipped datapoint (REFUSED_FORMAT with zigpy's cluster prefix): kept when it names
        this watch's Tuya cluster (the device's zigpy name, the endpoint and the cluster id as zigpy writes them)."""
        tuya = self.tuya
        endpoint = getattr(tuya, "endpoint", None)
        prefix = (getattr(getattr(endpoint, "device", None), "name", None), getattr(endpoint, "endpoint_id", None), getattr(tuya, "cluster_id", None))
        dp = args[3]
        if None in prefix or tuple(args[:3]) != prefix or not isinstance(dp, int):
            return
        dp = int(dp)
        if self._in_own_frame():
            self._heard.add(dp)
        self._refusal(dp, str(args[6]))

    def _refusal(self, dp: int, reason: str) -> None:
        """Keep a refused datapoint with why (empty: the log did not say). A refusal without a reason keeps the reason the
        log gave for that datapoint before: the same file refused it then."""
        now = self._clock()
        self.last_datapoints = now
        reason = reason or self.refused.get(dp, (now, ""))[1]
        if dp not in self.refused and len(self.refused) >= MAX_DATAPOINTS:
            # Full: the oldest refusal goes, so those of earlier checks never crowd out the ones of a check that runs now.
            del self.refused[min(self.refused, key=lambda old: self.refused[old][0])]
        self.refused[dp] = (now, reason if len(reason) <= MAX_REASON else reason[: MAX_REASON - 3] + "...")
        if self._on_refused is not None:
            self._on_refused(self, dp)

    def fresh_since(self, since: float) -> set[str]:
        return {key for key, at in self.fresh.items() if at >= since}

    def unmapped_since(self, since: float) -> list[int]:
        return sorted(dp for dp, at in self.unmapped.items() if at >= since)

    def refused_since(self, since: float) -> list[tuple[int, str]]:
        """(datapoint, why its Tuya cluster refused it) for the refusals heard since then."""
        return sorted((dp, reason) for dp, (at, reason) in self.refused.items() if at >= since)
