# Definition format

`custom_components/xquirks/definitions.jsonl` holds one device definition per line, a JSON object. The lines are sorted
by id in code-point order. A definition is a zhaquirks builder quirk as data: the calls of `QuirkBuilder` or
`TuyaQuirkBuilder` with their arguments, in their order. The loader (`custom_components/xquirks/loader.py`) builds it
with the real builder and adds its own filter. Where two definitions share a name, ZHA tries the later line first.

## Top level

| Key | Value |
|---|---|
| `id` | the definition's name, `[a-z0-9][a-z0-9_]*` with at most 100 characters, once in the file |
| `rev` | the revision, 8 hex digits computed from the definition (see Revision) |
| `risk` | `low`, `medium` or `high` |
| `verified` | `true` for a definition verified on a device. A `high` one that is not verified applies only with the high-risk option |
| `take_over` | `true` for a definition that takes the device over from ZHA's own quirk: it carries everything that quirk has and adds what ZHA lacks |
| `zha_digests` | with `take_over` only and required there: `["<16 hex digits>", ...]`, the digests of ZHA's own entries the definition was built over (see Take-over) |
| `claimed_elsewhere` | `[[manufacturer, model], ...]`: names a model-only fingerprint catches that Zigbee2MQTT gives to other devices |
| `endpoints` | `{"1": [cluster_id, ...]}`: the endpoints the device needs (1 to 240 in plain decimal digits), each with the input clusters the definition puts there |
| `inherits` | `{"Name": "zhaquirks.<module>:<Class>"}`: a ZHA cluster class a class of the definition builds on |
| `classes` | the definition's own enums and clusters, in order (see Classes) |
| `chain` | the builder calls (see Chain) |
| `alias` | instead of `chain`: `[{"targets": [[m, model]], "fingerprints": [[m, model]]}]`, copies of ZHA's entries for the targets under the fingerprints ZHA lists for none of its quirks. Each name has a manufacturer or a model. No fingerprint comes twice in a group |

`id`, `rev` and either `chain` or `alias` are required. Any other key refuses the definition. The fingerprints of a
chain are its own names (those of the constructor and of `applies_to`): literal strings or null, each pair with a
manufacturer or a model, none twice. The label of every entry is `xquirks:<id> <rev>`.

## Revision

`rev` is the first 8 hex digits (lower case) of the SHA-256 of the definition's canonical text. That is the definition
as read from its line without `rev`, `verified` and `risk` (a verification or a new risk rule is no new behaviour), as
Python's `json.dumps(definition, sort_keys=True, separators=(",", ":"), ensure_ascii=False)` writes it, in UTF-8:

- no whitespace, the keys of every object sorted by code point, lists in their order, `true`, `false`, `null`
- in strings `\"`, `\\`, `\b`, `\t`, `\n`, `\f`, `\r`, the other characters U+0000 to U+001F as `\u` with four
  lower-case hex digits (`\u001b`), every other character as it is
- an integer in decimal digits, a number with a fraction or an exponent (a float to the loader, `1.0` too) as Python's
  `repr`: the shortest digits that read back as the same double, in exponent form when its absolute value is below
  1e-4 (not 0) or at least 1e16 (`0.0001`, `1e-05`, `1.0`, `1e+16`, `1.5e-07`)

`{"rev": "0badc0de", "risk": "low", "id": "x", "endpoints": {"2": [0], "10": [6]}, "chain": [...]}` becomes
`{"chain":[...],"endpoints":{"10":[6],"2":[0]},"id":"x"}`. The generator writes the rev and the trial build checks it
(see Loader behaviour). The load never computes it: it would cost every ZHA start.

## Values

Arguments, class data and operands are JSON scalars and lists as they are or objects with one tag. Numbers are finite
and at most `2**64` in size (JSON reads `1e999` as infinity).

| Tag | Meaning |
|---|---|
| `{"ref": "A.b.c"}` | a name: `A` is a class of the definition, an inherited class, `TypedTuyaCluster` or a name of the loader's table `_NAMES` (zigpy's types as `t`, zigpy's standard cluster classes and the names of zhaquirks a quirk imports). Each further part is a public attribute. Neither the name nor a further part is a module (`t` alone is not) |
| `{"tuple": [...]}`, `{"set": [...]}` | a tuple, a set |
| `{"dict": [[key, value], ...]}` | a dict |
| `{"flags": [ref, ...]}` | flag members of one kind, or-ed (`ZCLAttributeAccess.Read \| ...Report`) |
| `{"list_of": ref}` | zigpy's `t.List[ref]` with `ref` an attribute type (see Classes): a field type of a command schema |
| `{"new": "ReportingConfig" \| "EntityFilter" \| "DPToAttributeMapping", "kwargs": {...}}` | one of these, keywords only. `ReportingConfig` takes `min_interval`, `max_interval` and `reportable_change` as integers. `EntityFilter` takes no function and no entity class but `IASZone`. The keywords of `EntityFilter` and `DPToAttributeMapping` are checked by name as arguments are (see Chain) |
| `{"ops": [...], "on_error": ...}` | a converter (see Converters) |

## Classes

An enum: `{"kind": "enum", "name": N, "base": {"ref": "t.enum8"}, "members": [[name, int], ...]}`. The base is one of
zigpy's enum or bitmap types (`t.enum8`, `t.enum16`, `t.enum32`, `t.bitmap8`, `t.bitmap16`, `t.bitmap32`). It has at
least one member, each name a public identifier and no Python keyword.

A cluster:

```json
{"kind": "cluster", "name": "N", "bases": [{"ref": "CustomCluster"}],
 "behaviours": ["aqara_struct_tags", "aqara_outage"],
 "data": {"cluster_id": 64529, "ep_attribute": "x", "_VALID_ATTRIBUTES": {"set": [0]}},
 "attribute_defs": {"base": {"ref": "BaseAttributeDefs"}, "members": [["name", {"id": 1, "type": {"ref": "t.Bool"}, "access": "r"}]]},
 "server_command_defs": {"base": ..., "members": [["name", {"id": 0, "schema": {"dict": [...]}}]]}}
```

- One base must be a zhaquirks cluster class (`_skip_registry`): zigpy would otherwise build every device's cluster of
  that id from the class.
- `attribute_defs` has a base on `BaseAttributeDefs` (such as `{"ref": "AnalogInput.AttributeDefs"}`). Each member takes
  `id` (0 to 0xFFFF), `type` (an attribute type: one of zigpy's integer, float, string and bytes types or an enum on one
  of the enum and bitmap types above), `access`, `manufacturer_code`, `is_manufacturer_specific` and `mandatory`.
  `server_command_defs` and `client_command_defs` have a base on `BaseCommandDefs`. Each member takes `id`, `schema`
  (each field an attribute type or a `list_of`), `direction`, `is_manufacturer_specific` and `manufacturer_code`.
- A class on an inherited class repeats none of its attribute or command names or ids.
- `data` keys, each value checked by its key (ZHA reads them at resolve and for entity states, where nothing catches an
  error): `cluster_id` 0 to 0xFFFF, `ep_attribute` a public name, `name` a string, `_VALID_ATTRIBUTES` a set of
  attribute ids, `_CONSTANT_ATTRIBUTES` attribute ids of the cluster with null or a value the attribute's type holds (an
  integer, a number or a string as the type is), `STRUCT_TAGS` integers with `{"tuple": [key, number]}`, `MIN_VOLTS_MV`
  and `MAX_VOLTS_MV` integers, the first below the second. `_CONSTANT_ATTRIBUTES` may be
  `{"base": ref, "items": [[key, value], ...]}` for `{**Base._CONSTANT_ATTRIBUTES, key: value}`.
- `behaviours` are the fixed methods the loader holds, put in front of the bases: `aqara_struct`, `aqara_struct_tags`
  (uses `STRUCT_TAGS`), `aqara_current`, `aqara_outage`, `aqara_current_outage`.
- The fixed Tuya cluster is no class of the definition: the `add_to_registry` of every `TuyaQuirkBuilder` chain names
  `{"ref": "TypedTuyaCluster"}` (or a class of the definition on it) as `replacement_cluster`. It checks each datapoint
  value against its attribute's type before zhaquirks stores it.

## Chain

`[[name, args?, kwargs?], ...]`: element 1 is the list of positional arguments or the keyword object, element 2 the
keyword object when there are positional arguments.

- The first call is `QuirkBuilder` or `TuyaQuirkBuilder` (manufacturer, model), the last `add_to_registry`.
- Between them only the builder methods the loader allows (`_METHODS`), none that takes a function, a device class or
  an entity class. No `filter`: the loader adds its own.
- Each argument is checked by its parameter's name (`_PARAMETERS`: `dp_id` 0 to 255, `cluster_id` 0 to 0xFFFF,
  `entity_type` an `EntityType`, `divisor` and `multiplier` not 0, a converter parameter a converter). An unknown
  parameter refuses the definition.
- Only the `add_to_registry` of a `TuyaQuirkBuilder` chain takes keywords: `replacement_cluster` (required),
  `force_add_cluster` and `mcu_write_command`.

## Converters

`{"ops": [op, ...], "on_error": "value_error" | "none" | "raise"}`. The operations run in turn on the value: at most 16,
those of `if` branches included, with `if` at most 3 deep. `value_error` raises `ValueError` from any error (Tuya
converters: zhaquirks skips the datapoint). `none` returns `None` (attribute converters). `raise` lets the error pass
(datapoint converters for writes). `converter` takes `value_error`, `attribute_converter` takes `none`, `dp_converter`
takes any of them.

| Operation | Python |
|---|---|
| `["int"]`, `["str"]`, `["bool"]` | `int(x)`, `str(x)`, `bool(x)` |
| `["not"]` | `not x` |
| `["enum", ref]` | `E(x)`, `E` an enum class |
| `["eq", c]`, `["gt", c]` | `x == c`, `x > c` |
| `["sub", c]`, `["mul", c]`, `["div", c]`, `["floordiv", c]`, `["band", c]` | `x - c`, `x * c`, `x / c`, `x // c`, `x & c` |
| `["rsub", c]` | `c - x` |
| `["be", start, width]` | `_be(x, start, width)`: big-endian unsigned bytes of a RAW datapoint (start 0 to 255, width 1 to 8) |
| `["power_24"]` | `_power_24(x)`: z2m's 24-bit power |
| `["zigbee_local_time"]` | `_zigbee_local_time(x)`: a device clock value as an instant |
| `["lookup", [[k, v], ...]]` | `{k: v, ...}[x]` |
| `["const", v]` | `v` |
| `["if", test, then, else]` | `then(x) if test(x) else else(x)`, each a list of operations on the same `x` |

`c` is a number (any value for `eq`).

## Take-over

A take-over definition stays over an entry of ZHA's own quirks only while the entry's digest is in `zha_digests`.
Otherwise it steps back with one log line and ZHA's quirk applies: a take-over never hides a change of ZHA's quirk it
was not built against. For the user's own quirks it steps back like every definition.

- The digest (`loader.zha_digest`) covers the registry entry as data: what it matches (names, firmware range, filters),
  its transforms (clusters, device triggers) and everything ZHA builds from it (entities with all their values, entity
  changes, friendly name, features).
- A class of zhaquirks counts with its content: definitions, constants, enum members, methods. A function counts with
  its source text, the module globals it reads, its defaults and closure. zigpy's and zha's classes, the builder's and
  the ones the loader names count by name: the definition uses them as they are.
- A change wherever the entry reaches changes the digest, also outside the quirk file (the Shelly door sensor's
  `LightLevel` lives in `zhaquirks/shelly/__init__.py`). An edit the entry does not reach does not (another device's
  quirk in the same file, a comment). A new order of entities counts as a change.
- The digest is the same in every Python run: no memory address, hash order or bytecode goes in.
- The generator writes the digests of every v2 entry the installed zha-quirks registers for the definition's exact
  names (manufacturer and model, `loader.built_over`). The loader digests the same entries at load and compares them
  when a device shows up.
- `zha_digests` lists the entries of every zha-quirks release the definition is meant for, such as the Home Assistant
  release and its beta. A release's digest belongs there only when the definition carries all of that release's quirk.

## Loader behaviour

- A chain definition is built when ZHA first tries it for a device with one of its names. An alias definition is built
  at load.
- A definition that fails is skipped with one log line: at load for JSON, the top level, its names and its registration
  (what ZHA's registry took of it is taken out again), at its first device for the build.
- A definition steps back for a device (ZHA goes on as without it) when
  - the options exclude the device
  - the definition is `high` risk and not verified while the high-risk option is off
  - the device has a name of `claimed_elsewhere`
  - the device does not fit `endpoints`: it lacks one of them, or the first of its endpoints with a cluster listed there
    is another one (the device's first endpoint where none has the cluster)
  - another quirk matches the device: one of the user's or one of ZHA's, except ZHA's quirk a take-over was built over
- Only the load reads files (ZHA imports the custom quirks in an executor job). Matching and building run in Home
  Assistant's event loop and touch no file.
- `python loader.py --check definitions.jsonl` builds every definition at once, registers it in a registry of its own
  and prints one line per failure (`id: reason`). Exit status 1 on any failure. `loader.check(file)` returns them by id
  (after `zhaquirks.setup()`). It fails
  - a line whose `rev` is not the computed one, naming that one
  - a definition that does not build
  - an entity on an attribute that its cluster lacks (a switch's invert attribute too) or whose type does not hold every
    value ZHA computes with while it loads its devices (where nothing catches an error). A number needs a number type
    (ZHA always multiplies its value). So does a sensor with a divisor or multiplier other than 1 and no attribute
    converter. An enum entity needs an unsigned integer type at most as wide as its enum class, on a device with the
    typed Tuya cluster an enum or Bool type (the cluster lets a negative value into a bitmap). The cluster is the one
    the entity reads on a device with the definition's endpoints and the entity's cluster, after the definition's
    transforms.
  - an alias group that registers nothing
  - a take-over when ZHA ships no v2 quirk for its exact names or one whose digest is not in `zha_digests` or cannot be
    computed
- Before registering, the loader removes every entry labelled `xquirks:`.
- `xquirks_options.json` next to the stub: `{"excluded": [ieee, ...], "high_risk": false}`. A missing or broken file
  means these defaults. It is read once per load.
