"""The risk of a file that is not verified on a device: how the change list names it, and the high-risk
files held back until the options offer them."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .conftest import MANIFEST, REPOS, STRINGS, Zha, apply_list, issue_of, issues_of, listed, quirk_text, render, setup_entry, start_fix, write_repo
from .integration import DOMAIN, const, content, pictures, text
from .integration import plan as plans
from .test_content import requested
from .test_pictures import png, stored

IEEE_A = "00:11:22:33:44:55:66:01"
NOTHING = {"remove": [], "update": [], "install": []}
DEVICES = {"d1": plans.Device("_TZE200_aaaa", "TS0601", "Radar", "")}
ENTRY = {"file": "quirks/z2m_tuya_a.py", "fingerprints": [["_TZE200_aaaa", "TS0601"]], "vendor": "Tuya", "model": "A", "status": "generated"}
INSTALL = "- **Install** `z2m_tuya_a.py` (Tuya A) for Radar. Status: generated, not verified on a device yet."
UPDATE = "- **Update** `z2m_tuya_a.py` (Tuya A) for Radar."
PATH = "/config/custom_zha_quirks/z2m_tuya_a.py"


def change_text(plan: plans.Plan, entry: dict, record: dict | None = None) -> list[str]:
    return text.change_list(plan, DEVICES, lambda name: "Tuya A", {}, {}, lambda name: entry, lambda path: record if path == PATH else None).splitlines()


def install(entry: dict) -> list[str]:
    return change_text(plans.Plan(install=[plans.Install("z2m_tuya_a.py", "0" * 64, ("d1",))]), entry)


def update(entry: dict, record: dict | None) -> list[str]:
    return change_text(plans.Plan(update=[plans.Update(PATH, "1" * 64, "0" * 64, "0123abcd", False, ("d1",))]), entry, record)


@pytest.mark.parametrize(
    ("fields", "line"),
    [
        ({"risk": "low", "risk_why": []}, f"{INSTALL} Risk: low."),
        ({"risk": "medium", "risk_why": ["number_settings"]}, f"{INSTALL} Risk: medium. It changes number settings. The device may accept a smaller range."),
        ({"risk": "high", "risk_why": ["cover"]}, f"{INSTALL} Risk: high. It controls a cover. Test it before you rely on it."),
        (
            {"risk": "high", "risk_why": ["take_over_drops", "number_settings", "take_over"]},
            f"{INSTALL} Risk: high. It replaces ZHA's own quirk and leaves out features that quirk shows. It changes number settings. The device may accept a smaller"
            " range. It replaces ZHA's own quirk. Test it before you rely on it.",
        ),
        # Missing or unknown: high, an unknown reason left out.
        ({}, f"{INSTALL} Risk: high. Test it before you rely on it."),
        ({"risk": "extreme", "risk_why": ["a_new_reason", "thermostat"]}, f"{INSTALL} Risk: high. It controls a thermostat. Test it before you rely on it."),
    ],
)
def test_an_install_names_the_risk_with_its_reasons(fields: dict, line: str):
    assert install({**ENTRY, **fields})[0] == line


def test_a_verified_install_names_no_risk():
    assert install({**ENTRY, "status": "verified", "risk": "high", "risk_why": ["cover"]})[0] == "- **Install** `z2m_tuya_a.py` (Tuya A) for Radar. Status: verified on a device."


@pytest.mark.parametrize(
    ("before", "after", "bullet"),
    [
        ({"status": "generated", "risk": "low"}, {"risk": "high", "risk_why": ["cover"]}, "    - risk now high. It controls a cover."),
        ({"status": "generated", "risk": "medium"}, {"risk": "high", "risk_why": ["defect_found", "local_occupancy"]},
         "    - risk now high. A device test found a fault. Its fix is not verified yet. ZHA shows delay settings for it that never reach the device."),
        ({"status": "generated", "risk": "low"}, {"risk": "medium", "risk_why": []}, "    - risk now medium."),
        # A verified version followed by one that is not: its risk now.
        ({"status": "verified", "risk": "high"}, {"risk": "low", "risk_why": []}, "    - risk now low."),
        ({"status": "verified", "risk": "low"}, {"risk": "nonsense", "risk_why": ["thermostat"]}, "    - risk now high. It controls a thermostat."),
    ],
)
def test_an_update_that_raises_the_risk_says_so_first(before: dict, after: dict, bullet: str):
    lines = update({**ENTRY, **after}, {"sha256": "0" * 64, "revision": "0123abcd", "names": {}, **before})
    assert lines[:2] == [UPDATE, bullet]


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ({"status": "generated", "risk": "high"}, {"risk": "high", "risk_why": ["cover"]}),
        ({"status": "generated", "risk": "medium"}, {"risk": "low"}),
        ({"status": "generated", "risk": "low"}, {"status": "verified", "risk": "high"}),
        ({"status": "verified", "risk": "low"}, {"status": "verified", "risk": "high"}),
        # A record that does not say counts as high: nothing ranks above it.
        ({}, {"risk": "high", "risk_why": ["cover"]}),
    ],
)
def test_an_update_that_does_not_raise_the_risk_says_nothing_of_it(before: dict, after: dict):
    assert update({**ENTRY, **after}, {"sha256": "0" * 64, "revision": "0123abcd", "names": {}, **before}) == [UPDATE]


def test_every_reason_has_a_sentence():
    assert tuple(text.RISK_REASONS) == content.RISK_REASONS
    assert text.TIERS == ("verified", "low", "medium", "high")
    assert all(sentence.endswith(".") and sentence[0].isupper() for sentence in text.RISK_REASONS.values())


async def test_the_dialog_names_the_risk_and_an_update_that_raises_it(hass: HomeAssistant, zha: Zha, repo_dir: Path):
    """The record keeps the status and risk of the version written: the next update compares with it."""
    write_repo(repo_dir, {"tuya_a": {**MANIFEST["tuya_a"], "risk": "medium", "risk_why": ["number_settings"]}})
    zha.add_device(IEEE_A, "_TZE200_aaaa", "TS0601", "Radar")
    entry = await setup_entry(hass, repo_dir)
    flow = await start_fix(hass, issue_of(hass, "changes"))
    assert "Status: generated, not verified on a device yet. Risk: medium. It changes number settings. The device may accept a smaller range." in flow["description_placeholders"]["changes"]
    await apply_list(hass)
    zha.reload({IEEE_A: "z2m_tuya_a:TuyaA"})
    write_repo(repo_dir, {"tuya_a": {**MANIFEST["tuya_a"], "risk": "high", "risk_why": ["number_settings", "state_dependency"]}}, version=2)
    REPOS[repo_dir].publish()
    await entry.runtime_data.async_check()
    assert listed(hass) == {**NOTHING, "update": ["z2m_tuya_a.py"]}
    flow = await start_fix(hass, issue_of(hass, "changes"))
    lines = flow["description_placeholders"]["changes"].splitlines()
    assert lines[1] == "    - risk now high. It changes number settings. The device may accept a smaller range. One of its settings changes how another value reads."


# Held back while the options do not offer high-risk files.


def high_risk(repo_dir: Path, version: int = 1, **fields) -> None:
    """The working tree with z2m_tuya_a.py at high risk."""
    write_repo(repo_dir, {**MANIFEST, "tuya_a": {**MANIFEST["tuya_a"], "risk": "high", "risk_why": ["cover", "number_settings"], **fields}}, version=version)


async def test_a_held_back_file_gets_an_ignorable_notice_and_is_not_downloaded(hass: HomeAssistant, zha: Zha, repo_dir: Path, quirks_dir: Path, aioclient_mock):
    high_risk(repo_dir)
    zha.add_device(IEEE_A, "_TZE200_aaaa", "TS0601", "Radar")
    entry = await setup_entry(hass, repo_dir)
    assert listed(hass) == NOTHING and list(quirks_dir.iterdir()) == []
    notice = issue_of(hass, "held_back", "z2m_tuya_a.py")
    assert notice.is_fixable and notice.severity is ir.IssueSeverity.WARNING
    assert render(STRINGS["issues"]["held_back"]["title"], notice.translation_placeholders) == "High-risk quirk held back: Tuya A"
    # A fixable notice shows its flow: the first page says what the notice found (opening it changes nothing).
    flow = await start_fix(hass, notice)
    assert render(STRINGS["issues"]["held_back"]["fix_flow"]["step"]["confirm"]["description"], flow["description_placeholders"]) == (
        "- Radar (\\_TZE200\\_aaaa TS0601), runs no quirk\n\n"
        "xquirks has `z2m_tuya_a.py` for it (generated, not verified on a device yet) but holds it back until it is verified on a device."
        " It controls a cover. It changes number settings. The device may accept a smaller range.\n\n"
        "**Offer high-risk files** turns on that option (Settings > Devices & services > xquirks > Configure > What xquirks offers)."
        " It releases every high-risk file held back now:\n\n- `z2m_tuya_a.py` (Tuya A)\n\n"
        "The change list then offers them and asks again before it installs anything. **Not now** puts this notice aside."
    )
    assert requested(aioclient_mock, "/quirks/z2m_tuya_a.py") == 0
    summary = entry.runtime_data.summary()
    assert summary["held_back_high_risk"] == {"z2m_tuya_a.py": ["_TZE200_aaaa TS0601"]} and summary["offer_high_risk"] is False
    assert "Radar" not in str(summary["held_back_high_risk"])

    # Ignored, it stays ignored while the same devices need the file.
    ir.async_ignore_issue(hass, DOMAIN, notice.issue_id, True)
    await entry.runtime_data.async_refresh()
    assert issues_of(hass, "held_back") == []
    # Turned on: the change list offers it, marked with its risk, the notice goes.
    hass.config_entries.async_update_entry(entry, options={const.CONF_HIGH_RISK: True})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert listed(hass) == {**NOTHING, "install": ["z2m_tuya_a.py"]}
    assert ir.async_get(hass).async_get_issue(DOMAIN, notice.issue_id) is None
    flow = await start_fix(hass, issue_of(hass, "changes"))
    assert "Risk: high. It controls a cover. It changes number settings. The device may accept a smaller range. Test it before you rely on it." in flow["description_placeholders"]["changes"]
    assert entry.runtime_data.summary()["held_back_high_risk"] == {}


async def test_an_installed_high_risk_file_gets_its_updates(hass: HomeAssistant, zha: Zha, repo_dir: Path, quirks_dir: Path):
    """Installed while offered (or before its risk rose): the option turned off again holds back no update."""
    high_risk(repo_dir)
    zha.add_device(IEEE_A, "_TZE200_aaaa", "TS0601", "Radar")
    entry = await setup_entry(hass, repo_dir, options={const.CONF_HIGH_RISK: True})
    await apply_list(hass)
    zha.reload({IEEE_A: "z2m_tuya_a:TuyaA"})
    hass.config_entries.async_update_entry(entry, options={const.CONF_HIGH_RISK: False})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert listed(hass) == NOTHING and issues_of(hass, "held_back") == []
    high_risk(repo_dir, version=2)
    REPOS[repo_dir].publish()
    await entry.runtime_data.async_check()
    assert listed(hass) == {**NOTHING, "update": ["z2m_tuya_a.py"]} and issues_of(hass, "held_back") == []
    await apply_list(hass)
    assert (quirks_dir / "z2m_tuya_a.py").read_bytes() == quirk_text("tuya_a", 2)


async def test_the_held_back_page_shows_the_picture_of_this_run(hass: HomeAssistant, zha: Zha, repo_dir: Path):
    """The first page builds the picture of this run (the address of a thumbnail carries a token of each Home Assistant
    run), never from the notice's placeholder."""
    picture = png(256, 256)
    stored(hass, picture).parent.mkdir(parents=True)
    stored(hass, picture).write_bytes(png(128, 128))
    high_risk(repo_dir, picture="A.png", picture_sha256=hashlib.sha256(picture).hexdigest())
    zha.add_device(IEEE_A, "_TZE200_aaaa", "TS0601", "Radar")
    entry = await setup_entry(hass, repo_dir)
    before = issue_of(hass, "held_back", "z2m_tuya_a.py").translation_placeholders["picture"]
    assert before
    hass.data[pictures.DATA_PICTURES].token = "f" * 32
    # The notice as an earlier run left it: the refresh of the first page does not renew it.
    with patch.object(entry.runtime_data, "_sync_issues"):
        flow = await start_fix(hass, issue_of(hass, "held_back", "z2m_tuya_a.py"))
    assert issue_of(hass, "held_back", "z2m_tuya_a.py").translation_placeholders["picture"] == before
    shown = flow["description_placeholders"]["picture"]
    assert shown == entry.runtime_data.picture("z2m_tuya_a.py") != before and "f" * 32 in shown
