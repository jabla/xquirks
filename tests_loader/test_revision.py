"""The revision: rev is computed from the definition itself (FORMAT.md, Revision).

The generator writes it. The trial build (check, loader.py --check) fails a line whose rev is another. The load takes it
as the line has it.
"""

import copy
import hashlib
import json
import subprocess
import sys

import pytest

import loader
from helpers import BY_ID, SAMPLE, raw_of

# A definition and its canonical text written out by hand: no rev, risk or verified, the keys of every object sorted,
# no whitespace, \n and \u001b escaped, Ä and ° as they are, 1.0 and 1e-06 as Python writes them.
CRAFTED = {
    "rev": "00000000", "risk": "high", "verified": True, "id": "x", "endpoints": {"2": [0], "10": [6]},
    "chain": [["QuirkBuilder", ["Ä", "m\n\x1b"]], ["sensor", {"unit": "°C", "multiplier": 1.0, "divisor": 1e-06}], ["add_to_registry"]],
}
CANONICAL = r'{"chain":[["QuirkBuilder",["Ä","m\n\u001b"]],["sensor",{"divisor":1e-06,"multiplier":1.0,"unit":"°C"}],["add_to_registry"]],"endpoints":{"10":[6],"2":[0]},"id":"x"}'


def test_the_rev_is_the_start_of_the_sha256_of_the_canonical_text():
    assert hashlib.sha256(CANONICAL.encode()).hexdigest()[:8] == "26227c23"  # as sha256sum gives it for the text
    assert loader.revision(CRAFTED) == "26227c23"


def reordered(value):
    """The value with the keys of every object in reverse order."""
    if isinstance(value, dict):
        return {key: reordered(value[key]) for key in reversed(value)}
    if isinstance(value, list):
        return [reordered(item) for item in value]
    return value


@pytest.mark.parametrize("ident", BY_ID)
def test_the_order_of_keys_does_not_count(ident):
    definition = BY_ID[ident]
    assert loader.revision(reordered(definition)) == loader.revision(definition) == definition["rev"]


@pytest.mark.parametrize("change", [{"rev": "0badc0de"}, {"risk": "low"}, {"verified": True}], ids=["rev", "risk", "verified"])
def test_rev_risk_and_the_verification_do_not_count(change):
    """A verification or a new risk rule is no new behaviour (the sample's ZBMicro is high risk and unverified)."""
    definition = BY_ID["sonoff_zbmicro"]
    assert loader.revision({**definition, **change}) == definition["rev"]
    assert loader.revision({key: value for key, value in definition.items() if key not in ("rev", "risk", "verified")}) == definition["rev"]


def members(definition) -> list:
    return definition["classes"][0]["attribute_defs"]["members"]


CHANGES = {
    # name: (definition, change). Every key but rev, risk and verified counts.
    "id": ("sonoff_zbmicro", lambda d: d.update(id="sonoff_zbmicro_2")),
    "a new key": ("sonoff_zbmicro", lambda d: d.update(claimed_elsewhere=[["SONOFF", "ZBMicro"]])),
    "an endpoint": ("sonoff_zbmicro", lambda d: d["endpoints"].update({"2": [6]})),
    "a cluster of an endpoint": ("sonoff_zbmicro", lambda d: d["endpoints"]["1"].append(6)),
    "a string": ("sonoff_zbmicro", lambda d: members(d)[0].__setitem__(0, "network_led_2")),
    "an integer as a float": ("sonoff_zbmicro", lambda d: members(d)[0][1].update(id=1.0)),
    "null for a value": ("sonoff_zbmicro", lambda d: members(d)[3][1].update(manufacturer_code=None)),
    "the order of a list": ("sonoff_zbmicro", lambda d: d["chain"].insert(1, d["chain"].pop(2))),
    "take_over": ("sonoff_zbminir2", lambda d: d.update(take_over=False)),
    "a digest of ZHA's quirk": ("sonoff_zbminir2", lambda d: d["zha_digests"].append("0123456789abcdef")),
    "inherits": ("shelly_sbdw_103c", lambda d: d["inherits"].update(Other="zhaquirks.shelly.door_window:Other")),
    "an alias fingerprint": ("alias_aqara_cl_l02d", lambda d: d["alias"][0]["fingerprints"].append(["Aqara", "lumi.light.zzz999"])),
}


@pytest.mark.parametrize("name", CHANGES)
def test_any_other_change_gives_another_rev(name):
    ident, change = CHANGES[name]
    definition = copy.deepcopy(BY_ID[ident])
    change(definition)
    assert json.dumps(definition) != json.dumps(BY_ID[ident]) and loader.revision(definition) != BY_ID[ident]["rev"]


def lines(*definitions) -> str:
    return "".join(json.dumps(definition, ensure_ascii=False) + "\n" for definition in definitions)


def test_the_trial_build_names_a_wrong_rev_with_the_computed_one(registry, tmp_path):
    """A line edited by hand without a new rev fails. So does a rev edited by hand (a chain, a take-over, an alias). The
    failure names the rev computed from the line. A new risk or verification keeps the rev."""
    edited = copy.deepcopy(BY_ID["sonoff_zbmicro"])
    edited["endpoints"]["1"].append(6)
    wrong = {ident: {**BY_ID[ident], "rev": "0badc0de"} for ident in ("sonoff_swv", "alias_aqara_cl_l02d")}
    path = tmp_path / "definitions.jsonl"
    path.write_text(lines(edited, *wrong.values(), {**BY_ID["coolo_cs_201z"], "risk": "high", "verified": True}))
    assert loader.check(str(path)) == {
        "sonoff_zbmicro": f"rev {edited['rev']} differs from the computed rev {loader.revision(edited)}",
        **{ident: f"rev 0badc0de differs from the computed rev {BY_ID[ident]['rev']}" for ident in wrong},
    }


def test_a_rev_that_cannot_be_computed_fails_its_line(registry, tmp_path):
    """A lone surrogate (only an escape can write one) has no UTF-8: its line fails, the trial build goes on."""
    path = tmp_path / "definitions.jsonl"
    path.write_text(json.dumps(BY_ID["alias_aqara_cl_l02d"]).replace("lumi.light.acn031", "lumi.light.acn031\\ud800") + "\n" + lines(BY_ID["coolo_cs_201z"]))
    failures = loader.check(str(path))
    assert list(failures) == ["alias_aqara_cl_l02d"] and failures["alias_aqara_cl_l02d"].startswith("rev cannot be computed: UnicodeEncodeError")


def test_the_trial_build_on_the_command_line_names_the_computed_rev(tmp_path):
    path = tmp_path / "definitions.jsonl"
    path.write_text(lines({**BY_ID["coolo_cs_201z"], "rev": "0badc0de"}))
    done = subprocess.run([sys.executable, loader.__file__, "--check", str(path)], capture_output=True, text=True, timeout=600, check=False)
    assert (done.returncode, done.stdout) == (1, f"coolo_cs_201z: rev 0badc0de differs from the computed rev {BY_ID['coolo_cs_201z']['rev']}\n")


def test_the_load_takes_the_rev_as_the_line_has_it(installation, registry, monkeypatch):
    """Computing it would cost every ZHA start: neither register() nor the build at the first device does."""
    computed = []
    monkeypatch.setattr(loader, "revision", lambda definition: computed.append(definition) or "00000000")
    installation.options(high_risk=True)  # the ZBMicro is high risk and unverified
    installation.definitions(lines(*({**d, "rev": "0badc0de"} if d["id"] == "sonoff_zbmicro" else d for d in SAMPLE)))
    path = installation.integration / loader.DEFINITIONS_FILE
    assert loader.register(str(installation.stub), "xquirks", str(path)) == (len(SAMPLE), 0)
    assert registry.match_entry(raw_of("sonoff_zbmicro")).source.label == "xquirks:sonoff_zbmicro 0badc0de"
    assert computed == []
