"""End to end: the config flow writes the stub, ZHA's custom quirk import (zhaquirks.setup on that folder) loads the
loader and the definitions from the integration's folder, and a device of a definition gets the same entities, with
the same unique ids, as with today's quirk file (tests_hacs/fixtures). A Tuya device takes the data query as ZHA's
service hands it on, a device left alone in the options gets no definition. The import runs in a process of its own:
it registers every quirk of zhaquirks."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from homeassistant import config_entries
from homeassistant.core import HomeAssistant

from .conftest import Zha
from .integration import DOMAIN, FOLDER, ROOT, install

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DEFINITIONS = [json.loads(line) for line in (FOLDER / "definitions.jsonl").read_text().splitlines()]
REVISIONS = {definition["id"]: definition["rev"] for definition in DEFINITIONS}
# The definitions with today's file frozen in fixtures/: (manufacturer, model, device type, input clusters of endpoint 1),
# and an entity the definition adds itself.
DEVICES = {
    "bosch_bsen_w": (("Bosch", "RBSH-WS-ZB-EU", 0x0402, [0x0000, 0x0001, 0x0003, 0x0500, 0xFCAC]), "-1-alarm_on_motion"),
    "aqara_qbcz15lm": (("LUMI", "lumi.plug.sacn03", 0x0051, [0x0000, 0x0003, 0x0004, 0x0005, 0x0006, 0xFCC0]), "-1-power_outage_memory"),
}
SCRIPT = """
import asyncio, json, pathlib, shutil, sys, warnings
from unittest import mock
warnings.simplefilter("ignore")
root, folder, fixtures = (pathlib.Path(arg) for arg in sys.argv[1:4])
devices = json.loads(sys.argv[4])
sys.path.insert(0, str(root))
import zhaquirks
import zigpy.device
import zigpy.types as t
from zha.quirks import DEVICE_REGISTRY


def joined(manufacturer, model, device_type, clusters):
    device = zigpy.device.Device(mock.MagicMock(), t.EUI64.convert("00:11:22:33:44:55:66:77"), 0x1234)
    endpoint = device.add_endpoint(1)
    endpoint.profile_id, endpoint.device_type = 260, device_type
    for cluster_id in clusters:
        endpoint.add_input_cluster(cluster_id)
    for cluster_id in (0x000A, 0x0019):
        endpoint.add_output_cluster(cluster_id)
    device.manufacturer, device.model = manufacturer, model
    return device


def zha_device(resolved):
    # As ZHA's gateway makes its device of a resolved zigpy device.
    from zha.zigbee.device import Device

    gateway = mock.MagicMock()
    gateway.config.config.device_options.consider_unavailable_mains = 7200
    gateway.config.config.device_options.consider_unavailable_battery = 21600
    gateway.state.node_info.ieee = t.EUI64.convert("00:00:00:00:00:00:00:00")
    return Device.new(resolved, gateway)


def entities(device):
    return sorted(entity.unique_id for entity in device.discover_entities() if not device._is_entity_removed_by_quirk(entity))


async def main():
    out = {}
    zhaquirks.setup(str(folder))
    loader = sys.modules["xquirks_loader"]
    options = loader.Options.read(folder / "xquirks_options.json")
    out["loader"], out["options"] = loader.__file__, [sorted(options.excluded), options.high_risk]
    out["register"] = loader.register(str(folder / "xquirks.py"), "xquirks")
    for ident, spec in devices.items():
        device = zha_device(DEVICE_REGISTRY.resolve(joined(*spec)))
        out[ident] = {"new": [device.quirk_class, entities(device)]}
    # The first low-risk Tuya definition that applies: the data query as ZHA's service hands it on.
    for line in pathlib.Path(loader.__file__).with_name("definitions.jsonl").read_text().splitlines():
        definition = json.loads(line)
        chain = definition.get("chain") or [[None]]
        if chain[0][0] != "TuyaQuirkBuilder" or definition.get("risk") != "low" or definition.get("endpoints") != {"1": [0xEF00]}:
            continue
        names = next(call[1] for call in chain if call[0] == "applies_to")
        resolved = DEVICE_REGISTRY.resolve(joined(*names, 0x0051, [0x0000, 0x0004, 0x0005, 0xEF00]))
        device = zha_device(resolved)
        if device.quirk_class.startswith("xquirks:"):
            resolved.request = mock.AsyncMock(return_value=None)
            await device.issue_cluster_command(1, 0xEF00, 0x03, "server", None, {}, cluster_type="in")
            call = resolved.request.call_args.kwargs
            out["tuya"] = [definition["id"], call["cluster"], call["dst_ep"], call["data"].hex()]
            break
    from custom_components.xquirks import diagnostics

    out["diagnostics"] = diagnostics._loader()
    # Today's files in a folder of their own, as quirks of the user's: the loader steps back for them.
    old = folder.parent / "today"
    old.mkdir()
    for ident in devices:
        shutil.copy(fixtures / f"z2m_{ident}.py.txt", old / f"z2m_{ident}.py")
    zhaquirks.setup(str(old))
    for ident, spec in devices.items():
        device = zha_device(DEVICE_REGISTRY.resolve(joined(*spec)))
        out[ident]["old"] = [device.quirk_class, entities(device)]
    print(json.dumps(out))


asyncio.run(main())
"""


async def test_setup_to_zha_entities(hass: HomeAssistant, zha: Zha, quirks_dir: Path):
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    await hass.config_entries.flow.async_configure(flow["flow_id"], {"next_step_id": "later"})
    await hass.async_block_till_done()
    await install.async_set_options(hass, quirks_dir, excluded=["AA:BB:CC:DD:EE:FF:00:11"], high_risk=True)
    devices = {ident: spec for ident, (spec, _own) in DEVICES.items()}
    args = [sys.executable, "-I", "-c", SCRIPT, str(ROOT), str(quirks_dir), str(FIXTURES), json.dumps(devices)]
    run = subprocess.run(args, capture_output=True, text=True, timeout=300, check=False)
    assert run.returncode == 0, run.stderr[-3000:]
    out = json.loads(run.stdout.splitlines()[-1])
    assert out["loader"] == str(FOLDER / "loader.py")
    # The loader reads the options as the integration wrote them.
    assert out["options"] == [["aa:bb:cc:dd:ee:ff:00:11"], True]
    assert out["register"] == [len(DEFINITIONS), 0]
    for ident, (_spec, own) in DEVICES.items():
        (new_quirk, new_ids), (old_quirk, old_ids) = out[ident]["new"], out[ident]["old"]
        assert new_quirk == f"xquirks:xquirks:{ident} {REVISIONS[ident]}" and old_quirk.startswith(f"z2m_{ident}:"), (new_quirk, old_quirk)
        assert new_ids == old_ids and f"00:11:22:33:44:55:66:77{own}" in new_ids
    # The command without payload (frame control 0x01: a cluster command from client to server, then the sequence number).
    ident, cluster, endpoint, frame = out["tuya"]
    assert REVISIONS[ident] and (cluster, endpoint, frame[:2], frame[4:]) == (0xEF00, 1, "01", "03")
    assert out["diagnostics"]["failed"] == [] and out["diagnostics"]["definitions"] == len(DEFINITIONS) and out["diagnostics"]["built"] >= 3


LABEL_OF = """
import json, pathlib, sys, warnings
from unittest import mock
warnings.simplefilter("ignore")
root, folder = (pathlib.Path(arg) for arg in sys.argv[1:3])
sys.path.insert(0, str(root))
import zhaquirks
import zigpy.device
import zigpy.types as t
from zha.quirks import DEVICE_REGISTRY

zhaquirks.setup(str(folder))
device = zigpy.device.Device(mock.MagicMock(), t.EUI64.convert(sys.argv[3]), 0x1234)
endpoint = device.add_endpoint(1)
endpoint.profile_id, endpoint.device_type = 260, 0x0402
for cluster_id in (0x0000, 0x0001, 0x0003, 0x0500, 0xFCAC):
    endpoint.add_input_cluster(cluster_id)
device.manufacturer, device.model = "Bosch", "RBSH-WS-ZB-EU"
entry = getattr(DEVICE_REGISTRY.resolve(device), "_quirk_registry_entry", None)
print(json.dumps(getattr(getattr(entry, "source", None), "label", None)))
"""


async def test_a_device_left_alone_keeps_zhas_quirk_or_none(hass: HomeAssistant, zha: Zha, quirks_dir: Path):
    """The options the integration writes: the loader steps back for an excluded device at the next ZHA start."""
    ieee = "00:11:22:33:44:55:66:88"
    await install.async_install(hass, quirks_dir)
    labels = []
    for excluded in (["00:11:22:33:44:55:66:99"], [ieee.upper()]):
        await install.async_set_options(hass, quirks_dir, excluded=excluded, high_risk=False)
        run = subprocess.run([sys.executable, "-I", "-c", LABEL_OF, str(ROOT), str(quirks_dir), ieee], capture_output=True, text=True, timeout=300, check=False)
        assert run.returncode == 0, run.stderr[-3000:]
        labels.append(json.loads(run.stdout.splitlines()[-1]))
    assert labels == [f"xquirks:bosch_bsen_w {REVISIONS['bosch_bsen_w']}", None]
