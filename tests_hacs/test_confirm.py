"""The shared confirmation rules (confirm.py): the issue title, its exact comparison and strict parser,
the defensive reading of a GitHub search answer, the oldest issue and its count, the threshold."""

from __future__ import annotations

import ast
import json
import subprocess
import sys

import pytest

from .integration import FOLDER, confirm

KEY = "1a2b3c4d"
FIXED = "[_TZE28C1000000_1youk3hj | TS0601 | 0x00000063 | 1a2b3c4d]"
TITLE = f"Verify: Sone SB1191 {FIXED}"


def item(number: int = 5, title: str = TITLE, thumbs=0, **extra) -> dict:
    """One hit of the issue search as GitHub sends it (the fields T1 measured), body included."""
    data = {
        "number": number,
        "title": title,
        "state": "open",
        "reactions": {"url": "https://api.github.com/x", "total_count": 0, "+1": thumbs, "-1": 0, "laugh": 0, "hooray": 0,
                      "confused": 0, "heart": 0, "rocket": 0, "eyes": 0},
        "body": "x" * 65536,
        "user": {"login": "someone", "html_url": "https://github.com/someone"},
        "html_url": f"https://github.com/jabla/xquirks/issues/{number}",
        "labels": [{"name": "verify"}],
        "created_at": "2026-11-18T12:46:22Z",
    }
    data.update(extra)
    return data


def answer(*items, total: int | None = None, **extra) -> dict:
    return {"total_count": len(items) if total is None else total, "incomplete_results": False, "items": list(items), "search_type": "lexical", **extra}


# The fixed part and the title

def test_fixed_part_example():
    assert confirm.fixed_part("_TZE28C1000000_1youk3hj", "TS0601", "0x00000063", KEY) == FIXED


@pytest.mark.parametrize("firmware", [None, "", " ", "\n\t", "\u00a0"])
def test_firmware_missing_is_unknown(firmware):
    assert confirm.fixed_part("_TZE200_aaaa", "TS0601", firmware, KEY) == "[_TZE200_aaaa | TS0601 | unknown | 1a2b3c4d]"


def test_manufacturer_and_model_missing_are_unknown():
    assert confirm.fixed_part(None, "", None, KEY) == "[unknown | unknown | unknown | 1a2b3c4d]"
    assert confirm.fixed_part("   ", "\n", "", KEY) == "[unknown | unknown | unknown | 1a2b3c4d]"


@pytest.mark.parametrize(("raw", "clean"), [
    ("_TZE200_aaaa", "_TZE200_aaaa"),
    ("Signify Netherlands B.V.", "Signify Netherlands B.V."),
    ("  Tuya \t TS0601\n", "Tuya TS0601"),  # outer whitespace goes, runs become one space
    ("a\r\nb", "a b"),
    ("a\u00a0\u2028b", "a b"),  # Unicode whitespace counts as whitespace
    ("a|b[c]d", "a_b_c_d"),  # the three separators
    ("[A | B | C | 1a2b3c4d]", "_A _ B _ C _ 1a2b3c4d_"),
    ("M\u00fcller", "M_ller"),  # not ASCII
    ("e\u0301", "e_"),  # a combining mark is a character of its own
    ("a\U0001f600b", "a_b"),
    ("a\x00b\x1bc\x7fd", "a_b_c_d"),  # control characters that are not whitespace
    ("a\u200bb", "a_b"),  # zero width space is no whitespace in Python
    ("**bold** `x` <b>#1</b>", "**bold** `x` <b>#1</b>"),  # markdown stays: plain ASCII
    ("|", "_"),
])
def test_field_normalization(raw, clean):
    assert confirm.fixed_part(raw, "M", "F", KEY) == f"[{clean} | M | F | {KEY}]"
    assert confirm.fixed_part("X", raw, "F", KEY) == f"[X | {clean} | F | {KEY}]"
    assert confirm.fixed_part("X", "M", raw, KEY) == f"[X | M | {clean} | {KEY}]"


def test_field_cap():
    assert confirm.FIELD_LIMIT == 32
    assert confirm.fixed_part("A" * 40, "B" * 33, "C" * 1000, KEY) == f"[{'A' * 32} | {'B' * 32} | {'C' * 32} | {KEY}]"
    # The cap cuts before the end: no space left at the end of a field.
    assert confirm.fixed_part("A" * 31 + " B", "M", None, KEY) == f"[{'A' * 31} | M | unknown | {KEY}]"
    # Characters count after the replacement (one "_" each).
    assert confirm.fixed_part("\u00fc" * 40, "M", None, KEY) == f"[{'_' * 32} | M | unknown | {KEY}]"


@pytest.mark.parametrize("key", ["1a2b3c4", "1a2b3c4d5", "1A2B3C4D", "1a2b3c4d\n", " 1a2b3c4d", "1a2b3c4g", "", "\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668",
                                 "\uff11a2b3c4d", None, 0x1a2b3c4d, b"1a2b3c4d", ["1a2b3c4d"], True])
def test_bad_key_refused(key):
    assert confirm.valid_key(key) is False
    assert confirm.fixed_part("_TZE200_aaaa", "TS0601", None, key) is None
    assert confirm.title("Tuya A", "_TZE200_aaaa", "TS0601", None, key) is None


@pytest.mark.parametrize("key", ["1a2b3c4d", "00000000", "deadbeef", "20251118", "31e41592"])
def test_good_key(key):
    assert confirm.valid_key(key) is True


@pytest.mark.parametrize("fields", [(5, "M", None), ("X", b"M", None), ("X", "M", 0x63), ("X", "M", ["F"]), (None, None, 1.5)])
def test_field_of_wrong_type_refused(fields):
    assert confirm.fixed_part(*fields, KEY) is None
    assert confirm.title("Name", *fields, KEY) is None


def test_title_example():
    assert confirm.title("Sone SB1191", "_TZE28C1000000_1youk3hj", "TS0601", "0x00000063", KEY) == TITLE


def test_title_readable_part():
    title = confirm.title("Sone | SB1191 [x](http://e)\n**y**", "_TZE200_aaaa", "TS0601", None, KEY)
    assert title == f"Verify: Sone _ SB1191 _x_(http://e) **y** [_TZE200_aaaa | TS0601 | unknown | {KEY}]"
    assert confirm.READABLE_LIMIT == 100
    assert confirm.title("R" * 150, "X", "M", "F", KEY) == f"Verify: {'R' * 100} [X | M | F | {KEY}]"
    # Nothing readable: no double space.
    assert confirm.title("", "X", "M", "F", KEY) == f"Verify: [X | M | F | {KEY}]"
    assert confirm.title(None, "X", "M", "F", KEY) == f"Verify: [X | M | F | {KEY}]"
    assert confirm.title(" \u00a0", "X", "M", "F", KEY) == f"Verify: [X | M | F | {KEY}]"
    assert confirm.title(5, "X", "M", "F", KEY) is None


def test_title_fits_github():
    """GitHub keeps 256 characters of a title: the longest title fits, with exactly one "[" and "]"."""
    title = confirm.title("\u00fc|[]" * 100, "|" * 100, "[" * 100, "]" * 100, KEY)
    assert len(title) <= confirm.MAX_TITLE == 256
    assert title.count("[") == title.count("]") == 1
    assert title.count("|") == 3
    assert title == title.strip()
    assert all(" " <= ch <= "~" for ch in title)


def test_title_is_stable():
    """A title read back (stripped, as GitHub stores it) builds the same fixed part again."""
    for raw in ["  Tuya \t A", "M\u00fcller|x", "a" * 40 + " b", "", None]:
        fixed = confirm.fixed_part(raw, raw, raw, KEY)
        fields = confirm.parse(confirm.title(raw, raw, raw, raw, KEY))
        assert fields is not None
        assert confirm.fixed_part(*fields) == fixed


# The exact comparison (integration) and the strict parser (a tool that groups the issues by variant)

def test_matches():
    assert confirm.matches(TITLE, FIXED)
    assert confirm.matches(f"  {TITLE} \n", FIXED)
    assert confirm.matches(FIXED, FIXED)
    assert confirm.matches(f"Anything at all | [ ] {FIXED}", FIXED)  # the integration compares the end only
    assert not confirm.matches(TITLE.replace(KEY, "1a2b3c4e"), FIXED)
    assert not confirm.matches(TITLE.replace(KEY, KEY.upper()), FIXED)  # the search ignores case, the comparison does not
    assert not confirm.matches(TITLE.replace("0x00000063", "unknown"), FIXED)
    assert not confirm.matches(TITLE.replace("TS0601", "TS0601 "), FIXED)
    assert not confirm.matches(f"{TITLE} extra", FIXED)
    assert not confirm.matches(f"{TITLE}.", FIXED)
    assert not confirm.matches(TITLE[:-1], FIXED)
    assert not confirm.matches("", FIXED)


@pytest.mark.parametrize("title", [None, 5, b"x", ["x"], {"title": TITLE}])
def test_matches_title_of_wrong_type(title):
    assert not confirm.matches(title, FIXED)


@pytest.mark.parametrize("fixed", [None, "", " ", "]", "1a2b3c4d]", f"{FIXED} ", FIXED[1:], "[A | B | C | 1A2B3C4D]", "[A | B | 1a2b3c4d]", "[A  | B | C | 1a2b3c4d]",
                                   "[ | B | C | 1a2b3c4d]", f"[{'A' * 33} | B | C | 1a2b3c4d]", "[\u00fc | B | C | 1a2b3c4d]", 5, b"[A | B | C | 1a2b3c4d]"])
def test_matches_refuses_a_bad_fixed_part(fixed):
    """A fixed part fixed_part() cannot build matches nothing (an empty one would match every title)."""
    assert not confirm.matches(f"Verify: x {fixed}" if isinstance(fixed, str) else TITLE, fixed)
    assert not confirm.matches(TITLE, fixed)


def test_parse():
    assert confirm.parse(TITLE) == ("_TZE28C1000000_1youk3hj", "TS0601", "0x00000063", KEY)
    fields = confirm.parse(TITLE)
    assert (fields.manufacturer, fields.model, fields.firmware, fields.key) == ("_TZE28C1000000_1youk3hj", "TS0601", "0x00000063", KEY)
    assert confirm.parse(f" {TITLE}\n") == fields
    assert confirm.parse("Verify: Tuya A [Signify Netherlands B.V. | LCT 001 | unknown | deadbeef]") == ("Signify Netherlands B.V.", "LCT 001", "unknown", "deadbeef")
    # From the right: the readable part does not count, as for the integration's comparison.
    assert confirm.parse(f"Verify: a | b [c] [d {FIXED}") == fields
    assert confirm.parse(FIXED) == fields


@pytest.mark.parametrize("title", [
    f"{TITLE} extra",  # text after the fixed part
    f"{TITLE}]",
    "Verify: x [A | B | C | D | 1a2b3c4d]",  # an extra separator
    "Verify: x [A | B | 1a2b3c4d]",  # a part missing
    "Verify: x [A | B | C]",
    "Verify: x [A | B | C | ]",
    "Verify: x [A | B | C | 1A2B3C4D]",  # bad keys
    "Verify: x [A | B | C | 1a2b3c4]",
    "Verify: x [A | B | C | 1a2b3c4d5]",
    "Verify: x [A | B | C | 1a2b3c4g]",
    "Verify: x [A | B | C | 1a2b3c4d\n]",
    "Verify: x [A] | B | C | 1a2b3c4d]",  # "]" inside
    "Verify: x [A | B | C | 1a2b3c4d] [A | B | C | 1a2b3c4d",
    "Verify: x [A  | B | C | 1a2b3c4d]",  # not as fixed_part() writes it: double space, outer spaces, empty
    "Verify: x [ A | B | C | 1a2b3c4d]",
    "Verify: x [A |B | C | 1a2b3c4d]",
    "Verify: x [A| B | C | 1a2b3c4d]",
    "Verify: x [ | B | C | 1a2b3c4d]",
    "Verify: x [A | B |  | 1a2b3c4d]",
    "Verify: x [A\tB | M | C | 1a2b3c4d]",
    f"Verify: x [{'A' * 33} | B | C | 1a2b3c4d]",  # longer than a field can be
    "Verify: x [M\u00fcller | B | C | 1a2b3c4d]",  # not ASCII
    "Verify: x A | B | C | 1a2b3c4d]",  # no "["
    "Verify: x (A | B | C | 1a2b3c4d)",
    "Verify: x [A | B | C | 1a2b3c4d",
    "",
    "[",
])
def test_parse_refuses(title):
    assert confirm.parse(title) is None


@pytest.mark.parametrize("title", [None, 5, b"x", ["x"]])
def test_parse_title_of_wrong_type(title):
    assert confirm.parse(title) is None


TITLES = [
    TITLE, f" {TITLE} ", f"{TITLE} x", FIXED, f"x [{FIXED}", f"x ]{FIXED}", f"x | {FIXED}", TITLE.replace(KEY, KEY.upper()),
    "Verify: x [_TZE200_1a2b3c4d | TS0601 | unknown | 11112222]", "Verify: x [_TZE200_aaaa | TS0601 | unknown | 1a2b3c4d]",
    "Verify: x [unknown | unknown | unknown | 1a2b3c4d]", "Verify: x [A  | B | C | 1a2b3c4d]", "Verify: [A | B | C | 1a2b3c4d] [A | B | C | 1a2b3c4d]",
    "Verify: x [A | B | C | D | 1a2b3c4d]", "Verify: x [A] | B | C | 1a2b3c4d]", f"Verify: x [{'A' * 32} | B | C | 1a2b3c4d]", "",
]


def test_parse_agrees_with_matches():
    """The comparison and the parser decide with the same rule: a title matches a fixed part exactly
    when the parser reads the fields of that fixed part from it."""
    fixeds = {FIXED, "[_TZE200_aaaa | TS0601 | unknown | 1a2b3c4d]", "[unknown | unknown | unknown | 1a2b3c4d]", "[A | B | C | 1a2b3c4d]",
              f"[{'A' * 32} | B | C | 1a2b3c4d]", "[_TZE200_1a2b3c4d | TS0601 | unknown | 11112222]"}
    fixeds |= {confirm.fixed_part(*fields) for title in TITLES if (fields := confirm.parse(title))}
    for title in TITLES:
        fields = confirm.parse(title)
        for fixed in fixeds:
            assert confirm.matches(title, fixed) == (fields is not None and confirm.fixed_part(*fields) == fixed), (title, fixed)


# Reading a search answer

def test_read_issues_keeps_only_number_title_thumbs():
    issues = confirm.read_issues([item(7, thumbs=3)])
    assert issues == [(7, TITLE, 3)]
    assert type(issues[0]) is confirm.Issue
    assert confirm.Issue._fields == ("number", "title", "thumbs")
    assert (issues[0].number, issues[0].title, issues[0].thumbs) == (7, TITLE, 3)


@pytest.mark.parametrize("items", [None, {}, "items", 5, (item(),), {"0": item()}])
def test_read_issues_not_a_list(items):
    assert confirm.read_issues(items) == []


@pytest.mark.parametrize("bad", [None, 5, "item", [], ["number", 5], True, 1.5])
def test_read_issues_skips_non_dicts(bad):
    assert confirm.read_issues([bad, item(3)]) == [(3, TITLE, 0)]


@pytest.mark.parametrize("field", ["number", "title", "state", "reactions"])
def test_read_issues_skips_missing_fields(field):
    broken = item(2)
    del broken[field]
    assert confirm.read_issues([broken, item(3)]) == [(3, TITLE, 0)]


@pytest.mark.parametrize("number", ["5", True, False, 0, -3, 5.0, None, [5], 2**31, 10**30])
def test_read_issues_skips_bad_numbers(number):
    assert confirm.read_issues([item(number), item(3)]) == [(3, TITLE, 0)]


def test_read_issues_number_bounds():
    assert confirm.MAX_NUMBER == 2**31 - 1
    assert confirm.read_issues([item(1), item(2**31 - 1)]) == [(1, TITLE, 0), (2**31 - 1, TITLE, 0)]


@pytest.mark.parametrize("title", [None, 5, b"x", ["x"], {"x": 1}, "x" * 257])
def test_read_issues_skips_bad_titles(title):
    assert confirm.read_issues([item(2, title), item(3)]) == [(3, TITLE, 0)]


def test_read_issues_title_limit():
    assert confirm.read_issues([item(2, "x" * 256)]) == [(2, "x" * 256, 0)]


@pytest.mark.parametrize("state", ["closed", "OPEN", "open ", None, 1, True, ["open"]])
def test_read_issues_skips_issues_not_open(state):
    assert confirm.read_issues([item(2, state=state), item(3)]) == [(3, TITLE, 0)]


@pytest.mark.parametrize("value", [{"url": "https://api.github.com/x", "merged_at": None}, None, {}])
def test_read_issues_skips_pull_requests(value):
    assert confirm.read_issues([item(2, pull_request=value), item(3)]) == [(3, TITLE, 0)]


@pytest.mark.parametrize("reactions", [None, [], "+1", 5, {"-1": 3}, {"+1": "3"}, {"+1": 3.0}, {"+1": True}, {"+1": False}, {"+1": None},
                                       {"+1": -1}, {"+1": [1]}, {"+1": float("nan")}])
def test_read_issues_skips_bad_reactions(reactions):
    assert confirm.read_issues([item(2, reactions=reactions), item(3)]) == [(3, TITLE, 0)]


def test_read_issues_clamps_huge_thumbs():
    assert confirm.MAX_THUMBS == 10_000
    assert confirm.read_issues([item(2, thumbs=10**12), item(3, thumbs=10_000), item(4, thumbs=9_999)]) == [(2, TITLE, 10_000), (3, TITLE, 10_000), (4, TITLE, 9_999)]


def test_read_issues_cap():
    assert confirm.MAX_ITEMS == 100
    issues = confirm.read_issues([item(n) for n in range(1, 151)])
    assert [issue.number for issue in issues] == list(range(1, 101))


# The oldest issue and its count

def test_count_is_author_plus_thumbs():
    assert confirm.count(confirm.Issue(4, TITLE, 0)) == 1
    assert confirm.count(confirm.Issue(4, TITLE, 9)) == 10


def test_oldest_matching_issue_counts_not_the_sum():
    issues = confirm.read_issues([item(7, thumbs=1), item(3, thumbs=4), item(12, thumbs=8), item(5, TITLE.replace(KEY, "1a2b3c4e"), thumbs=50)])
    assert confirm.oldest(issues, FIXED) == (3, TITLE, 4)
    assert confirm.count(confirm.oldest(issues, FIXED)) == 5
    # The older issue counts also when a younger one has more thumbs-ups.
    issues = confirm.read_issues([item(9, thumbs=20), item(4, thumbs=0)])
    assert confirm.count(confirm.oldest(issues, FIXED)) == 1


def test_oldest_none():
    assert confirm.oldest([], FIXED) is None
    assert confirm.oldest(confirm.read_issues([item(2, TITLE.replace(KEY, "1a2b3c4e"))]), FIXED) is None
    assert confirm.oldest(confirm.read_issues([item(2)]), None) is None
    assert confirm.oldest(confirm.read_issues([item(2)]), "") is None


# The tri-state result

def test_confirmation_found():
    result = confirm.confirmation(answer(item(4, thumbs=2)), FIXED)
    assert result == (confirm.FOUND, 4, 3)
    assert confirm.Confirmation._fields == ("state", "number", "count")
    assert (result.state, result.number, result.count) == ("found", 4, 3)


def test_confirmation_no_issue():
    assert confirm.confirmation(answer(), FIXED) == ("no_issue", None, 0)
    other = TITLE.replace(KEY, "1a2b3c4e")
    assert confirm.confirmation(answer(item(2, other), item(3, other)), FIXED) == (confirm.NO_ISSUE, None, 0)


@pytest.mark.parametrize("data", [None, [], [item()], "x", 5, True, {}, {"items": None}, {"items": {}}, {"items": "abc"}, {"items": item()},
                                  {"total_count": 1, "incomplete_results": False}])
def test_confirmation_unusable_answer(data):
    assert confirm.confirmation(data, FIXED) == ("unknown", None, None)


def test_confirmation_ignores_everything_else():
    """Only the number and the count leave: no title, body, link or account name."""
    result = confirm.confirmation(answer(item(4, thumbs=2, user={"login": "<script>"}, body="[x](javascript:1)")), FIXED)
    assert result == (confirm.FOUND, 4, 3)
    assert all(isinstance(value, (str, int)) for value in result)


def test_confirmation_two_matching_issues():
    assert confirm.confirmation(answer(item(7, thumbs=1), item(3, thumbs=4)), FIXED) == (confirm.FOUND, 3, 5)


def test_confirmation_key_in_another_manufacturer_id():
    """The search splits words at "_": the key also hits an older issue whose manufacturer id ends
    with the same 8 characters. Only the issue whose title ends with the fixed part counts."""
    fixed = confirm.fixed_part("_TZE200_aaaa", "TS0601", None, "deadbeef")
    ours = confirm.title("Tuya A", "_TZE200_aaaa", "TS0601", None, "deadbeef")
    stray = confirm.title("Tuya B", "_TZE200_deadbeef", "TS0601", None, "11112222")
    assert confirm.confirmation(answer(item(2, stray, thumbs=30), item(9, ours, thumbs=1)), fixed) == (confirm.FOUND, 9, 2)
    assert confirm.confirmation(answer(item(2, stray, thumbs=30)), fixed) == (confirm.NO_ISSUE, None, 0)


def test_confirmation_pull_request_and_closed_issue():
    assert confirm.confirmation(answer(item(2, pull_request={"url": "x"}, thumbs=30), item(3, state="closed", thumbs=30)), FIXED) == (confirm.NO_ISSUE, None, 0)
    assert confirm.confirmation(answer(item(2, pull_request={"url": "x"}, thumbs=30), item(8, thumbs=1)), FIXED) == (confirm.FOUND, 8, 2)


def test_confirmation_truncated_page():
    """A page that holds fewer items than the answer counts must not read as "no issue"."""
    other = TITLE.replace(KEY, "1a2b3c4e")
    page = [item(n, other) for n in range(1, 6)]
    assert confirm.confirmation(answer(*page, total=12), FIXED) == (confirm.UNKNOWN, None, None)
    assert confirm.confirmation(answer(*page, item(40, thumbs=3), total=12), FIXED) == (confirm.FOUND, 40, 4)
    assert confirm.confirmation(answer(*page, total=5), FIXED) == (confirm.NO_ISSUE, None, 0)
    assert confirm.confirmation(answer(*page, total=3), FIXED) == (confirm.NO_ISSUE, None, 0)


@pytest.mark.parametrize("total", [None, "5", True, False, -1, 5.0, [5]])
def test_confirmation_total_count_unusable(total):
    other = TITLE.replace(KEY, "1a2b3c4e")
    data = answer(*(item(n, other) for n in range(1, 6)))
    if total is None:
        del data["total_count"]
    else:
        data["total_count"] = total
    assert confirm.confirmation(data, FIXED) == (confirm.UNKNOWN, None, None)
    data["items"].append(item(9))
    assert confirm.confirmation(data, FIXED) == (confirm.FOUND, 9, 1)


@pytest.mark.parametrize("incomplete", [True, 1, "false", None])
def test_confirmation_incomplete_results(incomplete):
    """An incomplete answer may lack the oldest matching issue: a match in it is no answer either (a newer duplicate)."""
    other = TITLE.replace(KEY, "1a2b3c4e")
    assert confirm.confirmation(answer(item(2, other), incomplete_results=incomplete), FIXED) == (confirm.UNKNOWN, None, None)
    assert confirm.confirmation(answer(item(2, other), item(31, thumbs=9), incomplete_results=incomplete), FIXED) == (confirm.UNKNOWN, None, None)


def test_confirmation_without_incomplete_results():
    data = answer()
    del data["incomplete_results"]
    assert confirm.confirmation(data, FIXED) == (confirm.NO_ISSUE, None, 0)


def test_confirmation_more_items_than_looked_at():
    """Items past MAX_ITEMS are not looked at: no match before them is "unknown", not "no issue"."""
    other = TITLE.replace(KEY, "1a2b3c4e")
    page = [item(n, other) for n in range(1, 101)] + [item(101)]
    assert confirm.confirmation(answer(*page), FIXED) == (confirm.UNKNOWN, None, None)


@pytest.mark.parametrize("broken", [None, "x", item("2"), item(2, reactions=None), item(2, thumbs="9"), item(2, thumbs=-1), item(2, None),
                                    item(2, state=None), {"number": 2, "title": TITLE}])
def test_confirmation_malformed_item(broken):
    """A malformed item may be the issue: without a match the answer is "unknown". So is a match after it (the answer
    is oldest first: the malformed one may be the oldest). A match before it stands. A legitimately skipped item (a
    pull request, a closed issue) is no reason for that."""
    other = TITLE.replace(KEY, "1a2b3c4e")
    assert confirm.confirmation(answer(item(1, other), broken), FIXED) == (confirm.UNKNOWN, None, None)
    assert confirm.confirmation(answer(broken, item(5, thumbs=1)), FIXED) == (confirm.UNKNOWN, None, None)
    assert confirm.confirmation(answer(item(1, other), broken, item(5, thumbs=1)), FIXED) == (confirm.UNKNOWN, None, None)
    assert confirm.confirmation(answer(item(5, thumbs=1), broken), FIXED) == (confirm.FOUND, 5, 2)
    assert confirm.confirmation(answer(item(5, thumbs=1), broken, item(9, thumbs=7)), FIXED) == (confirm.FOUND, 5, 2)
    assert confirm.confirmation(answer(item(2, pull_request={"url": "x"}), item(3, state="closed"), item(5, thumbs=1)), FIXED) == (confirm.FOUND, 5, 2)


def test_confirmation_huge_thumbs():
    assert confirm.confirmation(answer(item(4, thumbs=10**12)), FIXED) == (confirm.FOUND, 4, 10_001)


@pytest.mark.parametrize("fixed", [None, "", "x", "[A | B | C | 1A2B3C4D]", 5])
def test_confirmation_bad_fixed_part(fixed):
    assert confirm.confirmation(answer(item(4, thumbs=2)), fixed) == (confirm.UNKNOWN, None, None)


def test_confirmation_markdown_and_newlines_in_titles():
    assert confirm.confirmation(answer(item(2, f"**x**\n[evil](http://e) | ] [ {FIXED}\n")), FIXED) == (confirm.FOUND, 2, 1)
    assert confirm.confirmation(answer(item(2, f"{FIXED}\n[evil](http://e)")), FIXED) == (confirm.NO_ISSUE, None, 0)


# The threshold and the decision

@pytest.mark.parametrize("value", [1, 2, 10, 1000])
def test_threshold_valid(value):
    assert confirm.valid_threshold(value) == value
    assert (confirm.THRESHOLD_MIN, confirm.THRESHOLD_MAX) == (1, 1000)


@pytest.mark.parametrize("value", [0, -1, 1001, 10**12, True, False, 10.0, "10", None, [10], {"threshold": 10}, float("inf")])
def test_threshold_invalid(value):
    assert confirm.valid_threshold(value) is None


@pytest.mark.parametrize(("count", "threshold", "reached"), [
    (10, 10, True), (11, 10, True), (9, 10, False), (2, 2, True), (1, 2, False), (0, 1, False), (10_001, 1000, True),
    (None, 10, False),  # unknown count
    (10, None, False),  # missing threshold
    (10, 0, False), (10, True, False), (10, "10", False), (5000, 1001, False),  # threshold of the wrong shape
    (True, 1, False), (10.0, 10, False), ("10", 10, False),  # count of the wrong shape
])
def test_reached(count, threshold, reached):
    assert confirm.reached(count, threshold) is reached


def test_decision_from_an_answer():
    assert confirm.reached(confirm.confirmation(answer(item(4, thumbs=9)), FIXED).count, 10)
    assert not confirm.reached(confirm.confirmation(answer(item(4, thumbs=8)), FIXED).count, 10)
    assert not confirm.reached(confirm.confirmation(answer(), FIXED).count, 1)
    assert not confirm.reached(confirm.confirmation(None, FIXED).count, 1)


# Loading by its path alone

PATH = FOLDER / "confirm.py"

STANDALONE = """
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("confirm_standalone", sys.argv[1])
confirm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(confirm)
fixed = confirm.fixed_part("_TZE200_aaaa", "TS0601", None, "1a2b3c4d")
title = confirm.title("Tuya A", "_TZE200_aaaa", "TS0601", None, "1a2b3c4d")
answer = {"total_count": 1, "incomplete_results": False, "items": [{"number": 4, "title": title, "state": "open", "reactions": {"+1": 2}}]}
result = confirm.confirmation(answer, fixed)
print(json.dumps([title, list(confirm.parse(title)), list(result), confirm.reached(result.count, 3),
                  sorted(name for name in sys.modules if name.startswith(("homeassistant", "custom_components")))]))
"""


def test_loads_by_its_path_alone():
    """Without site-packages (no Home Assistant at all) and outside the package: how a tool loads it by its path."""
    done = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", STANDALONE, str(PATH)], capture_output=True, text=True, check=True, timeout=60)
    title, fields, result, reached, loaded = json.loads(done.stdout)
    assert title == "Verify: Tuya A [_TZE200_aaaa | TS0601 | unknown | 1a2b3c4d]"
    assert fields == ["_TZE200_aaaa", "TS0601", "unknown", "1a2b3c4d"]
    assert result == ["found", 4, 3]
    assert reached is True
    assert loaded == []


def test_imports_only_the_standard_library():
    tree = ast.parse(PATH.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative import"
            assert node.module.split(".")[0] in sys.stdlib_module_names, node.module
        elif isinstance(node, ast.Import):
            assert all(alias.name.split(".")[0] in sys.stdlib_module_names for alias in node.names)
