"""Mowe MW833P: Smart presence sensor (24 GHz mmWave radar).

Generated from the Zigbee2MQTT definition.
Data: zigbee-herdsman-converters, MIT, Copyright (c) 2018 Koen Kanters.
Status: generated, not verified on hardware.
Revision: 6a3ebe9c
"""

import functools
import logging
import pathlib

import zhaquirks
from zha.quirks import DEVICE_REGISTRY, DeviceMatch
import zigpy.types as t
from zigpy.zcl import foundation
from zigpy.zcl.clusters.measurement import OccupancySensing

from zhaquirks.builder import (
    EntityPlatform,
    EntityType,
)
from zhaquirks.tuya import TUYA_CLUSTER_ID
from zhaquirks.tuya.builder import TuyaQuirkBuilder
from zhaquirks.tuya.mcu import TuyaMCUCluster
from zhaquirks.tuya.tuya_motion import TuyaOccupancySensing


class HumanMotionState(t.enum8):
    """human_motion_state values (z2m)."""

    none = 0x00
    peaceful = 0x01
    motion = 0x02


class Scene(t.enum8):
    """scene values (z2m)."""

    default = 0x00
    area = 0x01
    toilet = 0x02
    bedroom = 0x03
    parlour = 0x04
    office = 0x05
    hotel = 0x06


class MoveDirection(t.enum8):
    """move_direction values (z2m)."""

    none = 0x00
    close_to = 0x01
    far_away = 0x02


class NobodyTime(t.enum8):
    """nobody_time values (z2m)."""

    none = 0x00
    Time_10s = 0x01
    Time_30s = 0x02
    Time_1min = 0x03
    Time_2min = 0x04
    Time_5min = 0x05
    Time_10min = 0x06
    Time_30min = 0x07
    Time_1hour = 0x08


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
FINGERPRINTS = [('_TZE200_ops9sidw', 'TS0601')]
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
    .applies_to('_TZE200_ops9sidw', 'TS0601')
    .tuya_dp(dp_id=1, ep_attribute=TuyaOccupancySensing.ep_attribute, attribute_name=OccupancySensing.AttributeDefs.occupancy.name, converter=_value_error_on_error(lambda x: x == 1))
    .adds(TuyaOccupancySensing)
    .prevent_default_entity_creation(endpoint_id=1, cluster_id=OccupancySensing.cluster_id, unique_id_suffix='pir_o_to_u_delay')
    .prevent_default_entity_creation(endpoint_id=1, cluster_id=OccupancySensing.cluster_id, unique_id_suffix='pir_u_to_o_delay')
    .tuya_number(dp_id=2, type=t.uint32_t, attribute_name='sensitivity', min_value=1, max_value=3, step=1, entity_type=EntityType.CONFIG, translation_key='sensitivity', fallback_name='Sensitivity')
    .tuya_binary_sensor(dp_id=101, attribute_name='radar_reset_flag', entity_type=EntityType.DIAGNOSTIC, translation_key='radar_reset_flag', fallback_name='Radar reset flag')
    .tuya_dp_attribute(dp_id=102, attribute_name='human_motion_state', type=HumanMotionState, access=foundation.ZCLAttributeAccess.Read | foundation.ZCLAttributeAccess.Report, converter=_value_error_on_error(lambda x: HumanMotionState(int(x))))
    .enum(attribute_name='human_motion_state', enum_class=HumanMotionState, cluster_id=TUYA_CLUSTER_ID, entity_platform=EntityPlatform.SENSOR, entity_type=EntityType.STANDARD, translation_key='human_motion_state', fallback_name='Human motion state')
    .tuya_illuminance(dp_id=103)
    .tuya_sensor(dp_id=104, attribute_name='radar_detection_data', type=t.CharacterString, converter=_value_error_on_error(lambda x: str(x)), entity_type=EntityType.DIAGNOSTIC, translation_key='radar_detection_data', fallback_name='Radar detection data')
    .tuya_binary_sensor(dp_id=107, attribute_name='check_end_flag', entity_type=EntityType.DIAGNOSTIC, translation_key='check_end_flag', fallback_name='Check end flag')
    .tuya_switch(dp_id=108, attribute_name='radar_self_check', entity_type=EntityType.CONFIG, translation_key='radar_self_check', fallback_name='Radar self check')
    .tuya_sensor(dp_id=109, attribute_name='hardware_version', type=t.CharacterString, converter=_value_error_on_error(lambda x: str(x)), entity_type=EntityType.DIAGNOSTIC, translation_key='hardware_version', fallback_name='Hardware version')
    .tuya_sensor(dp_id=110, attribute_name='soft_version', type=t.CharacterString, converter=_value_error_on_error(lambda x: str(x)), entity_type=EntityType.DIAGNOSTIC, translation_key='soft_version', fallback_name='Soft version')
    .tuya_sensor(dp_id=111, attribute_name='radar_id', type=t.CharacterString, converter=_value_error_on_error(lambda x: str(x)), entity_type=EntityType.DIAGNOSTIC, translation_key='radar_id', fallback_name='Radar ID')
    .tuya_enum(dp_id=112, attribute_name='scene', enum_class=Scene, entity_type=EntityType.CONFIG, translation_key='scene', fallback_name='Scene')
    .tuya_dp_attribute(dp_id=114, attribute_name='move_direction', type=MoveDirection, access=foundation.ZCLAttributeAccess.Read | foundation.ZCLAttributeAccess.Report, converter=_value_error_on_error(lambda x: MoveDirection(int(x))))
    .enum(attribute_name='move_direction', enum_class=MoveDirection, cluster_id=TUYA_CLUSTER_ID, entity_platform=EntityPlatform.SENSOR, entity_type=EntityType.STANDARD, translation_key='move_direction', fallback_name='Move direction')
    .tuya_sensor(dp_id=115, attribute_name='body_motion', type=t.int32s, entity_type=EntityType.STANDARD, translation_key='body_motion', fallback_name='Body motion')
    .tuya_enum(dp_id=131, attribute_name='nobody_time', enum_class=NobodyTime, entity_type=EntityType.CONFIG, translation_key='nobody_time', fallback_name='Nobody time')
    .skip_configuration()
    .filter(no_builtin_quirk)
    .add_to_registry(replacement_cluster=TypedTuyaCluster)
)
