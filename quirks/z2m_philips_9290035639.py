"""Philips 9290035639: Hue Secure contact sensor.

Generated from the Zigbee2MQTT definition.
Carries over everything ZHA's own quirk for this device defines, with its names and unique ids, and adds what Zigbee2MQTT knows besides.
Data: zigbee-herdsman-converters, MIT, Copyright (c) 2018 Koen Kanters.
Status: generated, not verified on hardware.
Revision: f93a2f95
"""

import functools
import logging
import pathlib

import zhaquirks
from zha.quirks import DEVICE_REGISTRY, DeviceMatch
import zigpy.types as t
from zigpy.zcl.foundation import ZCLAttributeDef
from zhaquirks.philips.soc001 import PhilipsContactCluster

from zhaquirks.builder import (
    BinarySensorDeviceClass,
    EntityType,
    QuirkBuilder,
)


class ManuSpecificPhilipsContactCluster(PhilipsContactCluster):
    """PhilipsContactCluster of ZHA's quirk (zhaquirks.philips.soc001) as it is: z2m adds no definition it lacks."""



_LOGGER = logging.getLogger(__name__)
_SHIPPED = pathlib.Path(zhaquirks.__file__).resolve().parent
_STEPPED_BACK: set = set()

# The Zigbee fingerprints (manufacturer, model) this quirk is for; None matches any manufacturer.
FINGERPRINTS = [(None, 'SOC001'), ('Signify Netherlands B.V.', 'SOC001')]
# Names that a model-only fingerprint would catch but Zigbee2MQTT gives to other devices.
CLAIMED_ELSEWHERE = set()
# The endpoints this quirk needs, each with the input clusters it puts there where Zigbee2MQTT names none.
ENDPOINTS = {2: ()}


@functools.cache
def _is_shipped(source: str | None) -> bool:
    return bool(source) and pathlib.Path(source).resolve().is_relative_to(_SHIPPED)


def _misfit(device) -> str | None:
    """Why the device does not fit ENDPOINTS, None when it does.

    The device needs every endpoint in ENDPOINTS. Zigbee2MQTT sends a cluster it names no endpoint
    for to the first endpoint that announces it, or to the device's first endpoint when none does:
    each cluster in ENDPOINTS must land there on the endpoint this quirk puts it on.
    """
    announced = {ep_id: endpoint.in_clusters for ep_id, endpoint in sorted(device.endpoints.items()) if ep_id}
    for ep_id, cluster_ids in ENDPOINTS.items():
        if ep_id not in announced:
            return f"it has no endpoint {ep_id}"
        for cluster_id in cluster_ids:
            first = next((other for other, clusters in announced.items() if cluster_id in clusters), None)
            if first is None and min(announced) != ep_id:
                return f"no endpoint has cluster 0x{cluster_id:04X} and the first endpoint is {min(announced)}, not {ep_id}"
            if first is not None and first != ep_id:
                return f"the first endpoint with cluster 0x{cluster_id:04X} is {first}, not {ep_id}"
    return None


def _shipped_quirk(device):
    """The registry entry of a quirk shipped with ZHA that matches the device, None without one."""
    for entry in DEVICE_REGISTRY:
        # Green Power quirks (zha 2.4) never apply to a Zigbee device and cannot match one.
        if not isinstance(entry.device_match, DeviceMatch):
            continue
        if _is_shipped(getattr(entry.source, "file", None)) and entry.device_match.matches(device):
            return entry
    return None


def _upstream_first(device) -> bool:
    """Upstream first: step back for every device that a quirk shipped with ZHA matches.

    ZHA tries this custom quirk before the shipped ones under the same name. This check
    compares each device with the shipped quirks (name, endpoint signature, firmware range)
    and returns False when one matches, so ZHA falls through to the shipped quirk. Then it
    steps back for a device whose endpoints do not fit this quirk (ENDPOINTS).

    Except for the exact names in FINGERPRINTS: ZHA's own quirk for them shows fewer features
    than this file (compared when the file was built), so this quirk stays for them.
    """
    if (device.manufacturer, device.model) in CLAIMED_ELSEWHERE:
        return False
    shipped = None if device.manufacturer is not None and (device.manufacturer, device.model) in FINGERPRINTS else _shipped_quirk(device)
    if shipped is not None:
        if device.ieee not in _STEPPED_BACK:
            _STEPPED_BACK.add(device.ieee)
            _LOGGER.warning(
                "%s %s uses the quirk shipped with ZHA (%s); custom quirk %s is not needed for it",
                device.manufacturer, device.model, shipped.source.label, __name__,
            )
        return False
    misfit = _misfit(device)
    if misfit is not None:
        if (device.ieee, misfit) not in _STEPPED_BACK:
            _STEPPED_BACK.add((device.ieee, misfit))
            _LOGGER.warning(
                "%s %s does not fit custom quirk %s (%s); ZHA handles it without this quirk",
                device.manufacturer, device.model, __name__, misfit,
            )
        return False
    return True


def no_builtin_quirk(device) -> bool:
    """The filter ZHA calls: _upstream_first, and a step back when that check fails.

    ZHA calls it while it loads its devices, where an error would stop ZHA as a whole (a
    newer ZHA may register entries this check does not know yet). Then this quirk steps
    back for the device instead, and the log says so once.
    """
    try:
        return _upstream_first(device)
    except Exception:
        if "check failed" not in _STEPPED_BACK:
            _STEPPED_BACK.add("check failed")
            _LOGGER.exception("custom quirk %s could not check a device; ZHA handles it without this quirk", __name__)
        return False


(
    QuirkBuilder(None, 'SOC001')
    .applies_to('Signify Netherlands B.V.', 'SOC001')
    .replaces(ManuSpecificPhilipsContactCluster, endpoint_id=2)
    # Carried over from ZHA's own quirk (its names, keywords and unique ids):
    .binary_sensor(attribute_name=ManuSpecificPhilipsContactCluster.AttributeDefs.tamper.name, cluster_id=ManuSpecificPhilipsContactCluster.cluster_id, endpoint_id=2, device_class=BinarySensorDeviceClass.TAMPER, entity_type=EntityType.DIAGNOSTIC, fallback_name='Tamper')
    # Zigbee2MQTT features ZHA's own quirk lacks:
    .binary_sensor(attribute_name=ManuSpecificPhilipsContactCluster.AttributeDefs.contact.name, cluster_id=ManuSpecificPhilipsContactCluster.cluster_id, endpoint_id=2, entity_type=EntityType.STANDARD, device_class=BinarySensorDeviceClass.OPENING, translation_key='contact', fallback_name='Contact')
    # tamper: ZHA's quirk shows it (read-only, same attribute)
    # contact_last_changed: z2m's own code, not generated
    # tamper_last_changed: z2m's own code, not generated
    # battery: standard clusters, ZHA's own entities
    # identify: standard clusters, ZHA's own entities
    .filter(no_builtin_quirk)
    .add_to_registry()
)
