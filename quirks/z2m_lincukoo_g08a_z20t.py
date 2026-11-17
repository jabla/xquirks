"""Lincukoo G08A-Z20T: Smart sound and light siren with night light.

Generated from the Zigbee2MQTT definition.
Data: zigbee-herdsman-converters, MIT, Copyright (c) 2018 Koen Kanters.
Status: generated, not verified on hardware.
Revision: beeb3699
"""

import functools
import logging
import pathlib

import zhaquirks
from zha.quirks import DEVICE_REGISTRY, DeviceMatch
import zigpy.types as t
from zigpy.zcl import foundation

from zhaquirks.builder import (
    EntityType,
    NumberDeviceClass,
    UnitOfTime,
)
from zhaquirks.tuya import TUYA_CLUSTER_ID
from zhaquirks.tuya.builder import TuyaQuirkBuilder
from zhaquirks.tuya.mcu import TuyaMCUCluster


class AlarmState(t.enum8):
    """alarm_state values (z2m)."""

    alarm = 0x00
    normal = 0x01


class AlarmVolume(t.enum8):
    """alarm_volume values (z2m)."""

    low = 0x00
    middle = 0x01
    high = 0x02


class AlarmMode(t.enum8):
    """alarm_mode values (z2m)."""

    sound_alarm = 0x00
    light_alarm = 0x01
    sound_light_alarm = 0x02
    disable_alarm = 0x03


class AlarmRingtone(t.enum8):
    """alarm_ringtone values (z2m)."""

    ringtone_1 = 0x00
    ringtone_2 = 0x01
    ringtone_3 = 0x02


class NightLightModeValue(t.enum8):
    """night_light_mode wire values (z2m)."""

    off = 0x00
    on = 0x01


class NightLightBrightness(t.enum8):
    """night_light_brightness values (z2m)."""

    low = 0x00
    middle = 0x01
    high = 0x02


class PlayRingtone(t.enum8):
    """play_ringtone values (z2m)."""

    warning_smoke_detected = 0x00
    warning_gas_leak_detected = 0x01
    warning_carbon_monoxide_detected = 0x02
    warning_methane_detected = 0x03
    fall_detected = 0x04
    abnormal_heart_rate = 0x05
    door_opened = 0x06
    water_leak_detected = 0x07
    motion_detected = 0x08
    vibration_detected = 0x09
    high_temperature = 0x0A
    low_temperature = 0x0B
    high_humidity = 0x0C
    low_humidity = 0x0D
    soil_dry = 0x0E
    high_liquid_level = 0x0F
    low_liquid_level = 0x10
    poor_water_quality = 0x11
    high_noise = 0x12
    high_pressure = 0x13
    high_pm25_level = 0x14
    high_carbon_dioxide = 0x15
    high_formaldehyde = 0x16
    high_voc_level = 0x17
    power_on = 0x18
    power_off = 0x19
    light_on = 0x1A
    light_off = 0x1B
    water_valve_open = 0x1C
    water_valve_closed = 0x1D
    curtain_open = 0x1E
    curtain_closed = 0x1F


def _value_error_on_error(converter):
    """The Tuya converter, raising ValueError where it raises: zhaquirks skips a datapoint with a
    ValueError (a warning, the attribute keeps its value, the frame's other datapoints still count).
    Any other error would end the whole frame and reach the radio library."""

    def convert(value):
        try:
            return converter(value)
        except Exception as exc:
            raise ValueError(repr(exc)) from exc

    return convert


class TypedTuyaCluster(TuyaMCUCluster):
    """The Tuya cluster of the file: every datapoint value is checked against its attribute's type before zhaquirks stores it."""

    def _dp_2_attr_update(self, datapoint):
        """Each value of the datapoint checked against the type of the attribute it goes to, then zhaquirks' update.

        ZHA computes entity states from these attributes while it loads its devices, where nothing catches an
        error: text where a number is meant (a STRING or RAW datapoint) would stop ZHA at every start, so would a
        number its formatter cannot take (Illuminance raises 10 to the value, a UTCTime adds it to a date). A
        value for an attribute of a number type must be a number its type holds. ZHA looks the value of an enum, a
        bitmap or a Bool up, so there it must be an int that type holds. A plain integer type of another cluster
        holds its range: ZHA's own entities read such an attribute as its ZCL type, as zigpy stores a ZCL report.
        On this cluster (the file's entities read it with plain arithmetic) it holds what its bits hold read
        signed or unsigned: a VALUE datapoint is signed on the wire, a sensor typed unsigned may read below zero.
        A float type holds what a single precision float holds. Anything else raises ValueError, as does any
        error of a converter: zhaquirks skips the datapoint with a warning, the attributes keep their values, the
        frame's other datapoints still count. zhaquirks' update runs the converters again (a converter holds no
        state).
        """
        try:
            endpoint = self.endpoint
            for mapped in self._dp_to_attributes.get(datapoint.dp, ()):
                if mapped.endpoint_id:
                    endpoint = self.endpoint.device.endpoints[mapped.endpoint_id]
                value = datapoint.data.payload
                if mapped.converter:
                    value = mapped.converter(value)
                cluster = getattr(endpoint, mapped.ep_attribute)
                kind = getattr(cluster.attributes_by_name.get(mapped.attribute_name), "type", None)
                if value is None or not isinstance(kind, type) or not issubclass(kind, (int, float)):
                    continue
                looked_up = hasattr(kind, "__members__")
                low, high = -(2.0 ** 128), 2.0 ** 128
                if issubclass(kind, int) and not looked_up:
                    low, high = (-(2 ** (kind._bits - 1)), 2 ** kind._bits) if cluster is self else (kind.min_value, kind.max_value + 1)
                if not isinstance(value, (int, float)) or not low <= value < high or looked_up and isinstance(value, float):
                    raise TypeError(f"{type(value).__name__} {value!r:.40} for {mapped.attribute_name} ({kind.__name__})")
                if looked_up:
                    kind(value)
        except Exception as exc:
            raise ValueError(repr(exc)) from exc
        super()._dp_2_attr_update(datapoint)



_LOGGER = logging.getLogger(__name__)
_SHIPPED = pathlib.Path(zhaquirks.__file__).resolve().parent
_STEPPED_BACK: set = set()

# The Zigbee fingerprints (manufacturer, model) this quirk is for; None matches any manufacturer.
FINGERPRINTS = [('_TZE204_euszleja', 'TS0601'), ('_TZE200_euszleja', 'TS0601'), ('_TZE28C1000000_euszleja', 'TS0601')]
# Names that a model-only fingerprint would catch but Zigbee2MQTT gives to other devices.
CLAIMED_ELSEWHERE = set()
# The endpoints this quirk needs, each with the input clusters it puts there where Zigbee2MQTT names none.
ENDPOINTS = {1: (0xEF00,)}


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
    """
    if (device.manufacturer, device.model) in CLAIMED_ELSEWHERE:
        return False
    shipped = _shipped_quirk(device)
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
    TuyaQuirkBuilder()
    .applies_to('_TZE204_euszleja', 'TS0601')
    .applies_to('_TZE200_euszleja', 'TS0601')
    .applies_to('_TZE28C1000000_euszleja', 'TS0601')
    .tuya_enum(dp_id=1, attribute_name='alarm_state', enum_class=AlarmState, entity_type=EntityType.STANDARD, translation_key='alarm_state', fallback_name='Alarm state')
    .tuya_enum(dp_id=5, attribute_name='alarm_volume', enum_class=AlarmVolume, entity_type=EntityType.CONFIG, translation_key='alarm_volume', fallback_name='Alarm volume')
    .tuya_number(dp_id=7, type=t.uint32_t, attribute_name='alarm_time', min_value=0, max_value=100, step=1, unit='times', entity_type=EntityType.CONFIG, translation_key='alarm_time', fallback_name='Alarm time')
    .tuya_switch(dp_id=16, attribute_name='mute', entity_type=EntityType.STANDARD, translation_key='mute', fallback_name='Mute')
    .tuya_enum(dp_id=19, attribute_name='alarm_mode', enum_class=AlarmMode, entity_type=EntityType.CONFIG, translation_key='alarm_mode', fallback_name='Alarm mode')
    .tuya_enum(dp_id=21, attribute_name='alarm_ringtone', enum_class=AlarmRingtone, entity_type=EntityType.CONFIG, translation_key='alarm_ringtone', fallback_name='Alarm ringtone')
    .tuya_dp_attribute(dp_id=23, attribute_name='night_light_mode', type=t.Bool, access=foundation.ZCLAttributeAccess.Read | foundation.ZCLAttributeAccess.Write, converter=_value_error_on_error(lambda x: x == 1), dp_converter=lambda x: NightLightModeValue.on if x else NightLightModeValue.off)
    .switch(attribute_name='night_light_mode', cluster_id=TUYA_CLUSTER_ID, entity_type=EntityType.STANDARD, translation_key='night_light_mode', fallback_name='Night light mode')
    .tuya_enum(dp_id=101, attribute_name='night_light_brightness', enum_class=NightLightBrightness, entity_type=EntityType.CONFIG, translation_key='night_light_brightness', fallback_name='Night light brightness')
    .tuya_number(dp_id=103, type=t.uint32_t, attribute_name='presence_hold_time', min_value=5, max_value=60, step=5, unit=UnitOfTime.SECONDS, device_class=NumberDeviceClass.DURATION, entity_type=EntityType.CONFIG, translation_key='presence_hold_time', fallback_name='Presence hold time')
    .tuya_enum(dp_id=104, attribute_name='play_ringtone', enum_class=PlayRingtone, entity_type=EntityType.CONFIG, translation_key='play_ringtone', fallback_name='Voice prompt')
    .skip_configuration()
    .filter(no_builtin_quirk)
    .add_to_registry(replacement_cluster=TypedTuyaCluster)
)
