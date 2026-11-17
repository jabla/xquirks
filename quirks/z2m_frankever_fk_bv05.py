"""FrankEver FK-BV05: Zigbee smart water valve with flow meter and temperature sensor.

Generated from the Zigbee2MQTT definition.
Data: zigbee-herdsman-converters, MIT, Copyright (c) 2018 Koen Kanters.
Status: generated, not verified on hardware.
Revision: efb04e65
"""

import functools
import logging
import pathlib

import zhaquirks
from zha.quirks import DEVICE_REGISTRY, DeviceMatch
import zigpy.types as t
from zigpy.zcl import foundation

from zhaquirks.builder import (
    EntityPlatform,
    EntityType,
    NumberDeviceClass,
    PERCENTAGE,
    SensorDeviceClass,
    SensorStateClass,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolume,
)
from zhaquirks.tuya import TUYA_CLUSTER_ID
from zhaquirks.tuya.builder import TuyaQuirkBuilder
from zhaquirks.tuya.mcu import TuyaMCUCluster


class WeatherDelay(t.enum8):
    """weather_delay values (z2m)."""

    cancel = 0x00
    Delay_24h = 0x01
    Delay_48h = 0x02
    Delay_72h = 0x03


class WaterLeakageState(t.enum8):
    """water_leakage_state values (z2m)."""

    water_leakage_yes = 0x00
    water_leakage_no = 0x01


class PowerOffState(t.enum8):
    """power_off_state values (z2m)."""

    off = 0x00
    on = 0x01
    maintain = 0x02


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
FINGERPRINTS = [('_TZE200_nbqnmkee', 'TS0601')]
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
    .applies_to('_TZE200_nbqnmkee', 'TS0601')
    .tuya_onoff(dp_id=1)
    .tuya_number(dp_id=2, type=t.uint32_t, attribute_name='threshold', min_value=0, max_value=100, step=10, unit=PERCENTAGE, entity_type=EntityType.CONFIG, translation_key='threshold', fallback_name='Threshold')
    .tuya_sensor(dp_id=3, attribute_name='position', type=t.int32s, state_class=SensorStateClass.MEASUREMENT, unit=PERCENTAGE, entity_type=EntityType.STANDARD, translation_key='z2m_position', fallback_name='Position')
    .tuya_binary_sensor(dp_id=4, attribute_name='fault', entity_type=EntityType.DIAGNOSTIC, translation_key='fault', fallback_name='Fault')
    .tuya_sensor(dp_id=5, attribute_name='water_consumed_last', type=t.int32s, divisor=10, state_class=SensorStateClass.MEASUREMENT, unit=UnitOfVolume.LITERS, entity_type=EntityType.STANDARD, translation_key='water_consumed_last', fallback_name='Water consumed last')
    .tuya_sensor(dp_id=6, attribute_name='water_consumed', type=t.int32s, state_class=SensorStateClass.MEASUREMENT, unit=UnitOfVolume.LITERS, entity_type=EntityType.STANDARD, translation_key='water_consumed', fallback_name='Water consumed')
    .tuya_enum(dp_id=10, attribute_name='weather_delay', enum_class=WeatherDelay, entity_type=EntityType.CONFIG, translation_key='weather_delay', fallback_name='Weather delay')
    .tuya_number(dp_id=11, type=t.uint32_t, attribute_name='countdown', min_value=0, max_value=86400, unit=UnitOfTime.SECONDS, device_class=NumberDeviceClass.DURATION, entity_type=EntityType.CONFIG, translation_key='countdown', fallback_name='Countdown')
    .tuya_sensor(dp_id=22, attribute_name='water_temperature', type=t.int32s, state_class=SensorStateClass.MEASUREMENT, device_class=SensorDeviceClass.TEMPERATURE, unit=UnitOfTemperature.CELSIUS, entity_type=EntityType.STANDARD, translation_key='water_temperature', fallback_name='Water temperature')
    .tuya_dp_attribute(dp_id=101, attribute_name='water_leakage_state', type=WaterLeakageState, access=foundation.ZCLAttributeAccess.Read | foundation.ZCLAttributeAccess.Report, converter=_value_error_on_error(lambda x: WaterLeakageState(int(x))))
    .enum(attribute_name='water_leakage_state', enum_class=WaterLeakageState, cluster_id=TUYA_CLUSTER_ID, entity_platform=EntityPlatform.SENSOR, entity_type=EntityType.STANDARD, translation_key='water_leakage_state', fallback_name='Water leakage state')
    .tuya_switch(dp_id=102, attribute_name='single_irrigation_switch', entity_type=EntityType.STANDARD, translation_key='single_irrigation_switch', fallback_name='Single irrigation switch')
    .tuya_number(dp_id=103, type=t.uint32_t, attribute_name='single_irrigation_set', min_value=0, max_value=1000, unit=UnitOfVolume.LITERS, entity_type=EntityType.CONFIG, translation_key='single_irrigation_set', fallback_name='Single irrigation set')
    .tuya_switch(dp_id=104, attribute_name='day_irrigation_switch', entity_type=EntityType.STANDARD, translation_key='day_irrigation_switch', fallback_name='Day irrigation switch')
    .tuya_number(dp_id=105, type=t.uint32_t, attribute_name='day_irrigation_set', min_value=0, max_value=1000, unit=UnitOfVolume.LITERS, entity_type=EntityType.CONFIG, translation_key='day_irrigation_set', fallback_name='Day irrigation set')
    .tuya_switch(dp_id=106, attribute_name='water_volume_alarm_switch', entity_type=EntityType.CONFIG, translation_key='water_volume_alarm_switch', fallback_name='Water volume alarm switch')
    .tuya_number(dp_id=107, type=t.uint32_t, attribute_name='water_volume_alarm_set', min_value=0, max_value=1000, unit=UnitOfVolume.LITERS, entity_type=EntityType.CONFIG, translation_key='water_volume_alarm_set', fallback_name='Water volume alarm set')
    .tuya_switch(dp_id=108, attribute_name='water_temp_alarm_switch', entity_type=EntityType.CONFIG, translation_key='water_temp_alarm_switch', fallback_name='Water temp alarm switch')
    .tuya_number(dp_id=109, type=t.uint32_t, attribute_name='water_temp_alarm_max', min_value=0, max_value=120, unit=UnitOfTemperature.CELSIUS, device_class=NumberDeviceClass.TEMPERATURE, entity_type=EntityType.CONFIG, translation_key='water_temp_alarm_max', fallback_name='Water temp alarm max')
    .tuya_enum(dp_id=110, attribute_name='power_off_state', enum_class=PowerOffState, entity_type=EntityType.CONFIG, translation_key='power_off_state', fallback_name='Power off state')
    .tuya_switch(dp_id=112, attribute_name='creep_switch', entity_type=EntityType.CONFIG, translation_key='creep_switch', fallback_name='Creep switch')
    .tuya_number(dp_id=113, type=t.uint32_t, attribute_name='water_temp_alarm_min', min_value=0, max_value=120, unit=UnitOfTemperature.CELSIUS, device_class=NumberDeviceClass.TEMPERATURE, entity_type=EntityType.CONFIG, translation_key='water_temp_alarm_min', fallback_name='Water temp alarm min')
    .tuya_binary_sensor(dp_id=114, attribute_name='water_temp_alarm', entity_type=EntityType.STANDARD, translation_key='water_temp_alarm', fallback_name='Water temp alarm')
    .tuya_binary_sensor(dp_id=115, attribute_name='water_volume_alarm', entity_type=EntityType.STANDARD, translation_key='water_volume_alarm', fallback_name='Water volume alarm')
    .skip_configuration()
    .filter(no_builtin_quirk)
    .add_to_registry(replacement_cluster=TypedTuyaCluster)
)
