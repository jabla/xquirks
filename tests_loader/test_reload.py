"""Reloads: the stub in a custom quirks folder, zhaquirks.setup run as ZHA runs it on every (re)load."""

import collections
import json
import logging
import sys

import zhaquirks

import loader
from helpers import BY_ID, IEEE, SAMPLE, label_of, raw_of


def labels(registry) -> collections.Counter:
    return collections.Counter(e.source.label for e in registry if str(getattr(e.source, "label", "")).startswith(loader.LABEL_PREFIX))


EXPECTED = collections.Counter(label_of(d) for d in SAMPLE)


def test_two_setups_leave_one_entry_per_definition(installation, registry, caplog):
    caplog.set_level(logging.INFO, logger="xquirks")
    installation.options(high_risk=True)
    installation.setup()
    assert labels(registry) == EXPECTED
    installation.setup()
    assert labels(registry) == EXPECTED
    assert len(EXPECTED) == len(SAMPLE) == 15
    # Each entry has the stub as its source: zhaquirks.setup purges it with the folder's other custom quirks.
    assert {e.source.file for e in registry if e.source and str(e.source.label).startswith(loader.LABEL_PREFIX)} == {str(installation.stub)}
    assert caplog.text.count("xquirks: 15 definitions loaded, 0 skipped") == 2
    # The integration's __init__ (it raises) never ran: the stub only located the folder.
    assert "custom_components.xquirks" not in sys.modules
    assert "the integration's __init__ ran" not in caplog.text


def test_exclusion_change_takes_effect_with_the_next_setup(installation, registry):
    installation.options(high_risk=True)
    installation.setup()
    assert registry.match_entry(raw_of("sonoff_zbmicro")).source.label == label_of(BY_ID["sonoff_zbmicro"])
    installation.options(high_risk=True, excluded=[IEEE.upper()])
    installation.setup()
    assert labels(registry) == EXPECTED
    assert registry.match_entry(raw_of("sonoff_zbmicro")) is None  # ZHA's own handling, no quirk
    installation.options(high_risk=True)
    installation.setup()
    resolved = registry.resolve(raw_of("sonoff_zbmicro"))
    assert resolved._quirk_registry_entry.source.label == label_of(BY_ID["sonoff_zbmicro"])


def test_high_risk_opt_in(installation, registry):
    """heat_decor_hd_t1000 is high risk and unverified: it applies only with the opt-in. sonoff_zbmicro is high risk too:
    verified on a device, it applies without."""
    verified = {**BY_ID["sonoff_zbmicro"], "verified": True}  # the same rev: a verification is no new behaviour
    installation.definitions("".join(json.dumps(verified if d["id"] == "sonoff_zbmicro" else d, ensure_ascii=False, separators=(",", ":")) + "\n" for d in SAMPLE))
    installation.setup()  # no options file: the defaults
    assert registry.match_entry(raw_of("heat_decor_hd_t1000")) is None
    assert registry.match_entry(raw_of("sonoff_zbmicro")).source.label == label_of(BY_ID["sonoff_zbmicro"])
    installation.options(high_risk=True)
    installation.setup()
    assert registry.match_entry(raw_of("heat_decor_hd_t1000")).source.label == label_of(BY_ID["heat_decor_hd_t1000"])


def test_broken_options_file_means_defaults(installation, registry, caplog):
    installation.options("{not json")
    installation.setup()
    assert labels(registry) == EXPECTED
    assert "xquirks_options.json unreadable" in caplog.text
    assert registry.match_entry(raw_of("heat_decor_hd_t1000")) is None  # high risk stays off


def test_stub_in_a_second_folder_leaves_no_stale_entries(installation, registry):
    """zhaquirks purges only the folder it loads. The loader removes its own earlier entries by label."""
    installation.options(high_risk=True)
    installation.setup()
    other = installation.config / "other_quirks"
    other.mkdir()
    (other / "xquirks.py").write_text(installation.stub.read_text())
    zhaquirks.setup(custom_quirks_path=str(other))
    assert labels(registry) == EXPECTED
    assert {e.source.file for e in registry if str(getattr(e.source, "label", "")).startswith(loader.LABEL_PREFIX)} == {str(other / "xquirks.py")}
