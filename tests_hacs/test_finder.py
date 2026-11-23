"""The finder page (site/): it shows the index that tools/build_index.py writes with the loader's own reading, matches
devices as ZHA does, keeps to its security policy and ships every file it uses. Pages publishes site/ of the latest
release.

The tool runs in a process of its own (it sets up zhaquirks' quirks). The page's modules (site/definitions.js,
site/matcher.js) run in node on the tool's index of the matching fixture: the devices they read and match are compared
with a reference of ZHA's matching. Loading goes through a stand-in fetch that records every request. app.js only
renders what the two modules hand it (checked by reading it).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser

import pytest
import yaml

from .integration import FOLDER, ROOT, const, diagnostics

SITE = ROOT / "site"
TOOL = ROOT / "tools" / "build_index.py"
NODE = shutil.which("node")
PAGE = (SITE / "index.html").read_text()
REPO = re.search(r'data-repo="([^"]*)"', PAGE)[1]
DEFINITIONS = [json.loads(line) for line in (FOLDER / "definitions.jsonl").read_text().splitlines() if line.strip()]
# A target of an alias the integration ships: the pinned zha-quirks ships a quirk for it (CI builds every definition).
# That quirk lists the name, so an alias leaves it to ZHA.
TARGET = next(definition["alias"][0]["targets"][0] for definition in DEFINITIONS if "alias" in definition)
RISKS = ("low", "medium", "high")


def code(path) -> str:
    """A module of the page without its comment lines."""
    return "\n".join(line for line in path.read_text().splitlines() if not line.lstrip().startswith("//"))


def node() -> str:
    """node runs the page's modules: the tests skip without it, but never in CI (CI is set there)."""
    if NODE is None:
        if os.environ.get("CI"):
            pytest.fail("the finder tests need node")
        pytest.skip("needs node")
    return NODE


def chain(builder, *calls, tuya=False) -> list:
    """A chain with the builder call, the calls and add_to_registry (a Tuya chain names its cluster there)."""
    end = ["add_to_registry", {"replacement_cluster": {"ref": "TypedTuyaCluster"}}] if tuya else ["add_to_registry"]
    return [builder, *calls, end]


def line(ident: str, rev: str = "0000000a", **fields) -> str:
    return json.dumps({"id": ident, "rev": rev, **fields})


def build_index(text: str, folder) -> subprocess.CompletedProcess:
    """tools/build_index.py on a definitions.jsonl text, writing folder/devices.json. In a process of its own: it sets up
    zhaquirks' quirks."""
    (folder / "definitions.jsonl").write_text(text)
    args = [sys.executable, "-I", str(TOOL), "--definitions", str(folder / "definitions.jsonl"), "--out", str(folder / "devices.json")]
    return subprocess.run(args, capture_output=True, text=True, timeout=300, check=False)


def test_the_index_is_what_the_loader_reads_from_the_definitions(tmp_path):
    """site/devices.json is what the tool writes from the integration's definitions.jsonl. When it is not, the check
    says what to run."""
    check = [sys.executable, "-I", str(TOOL), "--check"]
    run = subprocess.run(check, capture_output=True, text=True, timeout=300, check=False)
    assert run.returncode == 0, run.stderr[-3000:]
    # The index before a definition came.
    stale = tmp_path / "devices.json"
    lines = (SITE / "devices.json").read_text().splitlines(keepends=True)
    stale.write_text("".join(lines[:1] + lines[2:]))
    run = subprocess.run([*check, "--out", str(stale)], capture_output=True, text=True, timeout=300, check=False)
    assert (run.returncode, run.stderr.splitlines()[-1]) == (1, f"{stale} is not up to date: run python tools/build_index.py in the test environment and commit it")


# Lines the loader registers in reverse order of their ids, each with what the index holds of it. 1 and 0 pass the
# loader's check of verified and take_over (Python's "in (None, True, False)"): only true counts. An alias leaves the
# names ZHA's own quirks list to them. A group without a target registers nothing. The last friendly_name call the build
# takes names the device.
MAPPED = [
    (
        line("c_take_over", take_over=True, zha_digests=["0123456789abcdef"], claimed_elsewhere=[["Other Co", "MO-2"], [None, "MO-2"]], chain=chain(
            ["QuirkBuilder", [None, "MO-2"]], ["applies_to", ["Acme", "MO-2"]], ["friendly_name", {"manufacturer": "Acme", "model": "Mo"}],
            ["friendly_name", {"manufacturer": "Acme", "model": "Mo Two"}], ["friendly_name", ["Acme", "Positional"]],
        )),
        {"risk": None, "verified": False, "takeOver": True, "claimed": [["Other Co", "MO-2"], [None, "MO-2"]], "names": [[None, "MO-2"], ["Acme", "MO-2"]], "alias": None, "friendly": ["Acme", "Mo Two"]},
    ),
    (
        line("c_tuya", risk="high", verified=True, chain=chain(["TuyaQuirkBuilder"], ["applies_to", ["_TZE200_abcdefgh", "TS0601"]], ["applies_to", [None, "TS0601X"]], tuya=True)),
        {"risk": "high", "verified": True, "takeOver": False, "claimed": [], "names": [["_TZE200_abcdefgh", "TS0601"], [None, "TS0601X"]], "alias": None, "friendly": None},
    ),
    (
        line("c_kwargs", risk="medium", chain=chain(["QuirkBuilder", {"model": "KW-1", "manufacturer": "Kw"}], ["applies_to", [], {"model": "KW-2", "manufacturer": None}])),
        {"risk": "medium", "verified": False, "takeOver": False, "claimed": [], "names": [["Kw", "KW-1"], [None, "KW-2"]], "alias": None, "friendly": None},
    ),
    (
        line("c_flags", verified=1, take_over=0, chain=chain(["QuirkBuilder", ["F", "F-1"]])),
        {"risk": None, "verified": False, "takeOver": False, "claimed": [], "names": [["F", "F-1"]], "alias": None, "friendly": None},
    ),
    (
        line("c_alias_two", alias=[{"targets": [], "fingerprints": [["Gone", "G-1"]]}, {"targets": [TARGET], "fingerprints": [["Kept", "K-1"]]}]),
        {"risk": None, "verified": False, "takeOver": False, "claimed": [], "names": [["Kept", "K-1"]], "alias": [{"targets": [TARGET], "fingerprints": [["Kept", "K-1"]]}], "friendly": None},
    ),
    (
        line("c_alias", risk="low", alias=[{"targets": [TARGET], "fingerprints": [TARGET, ["_TZE200_finder01", "TS0601"], [None, "FINDER-2"]]}]),
        {
            "risk": "low", "verified": False, "takeOver": False, "claimed": [], "names": [["_TZE200_finder01", "TS0601"], [None, "FINDER-2"]],
            "alias": [{"targets": [TARGET], "fingerprints": [["_TZE200_finder01", "TS0601"], [None, "FINDER-2"]]}], "friendly": None,
        },
    ),
]


def test_the_index_holds_what_the_page_shows_of_each_definition(tmp_path):
    """The tool's index of crafted lines: one definition per line, sorted by id, each with the fields the page uses."""
    run = build_index("\n".join(text for text, _ in MAPPED), tmp_path)
    assert run.returncode == 0, run.stderr[-3000:]
    written = (tmp_path / "devices.json").read_text()
    expected = sorted(({"id": json.loads(text)["id"], "rev": "0000000a", **fields} for text, fields in MAPPED), key=lambda definition: definition["id"])
    assert json.loads(written) == {"format": 1, "definitions": expected}
    assert [json.loads(text.removesuffix(",")) for text in written.splitlines()[1:-1]] == expected


def test_the_tool_fails_on_a_line_the_loader_skips(tmp_path):
    """A line the loader skips never reaches the page: the tool names each with the reason, exits 1 and writes no index.
    So does a file without definitions."""
    text = "\n".join(
        [
            line("c_kept", chain=chain(["QuirkBuilder", ["Acme", "K-1"]])),
            line("c_unknown_key", vendor="Acme", chain=chain(["QuirkBuilder", ["Acme", "U-1"]])),
            "",
            "not json",
            line("c_kept", "0000000b", chain=chain(["QuirkBuilder", ["Acme", "K-2"]])),
        ]
    )
    run = build_index(text, tmp_path)
    assert run.returncode == 1 and not (tmp_path / "devices.json").exists()
    assert run.stderr.splitlines()[-4:] == [
        "the loader skips:",
        "c_unknown_key: DefinitionError: unknown keys ['vendor']",
        "line 4: JSONDecodeError: Expecting value: line 1 column 1 (char 0)",
        "c_kept: DefinitionError: id used twice",
    ]
    blank = build_index("\n \n", tmp_path)
    assert (blank.returncode, blank.stderr.splitlines()[-1]) == (1, "no definitions")


# Definitions for the device cases: exact names, a model-only name with a name left to another device, an alias, a
# name for any model of a manufacturer and a definition that renames its device.
MATCH = "\n".join(
    [
        line("acme_ex_1", risk="low", chain=chain(["QuirkBuilder", ["Acme", "EX-1"]], ["applies_to", ["Acme Inc", "EX-1"]], ["applies_to", ["Acme", "MO-2"]])),
        line("alias_vendor_al_3", risk="low", alias=[{"targets": [TARGET], "fingerprints": [["_TZE200_alias000", "TS0601"]]}]),
        line("only_co", risk="medium", chain=chain(["QuirkBuilder", ["Only Co"]])),
        line("other_co_mo_2", risk="high", chain=chain(["QuirkBuilder", ["Other Co", "MO-2"]])),
        line("sonoff_swv", risk="low", chain=chain(["QuirkBuilder", ["SONOFF", "SWV"]], ["friendly_name", {"manufacturer": "SONOFF", "model": "Smart water valve"}])),
        line("tuya_mo_2", risk="low", claimed_elsewhere=[["Other Co", "MO-2"]], chain=chain(["TuyaQuirkBuilder", [None, "MO-2"]], tuya=True)),
    ]
)


@pytest.fixture(scope="module")
def match_index(tmp_path_factory) -> dict:
    """The tool's index of the matching fixture: the names the loader registers for each definition."""
    folder = tmp_path_factory.mktemp("match")
    run = build_index(MATCH, folder)
    assert run.returncode == 0, run.stderr[-3000:]
    return json.loads((folder / "devices.json").read_text())


DRIVER = """
import { readFileSync } from "node:fs";
const definitions = await import(process.argv[1]);
const matcher = await import(process.argv[2]);
const input = JSON.parse(readFileSync(0, "utf8"));
const out = { template: matcher.TEMPLATE, files: definitions.FILES, risks: definitions.RISKS, cases: {}, loads: {} };
const known = definitions.readIndex(input.index);
for (const [name, { kind, value }] of Object.entries(input.cases)) {
  const devices = kind === "template" ? matcher.devicesFromTemplate(value) : matcher.devicesIn(value);
  const found = matcher.matches(known, devices).map(({ device, definition }) => [device, definition.id]);
  const hinted = matcher.hints(known, devices).map(({ device, definition }) => [device, definition.id]);
  out.cases[name] = { devices, json: matcher.devicesFromJson(value), matches: found, hints: hinted };
}
out.releases = input.releases.map((value) => {
  try {
    return definitions.readRelease(value);
  } catch (error) {
    return { error: error.message };
  }
});
out.releaseUrl = definitions.releaseUrl(input.repo, { tag: "v1.2.3", date: null });
out.riskShown = input.riskEntries.map((entry) => definitions.riskShown(entry));

// A stand-in fetch: answers from the scenario's table by name (as node's Response) and records each request, its abort
// signal apart.
function standIn(table, requests, signals) {
  return async (url, { signal, ...options }) => {
    requests.push([url, options]);
    signals.push(signal instanceof AbortSignal);
    const answer = table[url] ?? { status: 404 };
    if (answer.hang) return new Promise(() => {});
    if (answer.network) throw new TypeError("Failed to fetch");
    return new Response("json" in answer ? JSON.stringify(answer.json) : answer.text, { status: answer.status ?? 200 });
  };
}
for (const [name, table] of Object.entries(input.loads)) {
  const requests = [];
  const signals = [];
  // Short waits: a request that never answers ends the load within them.
  const result = await definitions.loadDefinitions(standIn(table, requests, signals), { wait: 100 });
  const summary = result.kind === "ready" ? { kind: result.kind, release: result.release, ids: result.definitions.map((d) => d.id) } : result;
  out.loads[name] = { result: summary, requests, signals };
}
out.ready = { loading: definitions.notReady(definitions.LOADING), ready: definitions.notReady({ kind: "ready" }), failed: definitions.notReady(definitions.failure(new Error("x"))) };
process.stdout.write(JSON.stringify(out));
"""

exact_device = ("Acme", "EX-1")
model_only_device = ("Some other vendor", "MO-2")
# tuya_mo_2 leaves this name to other_co_mo_2 (claimed_elsewhere).
claimed_device = ("Other Co", "MO-2")
alias_device = ("_TZE200_alias000", "TS0601")
any_model_device = ("Only Co", "Anything")
# An exact name of acme_ex_1 and the model of tuya_mo_2: two definitions.
two_definitions_device = ("Acme", "MO-2")
no_match = [("_TZE200_nomatch0", "TS0601"), ("IKEA of Sweden", "TRADFRI bulb E27 WW 806lm"), ("", "")]
# Names compare case-sensitively; astral characters sort after U+FF21 by code point.
odd = [("aCME", "EX-1"), ("Ａ", "MO-2"), ("\U0001f600", "MO-2")]
# The name sonoff_swv gives its device, in two spellings: a hint, never a match. Its own name matches.
renamed = [("SONOFF", "Smart water valve"), ("sonoff", "SMART WATER VALVE"), ("SONOFF", "SWV")]
picked = [exact_device, model_only_device, claimed_device, alias_device, any_model_device, two_definitions_device, *no_match, *odd, *renamed]


def integration_diagnostics(devices) -> dict:
    """The shape of ZHA's integration diagnostics: devices in a list, other objects around them."""
    return {
        "home_assistant": {"version": "2026.10.2", "installation_type": "Home Assistant OS"},
        "data": {
            "config_entry": {"title": "Zigbee", "data": {"radio_type": "ezsp"}},
            "devices": [{"manufacturer": m, "model": model, "logical_type": "EndDevice"} for m, model in devices],
            "not_devices": [{"manufacturer": 5, "model": "TS0601"}, {"manufacturer": "only a manufacturer"}, None, "text"],
        },
    }


def device_diagnostics(manufacturer: str, model: str) -> dict:
    """The shape of a single device's diagnostics: the pair at the top and in the signature."""
    return {"data": {"manufacturer": manufacturer, "model": model, "signature": {"manufacturer": manufacturer, "model": model, "endpoints": {}}}}


def made_up(names) -> list:
    """The names as devices: a made-up manufacturer or model where a name stands for any."""
    return sorted({(m if m is not None else "Any vendor", model if model is not None else "Any model") for m, model in names})


def matching_names() -> list:
    """The names the lines of the matching fixture write, and the names they leave to other devices, as devices."""
    names = set()
    for text in MATCH.splitlines():
        definition = json.loads(text)
        for group in definition.get("alias", []):
            names |= {tuple(pair) for pair in group["fingerprints"]}
        for call in definition.get("chain", []):
            if call[0] in ("QuirkBuilder", "TuyaQuirkBuilder", "applies_to") and len(call) > 1:
                names.add(tuple([*call[1], None][:2]))
        names |= {tuple(pair) for pair in definition.get("claimed_elsewhere", [])}
    return made_up(names)


CASES = {
    "picked": {"kind": "json", "value": integration_diagnostics(picked)},
    "device": {"kind": "json", "value": device_diagnostics(*exact_device)},
    "sweep": {"kind": "json", "value": integration_diagnostics(matching_names())},
    "template": {"kind": "template", "value": [f"{m}||{model}" for m, model in picked + matching_names()] + ["||TS0601", "Vendor||", "a||b||c"]},
}

# release.json as the deploy writes it, and what the page reads of it (None: refused).
RELEASES = [
    ({"tag": "v0.4.0", "date": "2026-11-30"}, {"tag": "v0.4.0", "date": "2026-11-30"}),
    ({"tag": "v10.20.30", "date": "30.11.2026", "extra": "<b>x</b>"}, {"tag": "v10.20.30", "date": None}),
    ({"tag": "v0.4.0", "date": ""}, {"tag": "v0.4.0", "date": None}),
    ({"tag": "v0.4.0b2"}, None),
    ({"tag": "content-3"}, None),
    ({"tag": "v0.4.0/../../main"}, None),
    ({"tag": "v01.4.0"}, None),
    ({"tag": 4}, None),
    ({}, None),
    (["v0.4.0"], None),
    (None, None),
]
NOTE = 'It loads only with "Use high-risk definitions" on in the options of xquirks.'
# (definition, what the page shows of its risk: badge, tooltip, device card note), None where it shows none.
RISK_SHOWN = [
    ({"risk": "low", "verified": False}, {"tier": "low", "badge": "low risk", "title": "Not verified on a device yet.", "note": None}),
    ({"risk": "medium", "verified": False}, {"tier": "medium", "badge": "medium risk", "title": "Not verified on a device yet.", "note": None}),
    ({"risk": "high", "verified": False}, {"tier": "high", "badge": "high risk", "title": f"Not verified on a device yet. {NOTE}", "note": f"High risk. {NOTE}"}),
    # Verified: only "verified". No risk: the loader holds nothing back, the page shows none.
    ({"risk": "high", "verified": True}, None),
    ({"risk": None, "verified": False}, None),
]

RELEASE = {"tag": "v0.4.0", "date": "2026-11-30"}
# What the stand-in fetch answers in place of the files next to the page: release.json and the tool's index of the
# matching fixture, unless a case answers otherwise.
LOADS = {
    "ready": {},
    "no_release": {"release.json": {"status": 404}},
    "release_not_json": {"release.json": {"text": "<html>"}},
    "release_of_a_beta": {"release.json": {"json": {"tag": "v0.4.0b2"}}},
    "no_index": {"devices.json": {"status": 404}},
    "index_not_json": {"devices.json": {"text": "<html>"}},
    "server_error": {"devices.json": {"status": 500}},
    "offline": {"release.json": {"network": True}, "devices.json": {"network": True}},
    "release_hangs": {"release.json": {"hang": True}},
    "index_hangs": {"devices.json": {"hang": True}},
    "another_format": {"devices.json": {"json": {"format": 2, "definitions": []}}},
    "no_definitions": {"devices.json": {"json": {"format": 1}}},
}
FAILED = "Could not load the definitions ({}). Reload the page to try again."


@pytest.fixture(scope="module")
def page(match_index):
    args = [node(), "--input-type=module", "-e", DRIVER, (SITE / "definitions.js").as_uri(), (SITE / "matcher.js").as_uri()]
    payload = {
        "index": match_index,
        "cases": CASES,
        "releases": [value for value, _ in RELEASES],
        "repo": REPO,
        "riskEntries": [entry for entry, _ in RISK_SHOWN],
        "loads": {name: {"release.json": {"json": RELEASE}, "devices.json": {"json": match_index}} | table for name, table in LOADS.items()},
    }
    result = subprocess.run(args, input=json.dumps(payload), capture_output=True, text=True, timeout=60, check=True)
    return json.loads(result.stdout)


def devices_in(value) -> set[tuple[str, str]]:
    """Every object with a string "manufacturer" and "model" is a device, at any depth."""
    found = set()
    if isinstance(value, dict):
        manufacturer, model = value.get("manufacturer"), value.get("model")
        if isinstance(manufacturer, str) and isinstance(model, str):
            found.add((manufacturer, model))
        for item in value.values():
            found |= devices_in(item)
    elif isinstance(value, list):
        for item in value:
            found |= devices_in(item)
    return found


def devices_from_template(items: list) -> set[tuple[str, str]]:
    """The template's output: "manufacturer||model" split at the first "||", pairs with an empty side dropped."""
    pairs = (item.split("||", 1) for item in items if isinstance(item, str) and "||" in item)
    return {(manufacturer, model) for manufacturer, model in pairs if manufacturer and model}


def zha_matches(index: dict, devices) -> list:
    """[device, id] for each definition with a name the loader registers that catches the device as ZHA's DeviceMatch
    does (None stands for any), unless the definition leaves that exact name to another device (claimed_elsewhere)."""
    claimed = {json.loads(text)["id"]: {tuple(pair) for pair in json.loads(text).get("claimed_elsewhere", [])} for text in MATCH.splitlines()}
    result = []
    for definition in sorted(index["definitions"], key=lambda definition: definition["id"]):
        for device in sorted(devices):
            if device not in claimed[definition["id"]] and any((m is None or m == device[0]) and (model is None or model == device[1]) for m, model in definition["names"]):
                result.append([list(device), definition["id"]])
    return result


@pytest.mark.parametrize("name", ["picked", "device", "sweep"])
def test_diagnostics_match_as_zha_matches(page, match_index, name):
    devices = devices_in(CASES[name]["value"])
    assert page["cases"][name]["devices"] == [list(d) for d in sorted(devices)]
    assert page["cases"][name]["json"] == page["cases"][name]["devices"]
    assert page["cases"][name]["matches"] == zha_matches(match_index, devices)


def test_template_output_matches_as_zha_matches(page, match_index):
    devices = devices_from_template(CASES["template"]["value"])
    assert page["cases"]["template"]["devices"] == [list(d) for d in sorted(devices)]
    assert page["cases"]["template"]["json"] == page["cases"]["template"]["devices"]
    assert page["cases"]["template"]["matches"] == zha_matches(match_index, devices)


def test_the_sweep_holds_every_name_the_loader_registers(match_index):
    assert set(made_up(name for definition in match_index["definitions"] for name in definition["names"])) <= set(matching_names())


def test_picked_devices(page):
    found = [(tuple(device), ident) for device, ident in page["cases"]["picked"]["matches"]]
    assert (exact_device, "acme_ex_1") in found
    assert (model_only_device, "tuya_mo_2") in found
    assert (claimed_device, "tuya_mo_2") not in found
    assert (claimed_device, "other_co_mo_2") in found
    assert (alias_device, "alias_vendor_al_3") in found
    assert (any_model_device, "only_co") in found
    assert [ident for device, ident in found if device == two_definitions_device] == ["acme_ex_1", "tuya_mo_2"]
    assert not {device for device, _ in found} & {*no_match, odd[0], renamed[0], renamed[1]}
    devices = [tuple(device) for device in page["cases"]["picked"]["devices"]]
    assert devices.index(odd[1]) < devices.index(odd[2])


def test_a_name_a_definition_gives_the_device_is_a_hint_never_a_match(page):
    """The registry, ZHA's integration diagnostics and the template show the name a definition gives a device
    (friendly_name), the definitions match the device's own: such a name, ignoring case, is a hint where nothing
    matches it."""
    assert page["cases"]["picked"]["hints"] == [[list(renamed[0]), "sonoff_swv"], [list(renamed[1]), "sonoff_swv"]]
    assert [list(renamed[2]), "sonoff_swv"] in page["cases"]["picked"]["matches"]
    for name, case in page["cases"].items():
        assert not {tuple(device) for device, _ in case["hints"]} & {tuple(device) for device, _ in case["matches"]}, name


def test_the_page_says_where_a_renamed_device_shows_and_lists_hints_apart():
    page = re.sub(r"\s+", " ", PAGE)
    renamed_text = "show a device under the name its quirk gives it"
    assert f"The integration's diagnostics {renamed_text}. A device's own diagnostics also carry the name the definitions match." in page
    assert "<p>The template shows a device under the name its quirk gives it. A device's own diagnostics carry the name the definitions match.</p>" in page
    assert '<div id="hinted" hidden>' in page and "<h3>Maybe renamed by its quirk</h3>" in page
    app = code(SITE / "app.js")
    assert "hints(loaded.definitions, rest)" in app and '$("hinted-list")' in app
    # A hinted device gets no "missing device" report: it may have a definition.
    assert "...unknown.map((device) =>" in app


def test_template_writes_what_the_page_reads(page):
    """Every ZHA device as "manufacturer||model", the shape devicesFromTemplate reads."""
    assert "integration_entities('zha')" in page["template"]
    assert "(device_attr(device, 'manufacturer') or '') ~ '||' ~ (device_attr(device, 'model') or '')" in page["template"]


def test_loading_reads_the_files_next_to_the_page(page, match_index):
    """release.json and the index as the tool wrote it, each past the browser's cache and without cookies."""
    options = {"credentials": "omit", "cache": "no-cache"}
    ids = [definition["id"] for definition in match_index["definitions"]]
    assert ids == sorted(json.loads(text)["id"] for text in MATCH.splitlines())
    assert page["files"] == {"release": "release.json", "definitions": "devices.json"}
    assert page["loads"]["ready"]["result"] == {"kind": "ready", "release": RELEASE, "ids": ids}
    assert page["loads"]["ready"]["requests"] == [["release.json", options], ["devices.json", options]]
    assert all(all(load["signals"]) for load in page["loads"].values())


def test_a_failed_load_says_why(page):
    expected = {
        "no_release": FAILED.format("release.json: 404"),
        "release_not_json": FAILED.format("release.json is not JSON"),
        "release_of_a_beta": FAILED.format("release.json names no release"),
        "no_index": FAILED.format("devices.json: 404"),
        "index_not_json": FAILED.format("devices.json is not JSON"),
        "server_error": FAILED.format("devices.json: 500"),
        "offline": FAILED.format("Failed to fetch"),
        "release_hangs": FAILED.format("release.json: no answer"),
        "index_hangs": FAILED.format("devices.json: no answer"),
        "another_format": FAILED.format("devices.json has a format this page does not read"),
        "no_definitions": FAILED.format("devices.json has a format this page does not read"),
    }
    assert set(expected) == set(LOADS) - {"ready"}
    for name, message in expected.items():
        assert page["loads"][name]["result"] == {"kind": "failed", "message": message}, name


def test_release_json_is_checked_before_use(page):
    """Only a release tag vX.Y.Z makes the link to the release; a date of another shape reads as none."""
    assert page["releases"] == [expected if expected else {"error": "release.json names no release"} for _, expected in RELEASES]
    assert page["releaseUrl"] == f"https://github.com/{REPO}/releases/tag/v1.2.3"


def test_devices_before_the_definitions_get_the_load_state(page):
    """A file dropped before the definitions are there gets the reason, not a wait that never ends."""
    assert page["ready"] == {"loading": "The definitions are still loading, try again in a moment.", "ready": None, "failed": FAILED.format("x")}
    app = code(SITE / "app.js")
    # Files and pasted text both ask first; a failed render leaves the definitions unavailable.
    assert app.count("const waiting = notReady(loaded);") == 2 and "loaded = outcome;" in app
    assert "unavailable(failure(error));" in app


def test_the_page_shows_the_risk_of_definitions_that_are_not_verified(page):
    """The badge next to "generated", why in its tooltip, the note of a high-risk definition's device card: the loader
    holds such a definition back unless "Use high-risk definitions" is on. A verified definition shows only "verified"."""
    assert tuple(page["risks"]) == RISKS
    assert page["riskShown"] == [shown for _, shown in RISK_SHOWN]
    app = code(SITE / "app.js")
    assert "riskBadge(definition)" in app and "riskNote(definition)" in app
    for tier in RISKS:
        assert f'{tier}: (definition) => riskShown(definition)?.tier === "{tier}",' in app
    options = json.loads((FOLDER / "strings.json").read_text())["options"]["step"]["init"]["data"]
    assert f'"{options["high_risk"]}"' in NOTE


def test_the_page_filters_and_explains_the_risks():
    options = re.findall(r'<option value="(\w+)">([^<]+)</option>', PAGE)
    assert [option for option in options if option[0] in RISKS] == [("high", "High risk"), ("medium", "Medium risk"), ("low", "Low risk")]
    assert {value for value, _ in options} == set(re.findall(r"^  (\w+): \((?:definition)?\) =>", code(SITE / "app.js"), re.M))
    assert ("<p>Risk: low for readings and simple controls, medium for number settings and take-overs of ZHA's own quirk, high for covers, thermostats and"
            " definitions with a known weak spot. Verified definitions show no risk.</p>") in PAGE
    style = (SITE / "style.css").read_text()
    for tier in RISKS:
        assert f".badge.risk-{tier} {{" in style and style.count(f"--risk-{tier}-bg:") == 2


def test_app_renders_what_the_modules_hand_it():
    """app.js fetches only through loadDefinitions; every link it builds is the release's or one of the repo's forms,
    never one from the definitions or the diagnostics."""
    app = code(SITE / "app.js")
    assert re.findall(r"\bfetch\(\s*([^\s,)]+)", app) == ["url"]
    assert "loadDefinitions((url, options) => fetch(url, options))" in app
    assert sorted(set(re.findall(r"\bhref: ([^,}]+?)\s*[,}]", app))) == ["releaseUrl(REPO", "url.href"]
    assert re.findall(r"const url = (.+);", app) == ["new URL(`https://github.com/${REPO}/issues/new`)"]
    assert set(re.findall(r"documentElement\.dataset\.(\w+)", app)) == {"repo"}
    assert not re.search(r"\b(location|URLSearchParams|getAttribute|localStorage|sessionStorage|innerHTML)\b", app)
    for module in ("definitions.js", "matcher.js"):
        assert not re.search(r"\b(document|window|location)\b", code(SITE / module)), module


def test_the_report_links_open_forms_that_exist():
    """The forms the page links, each field it fills in one the form has (GitHub fills a field by its id). The links of
    a definition suggest the titles of the integration's links (diagnostics.FORMS)."""
    app = code(SITE / "app.js")
    assert set(re.findall(r'issueLink\("[^"]+", "([\w.-]+)"', app)) == {"wrong-value.yml", "works.yml", "missing-device.yml"}
    keys = lambda fields: set(re.findall(r"(?:^|, )(\w+): ", fields.strip()))
    problem = re.search(r"const fields = \{(.*)\};", app)[1]
    # The fields of the problem link with a title of its own.
    works = re.search(r'issueLink\("Confirm it works", "works\.yml", \{ \.\.\.fields, (.*)\}\)', app)[1]
    filled = {
        "wrong-value.yml": keys(problem),
        "works.yml": keys(problem) | keys(works),
        "missing-device.yml": keys(re.search(r'issueLink\("report", "missing-device\.yml", \{(.*)\}\)', app)[1]),
    }
    assert filled == {"wrong-value.yml": {"title", "file", "revision", "ha"}, "works.yml": {"title", "file", "revision", "ha"}, "missing-device.yml": {"title", "fingerprint"}}
    for form, names in filled.items():
        template = yaml.safe_load((ROOT / ".github/ISSUE_TEMPLATE" / form).read_text())
        assert names - {"title"} <= {item["id"] for item in template["body"] if "id" in item}, form
        # A required field the page leaves empty says what goes there.
        empty = [item for item in template["body"] if item.get("validations", {}).get("required") and item["id"] not in names]
        assert all(item["attributes"].get("description") or item["attributes"].get("placeholder") for item in empty), form
    names = "${device[0]} ${device[1]}"
    for kind, fields in ((const.REPORT, problem), (const.WORKS, works)):
        assert f"title: `{diagnostics.FORMS[kind][1].format(names)}`" in fields, kind


def test_a_verified_definition_gets_no_works_link():
    """As the integration's button "Confirm it works (xquirks)": the "It works" form only while a definition is not
    verified, next to the problem link."""
    app = code(SITE / "app.js")
    assert app.count('"works.yml"') == 1
    assert 'const works = definition.verified ? null : [DOT, issueLink("Confirm it works", "works.yml", ' in app
    assert 'issueLink("Report a problem", "wrong-value.yml", fields), works);' in app


class Texts(HTMLParser):
    """What index.html shows: its text and the texts of link previews and form fields."""

    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []
        self.skip = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        self.skip = tag in ("script", "style")
        self.texts += [values[name] or "" for name in ("placeholder", "aria-label", "title", "alt") if name in values]
        if tag == "meta" and (values.get("name") == "description" or str(values.get("property")).startswith("og:")):
            self.texts.append(values.get("content") or "")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.texts.append(data)


def test_the_page_names_neither_the_probe_nor_the_change_list_nor_content_tags():
    """What v0.3.0 had and the rewrite dropped stays out of what the page shows and of its scripts' texts."""
    parsed = Texts()
    parsed.feed(PAGE)
    strings = [text for name in ("app.js", "definitions.js", "matcher.js") for text in re.findall(r'"(?:[^"\\]|\\.)*"|`(?:[^`\\]|\\.)*`', code(SITE / name))]
    texts = re.sub(r"\s+", " ", " ".join(parsed.texts + strings)).lower()
    assert "definitions for your devices" in texts and "high risk" in texts
    for gone in ("probe", "change list", "content tag", "content branch", "content-", "state.json", "manifest.json", "quirk file", "find files", "no file", "with a file", "zigbee2mqtt.io"):
        assert gone not in texts, gone


def test_policy_lets_the_page_reach_only_its_own_site():
    policy = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)">', PAGE)[1]
    directives = {name: values for name, *values in (part.split() for part in policy.split(";") if part.strip())}
    assert directives == {
        "default-src": ["'self'"],
        "img-src": ["'self'", "data:"],
        "connect-src": ["'self'"],
        "object-src": ["'none'"],
        "base-uri": ["'none'"],
        "form-action": ["'none'"],
    }


class Page(HTMLParser):
    """The local files index.html loads, its inline scripts and event handler attributes."""

    def __init__(self) -> None:
        super().__init__()
        self.files: set[str] = set()
        self.inline_scripts = 0
        self.handlers: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        self.handlers += [name for name, _ in attrs if name.startswith("on")]
        if tag == "script":
            if values.get("src"):
                self.files.add(values["src"])
            else:
                self.inline_scripts += 1
        if tag == "link" and values.get("rel") == "stylesheet":
            self.files.add(values["href"])
        if tag == "meta" and values.get("property") == "og:image":
            self.files.add(values["content"].rsplit("/", 1)[1])


def test_the_page_ships_every_file_it_loads_and_nothing_else(page):
    """site/ holds the page's own files and the index (package.json only lets node load the modules). The deploy adds
    release.json and the license."""
    parsed = Page()
    parsed.feed(PAGE)
    assert parsed.inline_scripts == 0 and parsed.handlers == []
    used, todo = {"index.html"} | parsed.files, [f for f in parsed.files if f.endswith(".js")]
    while todo:
        for name in re.findall(r'^import .* from "\./([\w.-]+)";$', (SITE / todo.pop()).read_text(), re.M):
            if name not in used:
                used.add(name)
                todo.append(name)
    shipped = {path.name for path in SITE.iterdir()}
    assert used | {page["files"]["definitions"]} == shipped - {"package.json"}
    assert page["files"]["release"] not in shipped


def test_the_page_offers_no_downloads():
    """The integration brings the definitions: no zip and no download links."""
    scripts = "".join(code(path) for path in SITE.glob("*.js"))
    assert not re.search(r"\bdownload\b\s*:", scripts)
    assert not re.search(r"<a\b[^>]*\bdownload\b", PAGE)
    assert "zip" not in PAGE.lower() and not re.search(r"\bzip\b", scripts)


def test_the_page_says_when_a_definition_steps_back():
    """A quirk of the user's or one of ZHA's own that covers a device wins over its definition (the loader steps back
    for it), apart from take-overs."""
    page = re.sub(r"\s+", " ", PAGE)
    assert "A definition steps back for a device that a quirk of yours or one of ZHA's own quirks covers, apart from take-overs." in page


def test_the_page_names_this_repo():
    """site/index.html names the repository the integration reads (const.py): the finder's data-repo and the link
    preview URLs under that repository's Pages address."""
    owner, name = const.REPOSITORY.split("/")
    assert re.findall(r'data-repo="([^"]*)"', PAGE) == [const.REPOSITORY]
    previews = re.findall(r'content="(https://[^"]*github\.io/[^"]*)"', PAGE)
    assert previews and all(url.startswith(f"https://{owner.lower()}.github.io/{name}/") for url in previews), previews


def pages_step(name: str) -> dict:
    (step,) = [step for step in yaml.safe_load((ROOT / ".github/workflows/pages.yml").read_text())["jobs"]["build"]["steps"] if step.get("name") == name]
    return step


def run_step(step: dict, cwd, env: dict) -> subprocess.CompletedProcess:
    """A run step as the build job runs it: bash with pipefail in the checkout, env in place of its expressions."""
    args = ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]]
    return subprocess.run(args, cwd=cwd, env=os.environ | env, capture_output=True, text=True, timeout=60, check=False)


def test_pages_deploys_site_of_the_latest_release(page):
    """pages.yml publishes site/ of the latest release's tag (GitHub's latest is never a pre-release), so the page and its
    index come from one commit, with release.json: after each release and by hand from main."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/pages.yml").read_text())
    # YAML 1.1 reads the key "on" as true.
    assert workflow[True] == {"workflow_run": {"workflows": ["Release"], "types": ["completed"]}, "workflow_dispatch": None}
    release = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    assert release["name"] == "Release"
    assert workflow["permissions"] == {"contents": "read"}
    build, deploy = workflow["jobs"]["build"], workflow["jobs"]["deploy"]
    assert build["if"] == "github.ref == 'refs/heads/main' && (github.event_name != 'workflow_run' || github.event.workflow_run.conclusion == 'success')"
    assert "permissions" not in build
    assert deploy["needs"] == "build" and deploy["permissions"] == {"pages": "write", "id-token": "write"}
    assert deploy["environment"]["name"] == "github-pages"
    # The latest release, a checkout of its tag, the page step, the upload of site/.
    steps = build["steps"]
    assert [step.get("name") or step["uses"].split("@")[0] for step in steps] == [
        "The latest release",
        "actions/checkout",
        "The page with the license and the release",
        "actions/upload-pages-artifact",
    ]
    assert steps[0]["id"] == "release" and 'gh release view --repo "$GITHUB_REPOSITORY" --json tagName,publishedAt' in steps[0]["run"]
    assert steps[1]["with"] == {"ref": "refs/tags/${{ steps.release.outputs.tag }}", "persist-credentials": False}
    assert steps[2]["env"] == {"TAG": "${{ steps.release.outputs.tag }}", "DATE": "${{ steps.release.outputs.date }}"}
    files = page["files"]
    assert f"[ -s site/{files['definitions']} ]" in steps[2]["run"] and f"> site/{files['release']}" in steps[2]["run"]
    assert steps[3]["with"] == {"path": "site"}


# A stand-in for gh, run by the tests' interpreter: $LATEST as the latest release ("tag date"), none when it is empty.
GH = """import os, sys
if sys.argv[1:3] != ["release", "view"]:
    sys.exit(f"unexpected gh {sys.argv[1:]}")
if not os.environ["LATEST"]:
    sys.exit("release not found")
print(os.environ["LATEST"])
"""


def test_pages_takes_the_tag_of_the_latest_release(tmp_path):
    """The build job's first step hands the checkout and the page step the latest release's tag (vX.Y.Z) and date. Run
    as the job runs it, with a stand-in for gh."""
    step = pages_step("The latest release")
    tools = tmp_path / "bin"
    tools.mkdir()
    (tools / "gh").write_text(f"#!{sys.executable}\n{GH}")
    (tools / "gh").chmod(0o755)

    def run(latest: str) -> tuple:
        output = tmp_path / "output"
        output.write_text("")
        env = {"PATH": f"{tools}{os.pathsep}{os.environ['PATH']}", "GITHUB_REPOSITORY": REPO, "GITHUB_OUTPUT": str(output), "LATEST": latest}
        done = run_step(step, tmp_path, env)
        return done.returncode, done.stdout, output.read_text()

    assert run("v0.4.0 2026-11-30") == (0, "", "tag=v0.4.0\ndate=2026-11-30\n")
    assert run("v0.4.0b2 2026-11-30") == (1, "::error::the latest release v0.4.0b2 is not vX.Y.Z\n", "")
    assert run("") == (1, "", "")


def test_pages_serves_the_license_of_the_device_data_next_to_it(tmp_path):
    """The MIT license of zigbee-herdsman-converters asks for its notice in every copy of the device data: the build job
    places the integration's THIRD_PARTY.md next to the page and its index, the footer links it. A tag without the index
    or the notice fails the run. Run as the job runs it: bash in a checkout of the tag."""
    footer = PAGE.split("<footer>", 1)[1].split("</footer>", 1)[0]
    ((href, text),) = re.findall(r'<a href="([^"#]+)">([^<]+)</a>', footer)
    assert (href, text) == ("THIRD_PARTY.md", "License of Zigbee2MQTT's device data")
    step = pages_step("The page with the license and the release")
    notice = FOLDER.relative_to(ROOT) / href

    def run(name: str, *missing) -> tuple:
        """The step in a checkout of its own without the files named: its site/ and the run."""
        checkout = tmp_path / name
        shutil.copytree(SITE, checkout / "site")
        (checkout / notice).parent.mkdir(parents=True)
        shutil.copy(ROOT / notice, checkout / notice)
        for path in missing:
            (checkout / path).unlink()
        return checkout / "site", run_step(step, checkout, {"TAG": "v0.4.0", "DATE": "2026-11-30"})

    site, good = run("good")
    assert (good.returncode, good.stdout) == (0, ""), good.stderr
    assert (site / href).read_bytes() == (ROOT / notice).read_bytes()
    assert (site / "devices.json").read_bytes() == (SITE / "devices.json").read_bytes()
    assert json.loads((site / "release.json").read_text()) == {"tag": "v0.4.0", "date": "2026-11-30"}
    _, no_index = run("no_index", "site/devices.json")
    assert (no_index.returncode, no_index.stdout) == (1, "::error::v0.4.0 has no site/devices.json\n")
    _, no_notice = run("no_notice", notice)
    assert no_notice.returncode == 1 and href in no_notice.stderr


def test_the_docs_name_the_risks_and_the_filter_the_finder_has():
    """README names the three risks in their order, CONTRIBUTING the finder's "Show" option for high risk."""
    readme = (ROOT / "README.md").read_text()
    risks = readme.split("\n## Risk levels\n", 1)[1].split("\n## ", 1)[0]
    assert tuple(re.findall(r"^- (\w+): ", risks, re.MULTILINE)) == RISKS
    assert "loads only with **Use high-risk definitions**." in re.sub(r"\s+", " ", risks)
    contributing = re.sub(r"\s+", " ", (ROOT / "CONTRIBUTING.md").read_text())
    label = re.search(r'the finder lists these definitions under "([^"]+)"', contributing)[1]
    assert f'<option value="high">{label}</option>' in PAGE
