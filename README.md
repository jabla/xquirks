<img src="brand/logo.svg" alt="xquirks logo" width="112" align="right">

# xquirks

Extra quirks for ZHA (Home Assistant's Zigbee integration) for devices that Zigbee2MQTT supports
and ZHA does not yet or not with all their settings and sensors. They are generated from the
Zigbee2MQTT device library
([zigbee-herdsman-converters](https://github.com/Koenkk/zigbee-herdsman-converters)). The aim is to
get them into [zha-device-handlers](https://github.com/zigpy/zha-device-handlers), not to replace it.

> [!IMPORTANT]
> **xquirks is in beta.** Unexpected issues may occur. Please [report](#reporting-problems) them if
> nobody has reported them yet.

**Is your device here?** Install the integration [from the Marketplace](#install-from-the-marketplace),
or look it up in the [xquirks finder](https://jabla.github.io/xquirks/).

## What is here

- `custom_components/xquirks/`: the integration, which installs the quirk files for your ZHA
  devices once you confirm.
- `site/`: the [quirk finder](https://jabla.github.io/xquirks/), which matches your devices in your
  browser.
- `tools/probe.py`: the device test from a shell. The integration's **Check a device** does the same
  inside Home Assistant ([CONTRIBUTING.md](CONTRIBUTING.md)): its report makes a file "verified".
- The `content` branch: the quirk files with their `manifest.json`. Each `content-<n>` tag is one
  published state ([CONTENT.md](CONTENT.md)).

Each quirk file names its status in its docstring:

- generated: built from the Zigbee2MQTT definition, not yet checked on a device
- verified: checked on the device (with the integration's device check or the probe)
- upstream: zha-device-handlers covers the device fully, the file is no longer shipped

A file that is not verified also names its risk: low (readings and simple controls), medium (number
settings the device may limit differently) or high (covers, thermostats, files that replace ZHA's own
quirk, files with a known weak spot). The integration offers high-risk files only when you turn them
on in its options.

## Install from the Marketplace

Needs Home Assistant with ZHA, at least the release named in [`hacs.json`](hacs.json).

1. If ZHA has no custom quirks folder yet: create `/config/custom_zha_quirks` (File editor, Samba
   or SSH app), add these lines to `configuration.yaml` and restart Home Assistant:
   ```yaml
   zha:
     custom_quirks_path: /config/custom_zha_quirks/
   ```
2. Settings > Marketplace > Add from link: enter `https://github.com/jabla/xquirks` and select
   Add. The first time, it asks you to connect GitHub.
3. Search for "xquirks", open it and select Install. Its setup opens on its own or add it under
   Settings > Devices & services > Add integration > "xquirks".

The Marketplace looks for a new xquirks release every two days. An update asks for a restart of
Home Assistant in Settings > System > Repairs. After the restart, reload the Home Assistant page in
your browser so that the dialogs of xquirks show their texts.

## How the integration works

Every 6 hours and whenever you press **Check for quirk updates**, it fetches the newest state of the
quirk files that fits this xquirks release and your Home Assistant release. It matches the files to
your ZHA devices by manufacturer and model.

It writes nothing on its own: every install, update and removal goes into one change list in
Settings > System > Repairs. **Apply all** makes all changes at once, then reloads ZHA, so your
Zigbee devices are unavailable for a few seconds. If a step fails, it puts back what it changed.
**Not now** puts the list aside (Repairs > three-dot menu > Show ignored repairs).

After ZHA reloads, the integration asks each Tuya device that got a new or updated file for its
current values: these devices send them by themselves only when they join. A battery device may miss
the request and show its settings only when it next reports.

For a day after that, it watches what these devices send: values outside a plausible range, values
that never come, Tuya datapoints the file does not show. It only listens, it asks the devices nothing.
Then one notice per file says whether its values look off, look fine or did not come at all. Nothing
leaves your Home Assistant by itself.

**Check a device** under Settings > Devices & services > xquirks > Configure asks one device for its
current values. You mark which match the device. The last dialog opens the "Device test" form on
GitHub filled in with the report, which names no address and none of the names you gave. That is
how a file becomes verified.

To keep a device out, add it under Settings > Devices & services > xquirks > Configure > What xquirks
offers. A file
another device needs is installed anyway. The integration never writes or deletes a file it did not
write, a file you changed or a symlink. Its notices say what happened and what to do. "Quirk file
refused" should never appear: please report it with the integration's diagnostics.

## Uninstall

Removing only the integration leaves the installed files in place. To remove them too:

1. Settings > Devices & services > xquirks > Configure > What xquirks offers: add all your ZHA devices
   under **Devices**.
2. Apply the change list in Settings > System > Repairs. It removes the files and reloads ZHA.
   Where xquirks replaced ZHA's own quirk, that quirk takes the device back. Files you changed
   yourself stay: delete them from the custom quirks folder.
3. Delete the xquirks entry: Settings > Devices & services > xquirks > three-dot menu > Delete.
4. Uninstall xquirks in the Marketplace, then restart Home Assistant when Repairs asks for it.

Entities only an xquirks file added stay as unavailable. Delete them on the device's page.

## What it fetches

- `api.github.com`: the list of `content-<n>` tags, at each check.
- `raw.githubusercontent.com`: the small files of a tag, plus a quirk file the change list would
  write, always at the tag's commit.
- `www.zigbee2mqtt.io`: each device picture once. Your browser contacts it only when you click a
  picture.

A report reaches GitHub only when you submit a prefilled form yourself.

## What you can check

- The history is public: every state of the files is a commit on the `content` branch with a
  `content-<n>` tag. Tags never move. A newer state gets the next number.
- The guard runs at your end: before it offers or writes a file, the integration checks its sha256
  against the manifest and every statement against the fixed code that ships with the integration
  release. A new state can change which devices a quirk covers and its values, never the code that
  runs.

## Install by hand

1. Find the files for your devices with the [quirk finder](https://jabla.github.io/xquirks/):
   drop the ZHA diagnostics on it (Settings > Devices & services > Zigbee Home Automation >
   Download diagnostics) or paste the output of its template. The page reads the file in your
   browser and uploads nothing. Each matching file links to GitHub for the download.
2. Copy only those files into your custom quirks folder (step 1 above): ZHA loads every file in it.
3. Restart Home Assistant.

The finder names the Home Assistant release the files need. To remove a file, delete it and restart
Home Assistant.

## Upstream first

A file from this repo steps back as soon as your ZHA version ships a quirk for the device, even if
the file stays in your folder. One exception: where ZHA's quirk lacks settings or sensors the file
has, the integration offers the file as a change you confirm. Such a file keeps everything ZHA's
quirk shows under the same entity IDs and adds the rest. The integration removes it once ZHA's
quirk catches up.

Where an open zha-device-handlers pull request adds your device, testing it there helps most. The
finder and the change list link it. ZHA logs "Loaded custom quirks. Please contribute them to
zha-device-handlers" for every custom quirk. For files from this repo, report problems here instead
and do not submit the files upstream yourself.

## Reporting problems

Reports go through this repository's issue forms. [CONTRIBUTING.md](CONTRIBUTING.md) says which
one fits and what helps most.

## License

Apache-2.0 (see [LICENSE](LICENSE)), the license of zha-device-handlers. Third-party notices are in
[THIRD_PARTY.md](THIRD_PARTY.md).
