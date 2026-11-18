"""The notices that share the self-check notice's questions: the link to the open reports before a report link, the
notice of a file blocked by a custom quirk for the devices not left out, the button that offers high-risk files and the
device variant the device test form gets.

No test reaches the network: the reports link is built here, GitHub's search page is never asked.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir

from . import integration
from .conftest import MANIFEST, REPOS, STRINGS, Zha, continue_fix, issue_of, issues_of, listed, manager_of, setup_entry, start_fix, write_repo
from .integration import DOMAIN, confirm, const, text
from .integration import plan as plans
from .test_checks import FORMS, form_ids, link_fields, result_with
from .test_plan import dev, plan_for, state

IEEE_A = "00:11:22:33:44:55:66:01"
IEEE_B = "00:11:22:33:44:55:66:02"
IEEE_C = "00:11:22:33:44:55:66:03"
NOTHING = {"remove": [], "update": [], "install": []}
REPORTS = f"https://github.com/{const.REPOSITORY}/issues"
QUERY = 'is:issue is:open "'
TRANSLATIONS = integration.FOLDER / "translations"


# The link to the open reports


def search_of(url: str) -> str:
    """The search a reports link opens: GitHub's issue list of the xquirks repository with one query field."""
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == REPORTS and parts.fragment == ""
    query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True)
    assert list(query) == ["q"] and len(query["q"]) == 1, query
    return query["q"][0]


def test_the_reports_link_searches_the_open_issues_for_the_file_name():
    url = text.reports_url("z2m_tuya_a.py")
    assert url == f"{REPORTS}?q=is%3Aissue%20is%3Aopen%20%22z2m_tuya_a.py%22"
    assert search_of(url) == 'is:issue is:open "z2m_tuya_a.py"'


@pytest.mark.parametrize(
    "name",
    [
        "z2m a b.py",
        "z2m_&_x.py",
        "z2m#fragment.py",
        "z2m?x=1&q=other.py",
        'z2m_"quoted".py',
        'x" repo:someone/else is:closed "y',
        "z2m_'single'.py",
        "z2m_ümlaut_日本_🙂.py",
        "z2m_)(][<>`*_|.py",
        "z2m\n_new\rline\t.py",
        "z2m_ ‮\x00.py",
        "z2m_%41_%zz.py",
        "z2m_" + "x" * 5000 + ".py",
        '"""',
        "",
    ],
    ids=lambda name: repr(name)[:24],
)
def test_a_hostile_name_stays_one_phrase_of_one_query_field(name: str):
    url = text.reports_url(name)
    # One markdown link that nothing in the name ends or extends: an ASCII address, the query percent-encoded.
    assert url.isascii() and url.startswith(f"{REPORTS}?q=")
    assert re.fullmatch(r"[A-Za-z0-9_.~%-]*", url.removeprefix(f"{REPORTS}?q=")), url
    assert len(url) < 1000
    query = search_of(url)
    # The name is one exact phrase: no quote of its own ends it early, no qualifier gets out of it.
    assert query.startswith(QUERY) and query.endswith('"') and query.count('"') == 2
    phrase = query[len(QUERY):-1]
    kept = "".join(char for char in name if char != '"' and char.isprintable())
    assert phrase == kept[:text.SEARCH_TERM_LIMIT].strip()
    assert len(phrase) <= text.SEARCH_TERM_LIMIT and phrase.isprintable()


def test_the_tuya_check_searches_its_fixed_words():
    assert search_of(text.reports_url(text.TUYA_REPORTS)) == f'{QUERY}{text.TUYA_REPORTS}"'
    assert text.TUYA_REPORTS in STRINGS["issues"]["tuya_check"]["title"]


# Where the link stands: before the report it saves, in both languages.
REPORTS_TEXTS = {
    "misfit": ("description",),
    "unavailable": ("description",),
    "refused": ("description",),
    "tuya_check": ("description",),
    "self_check_off": ("fix_flow", "step", "confirm", "description"),
}


@pytest.mark.parametrize("path", sorted(TRANSLATIONS.glob("*.json")), ids=lambda path: path.name)
def test_every_notice_that_asks_for_a_report_links_the_open_reports_first(path: Path):
    issues = json.loads(path.read_text())["issues"]
    for kind, keys in REPORTS_TEXTS.items():
        found = issues[kind]
        for key in keys:
            found = found[key]
        assert found.count("]({reports})") == 1, (path.name, kind)
        if "({link})" in found:
            assert found.index("({reports})") < found.index("({link})"), (path.name, kind)
        else:
            # The report is asked for in words after the link (misfit: the device links are above, the sentence says "before").
            assert found.index("({reports})") > 0, (path.name, kind)


async def test_the_notices_carry_the_reports_link_of_their_file(hass: HomeAssistant, zha: Zha, repo_dir: Path):
    """A misfit notice: the reports link of its file next to the per-device report links (the other notices: their tests)."""
    write_repo(repo_dir, {**MANIFEST, "acme_c": {**MANIFEST["acme_c"], "endpoints": {"1": [0xFC00]}}})
    zha.signatures[IEEE_C] = {"endpoints": {1: {"profile_id": 260, "device_type": 81, "input_clusters": [0], "output_clusters": []},
                                            2: {"profile_id": 260, "device_type": 81, "input_clusters": [0xFC00], "output_clusters": []}}}
    zha.add_device(IEEE_C, "Acme", "C-1", "Hall")
    await setup_entry(hass, repo_dir)
    placeholders = issue_of(hass, "misfit").translation_placeholders
    assert placeholders["reports"] == text.reports_url("z2m_acme_c.py")
    assert "Hall" not in placeholders["reports"]


# A file blocked by a custom quirk: the notice names only the devices not left out


async def radars(hass: HomeAssistant, zha: Zha, repo_dir: Path, excluded: tuple[str, ...] = (), disabled: tuple[str, ...] = ()):
    """Three radars on the user's own quirk (the file would claim each), some left out in the options or disabled."""
    ids = {ieee: zha.add_device(ieee, "_TZE200_aaaa", "TS0601", name) for ieee, name in ((IEEE_A, "Kitchen"), (IEEE_B, "Hall"), (IEEE_C, "Attic"))}
    zha.apply_quirks(dict.fromkeys(ids, "my_radar:Radar"))
    for ieee in disabled:
        dr.async_get(hass).async_update_device(ids[ieee], disabled_by=dr.DeviceEntryDisabler.USER)
    entry = await setup_entry(hass, repo_dir, options={const.CONF_EXCLUDED: [ids[ieee] for ieee in excluded]})
    return entry, ids


async def test_a_blocked_notice_names_every_device_when_none_is_left_out(hass: HomeAssistant, zha: Zha, repo_dir: Path):
    _entry, ids = await radars(hass, zha, repo_dir)
    notice = issue_of(hass, "blocked")
    placeholders = notice.translation_placeholders
    assert [line.split(" (")[0] for line in placeholders["devices"].splitlines()] == ["- Attic", "- Hall", "- Kitchen"]
    assert placeholders["count"] == "3"
    assert manager_of(hass).plan.blocked == [plans.Blocked("z2m_tuya_a.py", tuple(sorted(ids.values())))]


async def test_a_blocked_notice_leaves_out_the_devices_left_out(hass: HomeAssistant, zha: Zha, repo_dir: Path):
    _entry, ids = await radars(hass, zha, repo_dir, excluded=(IEEE_A,), disabled=(IEEE_C,))
    placeholders = issue_of(hass, "blocked").translation_placeholders
    assert [line.split(" (")[0] for line in placeholders["devices"].splitlines()] == ["- Hall"]
    assert placeholders["count"] == "1" and "Kitchen" not in str(placeholders) and "Attic" not in str(placeholders)
    # The protection still counts all three: the file is written for none of them.
    assert manager_of(hass).plan.blocked == [plans.Blocked("z2m_tuya_a.py", tuple(sorted(ids.values())))]
    assert listed(hass) == NOTHING


async def test_a_blocked_notice_goes_when_every_device_is_left_out(hass: HomeAssistant, zha: Zha, repo_dir: Path, quirks_dir: Path):
    entry, ids = await radars(hass, zha, repo_dir, excluded=(IEEE_A, IEEE_B), disabled=(IEEE_C,))
    assert issues_of(hass, "blocked") == []
    assert manager_of(hass).plan.blocked == [plans.Blocked("z2m_tuya_a.py", tuple(sorted(ids.values())))]
    assert listed(hass) == NOTHING and list(quirks_dir.iterdir()) == []
    # Taken back into the options: the notice is back for that device, as a notice of its own.
    hass.config_entries.async_update_entry(entry, options={const.CONF_EXCLUDED: [ids[IEEE_A]]})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert [line.split(" (")[0] for line in issue_of(hass, "blocked").translation_placeholders["devices"].splitlines()] == ["- Hall"]


def test_the_plan_blocks_the_file_for_a_left_out_device_on_a_custom_quirk():
    """Unchanged by the notice: a left-out device on another custom quirk still blocks the file for a device that needs it
    (ZHA would apply the file to both). Only the notice leaves it out."""
    st = state(a={"fingerprints": [("Acme", "A-1")]})
    devices = {"d1": dev("Acme", "A-1", quirk="my_quirk"), "d2": dev("Acme", "A-1")}
    for left_out in ({"excluded": frozenset({"d1"})}, {}):
        plan = plan_for(st, devices, **left_out)
        assert plan.install == [] and [(b.name, b.devices) for b in plan.blocked] == [("z2m_a.py", ("d1",))], left_out
    disabled = {"d1": dev("Acme", "A-1", quirk="my_quirk", disabled=True), "d2": dev("Acme", "A-1")}
    plan = plan_for(st, disabled)
    assert plan.install == [] and [(b.name, b.devices) for b in plan.blocked] == [("z2m_a.py", ("d1",))]


# "Offer high-risk files" on the notice of a held-back file


def two_high_risk(repo_dir: Path, version: int = 1, **manifest: dict) -> None:
    """z2m_tuya_a.py and z2m_acme_c.py at high risk, not verified (manifest: more entries or replacements)."""
    write_repo(repo_dir, {**MANIFEST, "tuya_a": {**MANIFEST["tuya_a"], "risk": "high", "risk_why": ["cover"]},
                          "acme_c": {**MANIFEST["acme_c"], "risk": "high", "risk_why": ["thermostat"]}, **manifest}, version=version)


async def held(hass: HomeAssistant, zha: Zha, repo_dir: Path, options: dict | None = None):
    two_high_risk(repo_dir)
    zha.add_device(IEEE_A, "_TZE200_aaaa", "TS0601", "Radar")
    plug = zha.add_device(IEEE_C, "Acme", "C-1", "Plug")
    entry = await setup_entry(hass, repo_dir, options=options)
    return entry, plug


async def test_offer_high_risk_files_turns_the_option_on_and_names_every_file_it_releases(hass: HomeAssistant, zha: Zha, repo_dir: Path, quirks_dir: Path):
    entry, plug = await held(hass, zha, repo_dir, {const.CONF_EXCLUDED: ["other"], const.CONF_HIGH_RISK: False})
    assert [issue.data["file"] for issue in issues_of(hass, "held_back")] == ["z2m_acme_c.py", "z2m_tuya_a.py"]
    notice = issue_of(hass, "held_back", "z2m_tuya_a.py")
    assert notice.is_fixable and notice.severity is ir.IssueSeverity.WARNING
    flow = await start_fix(hass, notice)
    assert flow["type"] is FlowResultType.MENU and flow["step_id"] == "confirm" and flow["menu_options"] == ["apply", "not_now"]
    placeholders = flow["description_placeholders"]
    # Every file the option releases, the one of this notice among them.
    assert placeholders["files"] == "- `z2m_acme_c.py` (Acme C-1)\n- `z2m_tuya_a.py` (Tuya A)"
    assert placeholders["file"] == "z2m_tuya_a.py" and placeholders["devices"].startswith("- Radar (")
    assert placeholders["reasons"] == "It controls a cover."
    # Nothing changed by opening it.
    assert entry.options[const.CONF_HIGH_RISK] is False
    flow = await continue_fix(hass, flow, {"next_step_id": "apply"})
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "done"
    await hass.async_block_till_done(wait_background_tasks=True)
    # The other options stay. Nothing is installed: the change list asks first.
    assert entry.options == {const.CONF_EXCLUDED: ["other"], const.CONF_HIGH_RISK: True}
    assert list(quirks_dir.iterdir()) == []
    assert listed(hass) == {**NOTHING, "install": ["z2m_acme_c.py", "z2m_tuya_a.py"]} and issues_of(hass, "held_back") == []


async def test_offer_high_risk_files_on_an_option_turned_on_meanwhile(hass: HomeAssistant, zha: Zha, repo_dir: Path):
    entry, _plug = await held(hass, zha, repo_dir)
    flow = await start_fix(hass, issue_of(hass, "held_back", "z2m_tuya_a.py"))
    hass.config_entries.async_update_entry(entry, options={const.CONF_EXCLUDED: ["kept"], const.CONF_HIGH_RISK: True})
    await hass.async_block_till_done(wait_background_tasks=True)
    flow = await continue_fix(hass, flow, {"next_step_id": "apply"})
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "already_on"
    assert entry.options == {const.CONF_EXCLUDED: ["kept"], const.CONF_HIGH_RISK: True}


async def test_offer_high_risk_files_after_the_files_went(hass: HomeAssistant, zha: Zha, repo_dir: Path):
    """The file of the notice is no longer held back (verified now): nothing is changed, the option stays off."""
    entry, _plug = await held(hass, zha, repo_dir)
    flow = await start_fix(hass, issue_of(hass, "held_back", "z2m_tuya_a.py"))
    two_high_risk(repo_dir, version=2, tuya_a={**MANIFEST["tuya_a"], "status": "verified", "risk": "high", "risk_why": ["cover"]})
    REPOS[repo_dir].publish()
    await entry.runtime_data.async_check()
    flow = await continue_fix(hass, flow, {"next_step_id": "apply"})
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "stale"
    assert not entry.options.get(const.CONF_HIGH_RISK)
    assert [issue.data["file"] for issue in issues_of(hass, "held_back")] == ["z2m_acme_c.py"]


async def test_offer_high_risk_files_after_another_file_was_held_back(hass: HomeAssistant, zha: Zha, repo_dir: Path):
    """The button would release a file the page did not name: nothing is changed, the next page names it."""
    entry, plug = await held(hass, zha, repo_dir)
    dr.async_get(hass).async_update_device(plug, disabled_by=dr.DeviceEntryDisabler.USER)
    await hass.async_block_till_done(wait_background_tasks=True)
    await entry.runtime_data.async_refresh()
    assert [issue.data["file"] for issue in issues_of(hass, "held_back")] == ["z2m_tuya_a.py"]
    flow = await start_fix(hass, issue_of(hass, "held_back", "z2m_tuya_a.py"))
    assert flow["description_placeholders"]["files"] == "- `z2m_tuya_a.py` (Tuya A)"
    dr.async_get(hass).async_update_device(plug, disabled_by=None)
    await hass.async_block_till_done(wait_background_tasks=True)
    flow = await continue_fix(hass, flow, {"next_step_id": "apply"})
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "stale"
    assert not entry.options.get(const.CONF_HIGH_RISK)
    flow = await start_fix(hass, issue_of(hass, "held_back", "z2m_tuya_a.py"))
    assert flow["description_placeholders"]["files"] == "- `z2m_acme_c.py` (Acme C-1)\n- `z2m_tuya_a.py` (Tuya A)"


async def test_not_now_puts_the_held_back_notice_aside(hass: HomeAssistant, zha: Zha, repo_dir: Path):
    entry, _plug = await held(hass, zha, repo_dir)
    notice = issue_of(hass, "held_back", "z2m_tuya_a.py")
    flow = await continue_fix(hass, await start_fix(hass, notice), {"next_step_id": "not_now"})
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "not_now"
    await entry.runtime_data.async_refresh()
    assert ir.async_get(hass).async_get_issue(DOMAIN, notice.issue_id).dismissed_version
    assert [issue.data["file"] for issue in issues_of(hass, "held_back")] == ["z2m_acme_c.py"]
    assert not entry.options.get(const.CONF_HIGH_RISK)


async def test_offer_high_risk_files_without_the_integration_loaded(hass: HomeAssistant, zha: Zha, repo_dir: Path):
    entry, _plug = await held(hass, zha, repo_dir)
    notice = issue_of(hass, "held_back", "z2m_tuya_a.py")
    flow = await start_fix(hass, notice)
    await hass.config_entries.async_unload(entry.entry_id)
    flow = await continue_fix(hass, flow, {"next_step_id": "apply"})
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "not_loaded"
    assert not entry.options.get(const.CONF_HIGH_RISK)


async def test_offer_high_risk_files_while_the_plan_cannot_tell(hass: HomeAssistant, zha: Zha, repo_dir: Path):
    """A change waits for ZHA to load the folder: the plan stops before it knows the files held back, the notice stays as
    it was. Nothing is changed until the plan can tell again."""
    entry, _plug = await held(hass, zha, repo_dir)
    notice = issue_of(hass, "held_back", "z2m_tuya_a.py")
    flow = await start_fix(hass, notice)
    entry.runtime_data.data["pending"]["changes"] = [{"file": "z2m_acme_b.py", "change": "installed"}]
    flow = await continue_fix(hass, flow, {"next_step_id": "apply"})
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "held"
    assert entry.runtime_data.plan.held == plans.PENDING and entry.runtime_data.plan.held_back == []
    assert issue_of(hass, "held_back", "z2m_tuya_a.py").issue_id == notice.issue_id
    flow = await start_fix(hass, notice)
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "held"
    assert not entry.options.get(const.CONF_HIGH_RISK)


def test_the_held_back_notice_is_a_fix_flow_only():
    """Home Assistant shows a fixable notice's flow, never a description: hassfest allows one of the two."""
    for path in sorted(TRANSLATIONS.glob("*.json")):
        held_back = json.loads(path.read_text())["issues"]["held_back"]
        assert set(held_back) == {"title", "fix_flow"}, path.name
        assert set(held_back["fix_flow"]["abort"]) == {"not_loaded", "stale", "already_on", "held", "done", "not_now"}, path.name


# The device variant in the device test form


def test_the_device_test_form_has_an_optional_variant_field():
    form = yaml.safe_load((FORMS / text.VERIFY_FORM).read_text())
    (field,) = [field for field in form["body"] if field.get("id") == text.VARIANT_FIELD]
    assert text.VARIANT_FIELD == "variant" and field["type"] == "input"
    assert not field.get("validations", {}).get("required", False)
    assert confirm.parse(f"x {field['attributes']['placeholder']}") is not None


@pytest.mark.parametrize(
    ("manufacturer", "model", "firmware"),
    [
        ("Acme", "C-1", "0x46"),
        ("_TZE200_aaaa", "TS0601", None),
        ("A | B ] [ c", "M\n| [x]", "1.0 | 2"),
        ("M" * 200, "😀" * 50, " " * 3),
    ],
    ids=["plain", "no_firmware", "separators", "long"],
)
def test_the_variant_field_holds_the_fixed_part_the_title_ends_with(manufacturer: str, model: str, firmware: str | None):
    result = dataclasses.replace(result_with(3), manufacturer=manufacturer, model=model, firmware=firmware)
    url = text.check_url({"vendor": "Acme", "model": "C-1"}, result, {"1-value_0": "yes"}, "2026.11.0", "1a2b3c4d")
    fields = link_fields(url)
    assert fields["template"] == text.VERIFY_FORM and set(fields) - {"template", "title"} <= form_ids(text.VERIFY_FORM)
    fixed = confirm.fixed_part(manufacturer, model, firmware, "1a2b3c4d")
    assert fields[text.VARIANT_FIELD] == fixed and fields["title"].endswith(f" {fixed}")
    assert confirm.matches(fields["title"], fields[text.VARIANT_FIELD])
    assert confirm.parse(fields["title"]) == confirm.parse(f"x {fields[text.VARIANT_FIELD]}")


def test_the_variant_field_keeps_the_link_under_the_cap():
    result = dataclasses.replace(result_with(400), manufacturer="M" * 32, model="|" * 32, firmware="F" * 32)
    url = text.check_url({"vendor": "V" * 100, "model": "C-1"}, result, {"1-value_0": "yes"}, "2026.11.0", "1a2b3c4d")
    assert len(url) <= text.REPORT_LIMIT
    fields = link_fields(url)
    assert fields["report"] == text.CHECK_TOO_LONG
    assert fields[text.VARIANT_FIELD] == confirm.fixed_part("M" * 32, "|" * 32, "F" * 32, "1a2b3c4d")
    assert fields["title"].endswith(fields[text.VARIANT_FIELD])


@pytest.mark.parametrize("key", [None, "1A2B3C4D", "1a2b3c4", "not a key"])
def test_without_a_valid_key_the_form_gets_no_variant(key: str | None):
    url = text.check_url({"vendor": "Acme", "model": "C-1"}, result_with(3), {"1-value_0": "yes"}, "2026.11.0", key)
    fields = link_fields(url)
    assert text.VARIANT_FIELD not in fields and fields["title"] == "Verify: Acme C-1"


def test_a_problem_report_gets_no_variant():
    result = result_with(3)
    url = text.check_url({"vendor": "Acme", "model": "C-1"}, result, {"1-value_0": "no"}, "2026.11.0", "1a2b3c4d")
    fields = link_fields(url)
    assert fields["template"] == text.PROBLEM_FORM and text.VARIANT_FIELD not in fields
