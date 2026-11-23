"""The files xquirks keeps in ZHA's custom quirks folder: the stub xquirks.py, which loads the definitions when ZHA
imports its custom quirks, and xquirks_options.json beside it, which the loader reads then. File I/O runs in the
executor."""

from __future__ import annotations

import contextlib
import errno
import json
import os
import pathlib
import tempfile
from collections.abc import Iterable

from homeassistant.core import HomeAssistant

from .const import CONF_CUSTOM_QUIRKS_PATH, CONF_ENABLE_QUIRKS, ZHA_DOMAIN

STUB_FILE = "xquirks.py"
OPTIONS_FILE = "xquirks_options.json"
# The stub's text: a .py file in this folder could be imported as a module of the integration.
STUB = pathlib.Path(__file__).with_name("stub.py.txt")
# The first line of every release's stub has it: a file named xquirks.py without it is the user's and stays.
STUB_MARK = b"written by the xquirks integration"


def custom_quirks_dir(hass: HomeAssistant) -> pathlib.Path | None:
    """ZHA's custom quirks folder (custom_quirks_path of its YAML config), None when it has none."""
    try:
        path = hass.data[ZHA_DOMAIN].yaml_config.get(CONF_CUSTOM_QUIRKS_PATH)
    except (AttributeError, KeyError):
        return None
    # ZHA hands the value to pathlib as it is: a relative path starts at the working directory.
    return pathlib.Path(os.path.abspath(path)) if path else None


def quirks_enabled(hass: HomeAssistant) -> bool:
    """Whether ZHA loads quirks (enable_quirks of its YAML config, on by default). Off, it imports no custom quirk."""
    try:
        return hass.data[ZHA_DOMAIN].yaml_config.get(CONF_ENABLE_QUIRKS, True) is not False
    except (AttributeError, KeyError):
        return True


def read_options(quirks_dir: pathlib.Path) -> tuple[list[str], bool]:
    """The excluded IEEE addresses and the high-risk opt-in, the defaults without the file. OSError for a file that
    cannot be read or that the loader does not use (it takes the defaults then): nothing may be written over it."""
    path = quirks_dir / OPTIONS_FILE
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return [], False
    try:
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        excluded = data.get("excluded", [])
        if not isinstance(excluded, list) or not all(isinstance(ieee, str) for ieee in excluded):
            raise ValueError("excluded is not a list of IEEE addresses")
    except ValueError as err:
        raise OSError(errno.EINVAL, f"Broken ({err})", str(path)) from err
    return sorted({ieee.lower() for ieee in excluded}), data.get("high_risk", False) is True


def _write(path: pathlib.Path, data: bytes) -> None:
    """The file written in one step and on disk when this returns: a temporary file beside it (no .py, ZHA imports
    none), synced, renamed, then the folder synced."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".xquirks-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as file:
            os.fchmod(file.fileno(), 0o644)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _sync_folder(path.parent)


def _sync_folder(folder: pathlib.Path) -> None:
    """The rename on disk. Some file systems cannot sync a folder: the file is written all the same."""
    with contextlib.suppress(OSError):
        fd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _save(quirks_dir: pathlib.Path, excluded: set[str], high_risk: bool) -> None:
    _write(quirks_dir / OPTIONS_FILE, (json.dumps({"excluded": sorted(excluded), "high_risk": high_risk}, indent=2) + "\n").encode())


def _ours(path: pathlib.Path) -> bool:
    """Whether the stub may go to path: nothing there, or a stub of any release."""
    try:
        with path.open("rb") as file:
            return STUB_MARK in file.readline(512)
    except FileNotFoundError:
        return True


def _install(quirks_dir: pathlib.Path) -> None:
    stub = quirks_dir / STUB_FILE
    if not _ours(stub):
        raise FileExistsError(errno.EEXIST, "Not written by xquirks, rename or delete it", str(stub))
    # The options first: the stub never loads without the devices to leave alone.
    excluded, high_risk = read_options(quirks_dir)
    _save(quirks_dir, set(excluded), high_risk)
    # As bytes: the stub counts as current only byte for byte.
    _write(stub, STUB.read_bytes())


async def async_install(hass: HomeAssistant, quirks_dir: pathlib.Path) -> None:
    """Write the stub and its options file, the options as they are (the defaults without the file). A file of the
    user's named like the stub stays (FileExistsError), so does a broken options file (OSError): nothing written."""
    await hass.async_add_executor_job(_install, quirks_dir)


async def async_read_options(hass: HomeAssistant, quirks_dir: pathlib.Path) -> tuple[list[str], bool]:
    return await hass.async_add_executor_job(read_options, quirks_dir)


async def async_set_options(hass: HomeAssistant, quirks_dir: pathlib.Path, *, excluded: Iterable[str], high_risk: bool) -> None:
    """Replace the options."""
    await hass.async_add_executor_job(_save, quirks_dir, {ieee.lower() for ieee in excluded}, high_risk)


def _stub_state(quirks_dir: pathlib.Path) -> str:
    try:
        text = (quirks_dir / STUB_FILE).read_bytes()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "changed"
    return "current" if text == STUB.read_bytes() else "changed"


async def async_stub_state(hass: HomeAssistant, quirks_dir: pathlib.Path) -> str:
    """The stub in the folder: "current" (the text this release ships), "missing" or "changed"."""
    return await hass.async_add_executor_job(_stub_state, quirks_dir)


def _remove(quirks_dir: pathlib.Path) -> None:
    # The options last: a stub that cannot be deleted keeps the devices to leave alone.
    stub = quirks_dir / STUB_FILE
    paths = [stub, *(quirks_dir / "__pycache__").glob("xquirks.*.pyc")] if _ours(stub) else []
    for path in [*paths, quirks_dir / OPTIONS_FILE]:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


async def async_remove(hass: HomeAssistant, quirks_dir: pathlib.Path) -> None:
    """Delete the stub, the bytecode Python cached for it and the options. A file of the user's named like the stub
    stays, with its bytecode."""
    await hass.async_add_executor_job(_remove, quirks_dir)
