"""Markdown for the repair issues and fix flows (English, like the quirks' entity names).

Strings from the manifest, withdrawn.toml and the device registry go in as plain text (plain()) or
as known values only: the dialogs render markdown, images and links included.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from urllib.parse import quote, urlencode

from . import plan as plans
from .changes import LIMIT, Summary, plain
from .const import REPOSITORY
from .content import HIGH, RISKS, risk, risk_why, verified
from .plan import Device, Lost, Misfit, Plan

UPSTREAM_PR = "https://github.com/zigpy/zha-device-handlers/pull/{number}"
# A new issue in the xquirks repository through its "Device layout" form
# (.github/ISSUE_TEMPLATE/device-layout.yml): GitHub fills the form's fields from the query by their ids.
REPORT = f"https://github.com/{REPOSITORY}/issues/new"
REPORT_FORM = "device-layout.yml"
# GitHub answers a longer address with 414: past this the signature leaves its output clusters out,
# then gives way to a note.
REPORT_LIMIT = 6000
# Manage Zigbee device > Signature shows the quirk's signature on a device that runs one; the device's
# diagnostics hold the one before the quirk (original_signature), which a device layout report needs.
REPORT_TOO_LONG = ("Too long for the link. Paste the device's diagnostics here: the device's page > three-dot menu > Download diagnostics."
                   " They also hold its network address and attribute values, which this issue then shows."
                   " For a device that runs no quirk, Manage Zigbee device > Signature in that menu is enough.")

STATUS = {
    "generated": "generated, not verified on a device yet",
    "verified": "verified on a device",
}

# The reasons for a risk (content.RISK_REASONS), as the change list and the notices say them. The
# finder says the same (site/content.js RISK_REASONS).
RISK_REASONS = {
    "cover": "It controls a cover.",
    "thermostat": "It controls a thermostat.",
    "state_dependency": "One of its settings changes how another value reads.",
    "mixed_manufacturer_code": "It writes a setting with a manufacturer code the rest of its cluster does not use.",
    "take_over_drops": "It replaces ZHA's own quirk and leaves out features that quirk shows.",
    "defect_found": "A device test found a fault. Its fix is not verified yet.",
    "number_settings": "It changes number settings. The device may accept a smaller range.",
    "local_occupancy": "ZHA shows delay settings for it that never reach the device.",
    "take_over": "It replaces ZHA's own quirk.",
    "alias_differs": "ZHA's quirk reads one of its values differently.",
}
# What the change list compares when an update changes a file's risk: verified first, then the risks.
TIERS = ("verified", *RISKS)

# Why a file of ours goes.
REMOVALS = {
    plans.MOVED: "another xquirks file is for one of its devices",
    plans.SHIPPED: "ZHA ships a quirk for this device now. This file steps back",
    plans.HANDED_BACK: "ZHA can handle this device by itself now, xquirks is no longer needed for it",
    plans.LEFT_OUT: "the devices that used it are left out (disabled or excluded in the options)",
    plans.UNUSED: "none of your ZHA devices uses it any more",
    plans.OUTSIDE: "it is in a folder ZHA no longer loads custom quirks from",
}

# After a removal: the entities ZHA showed only for the file stay in Home Assistant.
ORPHANS = "    - entities only this file added stay as unavailable: delete them on the device's page"


def status(entry: dict) -> str:
    return STATUS.get(entry["status"], "unknown")


def reasons(entry: dict) -> str:
    """The reasons for an entry's risk as sentences; empty without one this release knows."""
    return " ".join(RISK_REASONS[code] for code in risk_why(entry))


def tier(entry: dict) -> str:
    """How the change list ranks a file (TIERS): verified, else its risk. Also of a record, which keeps
    the status and risk of the version written."""
    return "verified" if verified(entry) else risk(entry)


def risk_sentence(entry: dict) -> str:
    """The risk of a file that is not verified, with its reasons; empty for a verified one."""
    if verified(entry):
        return ""
    found = risk(entry)
    return " ".join(part for part in (f"Risk: {found}.", reasons(entry), "Test it before you rely on it." if found == HIGH else "") if part)


def label(entry: dict | None, name: str) -> str:
    """How the notices name a quirk file: the device it is for, the file name when the state in use no longer has it."""
    if not entry:
        return name
    device = f"{entry['vendor']} {entry['model']}"
    return f"{device}: {entry['description']}" if entry.get("description") else device


def offer_title(entry: dict, count: int) -> dict[str, str]:
    return {"vendor": entry["vendor"], "model": entry["model"], "count": str(count), "quirk": label(entry, "")}


def runs(device: Device, ours: frozenset[str] = frozenset()) -> str:
    """What ZHA runs on the device, for the dialogs (ours: the names of the files of ours in the folder)."""
    quirk = device.quirk
    if quirk is None:
        return "not known yet"
    if quirk == "":
        return "no quirk"
    if quirk.startswith("zhaquirks."):
        return f"the quirk ZHA ships (`{quirk}`)"
    if f"{quirk}.py" in ours:
        return f"`{quirk}` from xquirks"
    if quirk.startswith(plans.PREFIX):
        return f"`{quirk}`, a file xquirks does not manage (yours or gone since ZHA loaded the folder)"
    return f"the custom quirk `{quirk}`"


def _known(ids, devices: dict[str, Device]) -> list[str]:
    """The ids of devices that are still known, by name: a list made from a plan can name a device a later refresh no
    longer has (the change list dialog renders after its downloads)."""
    return sorted((i for i in ids if i in devices), key=lambda i: devices[i].name)


def device_list(ids, devices: dict[str, Device], ours: frozenset[str] = frozenset()) -> str:
    lines = [
        f"- {plain(devices[i].name)} ({plain(devices[i].manufacturer)} {plain(devices[i].model)}), runs {runs(devices[i], ours)}"
        for i in _known(ids, devices)
    ]
    return "\n".join(lines) or "- no device"


def report_url(entry: dict, name: str, device: Device, signature: dict, why: str, version: str) -> str:
    """A prefilled "Device layout" issue that reports a device a file does not fit: the file and its
    revision, why, the Home Assistant version and the device's signature as it joined (zigpy's
    original signature: manufacturer, model, endpoints with their clusters, as compact JSON). Nothing
    that names the device in the user's home: no address, no name. A link past REPORT_LIMIT leaves
    the output clusters out, then the signature (with a note on where to find it)."""

    def layout(outputs: bool) -> str:
        return json.dumps({
            "manufacturer": device.manufacturer,
            "model": device.model,
            "class": "zigpy.device.Device",
            "endpoints": {
                str(ep): {
                    # None where zigpy does not know them (manager._signature).
                    "profile_id": f"0x{data['profile_id']:04x}" if data["profile_id"] is not None else None,
                    "device_type": f"0x{data['device_type']:04x}" if data["device_type"] is not None else None,
                    "input_clusters": [f"0x{c:04x}" for c in data["input_clusters"]],
                    "output_clusters": [f"0x{c:04x}" for c in data["output_clusters"]] if outputs else [],
                }
                for ep, data in sorted(signature.items())
            },
        }, separators=(",", ":"))

    def link(text: str) -> str:
        query = {
            "template": REPORT_FORM,
            "title": f"Device layout: {device.manufacturer} {device.model}",
            "file": f"{name} (revision {entry.get('revision') or 'unknown'})",
            "reason": why,
            "ha": version,
            "signature": text,
        }
        return f"{REPORT}?{urlencode(query, quote_via=quote, safe='')}"

    for text in (layout(True), layout(False), REPORT_TOO_LONG):
        url = link(text)
        if len(url) <= REPORT_LIMIT:
            return url
    return url


def misfit_list(misfit: Misfit, devices: dict[str, Device], links: Mapping[str, str]) -> str:
    """The devices a file does not fit, each with why and its report link (links: device id -> URL)."""
    lines = []
    for device_id, why in sorted(misfit.devices, key=lambda item: devices[item[0]].name if item[0] in devices else ""):
        if device_id not in devices:
            continue
        device = devices[device_id]
        link = f" [Report this device]({links[device_id]})" if device_id in links else ""
        lines.append(f"- {plain(device.name)} ({plain(device.manufacturer)} {plain(device.model)}): {plain(why)}.{link}")
    return "\n".join(lines) or "- no device"


def name_list(names: list[str]) -> str:
    return "\n".join(f"- {name}" for name in names) or "- nothing"


def _sentence(text: str) -> str:
    """The text with one full stop at its end (a withdrawal's reason may bring its own)."""
    return text if text.endswith((".", "!", "?")) else f"{text}."


def _names(ids, devices: dict[str, Device]) -> str:
    return ", ".join(plain(devices[i].name) for i in _known(ids, devices)) or "no device"


def _lost(lost: Lost, devices: dict[str, Device]) -> str:
    why = {
        plans.NO_FILE: "xquirks no longer has a quirk for it",
        plans.TWO_FILES: "two xquirks files claim it",
        plans.FILE_WITHDRAWN: f"the revision of `{lost.file}` on offer is withdrawn",
        plans.FILE_REFUSED: f"`{lost.file}` cannot be written: the guard refused it or it cannot be downloaded (see its repair issue)",
        plans.FILE_ON_HOLD: f"`{lost.file}` is on hold while the type check of the Tuya quirk files fails (see its repair issue)",
        plans.FILE_CONFLICT: f"`{lost.file}` stays out: two xquirks files claim one of its devices",
        plans.USERS_NAME: f"a file of yours is named `{lost.file}`",
        plans.USERS_QUIRK: f"`{lost.file}` would also claim {_names(lost.others, devices)}, on a custom quirk of yours",
        plans.FILE_MISFIT: f"its endpoints do not fit `{lost.file}` (see the notice for that file)",
        plans.FILE_NO_TAKE_OVER: f"`{lost.file}` does not take it over from ZHA's own quirk",
        plans.FILE_HELD_BACK: f"`{lost.file}` is held back until it is verified on a device (see its notice)",
    }[lost.reason] + (". ZHA's own quirk takes it back." if lost.back else "")
    line = f"    - {_names([lost.device], devices)} gets no file from xquirks: {why}"
    return "\n".join([line, *("    " + item for item in _new_ids(lost.new_ids))])


def _moves(moves: tuple[tuple[str, str], ...], devices: dict[str, Device]) -> list[str]:
    return [f"    - {_names([device], devices)} moves to `{file}`" for device, file in moves]


def _left_out(ids: tuple[str, ...], devices: dict[str, Device]) -> list[str]:
    return [f"    - also applies to {_names(ids, devices)}, which you left out: ZHA applies a file to every device it lists"] if ids else []


def _listed(names: list[str]) -> str:
    """At most LIMIT entity names (as many as an update's changes), the rest counted."""
    shown = [plain(name) for name in names[:LIMIT]]
    more = len(names) - len(shown)
    return ", ".join(shown) + (f" and {more} more" if more else "")


def _new_ids(names) -> list[str]:
    """The entities that get another ID in Home Assistant when ZHA's own quirk and the file swap."""
    return [f"    - new IDs in Home Assistant for {_listed(list(names))}: automations that use them need updating"] if names else []


def _taken_over(ids: tuple[str, ...], entry: dict | None, devices: dict[str, Device], new_ids: tuple[str, ...] = ()) -> list[str]:
    """For a file that takes these devices over from ZHA's own quirk: what each side shows; how to keep ZHA's quirk."""
    over = (entry or {}).get("over_zha")
    if not ids or over is None:
        return []
    line = f"    - replaces ZHA's own quirk on {_names(ids, devices)}, which shows less"
    if over["adds"]:
        line += f". Adds: {_listed(over['adds'])}"
    if over["zha_only"]:
        line += f". Only in ZHA's quirk: {_listed(over['zha_only'])}"
    return [line, *_new_ids(new_ids), f"    - to keep ZHA's quirk, leave {_names(ids, devices)} out in the xquirks options"]


def _pull_requests(entry: dict | None) -> list[str]:
    """The open upstream pull requests of an entry in one line; each link is built from the pull
    request's number (an int, content.check_entry), never taken from the manifest."""
    links = [f"[#{number}]({UPSTREAM_PR.format(number=number)})" for number in (int(pr["number"]) for pr in (entry or {}).get("upstream_open_prs", []))]
    if len(links) == 1:
        return [f"    - an open zha-device-handlers pull request adds this device. Testing it helps more: {links[0]}"]
    if links:
        return [f"    - open zha-device-handlers pull requests add this device. Testing them helps more: {', '.join(links)}"]
    return []


def _risk_raised(entry: dict | None, record: dict | None) -> list[str]:
    """For an update whose offered version ranks higher than the version written (its record): its
    risk now, with the reasons. Nothing for a record that does not say: it counts as high."""
    if not entry or not record or TIERS.index(tier(entry)) <= TIERS.index(tier(record)):
        return []
    return [" ".join(part for part in (f"    - risk now {tier(entry)}.", reasons(entry)) if part)]


def change_list(
    plan: Plan,
    devices: dict[str, Device],
    label: Callable[[str], str],
    withdrawn: Mapping[tuple[str, str], str],
    summaries: Mapping[str, Summary | None],
    entry: Callable[[str], dict | None] = lambda name: None,
    record: Callable[[str], dict | None] = lambda path: None,
) -> str:
    """The change list for its dialog: one bullet per change, with its devices and why (entry: the
    manifest entry of a file name, record: the record of an installed file's path)."""
    lines = []
    for removal in plan.remove:
        if removal.withdrawn and removal.reason != plans.OUTSIDE:
            why = f"its revision {removal.revision} is withdrawn: {plain(withdrawn.get((removal.name, removal.revision), ''))}"
        else:
            why = REMOVALS[removal.reason]
        lines.append(f"- **Remove** `{removal.name}` ({plain(label(removal.name))}): {_sentence(why)}")
        if removal.left_back:
            lines.append(f"    - ZHA's own quirk takes {_names(removal.left_back, devices)} back")
        lines += _new_ids(removal.new_ids) + _moves(removal.moves, devices) + [_lost(lost, devices) for lost in removal.lost]
        if removal.in_use:
            lines.append(ORPHANS)
    for update in plan.update:
        line = f"- **Update** `{update.name}` ({plain(label(update.name))}) for {_names(update.devices, devices)}."
        if update.withdrawn:
            line += f" The installed revision {update.revision} is withdrawn: {plain(withdrawn.get((update.name, update.revision), ''))}"
        lines.append(line)
        lines += _risk_raised(entry(update.name), record(update.path))
        if summary := summaries.get(update.path):
            items = summary.items()
            lines += [f"    - {item}" for item in items[:LIMIT]]
            if len(items) > LIMIT:
                lines.append(f"    - and {len(items) - LIMIT} more (all of them in the diagnostics)")
        lines += _moves(update.moves, devices) + [_lost(lost, devices) for lost in update.lost] + _left_out(update.left_out, devices)
        lines += _taken_over(update.took_over, entry(update.name), devices, update.new_ids)
    for install in plan.install:
        found = entry(install.name)
        known = " ".join(part for part in (f" Status: {status(found)}.", risk_sentence(found)) if part) if found else ""
        lines.append(f"- **Install** `{install.name}` ({plain(label(install.name))}) for {_names(install.devices, devices)}.{known}")
        lines += _taken_over(install.took_over, found, devices, install.new_ids) + _left_out(install.left_out, devices) + _pull_requests(found)
    return "\n".join(lines)


# The device check and the self-check

# The issue forms a device check and a self-check open (.github/ISSUE_TEMPLATE): "Device test" when the
# user found nothing wrong, "Wrong or missing value" otherwise. GitHub fills their fields by id.
VERIFY_FORM = "verify.yml"
PROBLEM_FORM = "wrong-value.yml"
CHECK_TOO_LONG = ("Too long for the link. Download the diagnostics of the xquirks integration (Settings > Devices & services > xquirks >"
                  " three-dot menu > Download diagnostics) and drop the file here: the report is in it under device_checks.")
# Findings a self-check's link carries at most (a self-check keeps at most 20 per device).
MAX_PROBLEM_LINES = 20
# How fresh a value is, as the report explains it.
# A device check while a change of the file waits for a ZHA reload (CheckResult.waiting): in the dialog, in the report.
WAITING = "A change xquirks made to this file waits for a ZHA reload. The device runs the version ZHA loaded before."
WAITING_SHORT = "a change of it waits for a ZHA reload"
FRESH_NOTE = ("From the device: now = it sent the value during the check, since load = it sent it earlier, after ZHA loaded the file,"
              " stored = no value from it since then (it may be from before this file).")


def _cell(value: str) -> str:
    return plain(value).replace("\n", " ")


def check_table(result, answers: Mapping[str, str] | None = None, *, compact: bool = False) -> str:
    """The file's entities of a device check as a markdown table: with the user's answers (answers:
    unique id suffix -> yes, no or not checked) for the report, without them for the dialog. Compact
    leaves the unique id suffix and the category out."""
    head = (["Matches"] if answers is not None else []) + ["Entity"] + ([] if compact else ["Unique id suffix", "Category"]) + ["State", "Unit", "From the device", "Remark"]
    lines = ["| " + " | ".join(head) + " |", "|" + " --- |" * len(head)]
    for row in result.rows:
        cells = ([answers.get(row.suffix, "not checked")] if answers is not None else []) + [row.name]
        cells += [] if compact else [row.suffix, row.category]
        cells += [row.state, row.unit, row.fresh, row.remark]
        lines.append("| " + " | ".join(_cell(cell) for cell in cells) + " |")
    return "\n".join(lines)


def _asked(result) -> str:
    """What the device check asked and what came of it (nothing to say when it asked nothing)."""
    return plain(result.asked) + (f", {plain(result.answer)}" if result.answer else "")


# A refused Tuya datapoint whose reason the log did not give (watch.py: the frame showed the refusal).
NO_REASON = "no reason in the log"


def _refused(result) -> str:
    """The Tuya datapoints its Tuya cluster refused during a device check, each with why."""
    return ", ".join(f"{dp} ({plain(reason) or NO_REASON})" for dp, reason in result.refused)


def check_dialog(result) -> str:
    """What the device check shows above its two lists: what was asked, the entities, what fresh means."""
    lines = [f"Asked the device: {_asked(result)}."]
    if result.waiting:
        lines.append(WAITING)
    if result.unmapped:
        lines.append(f"Tuya datapoints this file does not show: {', '.join(str(dp) for dp in result.unmapped)}.")
    if result.refused:
        lines.append(f"Tuya datapoints this file refused: {_refused(result)}.")
    return "\n\n".join([" ".join(lines), check_table(result), FRESH_NOTE])


def check_report(result, answers: Mapping[str, str], *, compact: bool = False) -> str:
    """The report of a device check: what the issue gets. Manufacturer and model as zigpy reports
    them, the file and its revision, entity names as the integration gave them: no address, no name
    of a device or an area, no entity id."""
    changed = ", changed on disk since xquirks wrote it" if result.changed else ""
    waiting = f" ({WAITING_SHORT})" if result.waiting else ""
    lines = [
        f"## xquirks device check: {plain(result.manufacturer)} {plain(result.model)}",
        "",
        f"- File: `{result.file}`, revision {result.revision or 'unknown'}{waiting}{changed}",
        f"- Quirk ZHA applied: `{result.quirk}`",
        f"- Firmware: {plain(result.firmware or 'unknown')}",
        f"- Versions: {result.versions}, quirk list {result.content or 'unknown'}",
        f"- Asked the device: {_asked(result)}",
    ]
    if result.unmapped:
        lines.append(f"- Tuya datapoints this file does not show: {', '.join(str(dp) for dp in result.unmapped)}")
    if result.refused:
        lines.append(f"- Tuya datapoints this file refused: {_refused(result)}")
    lines += [f"- Checked: {result.time}", "", check_table(result, answers, compact=compact), "", FRESH_NOTE]
    if result.disabled:
        lines += ["", f"Disabled in Home Assistant (not part of the check): {', '.join(plain(name) for name in result.disabled)}"]
    if result.others:
        lines += ["", f"Other entities of the device (not part of the check): {', '.join(plain(name) for name in result.others)}"]
    if result.left_over:
        lines += ["", f"Left over from an earlier quirk (not part of the check): {', '.join(plain(name) for name in result.left_over)}"]
    return "\n".join(lines)


def _issue(form: str, title: str, fields: Mapping[str, str]) -> str:
    return f"{REPORT}?{urlencode({'template': form, 'title': title, **fields}, quote_via=quote, safe='')}"


def _device_name(entry: dict | None, manufacturer: str, model: str) -> str:
    return f"{entry['vendor']} {entry['model']}" if entry else f"{manufacturer} {model}"


def check_url(entry: dict | None, result, answers: Mapping[str, str], version: str) -> str:
    """The issue a device check goes to, filled in: "Device test" with the report when the user marked
    nothing as wrong, "Wrong or missing value" with the wrong entities and the report otherwise. Past
    REPORT_LIMIT the report drops columns, then gives way to a note on the diagnostics, then the last
    wrong entities are counted instead of listed."""
    name = _device_name(entry, result.manufacturer, result.model)
    base = {"file": f"quirks/{result.file}", "revision": result.revision or "unknown", "ha": version}
    wrong = [f"- {_cell(row.name)}: Home Assistant shows {_cell(row.value) or 'nothing'}, the device shows something else"
             for row in result.rows if answers.get(row.suffix) == "no"]

    def link(report: str, listed: int) -> str:
        if not wrong:
            return _issue(VERIFY_FORM, f"Verify: {name}", {**base, "report": report})
        more = [f"- and {len(wrong) - listed} more (all of them in the report)"] if listed < len(wrong) else []
        return _issue(PROBLEM_FORM, f"{name}: ", {**base, "problem": "\n".join(wrong[:listed] + more), "diagnostics": report})

    for report in (check_report(result, answers), check_report(result, answers, compact=True), CHECK_TOO_LONG):
        url = link(report, len(wrong))
        if len(url) <= REPORT_LIMIT:
            return url
    for listed in range(len(wrong) - 1, 0, -1):
        url = link(CHECK_TOO_LONG, listed)
        if len(url) <= REPORT_LIMIT:
            return url
    return url


def self_check_list(lines: Mapping[str, list[str]], devices: dict[str, Device]) -> str:
    """The devices of a self-check, each with what it found (device id -> lines)."""
    out = []
    for device_id, found in sorted(lines.items(), key=lambda item: devices[item[0]].name if item[0] in devices else ""):
        if device_id not in devices:
            continue
        device = devices[device_id]
        out.append(f"- {plain(device.name)} ({plain(device.manufacturer)} {plain(device.model)})")
        out += [f"    - {plain(line)}" for line in found]
    return "\n".join(out) or "- no device"


def problem_url(entry: dict | None, name: str, revision: str, found: list[tuple[str, str, list[str]]], version: str) -> str:
    """A "Wrong or missing value" issue with what a self-check found, per device by manufacturer and
    model (found: [(manufacturer, model, findings)]). The diagnostics are left to the user."""
    rows = [f"- {plain(manufacturer)} {plain(model)}: {plain(line)}" for manufacturer, model, lines in found for line in lines][:MAX_PROBLEM_LINES]
    manufacturer, model = (found[0][0], found[0][1]) if found else ("", "")
    while True:
        problem = "\n".join(["Found by the self-check of the xquirks integration:", *rows])
        url = _issue(PROBLEM_FORM, f"{_device_name(entry, manufacturer, model)}: ", {"file": f"quirks/{name}", "revision": revision, "ha": version, "problem": problem})
        # Past REPORT_LIMIT the last findings stay out (the notice lists more of them).
        if len(url) <= REPORT_LIMIT or len(rows) <= 1:
            return url
        rows.pop()
