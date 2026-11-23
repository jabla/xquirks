// The finder page: reads diagnostics or the template's output in the browser and matches them with matcher.js against
// the definitions of the latest release (definitions.js).

import { LOADING, failure, loadDefinitions, notReady, releaseUrl, riskShown } from "./definitions.js";
import { TEMPLATE, catches, compareDevices, compareStrings, devicesFromJson, hints, matches } from "./matcher.js";

const REPO = document.documentElement.dataset.repo;
const $ = (id) => document.getElementById(id);

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs || {})) {
    if (value !== null && value !== undefined) node.setAttribute(name, value);
  }
  // Text goes in as text nodes: names from a diagnostics file never become markup.
  node.append(...children.flat(Infinity).filter((child) => child !== null && child !== undefined && child !== false));
  return node;
}

// kind: "problem" shows the message in red, "quiet" only announces it (the results show it).
const say = (text, kind = "", where = "status") => {
  const node = $(where);
  node.className = kind;
  if (text && node.textContent === text) {
    // The same message again: cleared first, so that it shows and is announced once more.
    node.textContent = "";
    requestAnimationFrame(() => {
      node.textContent = text;
    });
    return;
  }
  node.textContent = text;
};

const plural = (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`;
// Names in quotes where spaces at the ends or emptiness would not show ("AOYAN " is a name of its own).
const shown = (name) => (!name || /^\s|\s$/.test(name) ? JSON.stringify(name) : name);
const deviceText = ([manufacturer, model]) => `${shown(manufacturer)} / ${shown(model)}`;
// A name of a definition: null stands for any manufacturer or model.
const nameText = ([manufacturer, model]) => `${manufacturer === null ? "any manufacturer" : shown(manufacturer)} / ${model === null ? "any model" : shown(model)}`;
const DOT = " \u00b7 ";

for (const link of document.querySelectorAll("[data-repo-link]")) {
  link.href = `https://github.com/${REPO}${link.dataset.repoLink ? `/${link.dataset.repoLink}` : ""}`;
}
$("template").textContent = TEMPLATE;

function status(definition) {
  return definition.verified ? el("span", { class: "badge verified" }, "verified") : el("span", { class: "badge generated" }, "generated");
}

// The risk of a definition that is not verified, next to its status; why in the tooltip.
function riskBadge(definition) {
  const risk = riskShown(definition);
  return risk ? [" ", el("span", { class: `badge risk-${risk.tier}`, title: risk.title }, risk.badge)] : null;
}

function riskNote(definition) {
  const note = riskShown(definition)?.note;
  return note ? el("span", { class: "note" }, note) : null;
}

function revision(definition) {
  return el("span", { class: "revision", title: "Revision" }, definition.rev);
}

// The targets of an alias: all of them, or those of the groups whose fingerprints catch the device.
function targets(definition, device) {
  const groups = definition.alias.filter(({ fingerprints }) => !device || fingerprints.some((name) => catches(name, device)));
  return [...new Set(groups.flatMap((group) => group.targets.map(nameText)))].join(", ");
}

function aliasNote(definition, device) {
  return definition.alias ? el("span", { class: "note" }, `Alias: lets the quirk that ships with ZHA for ${targets(definition, device)} claim this name too.`) : null;
}

function takeOverNote(definition) {
  return definition.takeOver ? el("span", { class: "note" }, "ZHA ships its own quirk for this device, which shows less: this definition takes the device over and keeps everything that quirk shows.") : null;
}

// A link to one of the repo's issue forms (.github/ISSUE_TEMPLATE), its fields filled in from the query (GitHub fills a
// form field from the parameter with its id). Nothing is sent until the person submits the form on GitHub.
function issueLink(text, template, fields) {
  const url = new URL(`https://github.com/${REPO}/issues/new`);
  url.searchParams.set("template", template);
  for (const [name, value] of Object.entries(fields)) if (value) url.searchParams.set(name, value);
  return el("a", { href: url.href, title: "Opens the form on GitHub, filled in" }, text);
}

// The Home Assistant version of the diagnostics read last (ZHA diagnostics name it at the top).
let haVersion = null;

function reports(definition, device) {
  const fields = { title: `${device[0]} ${device[1]}: `, file: definition.id, revision: definition.rev, ha: haVersion };
  // Until the revision is verified, as the button "Confirm it works (xquirks)" on the device's page.
  const works = definition.verified ? null : [DOT, issueLink("Confirm it works", "works.yml", { ...fields, title: `${device[0]} ${device[1]}: works` })];
  return el("span", { class: "reports" }, issueLink("Report a problem", "wrong-value.yml", fields), works);
}

function definitionItem(definition, device) {
  return el(
    "li",
    {},
    el("code", { class: "id" }, definition.id),
    " ",
    revision(definition),
    " ",
    status(definition),
    riskBadge(definition),
    riskNote(definition),
    aliasNote(definition, device),
    takeOverNote(definition),
    reports(definition, device),
  );
}

function deviceCard(device, definitions) {
  return el("article", { class: "device" }, el("h3", {}, deviceText(device)), el("ul", {}, definitions.map((definition) => definitionItem(definition, device))));
}

// What loading the definitions came to (definitions.js loadDefinitions).
let loaded = LOADING;

// The found items grouped by device, in the order of the devices.
function byDevice(found) {
  const groups = new Map();
  for (const { device, definition } of found) {
    const key = JSON.stringify(device);
    if (!groups.has(key)) groups.set(key, { device, definitions: [] });
    groups.get(key).definitions.push(definition);
  }
  return groups;
}

function show(devices, source) {
  const matched = byDevice(matches(loaded.definitions, devices));
  const groups = [...matched.values()].sort((a, b) => compareDevices(a.device, b.device));
  const rest = devices.filter((device) => !matched.has(JSON.stringify(device)));
  // A name that matches no definition but may be a device a definition renamed (matcher.js hints): shown apart, never as a match.
  const maybe = byDevice(hints(loaded.definitions, rest));
  const unknown = rest.filter((device) => !maybe.has(JSON.stringify(device)));

  $("summary").textContent = `${plural(devices.length, "device")} read from ${source}: ${groups.length} with a definition here, ${rest.length} without.`;
  $("matched").replaceChildren(...groups.map(({ device, definitions }) => deviceCard(device, definitions)));
  $("install").hidden = groups.length === 0;
  $("hinted").hidden = maybe.size === 0;
  $("hinted-list").replaceChildren(
    ...[...maybe.values()].map(({ device, definitions }) => el("li", {}, deviceText(device), ": may be ", definitions.map((definition, i) => [i ? ", " : "", definition.id]))),
  );
  $("unmatched").hidden = unknown.length === 0;
  $("unmatched-list").replaceChildren(
    ...unknown.map((device) => el("li", {}, deviceText(device), DOT, issueLink("report", "missing-device.yml", { title: `Missing: ${device[0]} ${device[1]}`, fingerprint: deviceText(device) }))),
  );
  $("results").hidden = false;
  say(groups.length ? `Found definitions for ${plural(groups.length, "device")}.` : `None of your ${plural(devices.length, "device")} has a definition here.`, "quiet");
  $("results-heading").focus();
}

// The devices in a JSON text, null (and a message) when it is no JSON.
function read(text, what) {
  try {
    const value = JSON.parse(text.replace(/^\uFEFF/, ""));
    const version = value?.home_assistant?.version;
    if (typeof version === "string" && /^\d{4}\.\d{1,2}\.\d+/.test(version)) haVersion = version;
    return devicesFromJson(value);
  } catch {
    say(`${what} is not JSON. Use the ZHA diagnostics file or the template's result.`, "problem");
    return null;
  }
}

function useDevices(devices, source) {
  if (!devices) return;
  if (!devices.length) {
    say(`No devices in ${source}: it needs objects with "manufacturer" and "model", as in ZHA diagnostics, or the template's result.`, "problem");
    $("results").hidden = true;
    return;
  }
  try {
    show(devices, source);
  } catch (error) {
    say(`Could not show the results (${error?.message ?? error}). Please report this.`, "problem");
  }
}

async function readFiles(files) {
  if (!files.length) return;
  const waiting = notReady(loaded);
  if (waiting) {
    say(waiting, "problem");
    return;
  }
  const found = new Map();
  haVersion = null;
  for (const file of files) {
    const devices = read(await file.text(), file.name);
    if (!devices) return;
    for (const device of devices) found.set(JSON.stringify(device), device);
  }
  useDevices([...found.values()].sort(compareDevices), files.length === 1 ? files[0].name : plural(files.length, "file"));
}

$("file").addEventListener("change", (event) => {
  readFiles([...event.target.files]);
  event.target.value = "";
});

// Drops anywhere on the page count, so a near miss does not open the file in the browser.
const drop = $("drop");
document.addEventListener("dragover", (event) => {
  event.preventDefault();
  drop.classList.add("over");
});
document.addEventListener("dragleave", (event) => {
  if (!event.relatedTarget) drop.classList.remove("over");
});
document.addEventListener("drop", (event) => {
  event.preventDefault();
  drop.classList.remove("over");
  readFiles([...(event.dataTransfer?.files || [])]);
});

$("paste-go").addEventListener("click", () => {
  const waiting = notReady(loaded);
  if (waiting) {
    say(waiting, "problem");
    return;
  }
  const text = $("paste").value.trim();
  if (!text) {
    say("Paste the template's result first.", "problem");
    return;
  }
  haVersion = null;
  useDevices(read(text, "The pasted text"), "the pasted text");
});

$("copy-template").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(TEMPLATE);
    say("Template copied.");
  } catch {
    getSelection().selectAllChildren($("template"));
    say("Copying is blocked here: the template is selected, copy it with Ctrl+C.");
  }
});

// The table of all definitions: one row each, filtered in place.
function cell(label, className, ...children) {
  return el("td", { "data-label": label, class: className, role: "cell" }, el("div", {}, ...children));
}

// What the "Show" choice keeps.
const KINDS = {
  all: () => true,
  verified: (definition) => definition.verified,
  high: (definition) => riskShown(definition)?.tier === "high",
  medium: (definition) => riskShown(definition)?.tier === "medium",
  low: (definition) => riskShown(definition)?.tier === "low",
};

function row(definition) {
  const names = definition.names.map((name) => el("span", {}, nameText(name)));
  const claimed = definition.claimed.map((name) => el("span", { class: "except" }, `except ${nameText(name)}`));
  const notes = [definition.alias ? `alias of ${targets(definition, null)}` : null, definition.takeOver ? "over ZHA's quirk" : null].filter(Boolean);
  const tr = el(
    "tr",
    { role: "row" },
    cell("Definition", "definition-cell", el("strong", {}, definition.id), revision(definition)),
    cell("Manufacturer / model", "names", names, claimed),
    cell("Status", null, status(definition), riskBadge(definition), notes.map((note) => el("span", { class: "note" }, note))),
  );
  const words = [definition.id, definition.rev, ...definition.names.flat(), ...(definition.alias || []).flatMap((group) => group.targets.flat())];
  tr.dataset.search = words.filter(Boolean).join(" ").toLowerCase();
  tr.kinds = Object.keys(KINDS).filter((kind) => KINDS[kind](definition));
  return tr;
}

// The rows that pass search and "Show", shown PAGE at a time.
const PAGE = 25;
let page = 0;

function render() {
  const terms = $("search").value.toLowerCase().split(/\s+/).filter(Boolean);
  const kind = $("show").value;
  const rows = [...$("all-rows").rows];
  const found = rows.filter((tr) => tr.kinds.includes(kind) && terms.every((term) => tr.dataset.search.includes(term)));
  const pages = Math.max(1, Math.ceil(found.length / PAGE));
  page = Math.min(page, pages - 1);
  const visible = new Set(found.slice(page * PAGE, (page + 1) * PAGE));
  for (const tr of rows) tr.hidden = !visible.has(tr);

  const all = found.length === rows.length ? "" : ` (${rows.length} in all)`;
  if (!found.length) $("count").textContent = `No definition matches${all}.`;
  else if (pages === 1) $("count").textContent = `${plural(found.length, "definition")}${all}.`;
  else $("count").textContent = `${page * PAGE + 1} to ${page * PAGE + visible.size} of ${plural(found.length, "definition")}${all}.`;
  $("pager").hidden = pages === 1;
  $("page").replaceChildren(...Array.from({ length: pages }, (_, i) => el("option", { value: i }, `${i + 1}`)));
  $("page").value = page;
  $("pages").textContent = `of ${pages}`;
  $("prev").disabled = page === 0;
  $("next").disabled = page === pages - 1;
}

const filter = () => {
  page = 0;
  render();
};

// Back to the top of the table when the new page starts above the screen.
function turn(to) {
  page = to;
  render();
  if ($("all").getBoundingClientRect().top < 0) $("all-heading").scrollIntoView();
}

// The footer: the release the definitions come from.
function sources(release) {
  $("sources").replaceChildren("Definitions: ", el("a", { href: releaseUrl(REPO, release) }, `xquirks ${release.tag}`), release.date ? ` (${release.date})` : "", ".");
}

$("search").addEventListener("input", filter);
$("show").addEventListener("change", filter);
$("page").addEventListener("change", () => turn(Number($("page").value)));
$("prev").addEventListener("click", () => turn(page - 1));
$("next").addEventListener("click", () => turn(page + 1));

// The definitions could not load or render: the reason stays in the table's place too.
function unavailable(outcome) {
  loaded = outcome;
  say(outcome.message, "problem");
  $("count").textContent = outcome.message;
}

loadDefinitions((url, options) => fetch(url, options))
  .then((outcome) => {
    if (outcome.kind !== "ready") {
      unavailable(outcome);
      return;
    }
    const sorted = [...outcome.definitions].sort((a, b) => compareStrings(a.id, b.id));
    $("all-rows").replaceChildren(...sorted.map(row));
    filter();
    sources(outcome.release);
    // Only definitions that rendered take devices.
    loaded = outcome;
    for (const id of ["file", "paste-go", "search", "show"]) $(id).disabled = false;
    if ($("status").textContent === LOADING.message) say("");
  })
  .catch((error) => {
    $("all-rows").replaceChildren();
    unavailable(failure(error));
  });
