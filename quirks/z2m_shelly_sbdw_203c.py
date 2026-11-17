"""Shelly SBDW-203C: BLU DoorWindow ZB.

Generated from the Zigbee2MQTT definition.
Carries over everything ZHA's own quirk for this device defines, with its names and unique ids, and adds what Zigbee2MQTT knows besides.
Data: zigbee-herdsman-converters, MIT, Copyright (c) 2018 Koen Kanters.
Status: generated, not verified on hardware.
Revision: 94b357e6
"""

import functools
import logging
import pathlib

import zhaquirks
from zha.quirks import DEVICE_REGISTRY, DeviceMatch
import zigpy.types as t
from zigpy.zcl.foundation import ZCLAttributeDef
from zha.application.platforms.binary_sensor import IASZone
from zhaquirks.shelly.door_window import ShellyLightLevelCluster
from zigpy.zcl.clusters.security import IasZone

from zhaquirks.builder import (
    BinarySensorDeviceClass,
    EntityFilter,
    EntityPlatform,
    EntityType,
    LIGHT_LUX,
    NumberDeviceClass,
    QuirkBuilder,
    ReportingConfig,
)


class ShellyLightLevelCluster_2(ShellyLightLevelCluster):
    """ShellyLightLevelCluster of ZHA's quirk (zhaquirks.shelly.door_window) as it is: z2m adds no definition it lacks."""


class LightLevel(t.enum8):
    """LightLevel values (ZHA's quirk)."""

    dark = 0x00
    twilight = 0x01
    bright = 0x02


def _none_on_error(converter):
    """The converter, returning None where it raises: ZHA and zhaquirks call some converters where
    nothing catches an error (an entity's state while ZHA loads its devices). One value a converter
    cannot take would stop ZHA for every device. The entity shows no value instead."""

    def convert(value):
        try:
            return converter(value)
        except Exception:
            return None

    return convert



_LOGGER = logging.getLogger(__name__)
_SHIPPED = pathlib.Path(zhaquirks.__file__).resolve().parent
_STEPPED_BACK: set = set()

# The Zigbee fingerprints (manufacturer, model) this quirk is for; None matches any manufacturer.
FINGERPRINTS = [('Shelly', 'BLU DoorWindow Mini ZB')]
# Names that a model-only fingerprint would catch but Zigbee2MQTT gives to other devices.
CLAIMED_ELSEWHERE = set()
# The endpoints this quirk needs, each with the input clusters it puts there where Zigbee2MQTT names none.
ENDPOINTS = {1: ()}


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
    QuirkBuilder('Shelly', 'BLU DoorWindow Mini ZB')
    .replaces(ShellyLightLevelCluster_2)
    # Carried over from ZHA's own quirk (its names, keywords and unique ids):
    .enum(attribute_name=ShellyLightLevelCluster_2.AttributeDefs.light_level.name, enum_class=LightLevel, cluster_id=ShellyLightLevelCluster_2.cluster_id, entity_platform=EntityPlatform.SENSOR, entity_type=EntityType.STANDARD, reporting_config=ReportingConfig(min_interval=15, max_interval=300, reportable_change=1), translation_key='light_level', fallback_name='Light level')
    .binary_sensor(attribute_name=IasZone.AttributeDefs.zone_status.name, cluster_id=IasZone.cluster_id, device_class=BinarySensorDeviceClass.DOOR, entity_type=EntityType.STANDARD, attribute_converter=_none_on_error(lambda x: bool(x & 3)), unique_id_suffix='1280', fallback_name='Door')
    .binary_sensor(attribute_name=IasZone.AttributeDefs.zone_status.name, cluster_id=IasZone.cluster_id, entity_type=EntityType.STANDARD, attribute_converter=_none_on_error(lambda x: x & 3 == 1), unique_id_suffix='tilt', translation_key='tilt', fallback_name='Tilt')
    # Zigbee2MQTT features ZHA's own quirk lacks:
    .number(attribute_name=ShellyLightLevelCluster_2.AttributeDefs.dark_threshold.name, cluster_id=ShellyLightLevelCluster_2.cluster_id, min_value=0, max_value=65535, unit=LIGHT_LUX, device_class=NumberDeviceClass.ILLUMINANCE, entity_type=EntityType.STANDARD, translation_key='dark_threshold', fallback_name='Dark threshold')
    .number(attribute_name=ShellyLightLevelCluster_2.AttributeDefs.bright_threshold.name, cluster_id=ShellyLightLevelCluster_2.cluster_id, min_value=0, max_value=65535, unit=LIGHT_LUX, device_class=NumberDeviceClass.ILLUMINANCE, entity_type=EntityType.STANDARD, translation_key='bright_threshold', fallback_name='Bright threshold')
    .removes_entity(EntityFilter(entity_cls=IASZone, endpoint_id=1, cluster_id=0x0500))
    # light_level: ZHA's quirk shows it (read-only, same attribute)
    # battery: standard clusters, ZHA's own entities
    # identify: standard clusters, ZHA's own entities
    # contact: standard clusters, ZHA's own entities
    # handle_position: standard clusters, ZHA's own entities
    # battery_low: standard clusters, ZHA's own entities
    .filter(no_builtin_quirk)
    .add_to_registry()
)
