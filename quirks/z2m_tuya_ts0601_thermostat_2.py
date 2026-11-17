"""Tuya TS0601_thermostat_2: Thermostat radiator valve.

Generated from the Zigbee2MQTT definition.
Data: zigbee-herdsman-converters, MIT, Copyright (c) 2018 Koen Kanters.
Status: generated, not verified on hardware.
Revision: b72e826d
"""

import functools
import logging
import pathlib

import zhaquirks
from zha.quirks import DEVICE_REGISTRY, DeviceMatch
import zigpy.types as t
from zigpy.zcl import foundation
from zigpy.zcl.clusters.hvac import Thermostat

from zhaquirks.builder import (
    BinarySensorDeviceClass,
    EntityType,
    NumberDeviceClass,
    UnitOfTemperature,
)
from zhaquirks.tuya import TUYA_CLUSTER_ID
from zhaquirks.tuya.builder import TuyaQuirkBuilder
from zhaquirks.tuya.mcu import TuyaMCUCluster
from zhaquirks.tuya.tuya_trv import TuyaThermostatV2


class Z2mThermostat(TuyaThermostatV2):
    """Tuya thermostat with the setpoint range z2m gives (absolute limits 5 to 30 °C)."""

    _CONSTANT_ATTRIBUTES = {
        **TuyaThermostatV2._CONSTANT_ATTRIBUTES,
        Thermostat.AttributeDefs.abs_min_heat_setpoint_limit.id: 500,
        Thermostat.AttributeDefs.abs_max_heat_setpoint_limit.id: 3000,
    }


class Preset(t.enum8):
    """preset values (z2m)."""

    manual = 0x00
    holiday = 0x01
    program = 0x02


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
FINGERPRINTS = [('_TZE200_0hg58wyk', 'TS0601')]
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
    .applies_to('_TZE200_0hg58wyk', 'TS0601')
    .tuya_dp(dp_id=24, ep_attribute=Z2mThermostat.ep_attribute, attribute_name=Thermostat.AttributeDefs.local_temperature.name, converter=_value_error_on_error(lambda x: x * 10))
    .tuya_dp(dp_id=16, ep_attribute=Z2mThermostat.ep_attribute, attribute_name=Thermostat.AttributeDefs.occupied_heating_setpoint.name, converter=_value_error_on_error(lambda x: x * 10), dp_converter=lambda x: x // 10)
    .tuya_dp(dp_id=1, ep_attribute=Z2mThermostat.ep_attribute, attribute_name=Thermostat.AttributeDefs.system_mode.name, converter=_value_error_on_error(lambda x: {True: Thermostat.SystemMode.Heat, False: Thermostat.SystemMode.Off}[bool(x)]), dp_converter=lambda x: {Thermostat.SystemMode.Heat: True, Thermostat.SystemMode.Off: False}[x])
    .adds(Z2mThermostat)
    .tuya_enum(dp_id=2, attribute_name='preset', enum_class=Preset, entity_type=EntityType.STANDARD, translation_key='preset', fallback_name='Preset')
    .tuya_switch(dp_id=8, attribute_name='open_window', entity_type=EntityType.CONFIG, translation_key='open_window', fallback_name='Open window')
    .tuya_switch(dp_id=10, attribute_name='frost_protection', entity_type=EntityType.CONFIG, translation_key='frost_protection', fallback_name='Frost protection')
    .tuya_number(dp_id=27, type=t.int32s, attribute_name='local_temperature_calibration', min_value=-5, max_value=5, step=0.1, unit=UnitOfTemperature.CELSIUS, multiplier=0.1, device_class=NumberDeviceClass.TEMPERATURE_DELTA, entity_type=EntityType.CONFIG, translation_key='z2m_local_temperature_calibration', fallback_name='Local temperature calibration')
    .tuya_dp_attribute(dp_id=35, attribute_name='battery_low', type=t.Bool, access=foundation.ZCLAttributeAccess.Read | foundation.ZCLAttributeAccess.Report, converter=_value_error_on_error(lambda x: x == 0))
    .binary_sensor(attribute_name='battery_low', cluster_id=TUYA_CLUSTER_ID, entity_type=EntityType.DIAGNOSTIC, device_class=BinarySensorDeviceClass.BATTERY, translation_key='battery_low', fallback_name='Battery low')
    .tuya_switch(dp_id=40, attribute_name='child_lock', entity_type=EntityType.CONFIG, translation_key='child_lock', fallback_name='Child lock')
    .tuya_sensor(dp_id=45, attribute_name='error_status', type=t.int32s, entity_type=EntityType.STANDARD, translation_key='error_status', fallback_name='Error status')
    # DP 101 (schedule_monday): z2m class custom (string output), not generated
    # DP 102 (schedule_tuesday): z2m class custom (string output), not generated
    # DP 103 (schedule_wednesday): z2m class custom (string output), not generated
    # DP 104 (schedule_thursday): z2m class custom (string output), not generated
    # DP 105 (schedule_friday): z2m class custom (string output), not generated
    # DP 106 (schedule_saturday): z2m class custom (string output), not generated
    # DP 107 (schedule_sunday): z2m class custom (string output), not generated
    .skip_configuration()
    .filter(no_builtin_quirk)
    .add_to_registry(replacement_cluster=TypedTuyaCluster)
)
