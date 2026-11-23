"""Home Assistant's event loop: nothing the loader runs there touches the file system.

ZHA imports the custom quirks in an executor job (zha/application/gateway.py, Gateway.async_from_config): the stub,
the loader's own import and register() read files there. zigpy resolves devices in the event loop: every device of
its database at startup (zigpy/appdb.py, PersistingListener.load, for Home Assistant's early trigger cache and for the
gateway) and each device that joins or is interviewed again (zigpy/application.py, device_initialized). There the
loader's filters run, a definition is built at its first device (OnDemand) and ZHA runs the built transforms and
factory. Home Assistant reports the calls of homeassistant/block_async_io.py made in the event loop. The tests patch
those functions and the file calls below them that it cannot see: linecache reads source files after os.stat through
tokenize.open, which holds its own open. Path.resolve calls os.lstat. A test fails on any of these calls made in a
thread while it runs an event loop.
"""

import asyncio
import builtins
import glob
import http.client
import importlib
import inspect
import linecache
import logging
import os
import pathlib
import ssl
import sys
import time
import tokenize

import pytest
from zha.quirks import DEVICE_REGISTRY, QUIRK_REGISTRY_ENTRY_ATTR

from helpers import SAMPLE_TEXT, build_zha_device, make_raw
from test_broken import AT_LOAD, BROKEN
from test_on_demand import holders, sample_devices
from test_precedence import CASES, v2_file
from test_take_over import LABELS, ZBMINIR2_CHANGES, run_copy, swap

# homeassistant/block_async_io.py of Home Assistant 2026.11.2: what it reports in the event loop.
BLOCKING = (
    (builtins, "open"), (pathlib.Path, "open"), (pathlib.Path, "read_text"), (pathlib.Path, "read_bytes"), (pathlib.Path, "write_text"),
    (pathlib.Path, "write_bytes"), (os, "listdir"), (os, "scandir"), (os, "walk"), (glob, "glob"), (glob, "iglob"), (importlib, "import_module"),
    (time, "sleep"), (http.client.HTTPConnection, "putrequest"), (ssl.SSLContext, "load_default_certs"), (ssl.SSLContext, "load_verify_locations"),
    (ssl.SSLContext, "load_cert_chain"), (ssl.SSLContext, "set_default_verify_paths"),
)
BELOW = ((tokenize, "open"), (os, "stat"), (os, "lstat"))


def loop_running() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def callers() -> list:
    """file:line function of the callers, innermost first, read from the frames (linecache would call os.stat)."""
    out, frame = [], sys._getframe(2)
    while frame is not None and len(out) < 12:
        out.append(f"{frame.f_code.co_filename}:{frame.f_lineno} {frame.f_code.co_name}")
        frame = frame.f_back
    return out


@pytest.fixture
def blocking(monkeypatch) -> list:
    """The calls of BLOCKING and BELOW made in a thread that runs an event loop: (function, its callers)."""
    calls = []

    def guard(owner, name):
        original = getattr(owner, name)

        def guarded(*args, **kwargs):
            # As Home Assistant: a module already imported does not count.
            if loop_running() and not (name == "import_module" and args and args[0] in sys.modules):
                calls.append((f"{owner.__name__}.{name}", callers()))
            return original(*args, **kwargs)

        monkeypatch.setattr(owner, name, guarded)

    for owner, name in BLOCKING + BELOW:
        guard(owner, name)
    return calls


def report(calls) -> str:
    return "\n".join(f"{call} from\n  " + "\n  ".join(frames) for call, frames in calls)


def run_as_zha(installation, raws) -> list:
    """ZHA's order: the custom quirks imported in an executor job, then in the event loop each device resolved and its
    ZHA device built with its entities. The resolved devices."""

    async def run():
        await asyncio.get_running_loop().run_in_executor(None, installation.setup)
        resolved = [DEVICE_REGISTRY.resolve(raw) for raw in raws]
        for device in resolved:
            build_zha_device(device)
        return resolved

    return asyncio.run(run())


def test_the_guard_sees_what_home_assistant_reports(blocking, tmp_path):
    path = tmp_path / "file.txt"
    path.write_text("x")

    async def run():
        await asyncio.get_running_loop().run_in_executor(None, path.read_text)
        assert not blocking  # an executor job may read files
        path.read_bytes()
        os.listdir(tmp_path)
        linecache.clearcache()
        inspect.getsource(run_as_zha)

    asyncio.run(run())
    assert {call for call, _ in blocking} >= {"Path.read_bytes", "os.listdir", "os.stat", "tokenize.open"}


def test_matching_resolving_and_building_stay_off_the_file_system(installation, registry, tmp_path, blocking, caplog):
    """The sample with the broken definitions, a newer ZHA quirk under a take-over and a user's quirk checked by a
    definition's filter: each device resolved, each definition built at its first device, in the event loop."""
    caplog.set_level(logging.INFO, logger="xquirks")
    installation.definitions(SAMPLE_TEXT + "\n".join(BROKEN.values()) + "\n")
    installation.options(high_risk=True)
    ident, manufacturer, model = CASES["exact"]
    user = installation.user_file("a_user_v2.py", v2_file(manufacturer, model))  # before the stub: the filter meets it
    _, (newer,) = run_copy(tmp_path, registry, "sonoff/zbminir2.py", ZBMINIR2_CHANGES["switch values"])
    swap(registry, LABELS["sonoff_zbminir2"], newer)
    raws = [make_raw(consts, m, mo) for _, consts, m, mo in sample_devices()]
    resolved = run_as_zha(installation, raws)
    assert not blocking, report(blocking)
    # The paths ran in the event loop: builds, failed builds, a take-over kept and one stepped back, a user's quirk.
    states = {label.removeprefix("xquirks:").split()[0]: holder.state for label, holder in holders(registry).items()}
    assert {i for i, s in states.items() if s == "failed"} == set(BROKEN) - set(AT_LOAD)
    assert states["sonoff_swv"] == states["shelly_sbdw_103c"] == states["sonoff_zbmicro"] == "built"
    labels = {getattr(d, QUIRK_REGISTRY_ENTRY_ATTR).source.label.split()[0] for d in resolved if hasattr(d, QUIRK_REGISTRY_ENTRY_ATTR)}
    assert {"xquirks:sonoff_swv", "xquirks:shelly_sbdw_103c"} <= labels
    text = caplog.text
    assert "sonoff_zbminir2 steps back" in text and "is not the one this take-over was built over" in text
    assert f"{ident} steps back" in text and f"({user}) matches it" in text
