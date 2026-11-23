"""install.py: ZHA's custom quirks folder, the stub and the options file beside it."""

from __future__ import annotations

import json
import os
import pathlib
import stat
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from .conftest import Zha
from .integration import FOLDER, install

IEEE_A = "00:11:22:33:44:55:66:01"
IEEE_B = "00:11:22:33:44:55:66:02"
DEFAULTS = {"excluded": [], "high_risk": False}


def options_of(folder: Path) -> dict:
    return json.loads((folder / "xquirks_options.json").read_text())


@contextmanager
def io_threads():
    """The threads the file system calls inside ran in: the event loop's must not be among them."""
    threads: set[int] = set()

    def spy(original):
        def call(*args, **kwargs):
            threads.add(threading.get_ident())
            return original(*args, **kwargs)

        return call

    path = pathlib.Path
    with patch.multiple(os, replace=spy(os.replace), unlink=spy(os.unlink)), patch.object(tempfile, "mkstemp", spy(tempfile.mkstemp)), \
            patch.multiple(path, read_bytes=spy(path.read_bytes), read_text=spy(path.read_text), is_file=spy(path.is_file), unlink=spy(path.unlink),
                           glob=spy(path.glob)):
        yield threads


async def test_the_folder_is_zhas_custom_quirks_path(hass: HomeAssistant, zha: Zha, quirks_dir: Path, monkeypatch: pytest.MonkeyPatch):
    assert install.custom_quirks_dir(hass) == quirks_dir
    # ZHA hands a relative path to pathlib as it is: it starts at the working directory.
    monkeypatch.chdir(quirks_dir.parent)
    zha.data.yaml_config["custom_quirks_path"] = "custom_zha_quirks"
    assert install.custom_quirks_dir(hass) == quirks_dir
    del zha.data.yaml_config["custom_quirks_path"]
    assert install.custom_quirks_dir(hass) is None
    del hass.data["zha"]
    assert install.custom_quirks_dir(hass) is None


async def test_install_writes_the_stub_and_the_default_options(hass: HomeAssistant, zha: Zha, quirks_dir: Path):
    with io_threads() as threads:
        await install.async_install(hass, quirks_dir)
    assert threads and threading.get_ident() not in threads
    assert (quirks_dir / "xquirks.py").read_bytes() == (FOLDER / "stub.py.txt").read_bytes()
    assert options_of(quirks_dir) == DEFAULTS
    assert sorted(path.name for path in quirks_dir.iterdir()) == ["xquirks.py", "xquirks_options.json"]
    assert {(quirks_dir / name).stat().st_mode & 0o777 for name in ("xquirks.py", "xquirks_options.json")} == {0o644}


async def test_install_puts_the_options_on_disk_before_the_stub(hass: HomeAssistant, zha: Zha, quirks_dir: Path):
    """A power cut leaves no stub without its options and no empty file: each file synced before its rename, the folder
    after it."""
    steps = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd: int) -> None:
        found = os.fstat(fd)
        steps.append("sync folder" if stat.S_ISDIR(found.st_mode) else f"sync {found.st_size} bytes")
        real_fsync(fd)

    def replace(source, target) -> None:
        steps.append(f"rename to {Path(target).name}")
        real_replace(source, target)

    with patch.object(os, "fsync", fsync), patch.object(os, "replace", replace):
        await install.async_install(hass, quirks_dir)
    sizes = {name: (quirks_dir / name).stat().st_size for name in ("xquirks.py", "xquirks_options.json")}
    assert steps == [
        f"sync {sizes['xquirks_options.json']} bytes", "rename to xquirks_options.json", "sync folder",
        f"sync {sizes['xquirks.py']} bytes", "rename to xquirks.py", "sync folder",
    ]


async def test_install_never_replaces_a_file_of_the_users(hass: HomeAssistant, zha: Zha, quirks_dir: Path):
    """Only a stub of any release goes: its first line says that xquirks wrote it. Otherwise nothing is written."""
    (quirks_dir / "xquirks.py").write_text("# a quirk of the user's\n")
    with pytest.raises(FileExistsError, match="Not written by xquirks"):
        await install.async_install(hass, quirks_dir)
    assert sorted(path.name for path in quirks_dir.iterdir()) == ["xquirks.py"]
    assert (quirks_dir / "xquirks.py").read_text() == "# a quirk of the user's\n"
    # This release's stub has the mark, as an older one had.
    assert install.STUB_MARK in (FOLDER / "stub.py.txt").read_bytes().splitlines()[0]
    (quirks_dir / "xquirks.py").write_text('"""xquirks: an older loader (written by the xquirks integration, do not edit)."""\n')
    await install.async_install(hass, quirks_dir)
    assert (quirks_dir / "xquirks.py").read_bytes() == (FOLDER / "stub.py.txt").read_bytes()


async def test_install_keeps_the_options(hass: HomeAssistant, zha: Zha, quirks_dir: Path):
    (quirks_dir / "xquirks_options.json").write_text(json.dumps({"excluded": [IEEE_B.upper(), IEEE_A], "high_risk": True}))
    await install.async_install(hass, quirks_dir)
    assert options_of(quirks_dir) == {"excluded": [IEEE_A, IEEE_B], "high_risk": True}


@pytest.mark.parametrize(
    "text",
    [b"{", b"[]", b'{"excluded": "00:11", "high_risk": "yes"}', b'{"excluded": ["00:11:22:33:44:55:66:01", 5], "high_risk": true}', b"", b'{"excluded": ["\xff"]}'],
)
async def test_a_broken_options_file_is_never_written_over(hass: HomeAssistant, zha: Zha, quirks_dir: Path, text: bytes):
    """A file the loader does not use (it takes the defaults then, also for a list with anything but strings): an error
    that names the file, nothing written."""
    (quirks_dir / "xquirks_options.json").write_bytes(text)
    with pytest.raises(OSError, match="xquirks_options.json"):
        await install.async_read_options(hass, quirks_dir)
    with pytest.raises(OSError, match="xquirks_options.json"):
        await install.async_install(hass, quirks_dir)
    assert sorted(path.name for path in quirks_dir.iterdir()) == ["xquirks_options.json"]
    assert (quirks_dir / "xquirks_options.json").read_bytes() == text


async def test_an_options_file_that_cannot_be_read_is_never_written_over(hass: HomeAssistant, zha: Zha, quirks_dir: Path):
    path = quirks_dir / "xquirks_options.json"
    path.write_text(json.dumps({"excluded": [IEEE_A], "high_risk": True}))
    path.chmod(0)
    try:
        with open(path, "rb"):
            pytest.skip("this user reads files without permission")
    except PermissionError:
        pass
    with pytest.raises(PermissionError):
        await install.async_install(hass, quirks_dir)
    path.chmod(0o644)
    assert sorted(path.name for path in quirks_dir.iterdir()) == ["xquirks_options.json"]
    assert options_of(quirks_dir) == {"excluded": [IEEE_A], "high_risk": True}


async def test_set_options_replaces_them(hass: HomeAssistant, zha: Zha, quirks_dir: Path):
    (quirks_dir / "xquirks_options.json").write_text(json.dumps({"excluded": [IEEE_A, IEEE_B], "high_risk": True}))
    with io_threads() as threads:
        await install.async_set_options(hass, quirks_dir, excluded=[IEEE_B.upper()], high_risk=False)
        assert await install.async_read_options(hass, quirks_dir) == ([IEEE_B], False)
    assert threads and threading.get_ident() not in threads
    assert options_of(quirks_dir) == {"excluded": [IEEE_B], "high_risk": False}


async def test_remove_deletes_the_stub_its_bytecode_and_the_options(hass: HomeAssistant, zha: Zha, quirks_dir: Path):
    await install.async_install(hass, quirks_dir)
    (quirks_dir / "__pycache__").mkdir()
    for name in ("xquirks.cpython-314.pyc", "z2m_own.cpython-314.pyc"):
        (quirks_dir / "__pycache__" / name).write_bytes(b"")
    (quirks_dir / "z2m_own.py").write_text("# a quirk of the user's\n")
    with io_threads() as threads:
        await install.async_remove(hass, quirks_dir)
    assert threads and threading.get_ident() not in threads
    assert sorted(str(path.relative_to(quirks_dir)) for path in quirks_dir.rglob("*")) == ["__pycache__", "__pycache__/z2m_own.cpython-314.pyc", "z2m_own.py"]
    # Nothing left to delete: no error.
    await install.async_remove(hass, quirks_dir)
    assert await install.async_stub_state(hass, quirks_dir) == "missing"


async def test_remove_deletes_the_options_last(hass: HomeAssistant, zha: Zha, quirks_dir: Path):
    """A stub that cannot be deleted stays with the devices to leave alone."""
    await install.async_install(hass, quirks_dir)
    (quirks_dir / "__pycache__").mkdir()
    (quirks_dir / "__pycache__" / "xquirks.cpython-314.pyc").write_bytes(b"")
    deleted, unlink = [], pathlib.Path.unlink

    def record(path: Path, missing_ok: bool = False) -> None:
        deleted.append(path.name)
        unlink(path, missing_ok)

    with patch.object(pathlib.Path, "unlink", record):
        await install.async_remove(hass, quirks_dir)
    assert deleted == ["xquirks.py", "xquirks.cpython-314.pyc", "xquirks_options.json"]

    await install.async_install(hass, quirks_dir)
    await install.async_set_options(hass, quirks_dir, excluded=[IEEE_A], high_risk=False)

    def refuse(path: Path, missing_ok: bool = False) -> None:
        if path.name == "xquirks.py":
            raise PermissionError(1, "Operation not permitted", str(path))
        unlink(path, missing_ok)

    with patch.object(pathlib.Path, "unlink", refuse), pytest.raises(PermissionError):
        await install.async_remove(hass, quirks_dir)
    assert options_of(quirks_dir) == {"excluded": [IEEE_A], "high_risk": False} and (quirks_dir / "xquirks.py").exists()


async def test_remove_keeps_a_file_of_the_users_named_like_the_stub(hass: HomeAssistant, zha: Zha, quirks_dir: Path):
    (quirks_dir / "xquirks.py").write_text("# a quirk of the user's\n")
    (quirks_dir / "__pycache__").mkdir()
    (quirks_dir / "__pycache__" / "xquirks.cpython-314.pyc").write_bytes(b"")
    await install.async_set_options(hass, quirks_dir, excluded=[IEEE_A], high_risk=False)
    await install.async_remove(hass, quirks_dir)
    assert sorted(str(path.relative_to(quirks_dir)) for path in quirks_dir.rglob("*")) == ["__pycache__", "__pycache__/xquirks.cpython-314.pyc", "xquirks.py"]
    assert (quirks_dir / "xquirks.py").read_text() == "# a quirk of the user's\n"


async def test_the_stub_is_written_byte_for_byte(hass: HomeAssistant, zha: Zha, quirks_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Line endings included: a stub with CRLF is current once written."""
    stub = tmp_path / "stub.py.txt"
    stub.write_bytes((FOLDER / "stub.py.txt").read_bytes().replace(b"\n", b"\r\n"))
    monkeypatch.setattr(install, "STUB", stub)
    await install.async_install(hass, quirks_dir)
    assert (quirks_dir / "xquirks.py").read_bytes() == stub.read_bytes()
    assert await install.async_stub_state(hass, quirks_dir) == "current"


async def test_the_stub_is_current_only_with_the_text_of_this_release(hass: HomeAssistant, zha: Zha, quirks_dir: Path):
    assert await install.async_stub_state(hass, quirks_dir) == "missing"
    await install.async_install(hass, quirks_dir)
    assert await install.async_stub_state(hass, quirks_dir) == "current"
    (quirks_dir / "xquirks.py").write_bytes((FOLDER / "stub.py.txt").read_bytes() + b"# edited\n")
    assert await install.async_stub_state(hass, quirks_dir) == "changed"
    (quirks_dir / "xquirks.py").unlink()
    (quirks_dir / "xquirks.py").mkdir()
    assert await install.async_stub_state(hass, quirks_dir) == "changed"


async def test_a_failed_write_leaves_the_file_as_it_was(hass: HomeAssistant, zha: Zha, quirks_dir: Path):
    before = '"""xquirks: an older loader (written by the xquirks integration, do not edit)."""\n'
    (quirks_dir / "xquirks.py").write_text(before)
    real_replace = os.replace

    def replace(source, target):
        if str(target).endswith("xquirks.py"):
            raise OSError(28, "No space left on device")
        real_replace(source, target)

    with patch.object(os, "replace", replace), pytest.raises(OSError):
        await install.async_install(hass, quirks_dir)
    assert (quirks_dir / "xquirks.py").read_text() == before
    assert sorted(path.name for path in quirks_dir.iterdir()) == ["xquirks.py", "xquirks_options.json"]
