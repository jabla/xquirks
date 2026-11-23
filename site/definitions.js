// The definitions the finder shows: devices.json next to the page, which tools/build_index.py writes from the
// integration's definitions.jsonl with the loader's own reading (CI checks that it is up to date). The Pages deploy
// publishes site/ of the latest release with release.json, which names the release. Kept free of the DOM so that tests
// run it in node.

export const FILES = Object.freeze({ release: "release.json", definitions: "devices.json" });

// The format of devices.json this page reads (FORMAT in tools/build_index.py).
const FORMAT = 1;
const TAG = /^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/;
const DATE = /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/;

const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);

// The definitions of devices.json, refused when it has another format.
export function readIndex(value) {
  if (value?.format !== FORMAT || !Array.isArray(value.definitions)) throw new Error(`${FILES.definitions} has a format this page does not read`);
  return value.definitions;
}

// release.json, checked: the tag of a release (vX.Y.Z) and its date (null when missing or malformed).
export function readRelease(value) {
  if (!isObject(value) || typeof value.tag !== "string" || !TAG.test(value.tag)) throw new Error("release.json names no release");
  return { tag: value.tag, date: typeof value.date === "string" && DATE.test(value.date) ? value.date : null };
}

// The release's page on GitHub.
export const releaseUrl = (repo, release) => `https://github.com/${repo}/releases/tag/${release.tag}`;

// How likely a definition is to do something wrong on the device, lowest first.
export const RISKS = ["low", "medium", "high"];
const HIGH_RISK = 'It loads only with "Use high-risk definitions" on in the options of xquirks.';

// What the page shows of a definition's risk: null for a verified one or one without a risk, else its risk, the badge,
// the badge's tooltip and, for a high risk, the note of its device card. The loader holds a high-risk definition that
// is not verified back unless that option is on.
export function riskShown(definition) {
  if (definition.verified || !RISKS.includes(definition.risk)) return null;
  const high = definition.risk === "high";
  return {
    tier: definition.risk,
    badge: `${definition.risk} risk`,
    title: high ? `Not verified on a device yet. ${HIGH_RISK}` : "Not verified on a device yet.",
    note: high ? `High risk. ${HIGH_RISK}` : null,
  };
}

// What loading the definitions came to, with the text to show for anything but "ready".
export const LOADING = Object.freeze({ kind: "loading", message: "The definitions are still loading, try again in a moment." });

export function failure(error) {
  return { kind: "failed", message: `Could not load the definitions (${error?.message ?? error}). Reload the page to try again.` };
}

// What the page answers when it gets devices before the definitions are there; null once they are.
export const notReady = (loaded) => (loaded.kind === "ready" ? null : loaded.message);

// How long the page waits for a file on a slow line.
const WAIT = 60000;

// A file next to the page as JSON, asked past the browser's cache (a deploy replaces the files together). When the wait
// is over, the browser stops the request and the body it is still reading.
async function getJson(fetch, name, wait) {
  const abort = new AbortController();
  const read = async () => {
    const response = await fetch(name, { credentials: "omit", cache: "no-cache", signal: abort.signal });
    if (!response.ok) throw new Error(`${name}: ${response.status}`);
    try {
      return await response.json();
    } catch {
      throw new Error(`${name} is not JSON`);
    }
  };
  let timer;
  const late = new Promise((_, reject) => {
    timer = setTimeout(() => {
      abort.abort();
      reject(new Error(`${name}: no answer`));
    }, wait);
  });
  try {
    return await Promise.race([read(), late]);
  } finally {
    clearTimeout(timer);
  }
}

// The definitions next to the page through fetch (the browser's, or a stand-in in tests): {kind: "ready", release,
// definitions} or kind "failed" with its message. Never throws. wait: tests shorten WAIT.
export async function loadDefinitions(fetch, { wait = WAIT } = {}) {
  try {
    const [release, definitions] = await Promise.all([getJson(fetch, FILES.release, wait).then(readRelease), getJson(fetch, FILES.definitions, wait).then(readIndex)]);
    return { kind: "ready", release, definitions };
  } catch (error) {
    return failure(error);
  }
}
