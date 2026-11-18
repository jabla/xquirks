# Content format

The format of the quirk files this repository publishes, as the xquirks integration and the quirk
finder read them. This describes format 1. "Release" below means a release of the integration.

## Branch and tags

- The orphan branch `content` holds one commit per published state, each on top of the previous
  one. It is never rewritten.
- Each of these commits carries a lightweight tag `content-<n>`. `n` starts at 1 and grows by one
  with each state. A tag never moves. The branch head is the commit of the newest tag. Both are
  pushed together.
- Commit messages are not part of the format.

## Files at a tag

| Path | Content |
| --- | --- |
| `quirks/z2m_<name>.py` | the quirk files: exactly those `manifest.json` names |
| `manifest.json` | one entry per device (below) |
| `content.json` | `{"guard": "<64 hex digits>", "confirm_threshold": <n>}`: the guard the files were built for and the confirmation threshold (below) |
| `hacs.json` | `"homeassistant"`: the Home Assistant release the files need (below) |
| `data/withdrawn.toml` | revisions that turned out wrong (below) |
| `state.json` | `{"content": <n>, "date": "YYYY-MM-DD", "format": <FORMAT>}`: the tag's number, the date the files were generated (UTC) and the format, for the finder |
| `LICENSE`, `THIRD_PARTY.md` | the licenses |

## Guard and format

`content.json` names `guard.identity` (in `custom_components/xquirks/guard.py`) of the integration
release the files were built for. It covers `FORMAT`, `guard.py` and `fixed_code.json`, the fixed
code the guard compares every statement of a quirk file against. A release takes only a state that
names its own guard, so it never reads a format it does not know and never checks files against
fixed code it does not ship. `FORMAT` rises when a change to these files would make an older release
misread them. A new field does not raise it: readers ignore fields they do not know.

## Confirmation threshold

`"confirm_threshold"` in `content.json` is an integer from 1 to 1000. From this number of
confirmations on, the integration's notices no longer ask for a check of that device variant (a
notice that values look off still comes). Only the oldest open "Device test" issue whose title ends
with the same manufacturer, model, firmware and key counts: its author plus its thumbs-ups. Without
a valid value there is no threshold: the notices ask for every device.

## Home Assistant release

At a tag, `"homeassistant"` in `hacs.json` is the exact release the files were built for, patch
number included. A release takes the state only on that Home Assistant release or newer. Other keys
are not read. The `hacs.json` on `main` is a different file: HACS reads it to install the
integration.

## manifest.json

An object of entries by slug. Every entry has:

- `fingerprints`: `[[manufacturer or null, model], ...]`, the device names it is for
- `claimed_elsewhere` (optional, same shape): exact names it leaves to another entry
- `file`: `"quirks/z2m_<name>.py"` or null when no file is shipped
- `status`: `"generated"`, `"verified"` or `"upstream"`
- `swap` (optional): `{"ours": rows, "zha": rows}`, the entities of a file that takes a device over
  and those of ZHA's own quirk, each row `[cluster id, attribute id, platform, unique id suffix,
  name]`.

An entry matches a device (manufacturer, model) when one fingerprint has the device's model with its
manufacturer or null, unless that exact pair is in `claimed_elsewhere`.

An entry with a file also has:

- `sha256` (64 hex digits) and `revision` (8 hex digits, the file's `Revision:` line)
- `key` (8 lowercase hex digits): names what the file does to a device. It is the first 8 hex
  digits of the SHA-256 of the file's text with `\n` line ends, without its texts and names: the
  module docstring, the `.applies_to(...)` calls, the values of `FINGERPRINTS` and
  `CLAIMED_ELSEWHERE`, the names of an alias file with one group and the names in the builder call
  (a line left blank by that goes too). A new description, status line or manufacturer name keeps
  the key. Any other change gives a new one. "Device test" issue titles name it. A file is verified
  for one key.
- `vendor`, `model`, `description` (optional)
- `picture` and `picture_sha256` (optional): a picture without its sha256 is not shown
- `upstream_open_prs` (optional): `[{"number": <n>, "title": ..., "author": ...}]`, open
  zha-device-handlers pull requests for the device
- `sources` (optional): the zigbee-herdsman-converters and zha-device-handlers versions
- `over_zha` (optional): `{"adds": [...], "zha_only": [...]}`, entity names of a file that takes a
  device over
- `endpoints` (optional): `{"<endpoint>": [cluster ids]}`, what the file's own check needs on the
  device.
- `friendly_name` (optional): `[manufacturer, model]`, the name ZHA's own quirk shows for the device,
  where the file sets that name and it is neither the entry's `vendor` and `model` nor one of its
  fingerprints. Its manufacturer is the vendor or a fingerprint's. A file names the device with one of
  these only (ignoring case), never with another device's name, and with a manufacturer ZHA acts on for
  other devices too (`sengled`: the transition of its light groups) only where every fingerprint of the
  file has that manufacturer.
- `inherits` (optional, only beside `over_zha`): `[[module, class], ...]`, sorted, each pair once:
  classes of ZHA's own quirk that the file builds on, where ZHA's class has code a copy cannot express.
  Each module is a zhaquirks module (`zhaquirks.<name>...`, each part letters, digits and `_`, starting
  with a letter), each class a name that starts with a capital letter, none a builtin or a class quirk
  files import anyway. The file imports
  exactly these pairs and uses each one only as the one base of one class of its own. Before it offers
  or writes the file, the integration looks each class up inside Home Assistant among the zhaquirks
  modules ZHA has loaded (it imports none, and waits while one is not loaded). It refuses the file
  unless the class is a zhaquirks cluster class kept out of zigpy's registry with a cluster id and the
  file's class adds only definitions ZHA's class lacks (a command name counts in both command classes).
- `risk` (optional): `"low"`, `"medium"` or `"high"`: how likely the file is to do something wrong on
  the device. A reader takes a missing or unknown value as `"high"`. A file with status `"verified"`
  counts as checked, whatever its risk.
- `risk_why` (optional): the reasons as codes (cover, thermostat, state_dependency,
  mixed_manufacturer_code, take_over_drops, defect_found, number_settings, local_occupancy, take_over,
  alias_differs). A reader skips a code it does not know.

Only the finder reads `left_out_lines` (the Zigbee2MQTT features a file leaves out), `path` and
`target` (an alias file and the device it points to), `differs` and `not_built`. Fields not listed
here are not part of the format.

## data/withdrawn.toml

```toml
[[withdrawn]]
file = "z2m_<name>.py"
revision = "<8 hex digits>"
reason = "Why it must not be used."
```

These three fields never change, not even with a new format: every release reads the withdrawals of
a newer state it cannot use otherwise.

## How the integration reads it

1. It lists the `content-<n>` tags through the GitHub API. Annotated tags do not count.
2. From the newest tag down to the state it uses, it reads `content.json` and `hacs.json` at each
   tag's commit and takes the first state it can use (guard and Home Assistant release, above).
   Otherwise it keeps the state it has.
3. It reads `manifest.json` and `data/withdrawn.toml` of that commit, plus `data/withdrawn.toml` of
   a newer tag it cannot use.
4. It reads a quirk file only when its change list would write it, checked against `sha256` and the
   guard.

It reads every file at the commit a tag points to, never by the tag's name.

## How the finder reads it

1. `https://raw.githubusercontent.com/jabla/xquirks/refs/heads/content/state.json` names `n`.
2. `manifest.json` and `hacs.json` come from
   `https://raw.githubusercontent.com/jabla/xquirks/refs/tags/content-<n>/`.

The tag never moves, so both files belong to one state even while raw's cache still serves an older
`state.json`. A format the page does not read gets a notice instead of the device list. The finder
reads no quirk file. It links each one on GitHub at its tag.
