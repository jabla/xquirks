"""tools/probe.py against a stand-in for HA's websocket: the setting check and an unreachable device."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import struct

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("probe", ROOT / "tools/probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

SELECT = "select.sensor_mode"


class FakeWS:
    """Keeps one select's or number's state; a Tuya data query fails while the device is away. drops:
    how often the device drops a write of a value before it takes one."""

    def __init__(self, state: str, away: bool = False, drops: dict | None = None, entity_id: str = SELECT, times_out: bool = False) -> None:
        self.state, self.away, self.written, self.drops, self.entity_id = state, away, [], dict(drops or {}), entity_id
        # times_out: ZHA's service raises although the device answered (ZHA 2.4 waits for a reply a
        # Tuya device never sends). frames: what last_seen counts, one more per answered query.
        self.times_out, self.frames = times_out, 0

    def wait(self, seconds: float) -> None:
        probe.time.sleep(seconds)

    def call(self, message: dict):
        if message["type"] == "get_states":
            return [{"entity_id": self.entity_id, "state": self.state, "attributes": {"options": ["a", "b", "c"]}}]
        if message["type"] == "zha/device":
            return {"last_seen": f"2026-11-14T20:00:{self.frames:02d}"}
        if message.get("service") == "issue_zigbee_cluster_command":
            if self.away:
                raise RuntimeError("call_service: Failed to send request: device did not respond")
            self.frames += 1
            if self.times_out:
                raise RuntimeError("call_service: unknown_error")
        if message.get("service") in ("select_option", "set_value"):
            data = message["service_data"]
            value = data.get("option", data.get("value"))
            self.written.append(value)
            if self.drops.get(value, 0):
                self.drops[value] -= 1
            else:
                self.state = str(int(value)) if isinstance(value, float) else str(value)
        return None


def test_select_check_writes_every_other_option_then_restores(monkeypatch):
    monkeypatch.setattr(probe.time, "sleep", lambda _: None)
    ws = FakeWS("b")
    entity = {"entity_id": SELECT, "original_name": "Sensor mode"}
    lines = probe.check_setting(ws, entity, {"state": "b", "attributes": {"options": ["a", "b", "c"]}}, "ieee", 1, 0, None)
    assert ws.written == ["a", "c", "b"]
    assert lines == ["Sensor mode: wrote a, HA shows a", "Sensor mode: wrote c, HA shows c", "Sensor mode: wrote b, HA shows b"]


def test_unreachable_device_stops_the_check_and_still_restores(monkeypatch):
    monkeypatch.setattr(probe.time, "sleep", lambda _: None)
    ws = FakeWS("b", away=True)
    entity = {"entity_id": SELECT, "original_name": "Sensor mode"}
    lines = probe.check_setting(ws, entity, {"state": "b", "attributes": {"options": ["a", "b", "c"]}}, "ieee", 1, 0, None)
    assert lines[0].startswith("Sensor mode: stopped, call_service: Failed to send request")
    # The restore went out, only the query after it failed.
    assert lines[1].startswith("Sensor mode: wrote b back, but could not read it back: check that it shows b")
    assert ws.written == ["a", "b"]


def test_a_query_that_times_out_on_a_device_that_answered_goes_on(monkeypatch):
    """ZHA 2.4 waits for a reply to the Tuya data query that the device never sends and raises: the
    device answered all the same (its last_seen moved), so the check goes on."""
    monkeypatch.setattr(probe.time, "sleep", lambda _: None)
    ws = FakeWS("b", times_out=True)
    assert probe.tuya_query(ws, "ieee", 1, 0) is None
    lines = probe.check_setting(ws, {"entity_id": SELECT, "original_name": "Sensor mode"}, {"state": "b", "attributes": {"options": ["a", "b", "c"]}}, "ieee", 1, 0, None)
    assert lines == ["Sensor mode: wrote a, HA shows a", "Sensor mode: wrote c, HA shows c", "Sensor mode: wrote b, HA shows b"]


def test_a_query_without_a_frame_from_the_device_says_so(monkeypatch):
    monkeypatch.setattr(probe.time, "sleep", lambda _: None)
    ws = FakeWS("b", away=True)
    assert probe.tuya_query(ws, "ieee", 1, 0) == "call_service: Failed to send request: device did not respond"
    ws.away, ws.call = False, lambda message: {"last_seen": "2026-11-14T20:00:00"} if message["type"] == "zha/device" else None
    assert probe.tuya_query(ws, "ieee", 1, 0) == "no frame from the device after the query"


def test_restore_that_does_not_go_out_is_flagged(monkeypatch):
    monkeypatch.setattr(probe.time, "sleep", lambda _: None)
    ws = FakeWS("b", away=True)
    calls = []

    def refuse(ws_, entity_id, value):
        calls.append(value)
        if len(calls) > 1:
            raise OSError("timed out")
        ws_.state = value

    monkeypatch.setattr(probe, "set_value", refuse)
    lines = probe.check_setting(ws, {"entity_id": SELECT, "original_name": "Sensor mode"}, {"state": "b", "attributes": {"options": ["a", "b"]}}, "ieee", None, 0, None)
    assert lines == ["Sensor mode: wrote a, HA shows a", "Sensor mode: RESTORE FAILED, set it back to b by hand (timed out)"]


def test_unknown_value_is_never_written(monkeypatch):
    ws = FakeWS("unknown")
    lines = probe.check_setting(ws, {"entity_id": SELECT}, {"state": "unknown", "attributes": {}}, "ieee", 1, 0, None)
    assert ws.written == []
    # Named without its entity id, which carries area names into the public report.
    assert lines == ["the setting: skipped, the current value is unknown and could not be restored"]
    lines = probe.check_setting(ws, {"entity_id": SELECT, "unique_id": "aa:bb-1-sensor_mode"}, {"state": "unknown", "attributes": {}}, "ieee", 1, 0, None)
    assert lines == ["1-sensor_mode: skipped, the current value is unknown and could not be restored"]


def test_a_dropped_write_is_written_again(monkeypatch):
    """Seen on a Tuya presence sensor: a write right after another one did not reach the device."""
    monkeypatch.setattr(probe.time, "sleep", lambda _: None)
    ws = FakeWS("b", drops={"c": 1})
    lines = probe.check_setting(ws, {"entity_id": SELECT, "original_name": "Sensor mode"}, {"state": "b", "attributes": {"options": ["a", "b", "c"]}}, "ieee", 1, 0, None)
    assert ws.written == ["a", "c", "c", "b"]
    assert lines == ["Sensor mode: wrote a, HA shows a", "Sensor mode: wrote c, HA shows c (after 2 writes)", "Sensor mode: wrote b, HA shows b"]


def test_a_restore_the_device_never_takes_is_flagged(monkeypatch):
    monkeypatch.setattr(probe.time, "sleep", lambda _: None)
    number = "number.motion_sensitivity"
    ws = FakeWS("3", drops={3.0: 9}, entity_id=number)
    state = {"state": "3", "attributes": {"min": 0, "max": 7}}
    lines = probe.check_setting(ws, {"entity_id": number, "original_name": "Motion sensitivity"}, state, "ieee", 1, 0, None)
    assert ws.written == [0, 7, 3.0, 3.0, 3.0]
    assert lines == [
        "Motion sensitivity: wrote 0, HA shows 0",
        "Motion sensitivity: wrote 7, HA shows 7",
        "Motion sensitivity: wrote 3.0, HA shows 7 (after 3 writes)",
        "Motion sensitivity: RESTORE FAILED, set it back to 3 by hand (the device did not take it)",
    ]


def test_numbers_compare_by_value(monkeypatch):
    monkeypatch.setattr(probe.time, "sleep", lambda _: None)
    number = "number.output_time"
    ws = FakeWS("10", entity_id=number)
    lines = probe.check_setting(ws, {"entity_id": number, "original_name": "Output time"}, {"state": "10", "attributes": {"min": 10, "max": 1800}}, "ieee", 1, 0, None)
    assert ws.written == [10, 1800, 10.0]
    assert lines == ["Output time: wrote 10, HA shows 10", "Output time: wrote 1800, HA shows 1800", "Output time: wrote 10.0, HA shows 10"]


def test_any_failure_still_reports_the_restore(monkeypatch):
    """Not only RuntimeError and OSError: whatever stops the check, the report says whether the
    original value went back."""
    monkeypatch.setattr(probe.time, "sleep", lambda _: None)

    def broken(ws_, entity_id, value):
        raise ValueError("unexpected reply")

    monkeypatch.setattr(probe, "set_value", broken)
    lines = probe.check_setting(FakeWS("b"), {"entity_id": SELECT, "original_name": "Sensor mode"}, {"state": "b", "attributes": {"options": ["a", "b"]}}, "ieee", None, 0, None)
    assert lines == ["Sensor mode: stopped, unexpected reply", "Sensor mode: RESTORE FAILED, set it back to b by hand (unexpected reply)"]


def frame(opcode: int, payload: bytes) -> bytes:
    """A frame from HA (unmasked)."""
    length = bytes([len(payload)]) if len(payload) < 126 else bytes([126]) + struct.pack(">H", len(payload))
    return bytes([0x80 | opcode]) + length + payload


def client_frame(data: bytes) -> tuple[int, bytes]:
    """Opcode and payload of a frame from the client (masked)."""
    length, offset = data[1] & 0x7F, 2
    if length == 126:
        length, offset = struct.unpack(">H", data[2:4])[0], 4
    mask, payload = data[offset:offset + 4], data[offset + 4:offset + 4 + length]
    return data[0] & 0x0F, bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


class HASocket:
    """HA's end of the websocket: the handshake, the auth, then FakeWS answers each call. HA's
    heartbeat (aiohttp, every 55 s without traffic) pings before each answer when ping is set.
    close_after: the calls answered before HA closes the connection (a close frame, then nothing).
    clock: a clock the test's time.sleep moves on; with it HA, like aiohttp's heartbeat, pings a client
    it got no frame from for 55 s and drops it, without a close frame, after another 27.5 s without one."""

    def __init__(self, ha: FakeWS, ping: bool = True, close_after: int | None = None, clock: list[float] | None = None) -> None:
        self.ha, self.ping, self.close_after, self.clock = ha, ping, close_after, clock
        self.incoming, self.upgraded, self.closed, self.calls, self.pongs = b"", False, False, 0, []
        self.last = clock[0] if clock else 0.0

    def sendall(self, data: bytes) -> None:
        if self.clock:
            if self.clock[0] - self.last > 55 + 27.5:
                self.closed = True
            elif self.clock[0] - self.last > 55:
                self.incoming += frame(0x9, b"")
            self.last = self.clock[0]
        if self.closed:
            raise BrokenPipeError(32, "Broken pipe")
        if not self.upgraded:
            self.upgraded = True
            self.incoming += b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n" + frame(0x1, b'{"type": "auth_required"}')
            return
        opcode, payload = client_frame(data)
        if opcode == 0xA:
            self.pongs.append(payload)
            return
        if opcode == 0x9:  # aiohttp answers a ping from the client with a pong
            self.incoming += frame(0xA, payload)
            return
        message = json.loads(payload)
        if message["type"] == "auth":
            self.incoming += frame(0x1, b'{"type": "auth_ok"}')
        elif self.close_after is not None and self.calls == self.close_after:
            self.incoming += frame(0x8, struct.pack(">H", 1001) + b"going away")
            self.closed = True
        else:
            self.calls += 1
            reply = {"id": message["id"], "type": "result", "success": True, "result": self.ha.call(message)}
            self.incoming += (frame(0x9, b"hb") if self.ping else b"") + frame(0x1, json.dumps(reply).encode())

    def recv(self, n: int) -> bytes:
        data, self.incoming = self.incoming[:n], self.incoming[n:]
        return data


def connect(monkeypatch, ha_socket: HASocket):
    monkeypatch.setattr(probe.socket, "create_connection", lambda address, timeout: ha_socket)
    return probe.HAWebSocket("ws://homeassistant.local:8123", "token")


def test_the_heartbeat_ping_gets_its_pong(monkeypatch):
    """HA pings a client that sent nothing for 55 s (a --wait of 55 or more): the probe answers
    and goes on."""
    monkeypatch.setattr(probe.time, "sleep", lambda _: None)
    ha = HASocket(FakeWS("b"))
    lines = probe.check_setting(connect(monkeypatch, ha), {"entity_id": SELECT, "original_name": "Sensor mode"}, {"state": "b", "attributes": {"options": ["a", "b", "c"]}}, "ieee", None, 0, None)
    assert lines == ["Sensor mode: wrote a, HA shows a", "Sensor mode: wrote c, HA shows c", "Sensor mode: wrote b, HA shows b"]
    assert ha.ha.written == ["a", "c", "b"]
    assert ha.pongs == [b"hb"] * ha.calls


@pytest.mark.parametrize("close_after", [1, 2])
def test_a_closed_connection_is_reported_with_the_restore(monkeypatch, close_after):
    """HA closes the connection instead of answering a read-back or the next write: the check stops, and the
    report says that the original value could not be written back."""
    monkeypatch.setattr(probe.time, "sleep", lambda _: None)
    ha = HASocket(FakeWS("b"), ping=False, close_after=close_after)
    lines = probe.check_setting(connect(monkeypatch, ha), {"entity_id": SELECT, "original_name": "Sensor mode"}, {"state": "b", "attributes": {"options": ["a", "b", "c"]}}, "ieee", None, 0, None)
    assert lines[:-2] == ["Sensor mode: wrote a, HA shows a"][:close_after - 1]
    assert lines[-2] == "Sensor mode: stopped, websocket closed by Home Assistant (1001: going away)"
    assert lines[-1].startswith("Sensor mode: RESTORE FAILED, set it back to b by hand (")


@pytest.mark.parametrize("tuya", [None, 1])
def test_a_wait_longer_than_the_heartbeat_keeps_the_connection(monkeypatch, tuya):
    """HA drops a client it got nothing from for 55 + 27.5 s (aiohttp's heartbeat): a --wait of
    90 s still reads every value back and restores the original one."""
    clock = [0.0]
    monkeypatch.setattr(probe.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    ha = HASocket(FakeWS("b"), ping=False, clock=clock)
    lines = probe.check_setting(connect(monkeypatch, ha), {"entity_id": SELECT, "original_name": "Sensor mode"}, {"state": "b", "attributes": {"options": ["a", "b", "c"]}}, "ieee", tuya, 90, None)
    assert lines == ["Sensor mode: wrote a, HA shows a", "Sensor mode: wrote c, HA shows c", "Sensor mode: wrote b, HA shows b"]
    assert ha.ha.written == ["a", "c", "b"]
    assert clock[0] == (6 if tuya is not None else 3) * 90


# ZHA's quirk class of a device that runs z2m_sonoff_swv.py ("<module>:<label>") and zigpy's names of that device.
RUNS_SWV = "z2m_sonoff_swv:SonoffSWV"
SWV = ("SONOFF", "SWV")


def test_the_report_names_the_xquirks_file_and_its_revision():
    diagnostics = {"data": {"installed": {
        "/config/custom_zha_quirks/z2m_other.py": {"claims": ["SONOFF SWV"], "active_for": [], "revision": "11111111"},
        "/config/custom_zha_quirks/z2m_sonoff_swv.py": {"claims": ["SONOFF SWV"], "active_for": ["SONOFF SWV"], "revision": "a500ee63"},
    }}}
    # The file the device runs (its quirk class names the module) wins over another one that claims it.
    assert probe.xquirks_file(diagnostics, RUNS_SWV, SWV) == "z2m_sonoff_swv.py (revision a500ee63)"
    diagnostics["data"]["installed"]["/config/custom_zha_quirks/z2m_sonoff_swv.py"]["unchanged_on_disk"] = False
    assert probe.xquirks_file(diagnostics, RUNS_SWV, SWV) == "z2m_sonoff_swv.py (revision a500ee63, changed on disk since)"
    del diagnostics["data"]["installed"]["/config/custom_zha_quirks/z2m_sonoff_swv.py"]
    assert probe.xquirks_file(diagnostics, RUNS_SWV, SWV) == "z2m_other.py (revision 11111111, claims the device, ZHA does not run it)"
    assert probe.xquirks_file(diagnostics, "zhaquirks.acme:AcmeSensor", ("Acme", "B-1")) == probe.NO_FILE
    assert probe.xquirks_file({}, RUNS_SWV, SWV) == probe.NO_FILE
    # Without diagnostics the report does not claim to know.
    assert probe.xquirks_file(probe.UNREADABLE, RUNS_SWV, SWV) == probe.UNREADABLE
    assert probe.xquirks_file(probe.NOT_SET_UP, RUNS_SWV, SWV) == probe.NOT_SET_UP


def test_the_file_is_the_one_the_device_runs_not_one_named_like_it():
    """The registry shows a quirk's friendly name, and two devices of one model share zigpy's names: the device's own quirk
    class says which file it runs, zigpy's names which files claim it."""
    diagnostics = {"data": {"installed": {"/config/custom_zha_quirks/z2m_sonoff_swv.py": {"claims": ["SONOFF SWV"], "active_for": ["SONOFF SWV"], "revision": "a500ee63"}}}}
    assert probe.xquirks_file(diagnostics, RUNS_SWV, SWV) == "z2m_sonoff_swv.py (revision a500ee63)"
    # Another SWV runs the file, this one ZHA's own quirk (it is left out of xquirks).
    assert probe.xquirks_file(diagnostics, "zhaquirks.sonoff.swv:SonoffSmartWaterValve", SWV) == "z2m_sonoff_swv.py (revision a500ee63, claims the device, ZHA does not run it)"


def test_a_tuya_builder_file_is_found_although_the_quirk_class_names_no_file():
    """ZHA's quirk class of a quirk made with TuyaQuirkBuilder names the builder's module, for ZHA's own Tuya quirks and
    for the files in the custom quirks folder alike. The integration reads the quirk's source file and lists the devices
    that run each file by zigpy's names: the report goes by that for such a device."""
    from zhaquirks.builder import QuirkBuilder
    from zhaquirks.tuya.builder import TuyaQuirkBuilder

    # As ZHA builds the quirk class: the module that called QuirkBuilder.__init__, which for a TuyaQuirkBuilder is its own.
    assert TuyaQuirkBuilder().quirk_module == probe.TUYA_BUILDER and QuirkBuilder().quirk_module == __name__
    runs = "zhaquirks.tuya.builder:(_TZE200_aaaa / TS0601)"
    pair = ("_TZE200_aaaa", "TS0601")
    tuya = {"claims": ["_TZE200_aaaa TS0601"], "active_for": ["_TZE200_aaaa TS0601"], "revision": "0c0ffee0"}
    diagnostics = {"data": {"installed": {
        "/config/custom_zha_quirks/z2m_other.py": {"claims": ["_TZE200_aaaa TS0601"], "active_for": [], "revision": "11111111"},
        "/config/custom_zha_quirks/z2m_tuya_a.py": tuya,
    }}}
    assert probe.xquirks_file(diagnostics, runs, pair) == "z2m_tuya_a.py (revision 0c0ffee0)"
    # Two devices of these names, both on the file.
    tuya["claims"] = tuya["active_for"] = ["_TZE200_aaaa TS0601"] * 2
    assert probe.xquirks_file(diagnostics, runs, pair) == "z2m_tuya_a.py (revision 0c0ffee0)"
    # One of them runs another Tuya quirk: the quirk class does not tell which one this device is.
    tuya["active_for"] = ["_TZE200_aaaa TS0601"]
    assert probe.xquirks_file(diagnostics, runs, pair) == "z2m_tuya_a.py (revision 0c0ffee0, runs on 1 of 2 devices of this model, ZHA does not say on which)"
    # No device of these names runs it: the device runs ZHA's own Tuya quirk.
    tuya["active_for"] = []
    assert probe.xquirks_file(diagnostics, runs, pair) == "z2m_other.py (revision 11111111, claims the device, ZHA does not run it)"
    # A quirk class that names a module says which quirk the device runs: the integration's list is not asked.
    tuya["active_for"] = ["_TZE200_aaaa TS0601"]
    assert probe.xquirks_file(diagnostics, "zhaquirks.tuya.ts0601_sensor:TuyaSensor", pair) == "z2m_other.py (revision 11111111, claims the device, ZHA does not run it)"
    # A device of another name is none of the file's.
    assert probe.xquirks_file(diagnostics, "zhaquirks.tuya.builder:(_TZE200_bbbb / TS0601)", ("_TZE200_bbbb", "TS0601")) == probe.NO_FILE


def test_without_a_loaded_xquirks_entry_the_report_names_no_file(monkeypatch):
    class Entries:
        def __init__(self, entries):
            self.entries = entries

        def call(self, message):
            assert message == {"type": "config_entries/get", "domain": "xquirks"}
            return self.entries

    asked = []
    monkeypatch.setattr(probe, "rest_get", lambda url, token, path: asked.append(path) or {"data": {}})
    assert probe.xquirks_diagnostics(Entries([]), "http://ha", "t") == probe.NOT_SET_UP
    assert probe.xquirks_diagnostics(Entries([{"entry_id": "e1", "state": "not_loaded"}]), "http://ha", "t") == probe.NOT_SET_UP
    assert probe.xquirks_diagnostics(Entries([{"entry_id": "e1", "state": "loaded"}]), "http://ha", "t") == {"data": {}}
    assert asked == ["/api/diagnostics/config_entry/e1"]

    def refuse(url, token, path):
        raise probe.urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(probe, "rest_get", refuse)
    assert probe.xquirks_diagnostics(Entries([{"entry_id": "e1", "state": "loaded"}]), "http://ha", "t") == probe.UNREADABLE


def test_entities_left_over_from_an_earlier_quirk_are_kept_apart():
    entities = [{"entity_id": "sensor.mine"}, {"entity_id": "binary_sensor.old"}, {"entity_id": "sensor.offline"}]
    states = {
        "sensor.mine": {"state": "21.5", "attributes": {}},
        # Restored from the registry: nothing set it up in this run.
        "binary_sensor.old": {"state": "unavailable", "attributes": {"restored": True}},
        # The quirk shows it, the device is away: unavailable, not restored.
        "sensor.offline": {"state": "unavailable", "attributes": {}},
    }
    assert probe.left_over(entities, states) == {"binary_sensor.old"}


ZHA_ENTITIES = [
    {"entity_id": "switch.valve", "device_id": "dev1", "platform": "zha", "unique_id": "00:11-1-6", "original_name": None, "translation_key": "switch"},
    {"entity_id": "binary_sensor.valve_leak", "device_id": "dev1", "platform": "zha", "unique_id": "00:11-1-1280", "original_name": "Water leak"},
]
ZHA_STATES = [{"entity_id": "switch.valve", "state": "off", "attributes": {}},
              {"entity_id": "binary_sensor.valve_leak", "state": "unavailable", "attributes": {"restored": True}}]


def run_probe(monkeypatch, tmp_path, *args: str, entities: list[dict] = ZHA_ENTITIES, states: list[dict] = ZHA_STATES, device: dict | None = None,
              clusters: list[dict] | None = None, commands: dict | None = None, attributes: dict | None = None, calls: list | None = None) -> str:
    """main() against a stand-in for HA with one SONOFF SWV on its xquirks file; the report it writes. device: other fields of
    its registry entry; clusters, commands and attributes ((endpoint, cluster id) -> commands or attributes): what ZHA lists
    for it (a read of an attribute answers 2150); calls: every message main() sent, in order. Its diagnostics name the quirk
    it runs and zigpy's names of it."""
    registered = {"id": "dev1", "name": "Valve", "name_by_user": None, "manufacturer": "SONOFF", "model": "SWV",
                  "identifiers": [["zha", "00:11:22:33:44:55:66:77"]], "config_entries": ["zha-entry"], "sw_version": "1.0", **(device or {})}
    sent = [] if calls is None else calls

    class HA:
        def __init__(self, url, token):
            pass

        def wait(self, seconds):
            pass

        def call(self, message):
            sent.append(message)
            if message["type"] == "zha/devices/clusters/commands":
                return (commands or {}).get((message["endpoint_id"], message["cluster_id"]), [])
            if message["type"] == "zha/devices/clusters/attributes":
                return (attributes or {}).get((message["endpoint_id"], message["cluster_id"]), [])
            if message["type"] == "zha/devices/clusters/attributes/value":
                return "2150"
            if message["type"] == "zha/device":
                # Every frame moves last_seen: the device answers.
                return {"last_seen": str(len(sent))}
            return {
                "config_entries/get": [{"entry_id": f"{message.get('domain')}-entry", "state": "loaded"}],
                "config/device_registry/list": [registered],
                "config/entity_registry/list": entities,
                "get_states": states,
                "zha/devices/clusters": clusters or [],
                "call_service": None,
            }[message["type"]]

    def rest_get(url, token, path):
        if path == "/api/diagnostics/config_entry/xquirks-entry":
            return {"data": {"installed": {"/config/custom_zha_quirks/z2m_sonoff_swv.py": {"claims": ["SONOFF SWV"], "active_for": ["SONOFF SWV"], "revision": "a500ee63"}}}}
        return {"data": {"quirk_class": RUNS_SWV, "manufacturer": "SONOFF", "model": "SWV", "friendly_manufacturer": "SONOFF", "friendly_model": "SWV"}}

    monkeypatch.setattr(probe, "HAWebSocket", HA)
    monkeypatch.setattr(probe, "rest_get", rest_get)
    monkeypatch.setenv("HASS_URL", "http://ha")
    monkeypatch.setenv("HASS_TOKEN", "t")
    monkeypatch.setattr(probe.sys, "argv", ["probe.py", "Valve", "--out", str(tmp_path), *args])
    probe.main()
    return (tmp_path / "report.md").read_text()


def test_the_report_carries_the_file_and_keeps_left_over_entities_out_of_the_table(monkeypatch, tmp_path):
    report = run_probe(monkeypatch, tmp_path, "--write", "binary_sensor.valve_leak")
    assert f"- xquirks file: z2m_sonoff_swv.py (revision a500ee63)\n- Quirk class: {RUNS_SWV}\n" in report
    table, rest = report.split("## Left over from an earlier quirk")
    assert "| switch: switch |" in table and "Water leak" not in table
    assert "- binary_sensor: Water leak\n" in rest
    assert "- Water leak: left over from an earlier quirk, skipped\n" in rest
    # No entity id reaches the report: they carry area names.
    assert "binary_sensor.valve_leak" not in report and "switch.valve" not in report


def test_a_helper_linked_to_the_device_stays_out_of_the_report(monkeypatch, tmp_path):
    """A helper the user linked to the device carries the name they typed, its state and its unique id: the report is for
    a public issue, so it lists ZHA's entities only, and --write never touches anything else."""
    helper = {"entity_id": "number.kitchen_washer_daily", "device_id": "dev1", "platform": "template", "unique_id": "01JCKITCHENWASHER0000000000",
              "original_name": "Kitchen washer daily", "entity_category": "config"}
    states = [*ZHA_STATES, {"entity_id": helper["entity_id"], "state": "42", "attributes": {"min": 0, "max": 100}}]
    report = run_probe(monkeypatch, tmp_path, "--write", helper["entity_id"], entities=[*ZHA_ENTITIES, helper], states=states)
    assert "Kitchen washer daily" not in report and "01JCKITCHENWASHER" not in report and "| 42 |" not in report
    assert "- an entity that is not a ZHA entity of this device: skipped\n" in report
    assert "| switch: switch |" in report


def test_the_attribute_a_button_writes_is_not_read(monkeypatch, tmp_path):
    """A write button names the attribute it writes, in its unique id like an entity that reads one. Its state is when it
    was last pressed: the probe reads nothing for it, as the integration's device check does not, so a device that only
    takes writes of that attribute gets no "no answer" in the button's row."""
    entities = [
        {"entity_id": "sensor.valve_temperature", "device_id": "dev1", "platform": "zha", "unique_id": "00:11-1-measured_value", "original_name": "Temperature"},
        {"entity_id": "button.valve_self_test", "device_id": "dev1", "platform": "zha", "unique_id": "00:11-1-self_test", "original_name": "Self test",
         "entity_category": "config"},
    ]
    states = [{"entity_id": "sensor.valve_temperature", "state": "21.5", "attributes": {"unit_of_measurement": "°C"}},
              {"entity_id": "button.valve_self_test", "state": "unknown", "attributes": {}}]
    clusters = [{"type": "in", "id": 0x0402, "endpoint_id": 1, "name": "TemperatureMeasurement"}]
    attributes = {(1, 0x0402): [{"name": "measured_value", "id": 0}, {"name": "self_test", "id": 0x4000}]}
    calls: list = []
    report = run_probe(monkeypatch, tmp_path, "--query", entities=entities, states=states, clusters=clusters, attributes=attributes, calls=calls)
    assert [message["attribute"] for message in calls if message["type"] == "zha/devices/clusters/attributes/value"] == [0]
    assert "| [ ] | sensor: Temperature | 1-measured_value |  | 21.5 | °C | 2150 (ZHA had no value) |\n" in report
    assert "| [ ] | button: Self test | 1-self_test | config | unknown |  |  |\n" in report


def test_a_friendly_name_in_the_registry_does_not_hide_the_file(monkeypatch, tmp_path):
    """The file sets a friendly name: the registry shows it, the integration's diagnostics list zigpy's names."""
    report = run_probe(monkeypatch, tmp_path, device={"manufacturer": "Sonoff", "model": "Smart water valve"})
    assert "- xquirks file: z2m_sonoff_swv.py (revision a500ee63)\n" in report


TUYA_IN = {"type": "in", "id": 0xEF00, "endpoint_id": 1, "name": "TuyaMCUCluster"}
QUERY_DATA = {(1, 0xEF00): [{"type": "client", "id": 1, "name": "get_data"}, {"type": "server", "id": 3, "name": "query_data"}]}


def queries(calls: list) -> list[int]:
    """The endpoints the Tuya data queries main() sent went to."""
    return [m["service_data"]["endpoint_id"] for m in calls if m.get("service") == "issue_zigbee_cluster_command"]


@pytest.mark.parametrize(
    ("device", "clusters", "commands", "tuya"),
    [
        # A Tuya plug that talks ZCL: no Tuya MCU cluster, whatever its name says.
        ({"manufacturer": "_TZ3000_abcdefgh", "model": "TS011F"}, [{"type": "in", "id": 6, "endpoint_id": 1, "name": "OnOff"}], {}, False),
        # A TS0601 whose quirk names it otherwise: the registry shows that name, the quirk gives its Tuya cluster the query.
        ({"manufacturer": "Moes", "model": "Thermostat"}, [TUYA_IN], QUERY_DATA, True),
        # The raw Tuya cluster of a device without a quirk defines no commands: no Tuya data query.
        ({"manufacturer": "_TZE200_abcdefgh", "model": "TS0601"}, [TUYA_IN], {}, False),
    ],
)
def test_a_tuya_device_is_known_by_the_query_its_quirk_gives_it(monkeypatch, tmp_path, device, clusters, commands, tuya):
    """Like the integration, the probe asks the Tuya MCU cluster for a data query only where the quirk ZHA applied gives that
    cluster one; any other device gets the reads of its attributes."""
    calls: list = []
    report = run_probe(monkeypatch, tmp_path, "--query", device=device, clusters=clusters, commands=commands, calls=calls)
    assert queries(calls) == ([1] if tuya else [])
    assert f"- Asked the device: {'Tuya data query' if tuya else 'read of each quirk attribute'}\n" in report
    assert "did not answer" not in report
