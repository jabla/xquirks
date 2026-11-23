"""tools/build_integration.py (the release zip) and the repository's metadata and translations."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import types
import zipfile
from pathlib import Path

import pytest
from homeassistant import loader
from homeassistant.core import HomeAssistant

from .integration import const

ROOT = Path(__file__).resolve().parents[1]


def load_builder():
    spec = importlib.util.spec_from_file_location("build_integration", ROOT / "tools" / "build_integration.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def release(text: str) -> tuple[int, int, int]:
    """(year, month, patch) of a Home Assistant release such as 2026.11.0."""
    found = re.fullmatch(r"([0-9]{4})\.([0-9]{1,2})\.([0-9]+)", text)
    assert found, text
    return int(found[1]), int(found[2]), int(found[3])


def test_the_minimum_is_no_older_than_the_release_ci_tests():
    """HACS installs the integration only on the Home Assistant release hacs.json names or a newer one.
    CI tests it with Home Assistant's own test package of one release (tests_hacs/requirements.txt;
    a release that does not exist turns the check red). The minimum is never older than that
    release: the integration never installs on a release older than the one its tests ran on."""
    minimum = json.loads((ROOT / "hacs.json").read_text())["homeassistant"]
    tested = re.findall(r"^pytest-homeassistant==(\S+)$", (ROOT / "tests_hacs" / "requirements.txt").read_text(), re.MULTILINE)
    assert len(tested) == 1, tested
    assert release(tested[0]) <= release(minimum)
    # By number, not as text: 2026.9.0 is older than 2026.10.0.
    assert release("2026.9.0") < release("2026.10.0") and "2026.9.0" > "2026.10.0"


def test_release_zip_has_the_integration_at_its_root(tmp_path: Path):
    """The loader, the definitions and the stub's text ship in the zip."""
    builder = load_builder()
    folder = builder.integration_folder()
    path = builder.write_zip(folder, tmp_path / "dist", "1.2.3")
    assert path.name == json.loads((ROOT / "hacs.json").read_text())["filename"]
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
        assert archive.read("definitions.jsonl") == (folder / "definitions.jsonl").read_bytes()
    assert manifest["version"] == "1.2.3"
    assert {"__init__.py", "manifest.json", "translations/en.json", "loader.py", "definitions.jsonl", "stub.py.txt", "install.py", "THIRD_PARTY.md"} <= names
    assert not any("__pycache__" in name for name in names)
    assert not any(name.startswith(("quirks/", "z2m_")) for name in names)
    assert not any(part.startswith("test") for name in names for part in name.split("/"))
    # The source tree keeps its version.
    assert json.loads((folder / "manifest.json").read_text())["version"] == "0.0.0"


def test_the_zip_carries_the_license_of_the_definitions(tmp_path: Path):
    """definitions.jsonl is derived from zigbee-herdsman-converters, whose MIT license asks for its copyright and
    permission notice in every copy: the zip carries the text THIRD_PARTY.md has."""
    builder = load_builder()
    with zipfile.ZipFile(builder.write_zip(builder.integration_folder(), tmp_path, None)) as archive:
        notice = archive.read("THIRD_PARTY.md").decode()
    license_text = re.search(r"^```\n(MIT License\n.*?)^```$", (ROOT / "THIRD_PARTY.md").read_text(), re.DOTALL | re.MULTILINE)[1]
    assert "Copyright (c) 2018 Koen Kanters" in license_text and "`definitions.jsonl`" in notice and f"```\n{license_text}```" in notice


def test_hacs_and_integration_metadata():
    builder = load_builder()
    folder = builder.integration_folder()
    manifest = json.loads((folder / "manifest.json").read_text())
    hacs = json.loads((ROOT / "hacs.json").read_text())
    assert folder.name == manifest["domain"]
    assert hacs["filename"] == f"{manifest['domain']}.zip"
    assert hacs["zip_release"] is True
    for key in ("domain", "name", "version", "documentation", "issue_tracker", "codeowners", "iot_class", "config_flow"):
        assert manifest[key], key
    assert manifest["requirements"] == [] and manifest["dependencies"] == ["zha"]
    # strings.json and translations/en.json are the same file, byte for byte (read_text would hide line endings).
    assert (folder / "strings.json").read_bytes() == (folder / "translations" / "en.json").read_bytes()


def _strings(tree: dict, prefix: str = "") -> dict[str, str]:
    out = {}
    for key, value in tree.items():
        if isinstance(value, dict):
            out.update(_strings(value, f"{prefix}{key}."))
        else:
            out[prefix + key] = value
    return out


def test_every_issue_and_option_the_integration_shows_has_its_texts():
    """In each language: the issues of const.py, the options' fields and the choices of the setup."""
    folder = load_builder().integration_folder()
    for path in sorted((folder / "translations").glob("*.json")):
        strings = json.loads(path.read_text())
        assert set(const.ISSUES) <= set(strings["issues"]), path.name
        assert set(strings["options"]["step"]["init"]["data"]) == {"excluded", "high_risk", "reload"}, path.name
        assert set(strings["config"]["step"]["user"]["menu_options"]) == {"reload", "later"}, path.name


def test_every_translation_has_the_keys_and_placeholders_of_en():
    folder = load_builder().integration_folder() / "translations"
    english = _strings(json.loads((folder / "en.json").read_text()))
    others = sorted(path for path in folder.glob("*.json") if path.name != "en.json")
    assert others
    for path in others:
        strings = _strings(json.loads(path.read_text()))
        assert strings.keys() == english.keys(), path.name
        for key, text in english.items():
            assert set(re.findall(r"{(\w+)}", strings[key])) == set(re.findall(r"{(\w+)}", text)), f"{path.name}: {key}"


def test_release_version_must_be_x_y_z_or_a_beta(tmp_path: Path):
    builder = load_builder()
    folder = builder.integration_folder()
    for bad in ("release-1", "x1", "1.2.0 beta", "v1.2.3", "1.2", "1.2.3b", "1.2.3-b1", "1.2.3.b1", "1.2.3B1", "1.2.3rc1", "v1.2.3b1"):
        with pytest.raises(SystemExit):
            builder.write_zip(folder, tmp_path / "dist", bad)
    assert not (tmp_path / "dist").exists()


async def test_home_assistant_loads_the_release_versions(hass: HomeAssistant, tmp_path: Path):
    """The zip of a release and of a beta as Home Assistant's loader finds it in custom_components: it blocks a custom
    integration whose version it does not take."""
    builder = load_builder()
    folder = builder.integration_folder()
    for version in ("1.2.3", "1.2.3b1"):
        root = tmp_path / version / "custom_components"
        with zipfile.ZipFile(builder.write_zip(folder, tmp_path / version, version)) as archive:
            archive.extractall(root / folder.name)
        package = types.SimpleNamespace(__name__="custom_components", __path__=[str(root)])
        integration = await hass.async_add_executor_job(loader.Integration.resolve_from_root, hass, package, folder.name)
        assert integration is not None and str(integration.version) == version


def test_every_definition_builds():
    """The loader's trial build of the definitions the zip ships: a definition that fails would show only at the first
    device it matches. In a process of its own: it registers every quirk of zhaquirks."""
    folder = load_builder().integration_folder()
    args = [sys.executable, "-I", str(folder / "loader.py"), "--check", str(folder / "definitions.jsonl")]
    run = subprocess.run(args, capture_output=True, text=True, timeout=300, check=False)
    assert run.returncode == 0, (run.stdout[-3000:], run.stderr[-3000:])
