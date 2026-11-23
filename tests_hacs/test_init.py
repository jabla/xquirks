"""Setup: the repair issues of the custom quirks folder and their fix flows, the removal of the entry."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import async_fire_time_changed

from .conftest import STRINGS, Zha, continue_fix, label, our_issues, render, setup_entry, start_fix
from .integration import FOLDER, const, diagnostics, install

IEEE = "00:11:22:33:44:55:66:01"


async def test_with_the_stub_in_place_there_is_no_issue(hass: HomeAssistant, zha: Zha, quirks_dir: Path):
    await install.async_install(hass, quirks_dir)
    await setup_entry(hass)
    assert our_issues(hass) == {}


async def test_a_missing_stub_is_written_again_by_its_fix(hass: HomeAssistant, zha: Zha, quirks_dir: Path):
    await setup_entry(hass)
    assert list(our_issues(hass)) == [const.STUB_ISSUE] and our_issues(hass)[const.STUB_ISSUE].is_fixable
    flow = await start_fix(hass, const.STUB_ISSUE)
    assert flow["type"] is FlowResultType.FORM and flow["description_placeholders"] == {"folder": str(quirks_dir)}
    assert not (quirks_dir / "xquirks.py").exists()
    flow = await continue_fix(hass, flow, const.STUB_ISSUE, {})
    assert flow["type"] is FlowResultType.CREATE_ENTRY
    assert (quirks_dir / "xquirks.py").read_bytes() == (FOLDER / "stub.py.txt").read_bytes()
    assert zha.reloads == 1 and our_issues(hass) == {}
    # ZHA back: the stub is there, no issue.
    await zha.reload()
    assert our_issues(hass) == {}


async def test_a_stub_of_another_text_is_written_again_by_the_same_fix(hass: HomeAssistant, zha: Zha, quirks_dir: Path):
    """An older release's stub: the fix asks before it writes."""
    (quirks_dir / "xquirks.py").write_text('"""xquirks: an older loader (written by the xquirks integration, do not edit)."""\n')
    await setup_entry(hass)
    assert list(our_issues(hass)) == [const.STUB_ISSUE]
    await continue_fix(hass, await start_fix(hass, const.STUB_ISSUE), const.STUB_ISSUE, {})
    assert (quirks_dir / "xquirks.py").read_bytes() == (FOLDER / "stub.py.txt").read_bytes()
    assert zha.reloads == 1 and our_issues(hass) == {}


async def test_a_file_of_the_users_named_like_the_stub_stays(hass: HomeAssistant, zha: Zha, quirks_dir: Path):
    """The fix says why it writes nothing."""
    (quirks_dir / "xquirks.py").write_text("# a quirk of the user's\n")
    await setup_entry(hass)
    assert list(our_issues(hass)) == [const.STUB_ISSUE]
    flow = await continue_fix(hass, await start_fix(hass, const.STUB_ISSUE), const.STUB_ISSUE, {})
    assert flow["reason"] == "write_failed" and "Not written by xquirks, rename or delete it" in flow["description_placeholders"]["error"]
    assert [path.name for path in quirks_dir.iterdir()] == ["xquirks.py"] and (quirks_dir / "xquirks.py").read_text() == "# a quirk of the user's\n"
    assert list(our_issues(hass)) == [const.STUB_ISSUE] and zha.reloads == 0


async def test_a_broken_options_file_is_an_issue_and_stays_as_it_is(hass: HomeAssistant, zha: Zha, quirks_dir: Path):
    """The loader takes the defaults then: the devices left alone get definitions. The stub's fix writes nothing."""
    broken = f'{{"excluded": ["{IEEE}",]}}\n'
    (quirks_dir / "xquirks_options.json").write_text(broken)
    entry = await setup_entry(hass)
    assert set(our_issues(hass)) == {const.OPTIONS_ISSUE, const.STUB_ISSUE} and not our_issues(hass)[const.OPTIONS_ISSUE].is_fixable
    shown = render(STRINGS["issues"][const.OPTIONS_ISSUE]["description"], our_issues(hass)[const.OPTIONS_ISSUE].translation_placeholders)
    # Where the file is and where in it the error is.
    assert str(quirks_dir / "xquirks_options.json") in shown and "line 1 column 40" in shown
    flow = await continue_fix(hass, await start_fix(hass, const.STUB_ISSUE), const.STUB_ISSUE, {})
    assert flow["reason"] == "write_failed" and "xquirks_options.json" in flow["description_placeholders"]["error"]
    assert [path.name for path in quirks_dir.iterdir()] == ["xquirks_options.json"] and (quirks_dir / "xquirks_options.json").read_text() == broken
    assert (await diagnostics.async_get_config_entry_diagnostics(hass, entry))["options"] == "unreadable"
    # Fixed: the issue goes at the next ZHA start.
    (quirks_dir / "xquirks_options.json").write_text(f'{{"excluded": ["{IEEE}"]}}\n')
    await zha.reload()
    assert list(our_issues(hass)) == [const.STUB_ISSUE]


async def test_the_issues_follow_the_folder_at_each_zha_start(hass: HomeAssistant, zha: Zha, quirks_dir: Path):
    await install.async_install(hass, quirks_dir)
    await setup_entry(hass)
    (quirks_dir / "xquirks.py").unlink()
    await zha.reload()
    assert list(our_issues(hass)) == [const.STUB_ISSUE]
    await install.async_install(hass, quirks_dir)
    await zha.reload()
    assert our_issues(hass) == {}


async def test_without_a_folder_only_that_issue_shows(hass: HomeAssistant, zha: Zha):
    zha.data.yaml_config.pop("custom_quirks_path")
    await setup_entry(hass)
    assert list(our_issues(hass)) == [const.NO_FOLDER] and not our_issues(hass)[const.NO_FOLDER].is_fixable


async def test_with_quirks_off_in_zha_that_issue_shows(hass: HomeAssistant, zha: Zha, quirks_dir: Path):
    """ZHA imports no custom quirk then: no definition loads."""
    await install.async_install(hass, quirks_dir)
    zha.data.yaml_config["enable_quirks"] = False
    await setup_entry(hass)
    assert list(our_issues(hass)) == [const.QUIRKS_OFF] and not our_issues(hass)[const.QUIRKS_OFF].is_fixable
    # ZHA's config comes with a restart of Home Assistant: here with the next ZHA start.
    zha.data.yaml_config["enable_quirks"] = True
    await zha.reload()
    assert our_issues(hass) == {}


async def test_a_failed_write_keeps_the_issue(hass: HomeAssistant, zha: Zha, quirks_dir: Path):
    await setup_entry(hass)
    flow = await start_fix(hass, const.STUB_ISSUE)
    quirks_dir.rmdir()
    flow = await continue_fix(hass, flow, const.STUB_ISSUE, {})
    assert flow["reason"] == "write_failed" and "No such file or directory" in flow["description_placeholders"]["error"]
    assert list(our_issues(hass)) == [const.STUB_ISSUE] and zha.reloads == 0


async def test_removing_the_entry_takes_the_stub_the_options_and_the_records(hass: HomeAssistant, zha: Zha, quirks_dir: Path, hass_storage: dict[str, Any]):
    zha.add_device(IEEE, name="Radar", quirk=label("tuya_a"))
    (quirks_dir / "z2m_own.py").write_text("# a quirk of the user's\n")
    await install.async_install(hass, quirks_dir)
    entry = await setup_entry(hass)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5))
    await hass.async_block_till_done()
    assert hass_storage["xquirks.labels"]["data"]["labels"] == {IEEE: "xquirks:tuya_a 1a2b3c4d"}
    # The notice belongs to the entry: it goes with it.
    assert list(our_issues(hass)) == [const.CHANGED] and our_issues(hass)[const.CHANGED].config_entry_id == entry.entry_id
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert [path.name for path in quirks_dir.iterdir()] == ["z2m_own.py"]
    assert our_issues(hass) == {} and "xquirks.labels" not in hass_storage
