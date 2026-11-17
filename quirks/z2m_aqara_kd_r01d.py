"""Aqara KD-R01D: Dimmer switch H2 EU.

Generated from the Zigbee2MQTT definition.
Carries over everything ZHA's own quirk for this device defines, with its names and unique ids, and adds what Zigbee2MQTT knows besides.
Data: zigbee-herdsman-converters, MIT, Copyright (c) 2018 Koen Kanters.
Status: generated, not verified on hardware.
Revision: 78a00d9f
"""

import functools
import logging
import pathlib

import zhaquirks
from zha.quirks import DEVICE_REGISTRY, DeviceMatch
import zigpy.types as t
from zigpy.zcl.foundation import BaseAttributeDefs, ZCLAttributeDef
from zigpy.profiles.zha import DeviceType

from zhaquirks.builder import (
    EntityType,
    QuirkBuilder,
)
from zhaquirks.const import (
    BUTTON_1,
    COMMAND,
    COMMAND_DOUBLE,
    COMMAND_HOLD,
    COMMAND_RELEASE,
    COMMAND_SINGLE,
)
from zhaquirks.xiaomi import DeviceTemperatureCluster, MeteringCluster, XiaomiAqaraE1Cluster
from zhaquirks.xiaomi.aqara.opple_remote import MultistateInputCluster


class ManuSpecificLumiCluster(XiaomiAqaraE1Cluster):
    """manuSpecificLumi (z2m custom cluster) on zhaquirks' Xiaomi base."""

    class AttributeDefs(BaseAttributeDefs):
        """Attribute definitions."""

        mode_switch = ZCLAttributeDef(id=0x0004, type=t.uint16_t, access="rw", is_manufacturer_specific=True)
        flip_indicator_light = ZCLAttributeDef(id=0x00F0, type=t.uint8_t, access="rw", is_manufacturer_specific=True)
        reporting_interval = ZCLAttributeDef(id=0x00F6, type=t.uint16_t, access="rw", is_manufacturer_specific=True)
        operation_mode = ZCLAttributeDef(id=0x0200, type=t.uint8_t, access="rw", is_manufacturer_specific=True)
        led_indicator = ZCLAttributeDef(id=0x0203, type=t.Bool, access="rw", is_manufacturer_specific=True)
        sensitivity = ZCLAttributeDef(id=0x0234, type=t.uint16_t, access="rw", is_manufacturer_specific=True)
        phase = ZCLAttributeDef(id=0x030A, type=t.uint8_t, access="rw", is_manufacturer_specific=True)
        min_brightness = ZCLAttributeDef(id=0x0515, type=t.uint8_t, access="rw", is_manufacturer_specific=True)
        max_brightness = ZCLAttributeDef(id=0x0516, type=t.uint8_t, access="rw", is_manufacturer_specific=True)
        power_on_state = ZCLAttributeDef(id=0x0517, type=t.uint8_t, access="rw", is_manufacturer_specific=True)
        mode = ZCLAttributeDef(id=0x0009, type=t.uint8_t, manufacturer_code=0x115F)
        illuminance = ZCLAttributeDef(id=0x0112, type=t.uint32_t, manufacturer_code=0x115F)
        display_unit = ZCLAttributeDef(id=0x0114, type=t.uint8_t, manufacturer_code=0x115F)
        movement = ZCLAttributeDef(id=0x0118, type=t.uint8_t, access="r", manufacturer_code=0x115F)
        air_quality = ZCLAttributeDef(id=0x0129, type=t.uint8_t, manufacturer_code=0x115F)
        curtain_reverse = ZCLAttributeDef(id=0x0400, type=t.Bool, manufacturer_code=0x115F)
        curtain_hand_open = ZCLAttributeDef(id=0x0401, type=t.Bool, manufacturer_code=0x115F)
        curtain_calibrated = ZCLAttributeDef(id=0x0402, type=t.Bool, manufacturer_code=0x115F)
        multi_click = ZCLAttributeDef(id=0x0286, type=t.uint8_t, manufacturer_code=0x115F)

    # The struct keys of zhaquirks' Xiaomi base and the endpoint cluster it writes each one to.
    STRUCT_TARGETS = {
        "battery_voltage_mV": "power",
        "battery_percentage": "power",
        "temperature_measurement": "temperature",
        "humidity_measurement": "humidity",
        "pressure_measurement": "pressure",
        "pressure_measurement_precision": "pressure",
        "power": "electrical_measurement",
        "voltage": "electrical_measurement",
        "consumption": "smartenergy_metering",
        "illuminance_measurement": "illuminance",
        "tvoc_measurement": "voc_level",
        "smoke": "ias_zone",
    }

    # z2m's struct tags for this model, under the key the Xiaomi base routes (consumption to
    # metering in kWh, power in W, voltage as 0.1 V) and the factor from the raw value.
    STRUCT_TAGS = {149: ('consumption', 1.0)}

    def _parse_aqara_attributes(self, value):
        """Only the struct values whose cluster the endpoint has: the base class stops at a missing one."""
        attributes = super()._parse_aqara_attributes(value)
        for tag, (key, factor) in self.STRUCT_TAGS.items():
            if f"0xff01-{tag}" in attributes:
                attributes[key] = attributes.pop(f"0xff01-{tag}") * factor
        return {key: v for key, v in attributes.items() if key not in self.STRUCT_TARGETS or hasattr(self.endpoint, self.STRUCT_TARGETS[key])}


class ModeSwitch(t.enum16):
    """ModeSwitch values (ZHA's quirk)."""

    Quick = 0x01
    Anti_Flicker = 0x04


class OperationMode(t.enum8):
    """OperationMode values (ZHA's quirk)."""

    Decoupled = 0x00
    Relay = 0x01


class Phase(t.enum8):
    """Phase values (ZHA's quirk)."""

    Leading = 0x00
    Trailing = 0x01


class PowerOnState(t.enum8):
    """PowerOnState values (ZHA's quirk)."""

    On = 0x00
    Previous = 0x01
    Off = 0x02
    Inverted = 0x03


class EventMode(t.enum8):
    """event_mode values (z2m)."""

    command = 0x00
    event = 0x01



_LOGGER = logging.getLogger(__name__)
_SHIPPED = pathlib.Path(zhaquirks.__file__).resolve().parent
_STEPPED_BACK: set = set()

# The Zigbee fingerprints (manufacturer, model) this quirk is for; None matches any manufacturer.
FINGERPRINTS = [(None, 'lumi.switch.agl011'), ('Aqara', 'lumi.switch.agl011')]
# Names that a model-only fingerprint would catch but Zigbee2MQTT gives to other devices.
CLAIMED_ELSEWHERE = set()
# The endpoints this quirk needs, each with the input clusters it puts there where Zigbee2MQTT names none.
ENDPOINTS = {1: (0x0012,)}


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
    QuirkBuilder(None, 'lumi.switch.agl011')
    .applies_to('Aqara', 'lumi.switch.agl011')
    .adds(ManuSpecificLumiCluster)
    .adds(DeviceTemperatureCluster)
    .replaces(MultistateInputCluster)
    .adds(MeteringCluster)
    # Carried over from ZHA's own quirk (its names, keywords and unique ids):
    .switch(attribute_name=ManuSpecificLumiCluster.AttributeDefs.flip_indicator_light.name, cluster_id=ManuSpecificLumiCluster.cluster_id, entity_type=EntityType.CONFIG, translation_key='flip_indicator_light', fallback_name='Flip indicator light')
    .switch(attribute_name=ManuSpecificLumiCluster.AttributeDefs.led_indicator.name, cluster_id=ManuSpecificLumiCluster.cluster_id, entity_type=EntityType.CONFIG, translation_key='led_indicator', fallback_name='LED indicator')
    .number(attribute_name=ManuSpecificLumiCluster.AttributeDefs.max_brightness.name, cluster_id=ManuSpecificLumiCluster.cluster_id, min_value=1, max_value=100, step=1, entity_type=EntityType.CONFIG, translation_key='max_brightness', fallback_name='Maximum brightness')
    .number(attribute_name=ManuSpecificLumiCluster.AttributeDefs.min_brightness.name, cluster_id=ManuSpecificLumiCluster.cluster_id, min_value=0, max_value=99, step=1, entity_type=EntityType.CONFIG, translation_key='min_brightness', fallback_name='Minimum brightness')
    .enum(attribute_name=ManuSpecificLumiCluster.AttributeDefs.mode_switch.name, enum_class=ModeSwitch, cluster_id=ManuSpecificLumiCluster.cluster_id, entity_type=EntityType.CONFIG, translation_key='mode_switch', fallback_name='Mode switch')
    .enum(attribute_name=ManuSpecificLumiCluster.AttributeDefs.operation_mode.name, enum_class=OperationMode, cluster_id=ManuSpecificLumiCluster.cluster_id, entity_type=EntityType.CONFIG, translation_key='operation_mode', fallback_name='Operation mode')
    .enum(attribute_name=ManuSpecificLumiCluster.AttributeDefs.phase.name, enum_class=Phase, cluster_id=ManuSpecificLumiCluster.cluster_id, entity_type=EntityType.CONFIG, translation_key='phase', fallback_name='Phase')
    .enum(attribute_name=ManuSpecificLumiCluster.AttributeDefs.power_on_state.name, enum_class=PowerOnState, cluster_id=ManuSpecificLumiCluster.cluster_id, entity_type=EntityType.CONFIG, translation_key='power_on_state', fallback_name='Power on state')
    .number(attribute_name=ManuSpecificLumiCluster.AttributeDefs.reporting_interval.name, cluster_id=ManuSpecificLumiCluster.cluster_id, min_value=1, max_value=3600, step=1, entity_type=EntityType.CONFIG, translation_key='reporting_interval', fallback_name='Reporting interval')
    .number(attribute_name=ManuSpecificLumiCluster.AttributeDefs.sensitivity.name, cluster_id=ManuSpecificLumiCluster.cluster_id, min_value=1, max_value=65535, step=1, entity_type=EntityType.CONFIG, translation_key='sensitivity', fallback_name='Sensitivity')
    # Zigbee2MQTT features ZHA's own quirk lacks:
    .switch(attribute_name=ManuSpecificLumiCluster.AttributeDefs.multi_click.name, cluster_id=ManuSpecificLumiCluster.cluster_id, on_value=2, off_value=1, entity_type=EntityType.CONFIG, translation_key='z2m_multi_click', fallback_name='Multi click')
    .enum(attribute_name=ManuSpecificLumiCluster.AttributeDefs.mode.name, cluster_id=ManuSpecificLumiCluster.cluster_id, enum_class=EventMode, entity_type=EntityType.CONFIG, translation_key='event_mode', fallback_name='Event mode')
    .replaces_endpoint(1, device_type=DeviceType.DIMMABLE_LIGHT)
    .device_automation_triggers(
        {
            (COMMAND_HOLD, BUTTON_1): {COMMAND: '1_hold'},
            (COMMAND_SINGLE, BUTTON_1): {COMMAND: '1_single'},
            (COMMAND_DOUBLE, BUTTON_1): {COMMAND: '1_double'},
            (COMMAND_RELEASE, BUTTON_1): {COMMAND: '1_release'},
        }
    )
    # power: standard clusters, ZHA's own entities
    # led_indicator: ZHA's quirk shows it (writable, same attribute)
    # flip_indicator_light: ZHA's quirk shows it (writable, same attribute)
    # power_on_behavior: ZHA's quirk shows it (writable, same attribute)
    # operation_mode: ZHA's quirk shows it (writable, same attribute)
    # mode_switch: ZHA's quirk shows it (writable, same attribute)
    # sensitivity: ZHA's quirk shows it (writable, same attribute)
    # phase: ZHA's quirk shows it (writable, same attribute)
    # min_brightness: ZHA's quirk shows it (writable, same attribute)
    # max_brightness: ZHA's quirk shows it (writable, same attribute)
    # energy: Aqara struct (0x00F7), MeteringCluster
    # voltage: Aqara struct (0x00F7), not generated yet
    # current: Aqara struct (0x00F7), not generated yet
    # light: standard clusters, ZHA's own entities
    # effect: standard clusters, ZHA's own entities
    # action: z2m's own code, not generated
    # action_rotation_angle: z2m's own code, not generated
    # action_rotation_angle_speed: z2m's own code, not generated
    # action_rotation_percent: z2m's own code, not generated
    # action_rotation_percent_speed: z2m's own code, not generated
    # action_rotation_time: z2m's own code, not generated
    # action_rotation_button_state: z2m's own code, not generated
    # device_temperature: standard clusters, ZHA's own entities
    .filter(no_builtin_quirk)
    .add_to_registry()
)
