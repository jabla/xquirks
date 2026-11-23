// Which definitions a device gets, as ZHA matches a registry entry to a device (zha.quirks DeviceMatch) and the
// loader's filter leaves a name to another device (claimed_elsewhere), and the devices of a diagnostics file or of
// the template's output. Kept free of the DOM so that tests run it in node.

// The template the page shows: in Developer tools > Template it lists every ZHA device as
// "manufacturer||model".
export const TEMPLATE = [
  "{%- set ns = namespace(devices=[]) -%}",
  "{%- for entity in integration_entities('zha') -%}",
  "{%- set device = device_id(entity) -%}",
  "{%- if device -%}{%- set ns.devices = ns.devices + [(device_attr(device, 'manufacturer') or '') ~ '||' ~ (device_attr(device, 'model') or '')] -%}{%- endif -%}",
  "{%- endfor -%}",
  "{{ ns.devices | unique | list | tojson }}",
].join("\n");

const key = (pair) => JSON.stringify(pair);

// Python orders strings by code point; the < operator of JS compares UTF-16 units.
export function compareStrings(a, b) {
  let i = 0;
  while (i < a.length && i < b.length) {
    const x = a.codePointAt(i);
    const y = b.codePointAt(i);
    if (x !== y) return x - y;
    i += x > 0xffff ? 2 : 1;
  }
  return (i < a.length) - (i < b.length);
}

export const compareDevices = (a, b) => compareStrings(a[0], b[0]) || compareStrings(a[1], b[1]);

// Every object with a string "manufacturer" and "model" counts as a device, at any depth
// (integration diagnostics list all devices, a device's diagnostics one).
function collect(value, found) {
  if (Array.isArray(value)) {
    for (const item of value) collect(item, found);
  } else if (value !== null && typeof value === "object") {
    const { manufacturer, model } = value;
    if (typeof manufacturer === "string" && typeof model === "string") found.set(key([manufacturer, model]), [manufacturer, model]);
    for (const item of Object.values(value)) collect(item, found);
  }
  return found;
}

export function devicesIn(value) {
  return [...collect(value, new Map()).values()].sort(compareDevices);
}

// The output of TEMPLATE: "manufacturer||model", split at the first "||", pairs with an empty
// side dropped.
export function devicesFromTemplate(list) {
  const found = new Map();
  for (const item of list) {
    const at = typeof item === "string" ? item.indexOf("||") : -1;
    if (at < 0) continue;
    const pair = [item.slice(0, at), item.slice(at + 2)];
    if (pair[0] && pair[1]) found.set(key(pair), pair);
  }
  return [...found.values()].sort(compareDevices);
}

// A dropped or pasted JSON: diagnostics, the template's output or a list with both.
export function devicesFromJson(value) {
  const found = collect(value, new Map());
  if (Array.isArray(value)) for (const pair of devicesFromTemplate(value)) found.set(key(pair), pair);
  return [...found.values()].sort(compareDevices);
}

// A name compared ignoring case, as ZHA shows a device a definition renames (friendly_name).
const folded = (pair) => key(pair.map((part) => part.toLowerCase()));

const byId = (definitions) => [...definitions].sort((a, b) => compareStrings(a.id, b.id));

// Whether a name (manufacturer, model) catches the device: null stands for any.
export const catches = ([manufacturer, model], device) => (manufacturer === null || manufacturer === device[0]) && (model === null || model === device[1]);

// matches(): per definition (by id) and device, the definitions with a name that catches the device, unless the
// definition leaves that exact name to another device (claimed_elsewhere).
export function matches(definitions, devices) {
  const sorted = [...devices].sort(compareDevices);
  const result = [];
  for (const definition of byId(definitions)) {
    const claimed = new Set(definition.claimed.map(key));
    for (const device of sorted) {
      if (definition.names.some((name) => catches(name, device)) && !claimed.has(key(device))) result.push({ device, definition });
    }
  }
  return result;
}

// hints(): per device that matches() gives no definition, the definitions that show their device under that name
// (friendly_name), ignoring case. The device registry, ZHA's integration diagnostics and the template show that name,
// the definitions match the device's own. Sorted like matches(). Never a match: the device's own diagnostics tell.
export function hints(definitions, devices) {
  const matched = new Set(matches(definitions, devices).map(({ device }) => key(device)));
  const rest = [...devices].filter((device) => !matched.has(key(device))).sort(compareDevices);
  const result = [];
  for (const definition of byId(definitions)) {
    if (!definition.friendly) continue;
    const name = folded(definition.friendly);
    for (const device of rest) if (folded(device) === name) result.push({ device, definition });
  }
  return result;
}
