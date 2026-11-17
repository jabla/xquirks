"""The typed Tuya cluster of the quirk files, tested inside Home Assistant on the zhaquirks ZHA loaded.

Every Tuya file registers the device's Tuya cluster on a class that holds the fixed method guard.TYPED_TUYA
(fixed_code.json): it checks each datapoint value against its attribute's type before zhaquirks stores it. The method
reads zhaquirks' internals (the cluster's datapoint table _dp_to_attributes, the fields endpoint_id, ep_attribute,
attribute_name and converter of each mapping) and relies on zhaquirks calling it for each datapoint (the dp_handler
TuyaQuirkBuilder gives every datapoint) and on handle_get_data skipping a datapoint that raises ValueError. A zhaquirks
release that changes any of this keeps working itself, while the method would skip every datapoint of every Tuya file
(each with a warning only) or let text through to a number. The guard and the tests know the pinned zhaquirks only.

So once per run of Home Assistant, before a Tuya file is offered, this self-test builds the fixed class on the
TuyaMCUCluster ZHA loaded and has zhaquirks' TuyaQuirkBuilder build the device's Tuya cluster on it, as a file's chain
does (add_to_registry(replacement_cluster=...), with a builder that registers nothing). It puts that cluster on a
stand-in device of its own (a zigpy device on a stand-in radio: nothing of ZHA, its devices or zigpy's database is
touched) and feeds it one frame through zhaquirks' handle_get_data: a STRING datapoint for an attribute of a number type,
which must be refused, then a VALUE datapoint the attribute's type holds on any reading, which must be stored as it
comes. So the test sees the fixed class work, not how zigpy stores a value or how zhaquirks decodes a VALUE: a negative
one on an attribute typed unsigned would be lost there without the typed class too (test_typed_tuya.py pins the method's
rule for it). Then it hands the method of that cluster itself a negative VALUE for the unsigned attribute, with zhaquirks'
update after it stubbed: the method must pass it on, as a reading below zero on the Tuya cluster itself,
whatever zigpy would store. Any other outcome, and any error of the test itself, fails it: it never passes what it could
not see work. While it fails, no Tuya file is installed or updated (manager.verdict_of) and one repair issue names the cause.

Like inherit.py it imports nothing: a module ZHA has not loaded raises inherit.NotLoaded (no verdict, the test runs
again on the next refresh). It runs in an executor job, never in the event loop.
"""

from __future__ import annotations

import ast
import inspect
from typing import Any

from .guard import TYPED_TUYA
from .inherit import NotLoaded, error_text, loaded

# What the self-test feeds: the value of the VALUE datapoint, one its unsigned 32 bit attribute holds read signed or
# unsigned.
VALUE = 50
# The value the fixed method itself must pass on for that attribute: a sensor typed unsigned that reads below zero.
NEGATIVE = -50
# The TuyaQuirkBuilder methods that name the method zhaquirks calls for each datapoint of the chain (dp_handler).
HANDLER_METHODS = ("tuya_dp", "tuya_dp_multi", "tuya_dp_attribute")


def holds_typed_class(content: bytes) -> bool:
    """Whether the file holds the fixed method of the typed Tuya cluster (every Tuya file does: the guard requires it on
    a Tuya chain), so its datapoints depend on the self-test. A file that does not parse holds none: it may be a file
    of ours the user edited, and deep nesting makes the parser raise MemoryError or RecursionError."""
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return False
    return any(isinstance(node, ast.FunctionDef) and node.name == TYPED_TUYA for node in ast.walk(tree))


def self_test(class_source: str | None) -> str:
    """Why the typed Tuya cluster (the fixed class text, guard.Fixed.typed_tuya_source) does not work on the zhaquirks ZHA
    loaded; empty when it does. NotLoaded while ZHA has not loaded a module the test needs."""
    try:
        return _failure(class_source)
    except NotLoaded:
        raise
    except Exception as err:  # noqa: BLE001 - a test that cannot run fails, it never passes
        return f"the self-test could not run: {error_text(err)}"


class _Radio:
    """The stand-in for zigpy's application the self-test's device is created with: it sends nothing and keeps nothing
    (no listener, no database)."""

    _dblistener = None

    def register_callback_listener(self, **_kwargs: Any):
        return lambda: None


def _built(builder: Any, quirk_builder: type, typed: type, zigpy_types: Any) -> Any:
    """The class zhaquirks' TuyaQuirkBuilder builds the device's Tuya cluster from for a chain of two datapoints that ends in
    add_to_registry(replacement_cluster=typed), as a file's chain does. Nothing is registered: the builder's add_to_registry
    ends before QuirkBuilder's, the class it hands to replaces() is kept. None when it hands none."""
    found: list[Any] = []

    class _Unregistered(quirk_builder):  # type: ignore[misc,valid-type]
        def add_to_registry(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    class _Probe(builder, _Unregistered):  # type: ignore[misc,valid-type]
        def replaces(self, replacement_cluster_class: Any, *_args: Any, **_kwargs: Any) -> Any:
            found.append(replacement_cluster_class)
            return self

    probe = _Probe()
    probe.tuya_dp_attribute(dp_id=1, attribute_name="checked_text", type=zigpy_types.uint32_t)
    probe.tuya_dp_attribute(dp_id=2, attribute_name="checked_value", type=zigpy_types.uint32_t)
    probe.add_to_registry(replacement_cluster=typed)
    return found[-1] if found else None


def _failure(class_source: str | None) -> str:
    if not class_source:
        return f"this release's fixed code has no class with {TYPED_TUYA}"
    tuya, mcu, builder = loaded("zhaquirks.tuya"), loaded("zhaquirks.tuya.mcu"), loaded("zhaquirks.tuya.builder")
    quirk_builder, zigpy_device, zigpy_types = loaded("zhaquirks.builder").QuirkBuilder, loaded("zigpy.device"), loaded("zigpy.types")
    for name in HANDLER_METHODS:
        handler = inspect.signature(getattr(builder.TuyaQuirkBuilder, name)).parameters["dp_handler"].default
        if handler != TYPED_TUYA:
            return f"zhaquirks' TuyaQuirkBuilder.{name} hands each datapoint to {handler!r}, not to {TYPED_TUYA}"
    if not issubclass(builder.TuyaQuirkBuilder, quirk_builder):
        return "zhaquirks' TuyaQuirkBuilder no longer builds on its QuirkBuilder"
    space: dict[str, Any] = {"TuyaMCUCluster": mcu.TuyaMCUCluster}
    # The fixed class text of this release (fixed_code.json, the same the files hold) on zhaquirks' cluster.
    exec(compile("class TypedTuyaCluster(TuyaMCUCluster):\n" + class_source.split("\n", 1)[1], "<typed Tuya cluster>", "exec"), space)  # noqa: S102
    typed = space["TypedTuyaCluster"]
    built = _built(builder.TuyaQuirkBuilder, quirk_builder, typed, zigpy_types)
    if not (isinstance(built, type) and issubclass(built, typed)):
        return f"zhaquirks' TuyaQuirkBuilder.add_to_registry does not build the device's Tuya cluster on the class it is handed ({built!r:.80})"

    class SelfTestCluster(built):  # type: ignore[misc,valid-type]
        """The device's Tuya cluster as zhaquirks built it, with a quiet log."""

        def log(self, *_args: Any, **_kwargs: Any) -> None:
            """The cluster's log stays quiet: the refused datapoint would be a warning at every start."""

    device = zigpy_device.Device(_Radio(), zigpy_types.EUI64.convert("00:00:00:00:00:00:00:00"), 0xFFFE)
    try:
        endpoint = device.add_endpoint(1)
        cluster = SelfTestCluster(endpoint, is_server=True)
        endpoint.add_input_cluster(cluster.cluster_id, cluster)

        def datapoint(dp: int, kind: str, raw: bytes) -> Any:
            data = tuya.TuyaData()
            data.dp_type, data.function, data.raw = getattr(tuya.TuyaDPType, kind), 0, raw
            return tuya.TuyaDatapointData(dp=dp, data=data)

        frame = [datapoint(1, "STRING", b"x"), datapoint(2, "VALUE", VALUE.to_bytes(4, "big", signed=True))]
        try:
            cluster.handle_get_data(tuya.TuyaCommand(status=0, tsn=1, datapoints=frame))
        except Exception as err:  # noqa: BLE001 - the frame must be acknowledged, the refused datapoint skipped
            return f"zhaquirks' handle_get_data did not skip the refused datapoint: {error_text(err)}"
        text, value = cluster.get("checked_text"), cluster.get("checked_value")
        # The fixed method alone with a negative reading: on a cluster of its own, zhaquirks' update after it stubbed.
        handed: list[Any] = []
        stubbed = device.add_endpoint(2)
        probe = _stubbed(SelfTestCluster, mcu.TuyaMCUCluster, handed)(stubbed, is_server=True)
        stubbed.add_input_cluster(probe.cluster_id, probe)
        negative = datapoint(2, "VALUE", NEGATIVE.to_bytes(4, "big", signed=True))
        try:
            probe._dp_2_attr_update(negative)
            refused = ""
        except Exception as err:  # noqa: BLE001 - the method refused it
            refused = error_text(err)
    finally:
        device.on_remove()
    if text is not None:
        return f"a STRING datapoint for an attribute of a number type was stored ({text!r:.40}), not refused"
    if value != VALUE:
        return f"a VALUE datapoint of {VALUE} was not stored (the attribute holds {value!r:.40}): every datapoint of a Tuya file would be skipped"
    if refused:
        return f"a VALUE datapoint of {NEGATIVE} for an unsigned attribute was refused ({refused}): negative readings would be skipped"
    if handed != [negative]:
        return f"a VALUE datapoint of {NEGATIVE} for an unsigned attribute did not reach zhaquirks' update"
    return ""


def _stubbed(cluster: type, base: type, handed: list[Any]) -> type:
    """The cluster class with zhaquirks' update (the method of base the fixed method calls after its checks) stubbed: it keeps
    each datapoint it is handed and stores nothing. The stub comes right after the fixed class in its method order."""

    class _Handed(base):  # type: ignore[misc,valid-type]
        def _dp_2_attr_update(self, datapoint: Any) -> None:
            handed.append(datapoint)

    class NegativeProbe(cluster, _Handed):  # type: ignore[misc,valid-type]
        """The device's Tuya cluster as zhaquirks built it, zhaquirks' update stubbed."""

    return NegativeProbe
