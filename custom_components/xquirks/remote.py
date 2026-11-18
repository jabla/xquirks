"""Content states in the repository: the content-<n> tags and the files at them.

Only reads: one GitHub API request for the tag list (conditional: an unchanged list comes back
without a body, but without a token GitHub still counts it against the rate limit), content.json and
hacs.json of a newer tag, the manifest and
withdrawn.toml of the state taken, withdrawn.toml of a newer tag this release cannot use. A quirk
file only when the change list would write it: its code is checked before the list. Files are read at the commit
a tag points to, never by the tag's name. Every file is checked against the sha256 its manifest
states before anything is done with it.

And the confirmation search of a self-check (async_search): GitHub's issue search without a token, page by
page (SEARCH_PAGES at most), which names a file's key and nothing else.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import time
from collections.abc import Callable, Collection

import aiohttp

from .confirm import UNKNOWN, confirmation, valid_key
from .const import FETCH_TIMEOUT, MAX_DOWNLOAD, MAX_SEARCH, RAW_URL, REPOSITORY, SEARCH_PAGE, SEARCH_PAGES, SEARCH_TIMEOUT, SEARCH_URL, TAGS_URL
from .content import COMMIT, BadContent, ContentState, from_manifest, parse_guard, parse_homeassistant, parse_threshold, parse_withdrawn, sha256

_LOGGER = logging.getLogger(__name__)

# At most nine digits: a longer number is no tag of this repository (and would not convert cheaply).
TAG = re.compile(r"refs/tags/(content-([1-9][0-9]{0,8}))")
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
DIGITS = re.compile(r"[0-9]{1,12}")
# The longest wait a rate limit answer is followed for: a larger value must not stop checks for good.
MAX_WAIT = 24 * 3600


class FetchError(Exception):
    """A request failed or returned something unusable; the message says what."""


class BadTag(FetchError):
    """What a tag holds cannot be used: a required file is missing, too large or malformed. Content
    at a commit never changes, so asking again in the same run does not help. The next start asks
    again: another release may read it, a 404 may have passed. The caller names the tag."""


class RateLimited(FetchError):
    """GitHub refuses requests until `until` (epoch seconds)."""

    def __init__(self, until: float) -> None:
        super().__init__(f"GitHub rate limit, next try after {time.strftime('%H:%M', time.localtime(until))}")
        self.until = until


@dataclasses.dataclass(frozen=True)
class Ref:
    """A content-<n> tag as the tag list shows it."""

    number: int
    tag: str
    # The commit the tag points to.
    commit: str


@dataclasses.dataclass(frozen=True)
class Tags:
    # Every content-<n> tag, newest first.
    found: tuple[Ref, ...]
    etag: str | None

    def get(self, tag: str) -> Ref | None:
        return next((ref for ref in self.found if ref.tag == tag), None)


@dataclasses.dataclass(frozen=True)
class Meta:
    """What a tag needs: the guard it was built for (None when it names none) and its Home Assistant release. And its
    confirmation threshold (None without a valid one)."""

    homeassistant: str | None
    guard: str | None = None
    confirm_threshold: int | None = None


def _timeout() -> aiohttp.ClientTimeout:
    return aiohttp.ClientTimeout(total=FETCH_TIMEOUT)


async def read_body(response: aiohttp.ClientResponse, too_large: Callable[[], Exception], limit: int | None = None) -> bytes:
    """The body of a response, refused beyond limit bytes (MAX_DOWNLOAD unless given) with the error too_large makes:
    by its Content-Length, else while it is read. The files of a content state, the device pictures and the
    confirmation search all come through here."""
    limit = MAX_DOWNLOAD if limit is None else limit
    if (length := response.headers.get("Content-Length", "")).isdigit() and int(length) > limit:
        raise too_large()
    chunks, size = [], 0
    async for chunk in response.content.iter_chunked(65536):
        size += len(chunk)
        if size > limit:
            raise too_large()
        chunks.append(chunk)
    return b"".join(chunks)


async def _read(response: aiohttp.ClientResponse, url: str) -> bytes:
    """The body, refused beyond MAX_DOWNLOAD bytes."""
    return await read_body(response, lambda: BadTag(f"{url}: larger than {MAX_DOWNLOAD} bytes"))


def _rate_limit(response: aiohttp.ClientResponse) -> float | None:
    """The time GitHub accepts requests again (at most MAX_WAIT ahead), None when the refusal is not a rate limit."""
    if response.status not in (403, 429):
        return None
    now = time.time()
    if retry := response.headers.get("Retry-After"):
        # Seconds; an HTTP date (or anything else) counts as a minute.
        return now + min(int(retry) if DIGITS.fullmatch(retry) else 60, MAX_WAIT)
    if response.headers.get("X-RateLimit-Remaining") == "0" and DIGITS.fullmatch(reset := response.headers.get("X-RateLimit-Reset", "")):
        return min(float(reset), now + MAX_WAIT)
    return now + 3600 if response.status == 429 else None


async def _fetch(session: aiohttp.ClientSession, url: str) -> bytes:
    """A file of a content state. Every state holds each file read here (CONTENT.md), so a 404 is a BadTag
    like any other file that cannot be used: it may pass, and the next start asks again."""
    try:
        async with session.get(url, timeout=_timeout()) as response:
            if response.status == 404:
                raise BadTag(f"{url}: HTTP 404")
            if (until := _rate_limit(response)) is not None:
                raise RateLimited(until)
            if response.status != 200:
                raise FetchError(f"{url}: HTTP {response.status}")
            return await _read(response, url)
    except (aiohttp.ClientError, TimeoutError) as err:
        raise FetchError(f"{url}: {str(err) or type(err).__name__}") from err


def _raw(commit: str, path: str) -> str:
    return RAW_URL.format(commit=commit, path=path)


async def async_tags(session: aiohttp.ClientSession, etag: str | None) -> Tags | None:
    """The content tags; None when the tag list did not change since etag."""
    headers = dict(HEADERS)
    if etag:
        headers["If-None-Match"] = etag
    try:
        async with session.get(TAGS_URL, headers=headers, timeout=_timeout()) as response:
            if response.status == 304:
                return None
            if (until := _rate_limit(response)) is not None:
                raise RateLimited(until)
            if response.status == 404:
                # matching-refs answers with an empty list; 404 means the repository is not reachable.
                raise FetchError(f"{TAGS_URL}: not found")
            if response.status != 200:
                raise FetchError(f"{TAGS_URL}: HTTP {response.status}")
            body = await _read(response, TAGS_URL)
            new_etag = response.headers.get("ETag")
        refs = json.loads(body)
    except (aiohttp.ClientError, TimeoutError) as err:
        raise FetchError(f"{TAGS_URL}: {str(err) or type(err).__name__}") from err
    except (ValueError, RecursionError) as err:
        raise FetchError(f"{TAGS_URL}: {err}") from err
    if not isinstance(refs, list):
        raise FetchError(f"{TAGS_URL}: not a list of refs")
    found: dict[int, Ref] = {}
    # GitHub sends objects only; anything else is a broken answer, not a list without tags.
    if not all(isinstance(item, dict) for item in refs):
        raise FetchError(f"{TAGS_URL}: not a list of refs")
    for item in refs:
        if not (match := TAG.fullmatch(str(item.get("ref", "")))):
            continue
        # Content tags are lightweight: an annotated one (type "tag") is no content state.
        target = item.get("object")
        if not isinstance(target, dict) or target.get("type") != "commit" or not COMMIT.fullmatch(str(target.get("sha", ""))):
            continue
        number = int(match.group(2))
        found[number] = Ref(number, match.group(1), target["sha"])
    return Tags(tuple(found[n] for n in sorted(found, reverse=True)), new_etag)


async def async_meta(session: aiohttp.ClientSession, commit: str) -> Meta:
    """The guard, the minimum Home Assistant release and the confirmation threshold of a tag (two small files)."""
    content_json = await _fetch(session, _raw(commit, "content.json"))
    hacs_json = await _fetch(session, _raw(commit, "hacs.json"))
    try:
        guard = parse_guard(content_json.decode())
        threshold = parse_threshold(content_json.decode())
        homeassistant = parse_homeassistant(hacs_json.decode())
    except (ValueError, AttributeError, RecursionError) as err:
        # BadContent and JSON errors are ValueErrors; JSON that is no object raises the other.
        raise BadTag(str(err)) from err
    return Meta(homeassistant, guard, threshold)


async def async_state(session: aiohttp.ClientSession, ref: Ref, homeassistant: str | None, confirm_threshold: object = None) -> ContentState:
    """The content state at the tag: its manifest and withdrawn revisions (and the threshold its content.json states,
    from async_meta)."""
    manifest = await _fetch(session, _raw(ref.commit, "manifest.json"))
    withdrawn_text = await _fetch(session, _raw(ref.commit, "data/withdrawn.toml"))
    try:
        withdrawn = parse_withdrawn(withdrawn_text.decode())
        return from_manifest(json.loads(manifest), tag=ref.tag, content=ref.number, commit=ref.commit, homeassistant=homeassistant, withdrawn=withdrawn,
                             confirm_threshold=confirm_threshold)
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError) as err:
        # BadContent and JSON errors are ValueErrors; from_manifest checks the shape of every entry.
        raise BadTag(str(err)) from err


async def async_withdrawn(session: aiohttp.ClientSession, commit: str) -> dict[tuple[str, str], str]:
    """The withdrawn revisions a tag lists, read also when this release cannot use its files (the
    fields of withdrawn.toml never change)."""
    text = await _fetch(session, _raw(commit, "data/withdrawn.toml"))
    try:
        return parse_withdrawn(text.decode())
    except (BadContent, UnicodeDecodeError) as err:
        raise BadTag(str(err)) from err


async def async_file(session: aiohttp.ClientSession, state: ContentState, name: str) -> bytes:
    """One quirk file of a fetched state, checked against the sha256 its manifest states."""
    if state.commit is None:
        raise FetchError(f"{state.tag}: no commit known for it")
    content = await _fetch(session, _raw(state.commit, state.paths[name]))
    if sha256(content) != state.shas[name]:
        raise BadTag(f"{name} at {state.tag} does not have the sha256 its manifest states")
    return content


async def async_search(session: aiohttp.ClientSession, key: object, wanted: Collection[str] = ()) -> object:
    """GitHub's answer to a search for the open "verify" issues whose title holds a file's key, oldest first, page by
    page. The next page only while the last one was full, GitHub counts more hits than were read, the answer is complete
    so far and a fixed part of wanted has no match yet (confirm.confirmation): the first match of a variant is its
    oldest issue. SEARCH_PAGES pages at most.

    The answer: the first page's parsed JSON as it came when it holds no list `items` (confirm.confirmation reads it,
    whatever its shape). Otherwise the hits of every page read, each without its body (only one page is held at a
    time), with the first page's total_count and incomplete_results true when a page said so. None when there is no
    answer: a key that is not 8 lowercase hex digits (nothing is sent then), or on any page any status but 200 (a rate
    limit too), a redirect, an error, a timeout, a body beyond MAX_SEARCH bytes or one that is no JSON. Also a later
    page without a list `items` or with another total_count (the hits changed between the requests: one may have
    moved to a page read before). Each page one request without a token: the key is all it says. No retry and nothing
    kept: the next self-check asks again."""
    if not valid_key(key):
        return None
    found: dict | None = None
    for page in range(1, SEARCH_PAGES + 1):
        answer = await _search_page(session, key, page)
        if answer is None:
            return None
        items = answer.get("items") if isinstance(answer, dict) else None
        if not isinstance(items, list):
            return answer if found is None else None
        for item in items:
            if isinstance(item, dict):
                item.pop("body", None)
        if found is None:
            found = answer
        elif answer.get("total_count") != found.get("total_count"):
            return None
        else:
            found["items"].extend(items)
            if answer.get("incomplete_results", False) is not False:
                found["incomplete_results"] = True
        if not _more(found, len(items), wanted):
            break
    return found


def _more(answer: dict, last: int, wanted: Collection[str]) -> bool:
    """Whether to ask the next page: the last page held SEARCH_PAGE hits, total_count counts more than the answer holds,
    the answer is complete and a fixed part of wanted has no match in it yet."""
    total = answer.get("total_count")
    return (last == SEARCH_PAGE and isinstance(total, int) and not isinstance(total, bool) and total > len(answer["items"])
            and answer.get("incomplete_results", False) is False and any(confirmation(answer, fixed).state == UNKNOWN for fixed in wanted))


async def _search_page(session: aiohttp.ClientSession, key: str, page: int) -> object:
    """One page of the search (async_search): the parsed JSON, None when there is no answer."""
    params = {"q": f"repo:{REPOSITORY} is:issue is:open label:verify in:title {key}", "sort": "created", "order": "asc", "per_page": str(SEARCH_PAGE)}
    if page > 1:
        params["page"] = str(page)
    try:
        async with session.get(SEARCH_URL, params=params, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=SEARCH_TIMEOUT), allow_redirects=False) as response:
            if response.status != 200:
                _LOGGER.debug("The confirmation search got HTTP %s", response.status)
                return None
            body = await read_body(response, lambda: FetchError(f"larger than {MAX_SEARCH} bytes"), MAX_SEARCH)
        return json.loads(body.decode())
    except (aiohttp.ClientError, TimeoutError, FetchError, ValueError, RecursionError) as err:
        # JSON and UTF-8 errors are ValueErrors; JSON nested deeper than the parser's recursion raises the other.
        _LOGGER.debug("The confirmation search failed: %s", str(err) or type(err).__name__)
        return None
