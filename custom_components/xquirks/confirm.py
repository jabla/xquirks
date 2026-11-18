"""The rules of a crowd confirmation: how many people confirmed a quirk file on one device variant.

A device test that found nothing wrong ends in a "verify" issue whose title names the variant:
a readable part, then a fixed part that holds the identity, built by title() (the form link uses it):

    Verify: Sone SB1191 [_TZE28C1000000_1youk3hj | TS0601 | 0x00000063 | 1a2b3c4d]

The fixed part is [<manufacturer> | <model> | <firmware> | <key>]: the manufacturer and the model as
zigpy reports them, the firmware as the device registry has it (`unknown` without one) and the file's
key from the manifest (8 lowercase hex digits). Each person who agrees adds a thumbs-up. The count of
a variant is the author plus the thumbs-ups of its OLDEST open issue (the lowest issue number: GitHub
numbers issues in the order they are opened). Only the oldest counts, never a sum, so one account
that opens many issues reaches 2 at most. Closed issues and pull requests do not count.

Each field and the readable part are normalized, so that a title survives GitHub's title storage
unchanged and its fixed part reads from the right without doubt: whitespace runs (Unicode whitespace
included) become one space, outer whitespace goes, then every character other than printable ASCII
(space to "~") and each of the separators "|", "[", "]" becomes "_" (one each). Then the text is cut
to its limit (FIELD_LIMIT per field, READABLE_LIMIT for the readable part) and a space at the cut
goes. A field that ends up empty (or None) is `unknown`. A key that is not 8 lowercase hex digits,
or a field that is neither a string nor None, makes title() and fixed_part() refuse (None).

The integration builds the fixed part and compares it with the end of each title (matches()). It
never parses a title. A tool that groups the issues by variant parses each title from the right
(parse(): the text after its last "[" must be a fixed part exactly as fixed_part() writes it). The
two agree on every title: a title matches a fixed part exactly when parse() reads that fixed part's
fields from it. So the integration stops asking exactly where such a tool sees the same count.

From a search answer (read_issues(), confirmation()) only the issue number (an int, the link is built
from it), the title (for the comparison) and the thumbs-up count are read. Body, links and account
names never leave this module.

Standard library only and no relative import: a tool can load this file by its path alone (like
guard.py), the integration imports it as part of its package.
"""

from __future__ import annotations

import re
from typing import NamedTuple

# A file's key (the manifest field `key`): 8 lowercase hex digits.
KEY = re.compile(r"[0-9a-f]{8}")
# Characters per field of the fixed part after normalization. The Zigbee basic cluster holds the
# manufacturer name and the model identifier in at most 32 characters.
FIELD_LIMIT = 32
# Characters of the readable part. The longest title (8 + 100 + 1 + 115) stays below MAX_TITLE.
READABLE_LIMIT = 100
# A field without a value.
MISSING = "unknown"
PREFIX = "Verify: "
# The longest title GitHub stores: a longer one in an answer is no GitHub title.
MAX_TITLE = 256
# Items of an answer looked at (GitHub's largest page). A tool that reads the issue listing reads it page by page.
MAX_ITEMS = 100
# The largest issue number taken from an answer.
MAX_NUMBER = 2**31 - 1
# Thumbs-ups above this count as this many (above THRESHOLD_MAX, so it never changes a decision).
MAX_THUMBS = 10_000
# The thresholds a content state may carry (the start value is 10, staging uses 2).
THRESHOLD_MIN = 1
THRESHOLD_MAX = 1000
# The states of a confirmation.
FOUND = "found"
NO_ISSUE = "no_issue"
UNKNOWN = "unknown"

_SEPARATORS = "|[]"
# A fixed part: four parts between the separators (each checked against the normalization after).
_FIXED = re.compile(r"\[([^\[\]|]+) \| ([^\[\]|]+) \| ([^\[\]|]+) \| ([0-9a-f]{8})\]")


class Fields(NamedTuple):
    """The fields of a fixed part, as fixed_part() wrote them."""

    manufacturer: str
    model: str
    firmware: str
    key: str


class Issue(NamedTuple):
    """An open issue of a search answer: its number, its title and its thumbs-ups (at most MAX_THUMBS)."""

    number: int
    title: str
    thumbs: int


class Confirmation(NamedTuple):
    """What an answer says about one variant. FOUND: the oldest matching open issue and its count.
    NO_ISSUE: no matching open issue (count 0). UNKNOWN: the answer cannot tell (number and count None)."""

    state: str
    number: int | None
    count: int | None


def _is_int(value: object) -> bool:
    """An int that is no bool."""
    return isinstance(value, int) and not isinstance(value, bool)


def _clean(value: str, limit: int) -> str:
    """Value normalized as the module docstring says (it may end up empty)."""
    text = " ".join(value.split())
    text = "".join(char if " " <= char <= "~" and char not in _SEPARATORS else "_" for char in text)
    return text[:limit].rstrip()


def _field(value: object) -> str | None:
    """A field of the fixed part, None for a value that is neither a string nor None."""
    if value is None:
        return MISSING
    if not isinstance(value, str):
        return None
    return _clean(value, FIELD_LIMIT) or MISSING


def valid_key(key: object) -> bool:
    """Whether key is a file's key: 8 lowercase hex digits, nothing else."""
    return isinstance(key, str) and KEY.fullmatch(key) is not None


def fixed_part(manufacturer: str | None, model: str | None, firmware: str | None, key: str) -> str | None:
    """The fixed part of a variant's title, e.g. `[_TZE200_aaaa | TS0601 | unknown | 1a2b3c4d]`.
    None for a bad key or a field of another type than str or None."""
    fields = [_field(value) for value in (manufacturer, model, firmware)]
    if any(field is None for field in fields) or not valid_key(key):
        return None
    return f"[{fields[0]} | {fields[1]} | {fields[2]} | {key}]"


def title(readable: str | None, manufacturer: str | None, model: str | None, firmware: str | None, key: str) -> str | None:
    """The title of a variant's verify issue: `Verify: <readable part> <fixed part>`. None where
    fixed_part() refuses or readable is neither a string nor None."""
    fixed = fixed_part(manufacturer, model, firmware, key)
    if fixed is None or not (readable is None or isinstance(readable, str)):
        return None
    name = _clean(readable, READABLE_LIMIT) if readable else ""
    return f"{PREFIX}{name} {fixed}" if name else f"{PREFIX}{fixed}"


def _fields(text: object) -> Fields | None:
    """The fields of text when it is a fixed part exactly as fixed_part() writes it, None otherwise."""
    if not isinstance(text, str) or (found := _FIXED.fullmatch(text)) is None:
        return None
    *fields, key = found.groups()
    if any(_clean(field, FIELD_LIMIT) != field for field in fields):
        return None
    return Fields(*fields, key)


def matches(title: object, fixed: object) -> bool:
    """Whether an issue title (outer whitespace stripped) ends with exactly this fixed part. A fixed
    part fixed_part() cannot write matches nothing."""
    return isinstance(title, str) and _fields(fixed) is not None and title.strip().endswith(fixed)


def parse(title: object) -> Fields | None:
    """The fields of a title's fixed part, read from the right: the text after the title's last "["
    (outer whitespace stripped) must be a fixed part exactly as fixed_part() writes it. None otherwise
    (extra separators, a bad key, a missing part, text after the "]")."""
    if not isinstance(title, str):
        return None
    text = title.strip()
    return _fields(text[text.rfind("["):]) if "[" in text else None


def _issue(item: object) -> Issue | None:
    """An open issue from an item of an answer, None when the item is malformed."""
    if not isinstance(item, dict) or item.get("state") != "open":
        return None
    number, text, reactions = item.get("number"), item.get("title"), item.get("reactions")
    if not _is_int(number) or not 0 < number <= MAX_NUMBER:
        return None
    if not isinstance(text, str) or len(text) > MAX_TITLE:
        return None
    thumbs = reactions.get("+1") if isinstance(reactions, dict) else None
    if not _is_int(thumbs) or thumbs < 0:
        return None
    return Issue(number, text, min(thumbs, MAX_THUMBS))


def _read(items: list) -> tuple[list[Issue], int | None]:
    """The open issues among the first MAX_ITEMS items and how many of them came before the first malformed item of
    those (None without one). A pull request or a closed issue is skipped without counting as malformed."""
    issues: list[Issue] = []
    before = None
    for item in items[:MAX_ITEMS]:
        if isinstance(item, dict) and ("pull_request" in item or item.get("state") == "closed"):
            continue
        if (issue := _issue(item)) is None:
            if before is None:
                before = len(issues)
        else:
            issues.append(issue)
    return issues, before


def read_issues(items: object) -> list[Issue]:
    """The open issues of a list of items (a search answer's `items` or a page of the issue listing),
    in their order. Looks at the first MAX_ITEMS items. Skips pull requests (a `pull_request` key),
    issues not open and items that are malformed: not a dict, a number that is no positive int up to
    MAX_NUMBER, a title that is no string or longer than MAX_TITLE, no int `reactions["+1"]` of 0 or
    more. Thumbs-ups above MAX_THUMBS count as MAX_THUMBS. Anything but a list gives no issue."""
    return _read(items)[0] if isinstance(items, list) else []


def oldest(issues: list[Issue], fixed: object) -> Issue | None:
    """The oldest issue (the lowest number) whose title ends with the fixed part, None without one."""
    if _fields(fixed) is None:
        return None
    found = [issue for issue in issues if matches(issue.title, fixed)]
    return min(found, key=lambda issue: issue.number) if found else None


def count(issue: Issue) -> int:
    """An issue's count: its author plus its thumbs-ups."""
    return 1 + issue.thumbs


def confirmation(answer: object, fixed: object) -> Confirmation:
    """What a search answer (the parsed JSON, of any type, its items oldest first) says about the variant of a fixed part.

    FOUND with the oldest matching open issue and its count. NO_ISSUE when the answer is complete and holds none.
    UNKNOWN when the fixed part is bad or the answer is no dict with a list `items`. UNKNOWN also for a match when the
    answer may lack an older one: an `incomplete_results` other than false (when present), or a malformed item before
    the match (it may have been an older one). Without a match also when the answer may hold more than what was looked
    at: more than MAX_ITEMS items, a `total_count` that is no int from 0 to the number of items, an
    `incomplete_results` other than false, or a malformed item (it may have been the issue)."""
    if _fields(fixed) is None or not isinstance(answer, dict) or not isinstance(items := answer.get("items"), list):
        return Confirmation(UNKNOWN, None, None)
    issues, before = _read(items)
    whole = answer.get("incomplete_results", False) is False
    if (issue := oldest(issues, fixed)) is not None:
        if whole and (before is None or issues.index(issue) < before):
            return Confirmation(FOUND, issue.number, count(issue))
        return Confirmation(UNKNOWN, None, None)
    total = answer.get("total_count")
    complete = len(items) <= MAX_ITEMS and _is_int(total) and 0 <= total <= len(items) and whole and before is None
    return Confirmation(NO_ISSUE, None, 0) if complete else Confirmation(UNKNOWN, None, None)


def valid_threshold(value: object) -> int | None:
    """The threshold when value is an int (no bool) from THRESHOLD_MIN to THRESHOLD_MAX, None
    (missing: ask as without one) otherwise."""
    return value if _is_int(value) and THRESHOLD_MIN <= value <= THRESHOLD_MAX else None


def reached(count: object, threshold: object) -> bool:
    """Whether a count reaches the threshold. Not reached for an unknown count (None) or a threshold
    valid_threshold() does not take."""
    limit = valid_threshold(threshold)
    return limit is not None and _is_int(count) and count >= limit
