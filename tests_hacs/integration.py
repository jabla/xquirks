"""The integration under test, found by its folder: the domain is written nowhere in tests_hacs/."""

from __future__ import annotations

import importlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
(FOLDER,) = [path.parent for path in (ROOT / "custom_components").glob("*/manifest.json")]
PACKAGE = f"custom_components.{FOLDER.name}"

const = importlib.import_module(f"{PACKAGE}.const")
button = importlib.import_module(f"{PACKAGE}.button")
diagnostics = importlib.import_module(f"{PACKAGE}.diagnostics")
install = importlib.import_module(f"{PACKAGE}.install")
DOMAIN = const.DOMAIN
