# Contributing

Thanks for helping. Most help needs no code: you report what a device you own does. The quirk files
are generated from the Zigbee2MQTT device library, so nobody edits them by hand: I turn reports into
corrections, which reach you with a later state of the files.

Reports go through the issue forms of this repository. The finder and the integration open them
prefilled. Before you open one, look at the open issues of the file: the integration's notices link
them. If one fits, a thumbs-up or a comment there is enough.

## A wrong or missing value

Use the "Wrong or missing value" form for a wrong value, a missing entity or a setting that does not
stick. What helps most:

- the quirk file and its revision (the `Revision:` line at its top or the integration's
  diagnostics)
- for a wrong value, two or three pairs of what Home Assistant and the device's display or app show
  at the same time: they become the expected values of the fix
- the device's diagnostics (the device's page > three-dot menu > Download diagnostics) or better a
  probe report.

## A device test ("verified")

A file becomes "verified" when someone checked its entities against the device itself: its display,
its app or what it does.

With the integration: Settings > Devices & services > xquirks > **Check a device**. Pick
the device. The integration asks it for its current values (a Tuya data query or a read of the
attributes behind the file's entities), which changes nothing on the device. Mark which values match
the device and which do not. The last dialog opens the "Device test" form filled in with the report.
When a value does not match, it opens the "Wrong or missing value" form instead. The report names no
address, no device or room name and no entity id. It is also in the integration's diagnostics.

About a day after the integration installed or updated a file, a notice says whether its values look
plausible. A device that gets new firmware is watched for another day. The notice leads through the
same check. When something looks off, it also links the "Wrong or missing value" form.

One "Device test" issue collects the tests of one device variant. Its title starts with "Verify:" and
ends with the manufacturer and model as zigpy reports them, the firmware and the file's key, for
example `[_TZE200_abcd1234 | TS0601 | 0x00000046 | 1a2b3c4d]`. The first person whose values all match
opens the prefilled form. Everyone after that gives the oldest open issue of the variant a thumbs-up:
the notice's last dialog links it. A bot closes a newer issue of the same variant as a duplicate with
a link to the older one. It also takes the end of the title from the form's "Device variant" field
and puts it back when someone edits the title. The author and each thumbs-up count as one
confirmation. Once a variant has enough of them ([CONTENT.md](CONTENT.md#confirmation-threshold)),
the integration's notices no longer ask for a check of its devices. The file can then become
"verified". An issue I do not accept, for example a test of another device, I close as "not
planned": a closed issue counts for nothing.

`tools/probe.py` collects what a device test needs from a shell, also without the integration. It can
also check a setting by writing it. Run it from a clone of this repository. It needs Python's standard
library only. `HASS_TOKEN` is a long-lived access token (your profile > Security > Long-lived access
tokens):

```bash
HASS_URL=http://homeassistant.local:8123 HASS_TOKEN=... python tools/probe.py "<device name>" --query
```

The probe reads the device, its entities, their states and the device diagnostics (IEEE addresses
removed). For the quirk file and its revision it also reads the diagnostics of the xquirks
integration. `--query` asks the device for its current values, which changes nothing on it. It writes
only with `--write <entity id>`: it sets one configuration number or select to its limits or
another option, reads it back and sets it back. It never writes a switch, button, siren or alarm.
Tick the boxes in `probe-report/report.md` that match the device, then attach it with
`probe-report/diagnostics.json` to the "Device test" form.

A test of a high-risk file helps most: the finder lists these files under "High risk".

## A file that does not fit your device

When a quirk does not fit your device, the integration says so in a notice. It links the "Device
layout" form with the device's endpoints and clusters filled in, without its name or address: submit
it as it is. Without the integration, paste the device's diagnostics into that form. The issue shows
the whole file, so check it first.

## A device without a file

Look it up in the [finder](https://jabla.github.io/xquirks/) first. If Zigbee2MQTT supports it and it
has no file here, use the "Device without a file" form with its Zigbee2MQTT device page. Some kinds
of devices get no file yet.

## Translations

The integration's texts are in `custom_components/xquirks/translations/<language>.json`. Pull
requests that add or improve a language are welcome. CI checks that each file has the keys and
placeholders of `en.json`. A change to the English text goes into both `en.json` and `strings.json`.

## The integration or the finder

Report bugs in `custom_components/xquirks/` or `site/` as issues or send a pull request. Tests:
`pip install -r tests_hacs/requirements.txt`, then `python -m pytest tests_hacs` (the finder's tests
need node).

## Upstream

Wrong values of a quirk that ships with ZHA belong to
[zha-device-handlers](https://github.com/zigpy/zha-device-handlers/issues). If ZHA's quirk misses a
setting or sensor your device has, report it here. A file from this repo can add it while keeping
everything ZHA's quirk shows.
