"""Fixtures of the loader tests: real zigpy, zha and zhaquirks, no mocked registry or builder."""

import importlib.util
import pathlib
import sys
import warnings

import pytest

warnings.simplefilter("ignore", DeprecationWarning)

TESTS = pathlib.Path(__file__).resolve().parent
ROOT = TESTS.parent
(FOLDER,) = [path.parent for path in (ROOT / "custom_components").glob("*/manifest.json")]
sys.path.insert(0, str(TESTS))

import zhaquirks  # noqa: E402

# zhaquirks registers its own quirks once, when it imports its modules: the snapshot every test restores
# (DEVICE_REGISTRY.preserve_state) must hold them, so the import comes first.
zhaquirks.setup()

# The loader as a module of its own, as the stub loads it: `import loader` in the tests.
_spec = importlib.util.spec_from_file_location("loader", FOLDER / "loader.py")
sys.modules["loader"] = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sys.modules["loader"])

import helpers  # noqa: E402


def pytest_sessionfinish(session, exitstatus):
    helpers.close_loop()


@pytest.fixture
def registry():
    """ZHA's registry, restored after the test."""
    from zha.quirks import DEVICE_REGISTRY

    with DEVICE_REGISTRY.preserve_state():
        yield DEVICE_REGISTRY


def _custom_modules():
    return [n for n in sys.modules if n.split(".")[0] in ("custom_components", "xquirks", "xquirks_loader") or "_user_" in n]


@pytest.fixture
def installation(tmp_path, monkeypatch, registry):
    """A config folder with the stub in custom_zha_quirks and the loader in custom_components/xquirks."""
    for name in _custom_modules():
        monkeypatch.delitem(sys.modules, name)
    install = helpers.Installation(tmp_path)
    monkeypatch.syspath_prepend(str(install.config))
    yield install
    # The v1 registry finds a quirk's file through its module: purge it before the modules go.
    zhaquirks.legacy.DEVICE_REGISTRY.purge_custom_quirks(install.quirks)
    for name in _custom_modules():
        sys.modules.pop(name, None)
