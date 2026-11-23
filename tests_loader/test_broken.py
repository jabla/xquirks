"""Broken definitions: each is skipped with one log line. The others, the user's files and ZHA load as before. The trial
build (check) names each of them before any device shows up.

A converter error at runtime never escapes either: Tuya converters raise ValueError (zhaquirks skips the datapoint),
attribute converters return None. The loader refuses any other wrapper for them. A filter that raises steps back instead
of ending ZHA's device resolution.
"""

import collections
import copy
import json
import logging
import subprocess
import sys

import pytest
from zha.quirks import ModelInfo
from zhaquirks.tuya import TuyaCommand, TuyaData, TuyaDatapointData

import loader
from helpers import BY_ID, SAMPLE, SAMPLE_TEXT, STUB_FILE, build_device, build_new, label_of, layout_of, raw_of
from test_reload import EXPECTED, labels


def derive(ident: str, change) -> str:
    """A sample definition under another id, changed, with its own rev (the trial build checks the rev first)."""
    definition = copy.deepcopy(BY_ID[ident])
    definition["id"] = f"broken_{change.__name__}"
    change(definition)
    return json.dumps({**definition, "rev": loader.revision(definition)})


def calls(definition, name):
    return [call for call in definition["chain"] if call[0] == name]


def unknown_operation(d):
    calls(d, "tuya_dp_attribute")[0][-1]["converter"]["ops"][0][0] = "sqrt"


def wrong_type_dp(d):
    calls(d, "tuya_number")[0][-1]["dp_id"] = "114"


def wrong_type_attribute_name(d):
    calls(d, "tuya_enum")[0][-1]["attribute_name"] = 5


def duplicate_dp(d):
    first = calls(d, "tuya_enum")[0]
    d["chain"].insert(d["chain"].index(first), copy.deepcopy(first))


def raising_converter(d):
    lookup = next(op for call in calls(d, "tuya_dp") for op in call[-1]["converter"]["ops"] if op[0] == "lookup")
    lookup[1][0][0] = [1, 2]  # a list as a key: the table cannot be built


def bare_attribute_converter(d):
    next(call for call in calls(d, "binary_sensor") if "attribute_converter" in call[-1])[-1]["attribute_converter"]["on_error"] = "raise"


def unknown_method(d):
    d["chain"].insert(-1, ["zha_device_class", [{"ref": "CustomCluster"}]])


def forbidden_name(d):
    calls(d, "switch")[0][-1]["translation_key"] = {"ref": "os.system"}


def module_reference(d):
    d["classes"][0]["attribute_defs"]["members"][0][1]["type"] = {"ref": "t.basic"}


def no_zhaquirks_base(d):
    d["classes"][0]["bases"] = [{"ref": "OnOff"}]


def dunder_member(d):
    next(c for c in d["classes"] if c["kind"] == "enum")["members"][0][0] = "__x__"


def unknown_key(d):
    d["code"] = "import os"


def list_of_cluster(d):
    d["classes"][0]["server_command_defs"] = {
        "base": {"ref": "BaseCommandDefs"}, "members": [["pair", {"id": 16, "schema": {"dict": [["data", {"list_of": {"ref": "OnOff"}}]]}}]],
    }


def list_attribute(d):
    d["classes"][0]["attribute_defs"]["members"][0][1]["type"] = {"list_of": {"ref": "t.uint8_t"}}  # a list type only in a command schema


def entity_filter_suffix(d):
    # ZHA's entity discovery would call 5.removeprefix() outside its per-entity try, at every start.
    d["chain"].insert(-1, ["removes_entity", [{"new": "EntityFilter", "kwargs": {"unique_id_suffix": 5}}]])


# Values ZHA computes entity states from where nothing catches an error, or a transform reads at resolve (it would
# leave the device without any quirk).


def volume_sensor(d) -> dict:
    return next(call for call in calls(d, "sensor") if "daily_irrigation_volume" in call[-1]["attribute_name"]["ref"])[-1]


def cluster_data(d) -> dict:
    return d["classes"][0].setdefault("data", {})


def infinite_multiplier(d):
    volume_sensor(d)["multiplier"] = 0.123456789  # written as 1e999 below: JSON reads that as inf


def divisor_zero(d):
    volume_sensor(d)["divisor"] = 0


def multiplier_zero(d):
    volume_sensor(d)["multiplier"] = 0


def constants_text(d):
    cluster_data(d)["_CONSTANT_ATTRIBUTES"] = "abc"


def constant_text_for_a_number(d):
    volume = next(m[1]["id"] for m in d["classes"][0]["attribute_defs"]["members"] if m[0] == "daily_irrigation_volume")
    cluster_data(d)["_CONSTANT_ATTRIBUTES"] = {"dict": [[volume, "text"]]}


def ep_attribute_list(d):
    cluster_data(d)["ep_attribute"] = [1]


def volts_equal(d):
    cluster_data(d).update(MIN_VOLTS_MV=3000, MAX_VOLTS_MV=3000)


def tuya_default_cluster(d):
    d["chain"][-1] = ["add_to_registry"]  # zhaquirks' TuyaMCUCluster would store the text of a STRING datapoint in a number


def tuya_plain_cluster(d):
    d["chain"][-1] = ["add_to_registry", {"replacement_cluster": {"ref": "TuyaMCUCluster"}}]


# Converters that would cost the event loop more with every value (a chain of 4,300-digit multiplications: seconds).


def thermostat_converter(d) -> dict:
    return calls(d, "tuya_dp")[0][-1]["converter"]  # dp 24: x * 10


def huge_operand(d):
    thermostat_converter(d)["ops"] = [["mul", 2**64 + 1]]


def many_operations(d):
    thermostat_converter(d)["ops"] += [["int"]] * 16


def deep_if(d):
    ops = [["int"]]
    for _ in range(4):
        ops = [["if", [["gt", 0]], ops, [["const", 0]]]]
    thermostat_converter(d)["ops"] = ops


def alias_without_names(d):
    # ZHA's register would raise for the second after it put the entry under the first.
    d["alias"][0]["fingerprints"] = [["Aqara", "lumi.light.zzz999"], [None, None], ["Aqara", "lumi.light.zzz998"]]


def repeated_name(d):
    # ZHA's remove would stop at the second (SONOFF, ZBMicro): the purge by label would leave the entry under (XQ, LATER).
    d["chain"][1:1] = [["applies_to", ["SONOFF", "ZBMicro"]], ["applies_to", ["SONOFF", "ZBMicro"]], ["applies_to", ["XQ", "LATER"]]]


def repeated_fingerprint(d):
    d["alias"][0]["fingerprints"].append(d["alias"][0]["fingerprints"][0])


def module_alone(d):
    # The attribute converter would hand ZHA's sensor state the module.
    next(call for call in calls(d, "sensor") if "attribute_converter" in call[-1])[-1]["attribute_converter"]["ops"] = [["const", {"ref": "t"}]]


def odd_endpoint(d):
    d["endpoints"] = {"²": d["endpoints"]["1"]}  # a digit to str.isdigit, not to int()


BROKEN = {
    # Lines JSON cannot read are named by their number (after the 15 valid ones).
    "line 16": '{"id": "broken_json", "rev": "00000000", "chain": [',
    "line 17": '{"id": "broken_nan", "rev": "00000000", "risk": NaN, "chain": []}',
    "line 18": derive("sonoff_swv", infinite_multiplier).replace("0.123456789", "1e999"),
    "line 19": derive("heat_decor_hd_t1000", huge_operand),
    "broken_unknown_operation": derive("coolo_cs_201z", unknown_operation),
    "broken_wrong_type_dp": derive("chayo_toqcb2_80", wrong_type_dp),
    "broken_wrong_type_attribute_name": derive("box_erc2206_z", wrong_type_attribute_name),
    "broken_duplicate_dp": derive("box_erc2206_z", duplicate_dp),
    "broken_raising_converter": derive("heat_decor_hd_t1000", raising_converter),
    "broken_bare_attribute_converter": derive("sonoff_swv", bare_attribute_converter),
    "broken_unknown_method": derive("sonoff_zbmicro", unknown_method),
    "broken_forbidden_name": derive("sonoff_zbmicro", forbidden_name),
    "broken_module_reference": derive("sonoff_zbmicro", module_reference),
    "broken_no_zhaquirks_base": derive("sonoff_zbmicro", no_zhaquirks_base),
    "broken_dunder_member": derive("chayo_toqcb2_80", dunder_member),
    "broken_unknown_key": derive("coolo_cs_201z", unknown_key),
    "broken_list_of_cluster": derive("sonoff_zbmicro", list_of_cluster),
    "broken_list_attribute": derive("sonoff_zbmicro", list_attribute),
    "broken_entity_filter_suffix": derive("sonoff_swv", entity_filter_suffix),
    "broken_divisor_zero": derive("sonoff_swv", divisor_zero),
    "broken_multiplier_zero": derive("sonoff_swv", multiplier_zero),
    "broken_constants_text": derive("sonoff_swv", constants_text),
    "broken_constant_text_for_a_number": derive("sonoff_swv", constant_text_for_a_number),
    "broken_ep_attribute_list": derive("sonoff_swv", ep_attribute_list),
    "broken_volts_equal": derive("sonoff_swv", volts_equal),
    "broken_tuya_default_cluster": derive("heat_decor_hd_t1000", tuya_default_cluster),
    "broken_tuya_plain_cluster": derive("heat_decor_hd_t1000", tuya_plain_cluster),
    "broken_alias_without_names": derive("alias_aqara_cl_l02d", alias_without_names),
    "broken_many_operations": derive("heat_decor_hd_t1000", many_operations),
    "broken_deep_if": derive("heat_decor_hd_t1000", deep_if),
    "broken_repeated_name": derive("sonoff_zbmicro", repeated_name),
    "broken_repeated_fingerprint": derive("alias_aqara_cl_l02d", repeated_fingerprint),
    "broken_module_alone": derive("sonoff_swv", module_alone),
    "broken_odd_endpoint": derive("sonoff_swv", odd_endpoint),
    "sonoff_zbmicro": json.dumps(BY_ID["sonoff_zbmicro"]),  # its id a second time
}


# Skipped when the line is read: JSON (its numbers too), the top level and the names, an alias (built at load), an id
# used twice. The others are skipped when a device with their names first shows up: they build only then.
AT_LOAD = (
    "line 16", "line 17", "line 18", "line 19", "broken_unknown_key", "broken_alias_without_names", "broken_repeated_name", "broken_repeated_fingerprint",
    "broken_odd_endpoint", "sonoff_zbmicro",
)
# A part of each one's reason.
REASONS = {
    "line 16": "JSONDecodeError", "line 17": "NaN is no number", "broken_unknown_operation": "unknown operation 'sqrt'", "broken_wrong_type_dp": "parameter 'dp_id'",
    "broken_wrong_type_attribute_name": "parameter 'attribute_name'", "broken_duplicate_dp": "DP 1 is already mapped", "broken_raising_converter": "unhashable",
    "broken_bare_attribute_converter": "parameter 'attribute_converter'", "broken_unknown_method": "not allowed", "broken_forbidden_name": "unknown name 'os'",
    "broken_module_reference": "reaches a module", "broken_no_zhaquirks_base": "no zhaquirks cluster base", "broken_dunder_member": "member '__x__'",
    "broken_unknown_key": "unknown keys ['code']", "broken_list_of_cluster": "no zigpy type", "broken_list_attribute": "network_led: type", "sonoff_zbmicro": "id used twice",
    "broken_entity_filter_suffix": "parameter 'unique_id_suffix'", "line 18": "1e999 is no finite number", "broken_divisor_zero": "parameter 'divisor'",
    "broken_multiplier_zero": "parameter 'multiplier'", "broken_constants_text": "_CONSTANT_ATTRIBUTES 'abc'", "broken_constant_text_for_a_number": "constant 'text'",
    "broken_ep_attribute_list": "ep_attribute [1]", "broken_volts_equal": "MIN_VOLTS_MV not below MAX_VOLTS_MV",
    "broken_tuya_default_cluster": "names TypedTuyaCluster", "broken_tuya_plain_cluster": "parameter 'replacement_cluster'",
    "broken_alias_without_names": "each with a manufacturer or a model", "line 19": "18446744073709551617 is no finite number of at most 2**64",
    "broken_many_operations": "more than 16 operations", "broken_deep_if": "if deeper than 3", "broken_repeated_name": "('SONOFF', 'ZBMicro') named twice",
    "broken_repeated_fingerprint": "a fingerprint named twice", "broken_module_alone": "reference 't' reaches a module",
    "broken_odd_endpoint": "DefinitionError: endpoints",
}


def skipped_lines(caplog) -> list:
    return [r.getMessage() for r in caplog.records if r.name == "xquirks" and " skipped: " in r.getMessage()]


def test_broken_definitions_are_skipped_with_one_line_each(installation, registry, caplog):
    caplog.set_level(logging.INFO, logger="xquirks")
    installation.definitions(SAMPLE_TEXT + "\n".join(BROKEN.values()) + "\n")
    user = installation.user_file("z_user_v2.py", "from zhaquirks.builder import QuirkBuilder\n\nQuirkBuilder('Vendor', 'Other').add_to_registry()\n")
    installation.options(high_risk=True)
    installation.setup()
    assert sorted(m.removeprefix("xquirks: definition ").split(" skipped: ")[0] for m in skipped_lines(caplog)) == sorted(AT_LOAD)
    assert f"xquirks: {len(SAMPLE) + len(BROKEN) - len(AT_LOAD)} definitions loaded, {len(AT_LOAD)} skipped" in caplog.text
    assert any(e.source and e.source.file == str(user) for e in registry)
    # Each broken definition keeps the names of the sample definition it derives from and comes later: ZHA tries it
    # first, it fails to build and steps back. The sample definition applies. Twice, still one line each.
    for ident in ("coolo_cs_201z", "chayo_toqcb2_80", "box_erc2206_z", "heat_decor_hd_t1000", "sonoff_swv", "sonoff_zbmicro"):
        for _ in range(2):
            assert registry.match_entry(raw_of(ident)).source.label == label_of(BY_ID[ident])
    skipped = skipped_lines(caplog)
    assert sorted(m.removeprefix("xquirks: definition ").split(" skipped: ")[0] for m in skipped) == sorted(BROKEN)
    assert {e.source.label for e in registry if str(getattr(e.source, "label", "")).startswith("xquirks:")} >= set(EXPECTED)
    reasons = "\n".join(skipped)
    for expected in REASONS.values():
        assert expected in reasons, expected


def test_the_trial_build_names_each_broken_definition(registry, tmp_path):
    """check() builds every line now, as ZHA would at the first device: each broken definition by its id with its
    reason, none of the sample's. It registers nothing."""
    path = tmp_path / "definitions.jsonl"
    path.write_text(SAMPLE_TEXT + "\n".join(BROKEN.values()) + "\n")
    before = list(registry)
    failures = loader.check(str(path))
    assert list(registry) == before
    assert sorted(failures) == sorted(BROKEN)
    assert all(REASONS[ident] in reason for ident, reason in failures.items()), failures


def test_the_trial_build_on_the_command_line(tmp_path):
    """python loader.py --check <file>: a line per failure and exit status 1 (0 without one: tests_hacs/test_build.py)."""
    path = tmp_path / "definitions.jsonl"
    path.write_text(SAMPLE_TEXT + BROKEN["broken_unknown_operation"] + "\n")
    done = subprocess.run([sys.executable, loader.__file__, "--check", str(path)], capture_output=True, text=True, timeout=600)
    assert (done.returncode, done.stdout) == (1, "broken_unknown_operation: DefinitionError: unknown operation 'sqrt'\n")


def test_a_definition_zha_does_not_register_is_taken_out_again(installation, registry, monkeypatch, caplog):
    """ZHA's register raises for a name with neither manufacturer nor model after it put the entry under the names
    before (the loader refuses such a name, slipped in here). The loader takes the definition out again and skips it
    with one line, the lines after it load. The trial build registers too and reports it."""
    names = loader._names
    monkeypatch.setattr(loader, "_names", lambda chain: (*names(chain), ModelInfo(None, None)) if chain == BY_ID["sonoff_zbmicro"]["chain"] else names(chain))
    path = installation.integration / loader.DEFINITIONS_FILE
    assert loader.register(str(installation.stub), "xquirks", str(path)) == (len(SAMPLE) - 1, 1)
    assert caplog.text.count("xquirks: definition sonoff_zbmicro skipped: ValueError") == 1
    assert labels(registry) == EXPECTED - collections.Counter([label_of(BY_ID["sonoff_zbmicro"])])
    assert list(loader.check(str(path))) == ["sonoff_zbmicro"]


def test_a_zha_quirk_that_cannot_be_digested(installation, registry, monkeypatch, caplog):
    """register() digests each of ZHA's entries a take-over may stay over in its own try: the take-over steps back from
    an entry whose digest fails (one line), the other take-overs stay."""
    digest = loader.zha_digest

    def failing(entry):
        if entry.source.label == "(SONOFF / SWV)":
            raise RecursionError("maximum recursion depth exceeded")
        return digest(entry)

    monkeypatch.setattr(loader, "_DIGESTS", {})
    monkeypatch.setattr(loader, "zha_digest", failing)
    path = installation.integration / loader.DEFINITIONS_FILE
    assert loader.register(str(installation.stub), "xquirks", str(path)) == (len(SAMPLE), 0)
    assert caplog.text.count("could not be digested") == 1
    assert registry.match_entry(raw_of("sonoff_swv", "SONOFF", "SWV")).source.label == "(SONOFF / SWV)"
    zbminir2 = raw_of("sonoff_zbminir2", "SONOFF", "ZBMINIR2")
    assert registry.match_entry(zbminir2).source.label == label_of(BY_ID["sonoff_zbminir2"])


def test_an_alias_says_what_it_did_not_register_in_one_line(caplog):
    """An alias whose groups register nothing fails with the reasons of all of them (register logs it as its skip line).
    One whose groups register in part logs one line for the others."""
    alias = copy.deepcopy(BY_ID["alias_aqara_cl_l02d"])
    nothing = {"targets": [["XQ", "none"]], "fingerprints": [["XQ", "fingerprint"]]}
    alias["alias"] = [nothing, nothing]
    with pytest.raises(loader.DefinitionError, match=r"no alias registered: .*\(ZHA ships no quirk for them\); "):
        loader.build(alias, STUB_FILE, "xquirks", loader.Options())
    assert not [r for r in caplog.records if r.name == "xquirks"]
    alias["alias"] = [*BY_ID["alias_aqara_cl_l02d"]["alias"], nothing, nothing]
    assert loader.build(alias, STUB_FILE, "xquirks", loader.Options())
    assert len([r for r in caplog.records if r.name == "xquirks"]) == 1


ACN031, ACN032 = BY_ID["alias_aqara_cl_l02d"]["alias"][0]["fingerprints"], BY_ID["alias_aqara_cl_l02d"]["alias"][0]["targets"]
GROUPS = {
    "all register": ([{"targets": ACN032, "fingerprints": ACN031}, {"targets": ACN032, "fingerprints": [["XQ", "unlisted"]]}], None),
    "no quirk of ZHA": (
        [{"targets": ACN032, "fingerprints": ACN031}, {"targets": [["XQ", "none"]], "fingerprints": [["XQ", "fingerprint"]]}],
        "alias for [ModelInfo(manufacturer='XQ', model='none')] (ZHA ships no quirk for them) not registered",
    ),
    "names ZHA lists": (
        [{"targets": ACN032, "fingerprints": ACN032}, {"targets": ACN032, "fingerprints": ACN031}],
        "alias for [ModelInfo(manufacturer='Aqara', model='lumi.light.acn032')] (ZHA lists its names) not registered",
    ),
    "none registers": (
        [{"targets": [["XQ", "none"]], "fingerprints": [["XQ", "fingerprint"]]}],
        "DefinitionError: no alias registered: [ModelInfo(manufacturer='XQ', model='none')] (ZHA ships no quirk for them)",
    ),
}


@pytest.mark.parametrize("case", GROUPS)
def test_the_trial_build_names_an_alias_group_that_registers_nothing(registry, tmp_path, case):
    """At load such a group costs one line while the alias's other groups apply (ZHA dropped the target or lists the
    fingerprint now): the trial build fails the alias for it."""
    groups, reason = GROUPS[case]
    definition = {**BY_ID["alias_aqara_cl_l02d"], "alias": groups}
    path = tmp_path / "definitions.jsonl"
    path.write_text(json.dumps({**definition, "rev": loader.revision(definition)}) + "\n")
    assert loader.check(str(path)) == ({} if reason is None else {"alias_aqara_cl_l02d": reason})


def test_no_definitions_file(installation, registry, caplog):
    (installation.integration / "definitions.jsonl").unlink()
    user = installation.user_file("z_user_v2.py", "from zhaquirks.builder import QuirkBuilder\n\nQuirkBuilder('Vendor', 'Other').add_to_registry()\n")
    installation.setup()
    assert "xquirks: no definitions" in caplog.text
    assert any(e.source and e.source.file == str(user) for e in registry)


def test_loader_that_fails_to_import(installation, registry, caplog):
    """A loader that cannot even be imported costs one log line. The folder's other quirks load."""
    (installation.integration / "loader.py").write_text("raise ImportError('broken release')\n")
    user = installation.user_file("z_user_v2.py", "from zhaquirks.builder import QuirkBuilder\n\nQuirkBuilder('Vendor', 'Other').add_to_registry()\n")
    installation.setup()
    assert "xquirks: definitions not loaded" in caplog.text
    assert any(e.source and e.source.file == str(user) for e in registry)
    assert not labels(registry)


def test_converter_errors_never_escape_at_runtime():
    """A Tuya value its converter cannot take is skipped. The frame's other datapoints count."""
    resolved, _, entities = build_device(build_new(BY_ID["heat_decor_hd_t1000"])[0], layout_of(BY_ID["heat_decor_hd_t1000"]))
    thermostat = resolved.endpoints[1].thermostat
    frame = [TuyaDatapointData(3, TuyaData(5)), TuyaDatapointData(24, TuyaData(215))]  # running state 5 is no key of the lookup
    resolved.endpoints[1].tuya_manufacturer.handle_get_data(TuyaCommand(status=0, tsn=1, datapoints=frame))
    assert thermostat.get("local_temperature") == 2150
    assert thermostat.get("running_state") is None


def test_attribute_converter_error_shows_no_value():
    """_zigbee_local_time of a value beyond any date: the sensor shows None, nothing raises."""
    resolved, _, entities = build_device(build_new(BY_ID["sonoff_swv"])[0], layout_of(BY_ID["sonoff_swv"]))
    cluster = resolved.endpoints[1].in_clusters[0xFC11]
    cluster.update_attribute(cluster.find_attribute("irrigation_start_time"), 0xFFFFFFFF)
    sensor = next(e for e in entities if e.unique_id.endswith("-irrigation_start_time"))
    assert sensor.native_value is None


def test_a_filter_that_raises_never_escapes_resolution(installation, registry, caplog, monkeypatch):
    installation.options(high_risk=True)
    installation.setup()
    loaded = sys.modules["xquirks_loader"]

    def broken(device, endpoints):
        raise RuntimeError("check broke")

    monkeypatch.setattr(loaded, "misfit", broken)
    for _ in range(2):
        raw = raw_of("sonoff_zbmicro")
        assert registry.resolve(raw) is raw  # resolution ends normally, without the definition
    assert caplog.text.count("xquirks: sonoff_zbmicro could not check a device and steps back") == 1  # logged once
