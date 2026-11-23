"""Write the finder's index, site/devices.json: the definitions of the integration's definitions.jsonl as its loader
reads them.

Usage, in the test environment (pip install -r tests_hacs/requirements.txt: the loader needs zha and zha-quirks):
    python tools/build_index.py           # after a change to definitions.jsonl: writes site/devices.json to commit
    python tools/build_index.py --check   # exit 1 when site/devices.json is not what this writes (CI)

Each definition becomes the object the page works with (site/definitions.js):
    id, rev, risk
    verified, takeOver: true or false
    claimed: the names left to other devices
    names: the pairs the loader registers, null for any (of an alias the fingerprints the pinned ZHA does not list)
    alias: the groups with a target, cut to those names
    friendly: the name the chain's last friendly_name call gives the device
A line the loader skips fails the run.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
INDEX = ROOT / "site" / "devices.json"
# The page refuses a file of another format.
FORMAT = 1


def friendly(chain: list, loader) -> list | None:
    name = None
    for call in chain:
        if isinstance(call, list) and call[:1] == ["friendly_name"]:
            try:
                args, kwargs = loader._call_parts(call)
            except loader.DefinitionError:  # the build refuses it
                continue
            if not args and all(isinstance(kwargs.get(key), str) and kwargs[key] for key in ("manufacturer", "model")):
                name = [kwargs["manufacturer"], kwargs["model"]]
    return name


def index(data: bytes, folder: pathlib.Path) -> str:
    from zha.quirks import DeviceRegistry
    import zhaquirks

    # ZHA's own quirks first, as Home Assistant imports them before the stub (an alias copies their entries).
    zhaquirks.setup()
    sys.path.insert(0, str(folder))
    import loader

    rows, skipped = [], []
    for ident, definition, entries in loader._load(data, "xquirks.py", "xquirks", loader.Options(), DeviceRegistry()):
        if definition is None:
            skipped.append(f"{ident}: {loader._why(entries)}")
            continue
        names = list(dict.fromkeys(tuple(pair) for entry in entries for pair in entry.device_match.applies_to))
        groups = [{"targets": g["targets"], "fingerprints": [p for p in g["fingerprints"] if tuple(p) in names]} for g in definition.get("alias", []) if g["targets"]]
        rows.append({
            "id": ident,
            "rev": definition["rev"],
            "risk": definition.get("risk"),
            "verified": definition.get("verified") is True,
            "takeOver": definition.get("take_over") is True,
            "claimed": definition.get("claimed_elsewhere", []),
            "names": [list(name) for name in names],
            "alias": [group for group in groups if group["fingerprints"]] if "alias" in definition else None,
            "friendly": friendly(definition["chain"], loader) if "chain" in definition else None,
        })
    if skipped:
        sys.exit("\n".join(["the loader skips:", *skipped]))
    if not rows:
        sys.exit("no definitions")
    rows.sort(key=lambda row: row["id"])
    # One definition per line: a change shows as the lines of its definitions.
    lines = ",\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows)
    return f'{{"format":{FORMAT},"definitions":[\n{lines}\n]}}\n'


def main() -> None:
    parser = argparse.ArgumentParser(description="Write the finder's index from definitions.jsonl as the loader reads it.")
    parser.add_argument("--check", action="store_true", help="exit 1 when the index is not up to date")
    parser.add_argument("--definitions", type=pathlib.Path, help="read this definitions.jsonl instead of the integration's (tests)")
    parser.add_argument("--out", type=pathlib.Path, default=INDEX, help="the index to write or check (default: site/devices.json)")
    args = parser.parse_args()
    (folder,) = [path.parent for path in (ROOT / "custom_components").glob("*/manifest.json")]
    text = index((args.definitions or folder / "definitions.jsonl").read_bytes(), folder)
    shown = args.out.relative_to(ROOT) if args.out.is_relative_to(ROOT) else args.out
    if not args.check:
        args.out.write_bytes(text.encode())
        print(f"wrote {shown}")
    elif not args.out.is_file() or args.out.read_bytes() != text.encode():
        sys.exit(f"{shown} is not up to date: run python tools/build_index.py in the test environment and commit it")


if __name__ == "__main__":
    main()
