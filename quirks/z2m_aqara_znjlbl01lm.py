"""Aqara ZNJLBL01LM: Roller shade driver E1.

Generated from the Zigbee2MQTT definition.
Carries over everything ZHA's own quirk for this device defines, with its names and unique ids, and adds what Zigbee2MQTT knows besides.
Data: zigbee-herdsman-converters, MIT, Copyright (c) 2018 Koen Kanters.
Status: generated, not verified on hardware.
Revision: 957d9924
"""

import functools
import logging
import pathlib

import zhaquirks
from zha.quirks import DEVICE_REGISTRY, DeviceMatch
import zigpy.types as t
from zigpy.zcl.foundation import ZCLAttributeDef
from zhaquirks.xiaomi.aqara.roller_curtain_e1 import AnalogOutputRollerE1
from zhaquirks.xiaomi.aqara.roller_curtain_e1 import BasicCluster
from zhaquirks.xiaomi.aqara.roller_curtain_e1 import PowerConfigurationRollerE1
from zhaquirks.xiaomi.aqara.roller_curtain_e1 import WindowCoveringRollerE1
from zhaquirks.xiaomi.aqara.roller_curtain_e1 import XiaomiAqaraRollerE1

from zhaquirks.builder import (
    BinarySensorDeviceClass,
    EntityType,
    QuirkBuilder,
)


class BasicCluster_2(BasicCluster):
    """BasicCluster of ZHA's quirk (zhaquirks.xiaomi.aqara.roller_curtain_e1) as it is: z2m adds no definition it lacks."""


class PowerConfigurationRollerE1Cluster(PowerConfigurationRollerE1):
    """PowerConfigurationRollerE1 of ZHA's quirk (zhaquirks.xiaomi.aqara.roller_curtain_e1) as it is: z2m adds no definition it lacks."""


class AnalogOutputRollerE1Cluster(AnalogOutputRollerE1):
    """AnalogOutputRollerE1 of ZHA's quirk (zhaquirks.xiaomi.aqara.roller_curtain_e1) as it is: z2m adds no definition it lacks."""


class WindowCoveringRollerE1Cluster(WindowCoveringRollerE1):
    """WindowCoveringRollerE1 of ZHA's quirk (zhaquirks.xiaomi.aqara.roller_curtain_e1) as it is: z2m adds no definition it lacks."""


class ManuSpecificLumiCluster(XiaomiAqaraRollerE1):
    """XiaomiAqaraRollerE1 of ZHA's quirk (zhaquirks.xiaomi.aqara.roller_curtain_e1) with what z2m adds."""

    class AttributeDefs(XiaomiAqaraRollerE1.AttributeDefs):
        """Attribute definitions."""

        mode = ZCLAttributeDef(id=0x0009, type=t.uint8_t, manufacturer_code=0x115F)
        illuminance = ZCLAttributeDef(id=0x0112, type=t.uint32_t, manufacturer_code=0x115F)
        display_unit = ZCLAttributeDef(id=0x0114, type=t.uint8_t, manufacturer_code=0x115F)
        movement = ZCLAttributeDef(id=0x0118, type=t.uint8_t, access="r", manufacturer_code=0x115F)
        air_quality = ZCLAttributeDef(id=0x0129, type=t.uint8_t, manufacturer_code=0x115F)
        curtain_hand_open = ZCLAttributeDef(id=0x0401, type=t.Bool, manufacturer_code=0x115F)


class AqaraRollerDriverSpeed(t.enum8):
    """AqaraRollerDriverSpeed values (ZHA's quirk)."""

    Low = 0x00
    Medium = 0x01
    High = 0x02


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
FINGERPRINTS = [(None, 'lumi.curtain.acn002'), ('LUMI', 'lumi.curtain.acn002')]
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
    QuirkBuilder(None, 'lumi.curtain.acn002')
    .applies_to('LUMI', 'lumi.curtain.acn002')
    .replaces(ManuSpecificLumiCluster)
    .replaces(WindowCoveringRollerE1Cluster)
    .replaces(AnalogOutputRollerE1Cluster)
    .replaces(PowerConfigurationRollerE1Cluster)
    .replaces(BasicCluster_2)
    # Carried over from ZHA's own quirk (its names, keywords and unique ids):
    .enum(attribute_name=ManuSpecificLumiCluster.AttributeDefs.speed.name, enum_class=AqaraRollerDriverSpeed, cluster_id=ManuSpecificLumiCluster.cluster_id, entity_type=EntityType.CONFIG, translation_key='speed', fallback_name='Speed')
    .binary_sensor(attribute_name=ManuSpecificLumiCluster.AttributeDefs.charging.name, cluster_id=ManuSpecificLumiCluster.cluster_id, device_class=BinarySensorDeviceClass.BATTERY_CHARGING, entity_type=EntityType.DIAGNOSTIC, attribute_converter=_none_on_error(lambda x: x == 1), fallback_name='Charging')
    .binary_sensor(attribute_name=ManuSpecificLumiCluster.AttributeDefs.positions_stored.name, cluster_id=ManuSpecificLumiCluster.cluster_id, entity_type=EntityType.DIAGNOSTIC, translation_key='calibrated', fallback_name='Calibrated')
    # Zigbee2MQTT features ZHA's own quirk lacks:
    .switch(attribute_name=ManuSpecificLumiCluster.AttributeDefs.reverse_direction.name, cluster_id=ManuSpecificLumiCluster.cluster_id, entity_type=EntityType.STANDARD, translation_key='reverse_direction', fallback_name='Reverse direction', unique_id_suffix='curtain_reverse')
    .prevent_default_entity_creation(endpoint_id=1, cluster_id=0x000D)
    .prevent_default_entity_creation(endpoint_id=1, cluster_id=0x0013)
    .prevent_default_entity_creation(endpoint_id=1, cluster_id=0x0006)
    # motor_speed: ZHA's quirk shows it (writable, same attribute)
    # cover: standard clusters, ZHA's own entities
    # battery: standard clusters or z2m's own code, not generated
    # device_temperature: standard clusters or z2m's own code, not generated
    # charging_status: standard clusters or z2m's own code, not generated
    # motor_state: standard cluster, no ZHA entity, not generated
    # running: standard cluster, no ZHA entity, not generated
    .filter(no_builtin_quirk)
    .add_to_registry()
)
