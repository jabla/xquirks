<img src="brand/logo.svg" alt="xquirks logo" width="112" align="right">

# xquirks

Extra quirks for ZHA (Home Assistant's Zigbee integration) for devices that Zigbee2MQTT supports and
ZHA does not yet or not with all their settings and sensors. They ship as device definitions: quirks
stored as data, generated from the Zigbee2MQTT device library
([zigbee-herdsman-converters](https://github.com/Koenkk/zigbee-herdsman-converters)). The aim is to
get them into [zha-device-handlers](https://github.com/zigpy/zha-device-handlers), not to replace it.

> [!IMPORTANT]
> **xquirks is in beta.** Unexpected issues may occur. Please [report](#reporting-problems) them if
> nobody has reported them yet.

**Is your device here?** Look it up in the [xquirks finder](https://jabla.github.io/xquirks/). It
shows the definitions of the latest release.

## How it works

The definitions ship inside the integration. Its setup writes two files into ZHA's custom quirks
folder: the start file `xquirks.py` and the options file `xquirks_options.json`. Each time ZHA
starts (with Home Assistant or when you reload it), the start file loads the definitions. ZHA
applies them like any custom quirk.

- A device that a quirk of yours or one of ZHA's own quirks covers keeps that quirk, apart from a
  few take-overs ([Upstream first](#upstream-first)).
- xquirks builds each definition with the quirk builder that ZHA's own quirks use. A definition is
  data and cannot bring code of its own.
- A definition that fails to build is skipped with a line in the log. ZHA loads the others.
- After each start of ZHA, a notice in Settings > System > Repairs lists the devices whose definition
  changed or waits for **Use high-risk definitions**. **OK** closes it until the next change.
- A Tuya device on a new or changed definition gets a data query: such devices send their values by
  themselves only when they join. A battery device may miss the query and show its values when it
  next reports.
- When a device leaves its definition, the entities only that definition added stay as unavailable.
  Delete them on the device's page.

Repairs also report problems with the setup: no custom quirks folder, quirks turned off in ZHA, a
missing or outdated start file or a broken options file.

## Install from the Marketplace

Needs Home Assistant with ZHA, at least the release named in [`hacs.json`](hacs.json). The
Marketplace is the successor of HACS, built into Home Assistant.

1. If ZHA has no custom quirks folder yet: create `/config/custom_zha_quirks` (File editor, Samba or
   SSH app), add these lines to `configuration.yaml` and restart Home Assistant:
   ```yaml
   zha:
     custom_quirks_path: /config/custom_zha_quirks/
   ```
2. Settings > Marketplace > Add from link: enter `https://github.com/jabla/xquirks` and select Add.
   The first time, it asks you to connect GitHub.
3. Search for "xquirks", open it and select Install. Its setup opens on its own. Otherwise add it
   under Settings > Devices & services > Add integration > "xquirks".
4. Setup offers to reload ZHA right away. Otherwise ZHA loads the definitions at the next restart of
   Home Assistant.

## Options

Settings > Devices & services > xquirks > Configure:

- **Devices xquirks leaves alone**: these devices run ZHA's own quirk, a quirk of yours or none instead.
- **Use high-risk definitions**: see [Risk levels](#risk-levels).
- **Reload ZHA now**: the changes apply right away. Otherwise they apply at the next restart of Home
  Assistant.

## Risk levels

Each definition has a risk level:

- low: readings and simple controls
- medium: number settings the device may limit differently, take-overs of ZHA's own quirk
- high: covers, thermostats and definitions with a known weak spot

A high-risk definition that is not verified on a device loads only with **Use high-risk
definitions**. The finder shows the risk of each definition.

## Updates

New and corrected definitions come with updates of the integration. The Marketplace looks for a new
release every two days. An update asks for a restart of Home Assistant in Settings > System >
Repairs. ZHA loads the new definitions at that restart. Reload the Home Assistant page in your
browser afterwards so that the dialogs of xquirks show their new texts.

Betas are GitHub pre-releases. The Marketplace offers them once you select **Include pre-releases**
in the menu of xquirks. To go back, select **Install another version** in the same menu.

## Reporting problems

Reports go through this repository's issue forms. [CONTRIBUTING.md](CONTRIBUTING.md) says which one
fits and what helps most.

### From the device's page

The page of each device on an xquirks definition has two buttons under Diagnostic: **Report a
problem (xquirks)** and, until the revision of its definition is verified, **Confirm it works
(xquirks)**. A press puts a notification under Notifications in the sidebar. If the definition has
open issues, it links them: a thumbs-up there helps as much as a new issue. Otherwise it links the
GitHub form, filled in with the definition, its revision, the versions and the device's values in
Home Assistant, without IEEE address or names. When GitHub does not answer, it links both.
Submitting a form or giving a thumbs-up needs a GitHub account.

I mark a revision as verified once two GitHub accounts confirmed that it works (the author of the "It
works" issue plus a thumbs-up) and no wrong-value issue is open for it. My own devices count like
anyone's. A high-risk definition also needs a test by hand. The release with the mark drops
**Confirm it works (xquirks)** from its devices.

### A device gets no definition

A definition steps back for a device when:

- a quirk of yours or one of ZHA's own quirks covers it
- you picked the device under **Devices xquirks leaves alone**
- the definition is high risk and high-risk definitions are off
- the device lacks an endpoint or cluster the definition needs
- Zigbee2MQTT gives the same name to another device

The log then has a line such as `xquirks: <definition> steps back for <IEEE address> (<reason>)`. To
see it, select **Enable debug logging** in the menu of xquirks (Settings > Devices & services >
xquirks) and reload ZHA. **Disable debug logging** then downloads the log. When the reason names an
endpoint, use the "Device layout" form.

## Privacy

xquirks asks GitHub only at a press of a button on a device's page, with the definition's id and
revision. GitHub also sees your IP address, as with any website you visit. The definitions come with
the integration, which the Marketplace downloads from GitHub. A report reaches GitHub only when you
submit a filled-in form or give a thumbs-up yourself.

## Uninstall

1. Delete the xquirks entry: Settings > Devices & services > xquirks > three-dot menu > Delete. This
   removes the start file and the options file.
2. Uninstall xquirks in the Marketplace.
3. Restart Home Assistant. ZHA then starts without the definitions. Where a definition took a device
   over from ZHA's own quirk, that quirk takes it back.

## Install by hand

1. Unpack `xquirks.zip` from the [latest release](https://github.com/jabla/xquirks/releases/latest)
   into `/config/custom_components/xquirks/`.
2. Restart Home Assistant, then add xquirks under Settings > Devices & services > Add integration.

For an update, replace the folder with the new release and restart Home Assistant.

## Upstream first

A definition steps back as soon as your ZHA version ships a quirk for the device. One exception:
where ZHA's quirk lacks settings or sensors that Zigbee2MQTT knows, a definition may take the device
over on purpose. Such a take-over keeps everything ZHA's quirk shows under the same entity IDs and
adds the rest. When a ZHA update changes that quirk, the device goes back to it until a release of
xquirks catches up.

Where an open zha-device-handlers pull request adds your device, testing it there helps most. ZHA
logs "Loaded custom quirks. Please contribute them to zha-device-handlers" for every custom quirk.
For xquirks definitions, report problems here instead and do not submit them upstream yourself.

## License

Apache-2.0 (see [LICENSE](LICENSE)), the license of zha-device-handlers. Third-party notices are in
[THIRD_PARTY.md](THIRD_PARTY.md).
