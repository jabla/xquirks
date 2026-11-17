"""A Tuya chain registers the device's Tuya cluster on a class of the file that holds the fixed type check
(guard.TYPED_TUYA): every datapoint value is checked against its attribute's type before zhaquirks stores it. Text
never reaches a number type, an enum, bitmap or Bool type holds only its own values. A plain integer type of another
cluster holds its range (ZHA's Illuminance raises 10 to the value, a UTCTime adds it to a date). On the Tuya cluster
itself it holds what its bits hold read signed or unsigned (a sensor typed unsigned may read below zero). A float type
holds what a single precision float holds.

A device chooses the type of each datapoint it sends. A STRING or RAW one where a number is meant reached the
attribute cache as text, zigpy's database kept it, and at the next start ZHA divided it (a sensor's divisor) or looked
it up in an enum (a select) while it loaded its devices, where nothing catches an error: ZHA did not start."""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest

from .integration import FOLDER, PACKAGE, guard
from .test_guard_files import CHAIN_END, FIXED_CODE, METER, SHIPPED, TUYA, TUYA_END, frozen, targets

ROOT = Path(__file__).resolve().parents[1]
TYPED_BODY = next(text for text in FIXED_CODE["class"] if f"def {guard.TYPED_TUYA}(" in text)
TYPED_CLASS = "class TypedTuyaCluster(TuyaMCUCluster):\n" + TYPED_BODY.split("\n", 1)[1]
# The meter with a tuya_enum (no converter: zhaquirks adds none) and a tuya_temperature (zhaquirks' own `x * scale`).
EXTRA = ("    .tuya_enum(dp_id=9, attribute_name='mode_x', enum_class=OnlineState, translation_key='mode_x', fallback_name='Mode')\n"
         "    .tuya_temperature(dp_id=8, scale=10)\n")
METER_PLUS = frozen(METER).replace(TUYA_END, EXTRA + TUYA_END, 1)


def problems(text: str, name: str = METER) -> list[str]:
    return list(SHIPPED.problems(text.encode(), targets(name)))


def edit(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, old
    return text.replace(old, new, 1)


def test_tuya_files_with_the_typed_cluster_pass():
    assert problems(frozen(TUYA), TUYA) == []
    assert problems(METER_PLUS) == []


METHOD = TYPED_CLASS.split("\n", 3)[3]  # the method's text, after the class line, its docstring and the blank line
REASON = "a Tuya chain that does not end in add_to_registry(replacement_cluster=Cls)"
REFUSED = {
    "no typed cluster": edit(METER_PLUS, TUYA_END, CHAIN_END),
    "zhaquirks' own Tuya cluster": edit(METER_PLUS, TUYA_END, "    .add_to_registry(replacement_cluster=TuyaMCUCluster)\n"),
    "a class without the method": edit(METER_PLUS, METHOD, "    cluster_id = 0xEF00\n"),
    "the method changed": edit(METER_PLUS, "                    continue\n", "                    pass\n"),
    "the method beside a cluster id": edit(METER_PLUS, METHOD, "    cluster_id = 0x0006\n\n" + METHOD),
    # The Aqara class body of the fixed code: fixed statements, but nothing beside the method.
    "the method beside another fixed method": edit(METER_PLUS, METHOD, METHOD + "\n" + FIXED_CODE["class"][0].split("\n", 1)[1]),
    "a class on another base too": edit(edit(METER_PLUS, "class TypedTuyaCluster(TuyaMCUCluster):", "class TypedTuyaCluster(TuyaMCUCluster, OnOff):"),
                                        "from zhaquirks.tuya.mcu import", "from zigpy.zcl.clusters.general import OnOff\nfrom zhaquirks.tuya.mcu import"),
    "a class on another zhaquirks cluster": edit(edit(METER_PLUS, "class TypedTuyaCluster(TuyaMCUCluster):", "class TypedTuyaCluster(CustomCluster):"),
                                                 "from zhaquirks.tuya.mcu import", "from zhaquirks.clusters import CustomCluster\nfrom zhaquirks.tuya.mcu import"),
    "another keyword beside it": edit(METER_PLUS, TUYA_END, "    .add_to_registry(replacement_cluster=TypedTuyaCluster, force_add_cluster=True)\n"),
    "the class as a positional argument": edit(METER_PLUS, TUYA_END, "    .add_to_registry(TypedTuyaCluster)\n"),
    "an enum in its place": edit(METER_PLUS, TUYA_END, "    .add_to_registry(replacement_cluster=OnlineState)\n"),
}


@pytest.mark.parametrize("text", REFUSED.values(), ids=REFUSED)
def test_a_tuya_chain_without_the_typed_cluster_is_refused(text):
    found = problems(text)
    assert any(REASON in problem or "add_to_registry()" in problem for problem in found), found


def test_a_zcl_chain_takes_no_argument_to_add_to_registry():
    from .test_guard_files import ZCL

    found = problems(edit(frozen(ZCL), CHAIN_END, "    .add_to_registry(replacement_cluster=BoschWaterAlarmCluster)\n"), ZCL)
    assert any("a builder chain that does not end in add_to_registry()" in problem for problem in found), found


def test_fixed_code_without_the_typed_cluster_refuses_every_tuya_chain():
    """A release whose fixed code lacks the method refuses Tuya files instead of passing them unchecked."""
    fixed = {**FIXED_CODE, "class": [text for text in FIXED_CODE["class"] if text != TYPED_BODY]}
    older = guard.Guard((FOLDER / "guard.py").read_bytes(), json.dumps(fixed).encode())
    assert older.fixed.typed_tuya is None
    assert any(REASON in problem for problem in older.problems(frozen(TUYA).encode(), targets(TUYA)))


def test_the_method_is_the_one_zhaquirks_calls_for_each_datapoint():
    from zhaquirks.tuya import TuyaNewManufCluster
    from zhaquirks.tuya.builder import TuyaQuirkBuilder
    from zhaquirks.tuya.mcu import TuyaMCUCluster

    for method in (TuyaQuirkBuilder.tuya_dp, TuyaQuirkBuilder.tuya_dp_multi, TuyaQuirkBuilder.tuya_dp_attribute):
        assert inspect.signature(method).parameters["dp_handler"].default == guard.TYPED_TUYA
    assert issubclass(TuyaMCUCluster, TuyaNewManufCluster) and hasattr(TuyaMCUCluster, guard.TYPED_TUYA)
    assert "replacement_cluster" in inspect.signature(TuyaQuirkBuilder.add_to_registry).parameters
    assert guard.TUYA_MCU == (TuyaMCUCluster.__module__, TuyaMCUCluster.__name__)


# The pinned zha-quirks with the meter applied to a zigpy device, as ZHA applies it: one frame per line of FRAMES.
RUNTIME = """
import logging, pathlib, re, sys, warnings
from unittest.mock import MagicMock
warnings.simplefilter("ignore")
import zhaquirks
import zigpy.device, zigpy.types as t
from zha.quirks import DEVICE_REGISTRY
from zhaquirks.tuya import TuyaCommand, TuyaData, TuyaDatapointData, TuyaDPType

warnings_seen = []
class Seen(logging.Handler):
    def emit(self, record):
        if found := re.search(r"Ignoring invalid datapoint (\\d+)", record.getMessage()):
            warnings_seen.append(int(found[1]))
logging.getLogger().addHandler(Seen())
logging.getLogger().setLevel(logging.DEBUG)
zhaquirks.setup(sys.argv[1])
device = zigpy.device.Device(MagicMock(), t.EUI64.convert("00:11:22:33:44:55:66:77"), 0x1234)
endpoint = device.add_endpoint(1)
endpoint.profile_id, endpoint.device_type = 260, 0x0051
for cluster_id in (0x0000, 0x0004, 0x0005, 0xEF00):
    endpoint.add_input_cluster(cluster_id)
device.manufacturer, device.model = "_TZE284_2fnssffc", "TS0601"
device = DEVICE_REGISTRY.resolve(device)
tuya = device.endpoints[1].in_clusters[0xEF00]

def datapoint(dp, kind, raw):
    data = TuyaData()
    data.dp_type, data.function, data.raw = getattr(TuyaDPType, kind), 0, raw
    return TuyaDatapointData(dp=dp, data=data)

def value(n):
    return n.to_bytes(4, "big", signed=True)

FRAMES = [
    [datapoint(1, "VALUE", value(12345)), datapoint(9, "ENUM", b"\\x01"), datapoint(8, "VALUE", value(215)), datapoint(44, "ENUM", b"\\x01")],
    [datapoint(1, "STRING", b"x"), datapoint(9, "STRING", b"x"), datapoint(8, "STRING", b"x"), datapoint(44, "STRING", b"x"), datapoint(49, "VALUE", value(5000))],
    [datapoint(1, "RAW", b"\\x01\\x02"), datapoint(9, "VALUE", value(300)), datapoint(1, "VALUE", value(-5))],
]
for frame in FRAMES:
    status = tuya.handle_get_data(TuyaCommand(status=0, tsn=1, datapoints=frame))
    print("status", int(status))
names = {"energy": tuya, "mode_x": tuya, "online_state": tuya, "ac_frequency": tuya, "measured_value": device.endpoints[1].temperature}
print("cache", sorted((name, int(cluster.get(name))) for name, cluster in names.items()))
print("skipped", warnings_seen)
"""


def test_a_datapoint_of_another_type_is_skipped_and_the_attribute_keeps_its_number(tmp_path: Path):
    """No stand-in: zha-quirks as pinned, the frozen meter with a tuya_enum and a tuya_temperature. A STRING or RAW
    datapoint where a number or an enum is meant, a number out of an enum's range: each is a warning, the attribute
    keeps its number, the frame's other datapoints still count, every frame is acknowledged (SUCCESS). A negative VALUE
    for the energy (a uint32 of the Tuya cluster) is stored as it comes."""
    assert problems(METER_PLUS) == []
    (tmp_path / "custom").mkdir()
    (tmp_path / "custom" / "z2m_moes_zm6lt1.py").write_text(METER_PLUS)
    out = subprocess.run([sys.executable, "-I", "-c", RUNTIME, str(tmp_path / "custom")], capture_output=True, text=True, timeout=300, check=False)
    lines = out.stdout.strip().splitlines()[-5:]
    assert lines[:3] == ["status 0", "status 0", "status 0"], out.stdout[-800:] + out.stderr[-2000:]
    assert lines[3] == "cache " + str(sorted([("ac_frequency", 5000), ("energy", -5), ("measured_value", 2150), ("mode_x", 1), ("online_state", 1)])), out.stdout[-800:] + out.stderr[-2000:]
    # Skipped: the STRING ones (energy, mode_x, the temperature, online_state), the RAW energy, 300 for an enum8.
    assert lines[4] == "skipped [1, 9, 8, 44, 1, 9]", out.stdout[-800:] + out.stderr[-2000:]


# The pinned zha-quirks with the meter applied (as RUNTIME), the self-check's listener (watch.py, loaded by path) on its
# device: one frame with a STRING datapoint for the energy (a uint32) and a VALUE one for the temperature.
WATCH = RUNTIME.split("FRAMES = [")[0] + """
import importlib.util
spec = importlib.util.spec_from_file_location("watch_under_test", sys.argv[2])
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)
logging.getLogger().setLevel(logging.WARNING)
listening = watch.Watch(device, tuya)
listening.attach()
listening.listen_refused(True)
tuya.handle_get_data(TuyaCommand(status=0, tsn=1, datapoints=[datapoint(1, "STRING", b"x"), datapoint(8, "VALUE", value(215))]))
print("refused", {dp: reason for dp, (_at, reason) in listening.refused.items()})
print("keys", watch.mapped_keys(tuya, 1))
listening.detach()
print("listening after detach", watch.REFUSALS in logging.getLogger(watch.REFUSED_LOGGER).handlers)
"""


def test_the_self_check_sees_what_the_typed_cluster_refuses(tmp_path: Path):
    """No stand-in: zhaquirks' warning for the refused datapoint, as zigpy's cluster log writes it, reaches
    the listener of the device's Tuya cluster with the datapoint, why and the attribute it goes to."""
    (tmp_path / "custom").mkdir()
    (tmp_path / "custom" / "z2m_moes_zm6lt1.py").write_text(METER_PLUS)
    out = subprocess.run([sys.executable, "-I", "-c", WATCH, str(tmp_path / "custom"), str(FOLDER / "watch.py")], capture_output=True, text=True, timeout=300, check=False)
    lines = out.stdout.strip().splitlines()[-3:]
    assert lines == ["refused {1: 'TypeError(\"CharacterString \\'x\\' for energy (uint32_t)\")'}", "keys ['1:61184:energy']",
                     "listening after detach False"], out.stdout[-800:] + out.stderr[-2000:]


# As WATCH, the frame the way zigpy hands one on (the device's last_seen first, then its Tuya cluster), once with zigpy's
# cluster log heard and once set above warnings.
FRAME = RUNTIME.split("FRAMES = [")[0] + """
import asyncio, collections, importlib.util
from unittest.mock import AsyncMock
spec = importlib.util.spec_from_file_location("watch_under_test", sys.argv[2])
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)
device.request = AsyncMock()
tuya.endpoint.reply = AsyncMock()
device._application._req_listeners = collections.defaultdict(list)
# Datapoint 1 as STRING (the energy is a uint32: refused), datapoint 8 as VALUE 215 (the temperature: taken). Each frame
# with a sequence number of its own: zigpy drops a frame it got already.
def data(tsn):
    return bytes([0x09, tsn, 0x02, 0x00, tsn, 0x01, 0x03, 0x00, 0x01, 0x78, 0x08, 0x02, 0x00, 0x04, 0, 0, 0, 0xD7])

async def main():
    for tsn, name, level in ((5, "heard", logging.WARNING), (6, "quiet", logging.ERROR)):
        logging.getLogger().setLevel(logging.WARNING)
        logging.getLogger("zigpy.zcl").setLevel(level)
        listening = watch.Watch(device, tuya)
        listening.attach()
        listening.listen_refused(True)
        nwk = t.AddrModeAddress(addr_mode=t.AddrMode.NWK, address=0x1234)
        device.packet_received(t.ZigbeePacket(src=nwk, src_ep=1, dst=t.AddrModeAddress(addr_mode=t.AddrMode.NWK, address=0), dst_ep=1, profile_id=260,
                                              cluster_id=0xEF00, data=t.SerializableBytes(data(tsn)), lqi=255, rssi=-40))
        await asyncio.sleep(0)
        print(name, watch.log_heard(), {dp: reason for dp, (_at, reason) in listening.refused.items()}, sorted(listening.fresh))
        listening.detach()

asyncio.run(main())
"""


def test_the_frame_shows_a_refusal_the_log_does_not_tell(tmp_path: Path):
    """No stand-in: zigpy hands the frame to the meter's Tuya cluster, whose typed class refuses the STRING energy. With the
    cluster log heard the warning gives the reason. Set above warnings, the frame still shows that datapoint 1 never
    reached its attribute, without a reason. The temperature of the same frame came either way."""
    (tmp_path / "custom").mkdir()
    (tmp_path / "custom" / "z2m_moes_zm6lt1.py").write_text(METER_PLUS)
    out = subprocess.run([sys.executable, "-I", "-c", FRAME, str(tmp_path / "custom"), str(FOLDER / "watch.py")], capture_output=True, text=True, timeout=300, check=False)
    lines = out.stdout.strip().splitlines()[-2:]
    fresh = "['1:1026:*', '1:1026:measured_value']"
    assert lines == ["heard True {1: 'TypeError(\"CharacterString \\'x\\' for energy (uint32_t)\")'} " + fresh, "quiet False {1: ''} " + fresh], out.stdout[-800:] + out.stderr[-2000:]


def test_the_check_takes_what_the_attribute_holds():
    """The method's rule on its own: an int the type holds for an enum, a bitmap or a Bool, no text, a float type what a
    single precision float holds, other types as they come. A plain integer type of another cluster holds its range,
    one of the Tuya cluster itself what its bits hold read signed or unsigned (-5.0 C on a sensor typed unsigned). Run on
    stand-ins for zhaquirks' datapoint and mapping, the method's text as fixed_code.json has it."""
    import types

    import zigpy.types as t

    stored = []

    class Base:
        def _dp_2_attr_update(self, datapoint):
            stored.append(datapoint.dp)

    namespace = {"TuyaMCUCluster": Base}
    exec(compile(TYPED_CLASS, "<fixed code>", "exec"), namespace)  # noqa: S102 - the fixed code itself

    class Enum8(t.enum8):
        a = 0

    kinds = {"u16": t.uint16_t, "u32": t.uint32_t, "s8": t.int8s, "single": t.Single, "enum": Enum8, "flags": t.bitmap8, "bool": t.Bool,
             "text": t.CharacterString, "utc": t.UTCTime}
    cluster = namespace["TypedTuyaCluster"]()
    cluster.attributes_by_name = {name: types.SimpleNamespace(type=kind) for name, kind in kinds.items()}
    other = types.SimpleNamespace(attributes_by_name=cluster.attributes_by_name)
    cluster.endpoint = types.SimpleNamespace(tuya=cluster, other=other, device=None)

    def check(name, value, converter=None, on="other"):
        cluster._dp_to_attributes = {1: [types.SimpleNamespace(endpoint_id=None, ep_attribute=on, attribute_name=name, converter=converter)]}
        stored.clear()
        try:
            cluster._dp_2_attr_update(types.SimpleNamespace(dp=1, data=types.SimpleNamespace(payload=value)))
        except ValueError:
            return False
        assert stored == [1]
        return True

    # Either cluster: text never, enums, bitmaps and Bool by their type, a float type within a single precision float.
    for on in ("other", "tuya"):
        for name, value in (("u16", 5), ("u16", 23.5), ("u16", True), ("u16", 0), ("u16", 65535), ("s8", -5), ("s8", -128), ("s8", 127), ("single", 1.5),
                            ("single", 7), ("single", -1e30), ("single", 3.4e38), ("enum", 1), ("enum", 200), ("flags", 3), ("bool", True), ("text", "x"),
                            ("text", 5), ("u16", None), ("missing", "x"), ("utc", 0), ("utc", 2**32 - 1)):
            assert check(name, value, on=on), (on, name, value)
        for name, value in (("u16", "5"), ("u16", b"\x05"), ("single", "1.5"), ("single", float("inf")), ("single", float("nan")), ("single", 2**1100),
                            ("single", 2.0**128), ("single", -1e39), ("enum", "a"), ("enum", 300), ("enum", -1), ("enum", 1.0), ("flags", 1.5), ("flags", 256),
                            ("bool", "x"), ("bool", 1.0), ("bool", -1), ("u16", float("nan")), ("u16", float("-inf")), ("u16", 2**1100), ("u16", [5]),
                            ("s8", "x"), ("u16", 65536), ("u16", 2**62), ("s8", 256), ("s8", -129), ("utc", 2**32), ("u32", 2**32)):
            assert not check(name, value, on=on), (on, name, value)
    # Another cluster: a plain integer type holds its range, as zigpy stores a ZCL report (ZHA's own entities read it).
    for name, value in (("u16", -1), ("u16", -50), ("u16", 70000), ("s8", 200), ("s8", 128), ("u32", -50), ("utc", -1), ("u16", 5000000)):
        assert not check(name, value), (name, value)
    # The Tuya cluster itself: what the type's bits hold read signed or unsigned (a VALUE datapoint is signed on the wire).
    for name, value in (("u16", -1), ("u16", -32768), ("u32", -50), ("u32", -(2**31)), ("u32", 2**32 - 1), ("s8", 200), ("s8", 255), ("u16", -0.5)):
        assert check(name, value, on="tuya"), (name, value)
    for name, value in (("u16", -32769), ("u16", 70000), ("u32", -(2**31) - 1), ("s8", -129), ("enum", -1)):
        assert not check(name, value, on="tuya"), (name, value)
    # A converter's error is a ValueError too: zhaquirks' own converters stand in no helper.
    assert not check("u16", "x", lambda x: 100 - x)
    assert check("u16", 7, lambda x: x * 10)
    assert not check("u16", "x", lambda x: x * 10)


def test_every_value_a_zha_formatter_gets_from_another_cluster_is_one_it_takes():
    """ZHA's own sensors of a standard cluster compute more than arithmetic. Illuminance raises 10 to the value
    (an overflow above about 3e6), the setpoint change timestamp adds a UTCTime to a date (an overflow past the year 9999).
    The method keeps a plain integer type of another cluster in its range: the extremes it lets through are ones these
    formatters take."""
    import zigpy.types as t
    from zha.application.platforms.sensor import Illuminance, Sensor, SetpointChangeSourceTimestamp
    from zigpy.zcl.clusters.hvac import Thermostat
    from zigpy.zcl.clusters.measurement import IlluminanceMeasurement

    def formatted(entity, attr_def, value):
        stub = type("Stub", (), {"_attr_def": attr_def, "_is_non_value": Sensor._is_non_value})()
        return entity.formatter(stub, value)

    illuminance = IlluminanceMeasurement.AttributeDefs.measured_value
    timestamp = Thermostat.AttributeDefs.setpoint_change_source_timestamp
    assert illuminance.type is t.uint16_t and issubclass(timestamp.type, t.uint32_t)
    for value in (0, 1, 65534, 65535, 1.5, 65535.9):
        formatted(Illuminance, illuminance, value)
    for value in (0, 2**32 - 1, 0.5):
        formatted(SetpointChangeSourceTimestamp, timestamp, value)
    # What the method refuses there would raise.
    with pytest.raises(OverflowError):
        formatted(Illuminance, illuminance, 5000000)
    with pytest.raises(OverflowError):
        formatted(SetpointChangeSourceTimestamp, timestamp, 2**40)


def test_a_value_zhas_illuminance_cannot_take_is_skipped(tmp_path: Path):
    """No stand-in: the meter with an illuminance whose converter scales x, as a file the guard passes may have
    it. A VALUE of 500000 becomes 5000000 for the uint16 measured_value, which ZHA's Illuminance would raise 10 to the power
    of at every start: skipped with a warning. One in range is stored."""
    text = frozen(METER).replace(TUYA_END, "    .tuya_illuminance(dp_id=21, converter=_value_error_on_error(lambda x: x * 10))\n" + TUYA_END, 1)
    assert problems(text) == []
    (tmp_path / "custom").mkdir()
    (tmp_path / "custom" / "z2m_moes_zm6lt1.py").write_text(text)
    script = RUNTIME.split("FRAMES = [")[0] + """
illuminance = device.endpoints[1].illuminance
for reading in (500000, 6553):
    status = tuya.handle_get_data(TuyaCommand(status=0, tsn=1, datapoints=[datapoint(21, "VALUE", value(reading))]))
    print("status", int(status), "stored", illuminance.get("measured_value"))
print("skipped", warnings_seen)
"""
    out = subprocess.run([sys.executable, "-I", "-c", script, str(tmp_path / "custom")], capture_output=True, text=True, timeout=300, check=False)
    lines = out.stdout.strip().splitlines()[-3:]
    assert lines == ["status 0 stored None", "status 0 stored 65530", "skipped [21]"], out.stdout[-800:] + out.stderr[-2000:]
