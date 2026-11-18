"""The self-test of the typed Tuya cluster inside Home Assistant (typed_tuya.py).

The fixed method every Tuya file holds reads zhaquirks' internals. The guard and test_typed_tuya.py know the pinned
zhaquirks only: once per run the self-test feeds one STRING and one VALUE datapoint through the fixed class on the
zhaquirks ZHA loaded. While it fails, no Tuya file is installed or updated and one notice names the cause; it never
passes what it could not see work."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant

from .conftest import MANIFEST, Zha, apply_list, issue_of, issues_of, listed, manager_of, setup_entry, write_repo
from .integration import diagnostics, guard, manager, text, typed_tuya
from .test_content import restart_with
from .test_guard_files import FROZEN, TUYA, frozen

SOURCE = guard.Guard.load().fixed.typed_tuya_source
IEEE_T = "00:11:22:33:44:55:77:01"
IEEE_C = "00:11:22:33:44:55:77:02"
FAILURE = "a VALUE datapoint of 50 was not stored (the attribute holds None): every datapoint of a Tuya file would be skipped"


@pytest.fixture
def zhaquirks_tuya():
    """The modules the self-test reads, loaded as ZHA loads them when it starts with its quirks: zhaquirks' Tuya base."""
    import zhaquirks.tuya.builder  # noqa: F401
    import zhaquirks.tuya.mcu  # noqa: F401
    import zigpy.device  # noqa: F401
    import zigpy.zcl.foundation  # noqa: F401
    from zhaquirks.tuya import TuyaNewManufCluster

    return TuyaNewManufCluster


def test_it_passes_on_the_zhaquirks_home_assistant_pins_and_leaves_no_trace(zhaquirks_tuya, caplog: pytest.LogCaptureFixture):
    import zigpy.zcl
    from zha.quirks import DEVICE_REGISTRY

    def registries():
        return (dict(zigpy.zcl.Cluster._registry), list(zigpy.zcl.Cluster._registry_range),
                {model: list(entries) for model, entries in DEVICE_REGISTRY._registry.items()}, list(DEVICE_REGISTRY._wildcard_registry))

    before = registries()
    caplog.set_level(logging.DEBUG)
    assert typed_tuya.self_test(SOURCE) == ""
    # No cluster class reaches zigpy's registry, no quirk ZHA's (the builder registers nothing), the refused datapoint is no
    # warning at every start.
    assert registries() == before
    assert [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING] == []


def test_a_zhaquirks_that_renames_its_datapoint_table_fails_it(zhaquirks_tuya, monkeypatch: pytest.MonkeyPatch):
    """A stand-in release: zhaquirks keeps the table under another name and its own method reads that name. zhaquirks
    alone works; the fixed method would skip every datapoint of every Tuya file."""
    cluster = zhaquirks_tuya
    init, update = cluster.__init__, cluster._dp_2_attr_update

    def renamed_init(self, *args, **kwargs):
        init(self, *args, **kwargs)
        self._dp_mappings = self.__dict__.pop("_dp_to_attributes")

    def own_update(self, datapoint):
        self._dp_to_attributes = self._dp_mappings
        try:
            return update(self, datapoint)
        finally:
            del self._dp_to_attributes

    monkeypatch.setattr(cluster, "__init__", renamed_init)
    monkeypatch.setattr(cluster, "_dp_2_attr_update", own_update)
    assert typed_tuya.self_test(SOURCE) == FAILURE


def converting_on_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """A zigpy that converts each value to its attribute's type as it stores it (uint32_t(-50) raises there)."""
    import zigpy.zcl.helpers

    original = zigpy.zcl.helpers.AttributeCache.set_value
    monkeypatch.setattr(zigpy.zcl.helpers.AttributeCache, "set_value", lambda self, attr_def, value, **kwargs: original(self, attr_def, attr_def.type(value), **kwargs))


def decoding_unsigned(monkeypatch: pytest.MonkeyPatch) -> None:
    """A zhaquirks that decodes a VALUE datapoint unsigned."""
    import zigpy.types as t
    from zhaquirks.tuya import TuyaData, TuyaDPType

    original = TuyaData.payload
    monkeypatch.setattr(TuyaData, "payload", property(lambda self: t.uint32_t.from_bytes(self.raw, "big") if self.dp_type == TuyaDPType.VALUE else original.fget(self)))


@pytest.mark.parametrize("release", [converting_on_store, decoding_unsigned], ids=["zigpy converts on store", "zhaquirks decodes VALUE unsigned"])
def test_how_zigpy_stores_or_zhaquirks_decodes_a_value_does_not_fail_it(zhaquirks_tuya, monkeypatch: pytest.MonkeyPatch, release):
    """The self-test checks the fixed class, not zigpy's storage. Either release would lose a negative reading on
    an attribute typed unsigned without the typed class as well, it must not stop every Tuya file. The method's rule for such
    a reading (stored on the Tuya cluster itself) is pinned on the method alone in test_typed_tuya.py."""
    release(monkeypatch)
    assert typed_tuya.self_test(SOURCE) == ""


def test_a_zhaquirks_whose_chain_resolves_to_another_object_than_the_cluster_fails_it(zhaquirks_tuya, monkeypatch: pytest.MonkeyPatch):
    """The chain's attribute resolves to an object that is not the Tuya cluster itself (here one that forwards to
    it, so zhaquirks' own update still stores 50 there). The method then holds the attribute to its ZCL range and would refuse
    every negative reading of a sensor typed unsigned: the self-test hands it -50 itself and fails."""
    import zigpy.endpoint

    class Forward:
        def __init__(self, cluster) -> None:
            self.__dict__["cluster"] = cluster

        def __getattr__(self, name: str):
            return getattr(self.cluster, name)

    lookup = zigpy.endpoint.Endpoint.__getattr__
    monkeypatch.setattr(zigpy.endpoint.Endpoint, "__getattr__", lambda self, name: Forward(found) if isinstance(found := lookup(self, name), zhaquirks_tuya) else found)
    assert typed_tuya.self_test(SOURCE).startswith("a VALUE datapoint of -50 for an unsigned attribute was refused (ValueError: TypeError(")
    assert typed_tuya.self_test(SOURCE).endswith("): negative readings would be skipped")


def test_a_zhaquirks_whose_frame_handler_does_not_skip_a_refused_datapoint_fails_it(zhaquirks_tuya, monkeypatch: pytest.MonkeyPatch):
    """zhaquirks before 2.4.0 (#5502): a ValueError of one datapoint ended the whole frame."""

    def handle_get_data(self, command):
        for record in command.datapoints:
            getattr(self, self.data_point_handlers[record.dp])(record)
        return 0

    monkeypatch.setattr(zhaquirks_tuya, "handle_get_data", handle_get_data)
    assert typed_tuya.self_test(SOURCE).startswith("zhaquirks' handle_get_data did not skip the refused datapoint: ValueError")


def test_a_zhaquirks_that_passes_by_the_typed_method_fails_it(zhaquirks_tuya, monkeypatch: pytest.MonkeyPatch):
    """A release that hands each datapoint to its own method by another way: the text reaches the number."""
    base = zhaquirks_tuya._dp_2_attr_update

    def handle_get_data(self, command):
        for record in command.datapoints:
            base(self, record)
        return 0

    monkeypatch.setattr(zhaquirks_tuya, "handle_get_data", handle_get_data)
    assert typed_tuya.self_test(SOURCE) == "a STRING datapoint for an attribute of a number type was stored ('x'), not refused"


def ignoring_the_class(original):
    """A release whose add_to_registry builds the Tuya cluster on its own class, whatever it is handed."""

    def add_to_registry(self, replacement_cluster=None, *args, **kwargs):
        from zhaquirks.tuya.mcu import TuyaMCUCluster

        return original(self, TuyaMCUCluster, *args, **kwargs)

    return add_to_registry


def renaming_the_handlers(original):
    """A release whose add_to_registry hands each datapoint to a method of another name."""

    def add_to_registry(self, *args, **kwargs):
        self.tuya_data_point_handlers = dict.fromkeys(self.tuya_data_point_handlers, "_dp_to_attributes_update")
        return original(self, *args, **kwargs)

    return add_to_registry


@pytest.mark.parametrize(("release", "failure"), [
    (ignoring_the_class, "zhaquirks' TuyaQuirkBuilder.add_to_registry does not build the device's Tuya cluster on the class it is handed (<class "),
    (renaming_the_handlers, "a VALUE datapoint of 50 was not stored (the attribute holds None)"),
], ids=["ignores replacement_cluster", "renames the datapoint handlers"])
def test_a_builder_that_registers_the_cluster_otherwise_fails_it(zhaquirks_tuya, monkeypatch: pytest.MonkeyPatch, release, failure):
    """The self-test takes the device's Tuya cluster from zhaquirks' TuyaQuirkBuilder.add_to_registry, as a file
    gets it: a release that builds it otherwise there fails it, also when the builder's methods look as before."""
    from zhaquirks.tuya.builder import TuyaQuirkBuilder

    monkeypatch.setattr(TuyaQuirkBuilder, "add_to_registry", release(TuyaQuirkBuilder.add_to_registry))
    assert typed_tuya.self_test(SOURCE).startswith(failure)


def test_a_builder_that_names_another_datapoint_method_fails_it(zhaquirks_tuya, monkeypatch: pytest.MonkeyPatch):
    from zhaquirks.tuya.builder import TuyaQuirkBuilder

    def tuya_dp_multi(self, dp_id: int, attribute_mapping: list, dp_handler: str = "_dp_to_attributes_update"):
        return self

    monkeypatch.setattr(TuyaQuirkBuilder, "tuya_dp_multi", tuya_dp_multi)
    assert typed_tuya.self_test(SOURCE) == "zhaquirks' TuyaQuirkBuilder.tuya_dp_multi hands each datapoint to '_dp_to_attributes_update', not to _dp_2_attr_update"


def test_a_self_test_that_cannot_run_fails_and_a_module_zha_has_not_loaded_gives_no_verdict(zhaquirks_tuya, monkeypatch: pytest.MonkeyPatch):
    import zigpy.device

    assert typed_tuya.self_test(None) == "this release's fixed code has no class with _dp_2_attr_update"

    def device(*args, **kwargs):
        raise AttributeError("'_Radio' object has no attribute 'new_listener'")

    with monkeypatch.context() as patched:
        patched.setattr(zigpy.device, "Device", device)
        assert typed_tuya.self_test(SOURCE) == "the self-test could not run: AttributeError: '_Radio' object has no attribute 'new_listener'"
    monkeypatch.delitem(sys.modules, "zhaquirks.tuya.builder")
    with pytest.raises(typed_tuya.NotLoaded):
        typed_tuya.self_test(SOURCE)


def test_the_self_test_cuts_an_error_like_the_class_check():
    """One helper turns an error of code outside this integration into a reason, for both checks inside Home Assistant."""
    import importlib

    from .integration import PACKAGE

    inherit = importlib.import_module(f"{PACKAGE}.inherit")
    assert typed_tuya.error_text is inherit.error_text
    assert inherit.error_text(TimeoutError()) == "TimeoutError" and inherit.error_text(ValueError("x" * 500)).endswith("...")
    assert len(inherit.error_text(ValueError("x" * 500))) == inherit.MAX_REASON


def test_only_a_file_with_the_typed_class_waits_for_the_self_test():
    """verdict_of: a file that holds the typed Tuya cluster gets no verdict before the self-test ran (it waits, never
    passes unchecked), TUYA_STOPPED while it fails; a file without the class does not depend on it."""
    targets = guard.Targets.of(FROZEN[TUYA])
    content = frozen(TUYA).encode()
    assert typed_tuya.holds_typed_class(content) and not typed_tuya.holds_typed_class(b"x = 1\n") and not typed_tuya.holds_typed_class(b"def (")
    with pytest.raises(typed_tuya.NotLoaded):
        manager.verdict_of(guard.Guard.load(), content, targets)
    assert manager.verdict_of(guard.Guard.load(), content, targets, FAILURE) == (manager.TUYA_STOPPED, FAILURE)
    assert manager.verdict_of(guard.Guard.load(), content, targets, "") == ()
    from .test_guard_files import ZCL

    assert manager.verdict_of(guard.Guard.load(), frozen(ZCL).encode(), guard.Targets.of(FROZEN[ZCL]), None) == ()


def tuya_repo(repo_dir: Path) -> None:
    """The repository with the frozen Tuya file (it holds the typed class) next to the stand-in files."""
    entry = {key: value for key, value in FROZEN[TUYA].items() if key not in ("sha256", "revision", "picture", "picture_sha256")} | {"risk": "low", "risk_why": []}
    write_repo(repo_dir, {**MANIFEST, "tuya_ts0105": entry})
    (repo_dir / entry["file"]).write_text(frozen(TUYA))


async def test_while_it_fails_no_tuya_file_is_installed_and_one_notice_says_why(hass: HomeAssistant, zha: Zha, repo_dir: Path, zhaquirks_tuya,
                                                                                monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.DEBUG, logger=manager.__name__)
    tuya_repo(repo_dir)
    zha.add_device(IEEE_T, "Tuya", "TS0105", "Switch")
    zha.add_device(IEEE_C, "Acme", "C-1", "Plug")
    monkeypatch.setattr(typed_tuya, "self_test", lambda source: FAILURE)
    entry = await setup_entry(hass, repo_dir)
    # The other files come as before; the Tuya file is not written, and no security notice blames it.
    assert listed(hass)["install"] == ["z2m_acme_c.py"]
    assert issues_of(hass, "refused") == []
    issue = issue_of(hass, manager.TUYA_CHECK)
    assert issue.severity == "error" and issue.translation_placeholders == {"reason": FAILURE, "files": "- `z2m_tuya_ts0105.py` (Tuya TS0105: 3 gang switch)",
                                                                            "reports": text.reports_url(text.TUYA_REPORTS)}
    errors = [record.getMessage() for record in caplog.records if record.levelno == logging.ERROR and "self-test" in record.getMessage()]
    assert errors == [f"The typed Tuya cluster of the Tuya quirk files fails its self-test on this zhaquirks, so no Tuya file is installed or updated: {FAILURE}"]
    # The one error says it: no warning per file in the words of the guard's security refusals.
    assert [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING and record.name.startswith(manager.__name__)] == errors
    assert any(record.levelno == logging.DEBUG and record.getMessage().startswith("Not writing z2m_tuya_ts0105.py of ") for record in caplog.records)
    found = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
    assert found["typed_tuya_self_test"] == FAILURE and found["not_written_while_the_tuya_self_test_fails"] == ["z2m_tuya_ts0105.py"]
    # Once per run: the next refresh does not test or log again.
    await manager_of(hass).async_refresh()
    assert len([record for record in caplog.records if record.levelno == logging.ERROR and "self-test" in record.getMessage()]) == 1


async def test_an_installed_tuya_file_stays_and_the_notice_names_it(hass: HomeAssistant, zha: Zha, repo_dir: Path, zhaquirks_tuya, monkeypatch: pytest.MonkeyPatch):
    """The real self-test passes: the Tuya file is installed. After a start on a zhaquirks it fails on, the file stays where
    it is (no update, no removal in the change list) and the notice lists it."""
    tuya_repo(repo_dir)
    zha.add_device(IEEE_T, "Tuya", "TS0105", "Switch")
    entry = await setup_entry(hass, repo_dir)
    assert manager_of(hass).tuya_check == ""
    assert listed(hass)["install"] == ["z2m_tuya_ts0105.py"]
    await apply_list(hass)
    assert issues_of(hass, manager.TUYA_CHECK) == []
    monkeypatch.setattr(typed_tuya, "self_test", lambda source: FAILURE)
    await restart_with(hass, entry, repo_dir)
    assert listed(hass)["remove"] == [] and listed(hass)["update"] == []
    assert issue_of(hass, manager.TUYA_CHECK).translation_placeholders["files"] == "- `z2m_tuya_ts0105.py` (Tuya TS0105: 3 gang switch)"
    # The self-test passes again (a fixed release): the notice goes.
    monkeypatch.setattr(typed_tuya, "self_test", lambda source: "")
    await restart_with(hass, entry, repo_dir)
    assert issues_of(hass, manager.TUYA_CHECK) == []


# Sources the parser cannot take: MemoryError ("Parser stack overflowed") and RecursionError.
TOO_DEEP = ("x = " + "-" * 200000 + "1\n", "x = " + "1+" * 200000 + "1\n")


@pytest.mark.parametrize("source", TOO_DEEP, ids=["parser stack", "recursion"])
def test_a_file_the_parser_cannot_take_holds_no_typed_class(source: str):
    assert typed_tuya.holds_typed_class(source.encode()) is False


@pytest.mark.parametrize("source", TOO_DEEP, ids=["parser stack", "recursion"])
async def test_an_installed_tuya_file_edited_beyond_the_parser_stops_nothing(hass: HomeAssistant, zha: Zha, repo_dir: Path, quirks_dir: Path,
                                                                            zhaquirks_tuya, monkeypatch: pytest.MonkeyPatch, source: str):
    """While the self-test fails, the notice reads the installed files of ours: one the user edited into source the parser
    cannot take is the user's file, the refreshes and the change list go on."""
    tuya_repo(repo_dir)
    zha.add_device(IEEE_T, "Tuya", "TS0105", "Switch")
    entry = await setup_entry(hass, repo_dir)
    await apply_list(hass)
    (quirks_dir / "z2m_tuya_ts0105.py").write_text(source)
    zha.add_device(IEEE_C, "Acme", "C-1", "Plug")
    monkeypatch.setattr(typed_tuya, "self_test", lambda source: FAILURE)
    await restart_with(hass, entry, repo_dir)
    await manager_of(hass).async_refresh()
    assert [issue.translation_placeholders["file"] for issue in issues_of(hass, "changed")] == ["z2m_tuya_ts0105.py"]
    assert issues_of(hass, manager.TUYA_CHECK) == []
    assert listed(hass)["install"] == ["z2m_acme_c.py"]
    await apply_list(hass)
    assert (quirks_dir / "z2m_acme_c.py").exists()


async def test_without_a_tuya_file_a_failing_self_test_shows_nothing(hass: HomeAssistant, zha: Zha, repo_dir: Path, monkeypatch: pytest.MonkeyPatch):
    zha.add_device(IEEE_C, "Acme", "C-1", "Plug")
    monkeypatch.setattr(typed_tuya, "self_test", lambda source: FAILURE)
    await setup_entry(hass, repo_dir)
    assert listed(hass)["install"] == ["z2m_acme_c.py"] and issues_of(hass, manager.TUYA_CHECK) == []
