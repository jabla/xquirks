# Contributing

Thanks for helping. Most help needs no code: you report what a device you own does. The definitions
are generated from the Zigbee2MQTT device library, so nobody edits them by hand: I turn reports into
corrections, which reach you with a later release of the integration.

Reports go through the issue forms of this repository. The buttons on a device's page link the open
issues of its definition or the form filled in ([README](README.md#from-the-devices-page)). The
finder fills the forms in too. If an open issue fits, a thumbs-up or a comment there is enough.

## A wrong or missing value

Use the "Wrong or missing value" form for a wrong value, a missing entity or a setting that does not
stick. Its button is **Report a problem (xquirks)**. For a wrong value, two or three pairs of what
Home Assistant and the device's display or app show at the same time help most: they become the
expected values of the fix.

## A definition that works

Use the "It works" form when a device on a definition shows the right values. Its button is
**Confirm it works (xquirks)**. Check the values against the device itself: its display, its app or
what it does.

An "It works" issue confirms one revision of the definition. The revision is computed from the
definition: a release that changes the definition gives it a new revision, which needs confirmations
of its own. [From the device's page](README.md#from-the-devices-page) in the README says when a
revision counts as verified.

A confirmation of a high-risk definition helps most ([Risk levels](README.md#risk-levels)): the
finder lists these definitions under "High risk".

## A definition that does not fit your device

When a definition steps back because your device lacks an endpoint or cluster it needs, use the
"Device layout" form. The log names the reason
([A device gets no definition](README.md#a-device-gets-no-definition)).

## A device without a definition

Look it up in the [finder](https://jabla.github.io/xquirks/) first. If Zigbee2MQTT supports it and
it has no definition here, use the "Device without a definition" form with its Zigbee2MQTT device
page. Some kinds of devices get no definition yet: the form lists them.

## Translations

The integration's texts are in `custom_components/xquirks/translations/<language>.json`. Pull
requests that add or improve a language are welcome. CI checks that each file has the keys and
placeholders of `en.json`. A change to the English text goes into both `en.json` and `strings.json`.

## The integration or the finder

Use the "Bug in the integration or the finder" form or send a pull request. The integration's code
is in `custom_components/xquirks/`, the finder's in `site/`. [FORMAT.md](FORMAT.md) describes the
definitions in `custom_components/xquirks/definitions.jsonl`. Tests:
`pip install -r tests_hacs/requirements.txt`, then `python -m pytest tests_hacs` (the finder's tests
need node) and `python -m pytest tests_loader`.

## Upstream

Wrong values of a quirk that ships with ZHA belong to
[zha-device-handlers](https://github.com/zigpy/zha-device-handlers/issues). If ZHA's quirk misses a
setting or sensor your device has, use the "Device without a definition" form here. A definition can
then take the device over ([Upstream first](README.md#upstream-first)).
