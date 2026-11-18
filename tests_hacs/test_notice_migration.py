"""Self-check notices of v0.1.0: that release made them without a fix flow, with only the kind and the file as their
data. Since then a self-check notice is fixable and its texts are its fix flow's alone (hassfest allows a description
or a fix flow, not both). At load an open notice of v0.1.0 becomes fixable with the data its fix flow needs (from the
self-check store). Without that data it goes. So does one for a file verified now that does not say "looks off". A
notice of another kind stays as it is. No test reaches the network."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir

from .conftest import Zha, issue_of, manager_of, start_fix
from .integration import const
from .test_confirm_search import keyed, prepare
from .test_content import restart_with
from .test_self_check_flow import FINE, FIRST, OFF, UNPROVEN, notice

NAME = "z2m_tuya_a.py"


def old_notice(hass: HomeAssistant, issue: ir.IssueEntry, **changes: object) -> str:
    """Make the notice as v0.1.0 did: not fixable, data with the kind and the file (changes: other data). Its id."""
    data = {"kind": issue.translation_key, "file": issue.data["file"], **changes}
    ir.async_create_issue(
        hass, const.DOMAIN, issue.issue_id, data={key: value for key, value in data.items() if value is not None}, is_fixable=False, is_persistent=True,
        severity=ir.IssueSeverity.WARNING, translation_key=issue.translation_key, translation_placeholders=issue.translation_placeholders,
    )
    return issue.issue_id


def stored(hass: HomeAssistant, issue_id: str) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(const.DOMAIN, issue_id)


@pytest.mark.parametrize("kind", [FINE, UNPROVEN, OFF])
@pytest.mark.parametrize("ignored", [False, True])
async def test_an_old_notice_becomes_fixable_at_load(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, kind: str, ignored: bool):
    """The notice keeps its id, its texts and whether the user ignored it. Its data gets the revision and the devices
    of its self-check. The fix flow opens on it."""
    devices, _search = await notice(hass, zha, repo_dir, aioclient_mock, kind)
    issue = issue_of(hass, kind)
    issue_id = old_notice(hass, issue)
    if ignored:
        ir.async_ignore_issue(hass, const.DOMAIN, issue_id, True)
    before = stored(hass, issue_id)
    assert before is not None and not before.is_fixable
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    await restart_with(hass, entry, repo_dir)
    after = stored(hass, issue_id)
    assert after is not None and after.is_fixable and after.is_persistent
    assert after.data == {"kind": kind, "file": NAME, "revision": issue.data["revision"], "devices": [devices[0].device_id]}
    assert after.translation_key == kind and after.translation_placeholders == issue.translation_placeholders
    assert after.severity is ir.IssueSeverity.WARNING and after.created == before.created
    assert (after.dismissed_version is not None) is ignored
    if ignored:
        ir.async_ignore_issue(hass, const.DOMAIN, issue_id, False)
    flow = await start_fix(hass, stored(hass, issue_id))
    assert flow["type"] is FlowResultType.MENU and flow["step_id"] == FIRST and flow["menu_options"] == ["check", "skip"], flow
    # A second load leaves it as it is.
    await restart_with(hass, entry, repo_dir)
    again = stored(hass, issue_id)
    assert again is not None and again.is_fixable and again.data == after.data and again.created == before.created


@pytest.mark.parametrize("missing", ["self_check", "closed", "other_revision", "file", "placeholder", "link"])
async def test_an_old_notice_without_the_data_of_its_fix_flow_goes_at_load(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, missing: str):
    """No self-check of its file at its revision in the store (or one closed since), no file in its data, or a text
    without a placeholder its fix flow shows: the notice goes."""
    await notice(hass, zha, repo_dir, aioclient_mock, OFF)
    issue = issue_of(hass, OFF)
    observation = manager_of(hass).checks.observations[NAME]
    if missing == "self_check":
        del manager_of(hass).checks.observations[NAME]
    elif missing == "closed":
        observation["closed"] = True
    if missing == "other_revision":
        # A notice id of another revision of the file than the self-check's.
        other = dataclasses.replace(issue, issue_id=issue.issue_id.replace(observation["revision"], "0" * len(observation["revision"])))
        ir.async_delete_issue(hass, const.DOMAIN, issue.issue_id)
        issue_id = old_notice(hass, other)
    elif missing == "file":
        issue_id = old_notice(hass, issue, file=None)
    elif missing in ("placeholder", "link"):
        shown = dict(issue.translation_placeholders)
        del shown["devices" if missing == "placeholder" else "link"]
        issue_id = old_notice(hass, dataclasses.replace(issue, translation_placeholders=shown))
    else:
        issue_id = old_notice(hass, issue)
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    await restart_with(hass, entry, repo_dir)
    assert stored(hass, issue_id) is None


@pytest.mark.parametrize("kind", [FINE, UNPROVEN, OFF])
async def test_an_old_notice_of_a_file_verified_now(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock, kind: str):
    """The state in use calls the file verified now: a fine or unproven notice goes (nobody needs to test the file).
    "Looks off" stays and becomes fixable."""
    await notice(hass, zha, repo_dir, aioclient_mock, kind)
    issue_id = old_notice(hass, issue_of(hass, kind))
    prepare(repo_dir, keyed(tuya_a={"status": "verified"}), threshold=3)
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    await restart_with(hass, entry, repo_dir)
    after = stored(hass, issue_id)
    if kind == OFF:
        assert after is not None and after.is_fixable
    else:
        assert after is None


async def test_notices_of_other_kinds_stay_as_they_are(hass: HomeAssistant, zha: Zha, repo_dir: Path, aioclient_mock):
    """The migration touches only the self-check notices v0.1.0 made: neither a notice of another kind without a fix
    flow nor a self-check notice of this release."""
    await notice(hass, zha, repo_dir, aioclient_mock, FINE)
    current = issue_of(hass, FINE)
    ir.async_create_issue(
        hass, const.DOMAIN, "unavailable_z2m_tuya_a", data={"kind": "unavailable", "file": NAME}, is_fixable=False, is_persistent=False,
        severity=ir.IssueSeverity.WARNING, translation_key="unavailable", translation_placeholders={"quirk": "Tuya A", "file": NAME, "error": "broken", "reports": "https://example.com"},
    )
    other = stored(hass, "unavailable_z2m_tuya_a")
    manager_of(hass).checks.migrate_notices()
    assert stored(hass, "unavailable_z2m_tuya_a") == other
    assert stored(hass, current.issue_id) == current
