"""Cost, an opt-in benchmark (XQUIRKS_COST=1): building every definition at load against building on demand, with 0, 1
and 10 matching devices.

Prints a table (run with -s): the time of one ZHA setup's registration, the time to resolve the devices for the first time
and the memory the registry keeps after both. It measures the shipped definitions.jsonl, XQUIRKS_DEFINITIONS=<file>
another file. The bounds only catch a gross regression. The numbers are the result.
"""

import gc
import json
import os
import pathlib
import statistics
import time
import tracemalloc

import pytest
from zha.quirks import DEVICE_REGISTRY

import loader
from conftest import FOLDER
from helpers import layout_of, make_raw
from test_on_demand import holders, register_eagerly, register_on_demand

pytestmark = pytest.mark.skipif(not os.environ.get("XQUIRKS_COST"), reason="opt-in: XQUIRKS_COST=1")

RUNS = 3
COUNTS = (0, 1, 10)


def retained(function) -> int:
    """Bytes still allocated after the call (what the registry keeps)."""
    gc.collect()
    tracemalloc.start()
    try:
        before = tracemalloc.take_snapshot()
        function()
        gc.collect()
        after = tracemalloc.take_snapshot()
    finally:
        tracemalloc.stop()
    return sum(stat.size_diff for stat in after.compare_to(before, "filename"))


def data() -> tuple[pathlib.Path, list]:
    """The definitions file and a device for each of its chain definitions: (id, layout)."""
    path = pathlib.Path(os.environ.get("XQUIRKS_DEFINITIONS") or FOLDER / loader.DEFINITIONS_FILE)
    definitions = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return path, [(definition["id"], layout_of(definition)) for definition in definitions if "chain" in definition]


def raws(devices) -> list:
    return [make_raw(layout) for _, layout in devices]


def timed_cycle(register, stub, path, devices) -> tuple[float, float]:
    """One setup's registration, then each device resolved once: the medians of RUNS cycles in seconds."""
    results = []
    for _ in range(RUNS):
        prepared = raws(devices)
        loader.purge()
        gc.collect()
        start = time.perf_counter()
        register(stub, path)
        middle = time.perf_counter()
        for raw in prepared:
            DEVICE_REGISTRY.resolve(raw)
        results.append((middle - start, time.perf_counter() - middle))
    return statistics.median(r[0] for r in results), statistics.median(r[1] for r in results)


def kept(register, stub, path, devices) -> int:
    prepared = raws(devices)
    loader.purge()

    def run():
        register(stub, path)
        for raw in prepared:
            DEVICE_REGISTRY.resolve(raw)

    return retained(run)


def test_cost(installation, registry, capsys):
    installation.options(high_risk=True)
    stub = installation.stub
    path, candidates = data()
    # The devices: chain definitions that apply to their own device when every definition is built.
    register_eagerly(stub, path)
    devices = [c for c in candidates if (e := registry.match_entry(make_raw(c[1]))) and e.source.label.startswith(f"xquirks:{c[0]} ")][:10]
    assert len(devices) == 10
    total = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    register_on_demand(stub, path)
    assert {h.state for h in holders(registry).values()} == {"pending"}  # nothing built before a device shows up
    rows = {}
    for count in COUNTS:
        chosen = devices[:count]
        for mode, register in (("at load", register_eagerly), ("on demand", register_on_demand)):
            register_time, devices_time = timed_cycle(register, stub, path, chosen)
            rows[(count, mode)] = (register_time, devices_time, kept(register, stub, path, chosen))
    loader.purge()

    lines = [f"{total} definitions ({path.name}), {RUNS} runs, median ms, MiB the registry keeps (tracemalloc)",
             f"{'devices':>7}  {'mode':<10}{'register':>9}{'devices':>9}{'total':>9}{'MiB':>8}"]
    for (count, mode), (register_time, devices_time, memory) in rows.items():
        lines.append(f"{count:>7}  {mode:<10}{register_time * 1000:9.1f}{devices_time * 1000:9.1f}{(register_time + devices_time) * 1000:9.1f}{memory / 2**20:8.2f}")
    with capsys.disabled():
        print("\n" + "\n".join(lines))
    eager, on_demand = rows[(0, "at load")], rows[(10, "on demand")]
    assert rows[(0, "on demand")][0] < eager[0] / 3
    assert on_demand[2] < eager[2] / 5
