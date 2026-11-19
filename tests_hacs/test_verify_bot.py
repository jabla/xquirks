"""tools/verify_bot.py on event files as GitHub sends them, with a stand-in for the REST API: the title's
fixed part from the form field, duplicates of an older open issue, title edits, API errors."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

from .integration import confirm

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("verify_bot", ROOT / "tools/verify_bot.py")
verify_bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify_bot)

OPENED = json.loads((ROOT / "tests_hacs/fixtures/verify-bot-opened.json").read_text())
EDITED = json.loads((ROOT / "tests_hacs/fixtures/verify-bot-edited.json").read_text())
FIXED = "[SONOFF | ZBMicro | 0x00001005 | ab6ae10d]"
TITLE = f"Verify: SONOFF ZBMicro {FIXED}"
OTHER = "[_TZE200_abcd1234 | TS0601 | 0x00000046 | 1a2b3c4d]"
LISTING = "/issues?state=open&labels=verify&sort=created&direction=asc&per_page=100&page="


def comment(number: int) -> str:
    return (f"Thanks for testing. Issue #{number} already collects the tests of this device variant. Please give it a thumbs-up there:"
            " that counts as your confirmation. I close this one as a duplicate.")


def item(number: int, title: str = TITLE, state: str = "open", **extra) -> dict:
    """One issue of the listing as GitHub sends it, body included."""
    return {"number": number, "title": title, "state": state, "body": "x" * 1000, "labels": [{"name": "verify"}],
            "user": {"login": "someone", "id": 7}, "reactions": {"total_count": 0, "+1": 2, "-1": 0}, **extra}


class FakeGitHub:
    """Answers the listing from pages (page n = pages[n - 1], [] after the last), records every call.
    fail: the method whose first call raises an HTTP error (403)."""

    def __init__(self, pages=(), fail: str | None = None) -> None:
        self.pages, self.fail, self.calls = list(pages), fail, []

    def __call__(self, method: str, path: str, data=None):
        self.calls.append((method, path, copy.deepcopy(data)))
        if method == self.fail:
            raise urllib.error.HTTPError(path, 403, "Forbidden", {}, None)
        if method == "GET":
            assert path.startswith(LISTING)
            page = int(path.removeprefix(LISTING))
            return self.pages[page - 1] if page <= len(self.pages) else []
        return {"number": 21}

    @property
    def writes(self) -> list:
        return [call for call in self.calls if call[0] != "GET"]


def opened(title: str = TITLE, body: str | None = None, number: int = 21, action: str = "opened", labels=("verify",)) -> dict:
    event = copy.deepcopy(OPENED)
    event["action"] = action
    event["issue"].update(number=number, title=title, labels=[{"name": name} for name in labels])
    if body is not None:
        event["issue"]["body"] = body
    return event


def edited(old: str | None, new: str, state: str = "open", changes: dict | None = None) -> dict:
    event = copy.deepcopy(EDITED)
    event["issue"].update(title=new, state=state)
    event["changes"] = changes if changes is not None else {"title": {"from": old}}
    return event


def body_with(value: str) -> str:
    """The fixture's form body with another value in the "Device variant" field."""
    return OPENED["issue"]["body"].replace(f"### Device variant\n\n{FIXED}\n", f"### Device variant\n\n{value}\n")


def run(name: str, event: dict, api: FakeGitHub) -> FakeGitHub:
    verify_bot.handle(name, event, api)
    return api


def close(number: int, title: str | None = None) -> list:
    data = {"state": "closed", "state_reason": "duplicate"} | ({"title": title} if title else {})
    return [("POST", "/issues/21/comments", {"body": comment(number)}), ("PATCH", "/issues/21", data)]


# opened


def test_opened_with_matching_title_and_no_older_issue_changes_nothing():
    api = run("opened", opened(), FakeGitHub([[item(21)]]))
    assert api.writes == [] and api.calls == [("GET", LISTING + "1", None)]


def test_opened_with_a_wrong_title_gets_the_fixed_part_of_the_field():
    api = run("opened", opened("Verify: SONOFF ZBMicro"), FakeGitHub([[item(21, "Verify: SONOFF ZBMicro")]]))
    assert api.writes == [("PATCH", "/issues/21", {"title": TITLE})]


def test_opened_with_the_form_default_title_or_another_fixed_part():
    """The readable part stays, a fixed part at its end gives way to the field's."""
    api = run("opened", opened(f"Verify: SONOFF ZBMicro {OTHER}"), FakeGitHub())
    assert api.writes == [("PATCH", "/issues/21", {"title": TITLE})]
    api = run("opened", opened("Verify: <vendor> <model>"), FakeGitHub())
    assert api.writes == [("PATCH", "/issues/21", {"title": f"Verify: <vendor> <model> {FIXED}"})]
    api = run("opened", opened(""), FakeGitHub())
    assert api.writes == [("PATCH", "/issues/21", {"title": f"Verify: {FIXED}"})]


@pytest.mark.parametrize("body", [
    body_with("_No response_"),
    body_with(""),
    body_with("[SONOFF|ZBMicro | 0x00001005 | ab6ae10d]"),
    body_with("[SONOFF | ZBMicro | 0x00001005 | AB6AE10D]"),
    body_with(f"{FIXED} extra"),
    body_with(f"see {FIXED}"),
    body_with(f"[x]({FIXED})"),
    body_with("[SONOFF | ZBMicro | 0x00001005 | extra | ab6ae10d]"),
    body_with(f"[SONOFF  | ZBMicro | 0x00001005 | ab6ae10d]"),
    body_with(f"{FIXED}\n### Probe report and diagnostics"),
    OPENED["issue"]["body"].replace("### Device variant\n\n", "### Device variant\n"),
    OPENED["issue"]["body"].replace("### Device variant", "### Device Variant"),
    OPENED["issue"]["body"].replace("2026.11.2\n\n### Device variant", "2026.11.2\n### Device variant"),
    OPENED["issue"]["body"] + f"\n\n### Device variant\n\n{FIXED}\n",
    OPENED["issue"]["body"].split("### Device variant")[0],
    "",
    None,
    42,
])
def test_opened_with_a_missing_or_malformed_field_does_nothing(body):
    event = opened("Verify: SONOFF ZBMicro")
    event["issue"]["body"] = body
    api = run("opened", event, FakeGitHub([[item(5)]]))
    assert api.calls == []


def test_opened_field_with_windows_line_ends_or_outer_spaces():
    api = run("opened", opened("Verify: SONOFF ZBMicro", OPENED["issue"]["body"].replace("\n", "\r\n")), FakeGitHub())
    assert api.writes == [("PATCH", "/issues/21", {"title": TITLE})]
    api = run("opened", opened("Verify: SONOFF ZBMicro", body_with(f"  {FIXED} ")), FakeGitHub())
    assert api.writes == [("PATCH", "/issues/21", {"title": TITLE})]


def test_opened_as_duplicate_of_an_older_open_issue_is_closed_with_a_pointer():
    api = run("opened", opened(), FakeGitHub([[item(3, f"Verify: other {OTHER}"), item(12, "Verify: an older name " + FIXED), item(17), item(21)]]))
    assert api.writes == close(12)


def test_duplicate_with_a_wrong_title_gets_the_title_in_the_same_edit():
    api = run("opened", opened("Verify: SONOFF ZBMicro"), FakeGitHub([[item(12)]]))
    assert api.writes == close(12, TITLE)


def test_opened_when_only_a_newer_issue_matches_stays_open():
    api = run("opened", opened(), FakeGitHub([[item(21), item(22), item(30)]]))
    assert api.writes == []


def test_opened_when_the_older_match_is_closed_or_no_issue_stays_open():
    """The listing holds open issues only: a closed one in it, a pull request or a malformed item does not count."""
    malformed = [item(4, state="closed"), item(5, pull_request={"url": "x"}), item(6, reactions=None), item(True), item(7, title=["x"]),
                 "x", item(9, title=TITLE.replace("ab6ae10d", "ab6ae10e"))]
    api = run("opened", opened(), FakeGitHub([malformed + [item(21)]]))
    assert api.writes == [] and len(api.calls) == 1
    # " " at the end of a title is no other fixed part: GitHub strips it, matches() too
    api = run("opened", opened(), FakeGitHub([[item(8, title=" " + TITLE + " ")]]))
    assert api.writes == close(8)


def test_listing_reads_pages_until_a_short_page_or_the_cap():
    filler = [item(n, f"Verify: x {OTHER}") for n in range(100, 200)]
    api = run("opened", opened(number=500), FakeGitHub([filler, filler, [item(12)]]))
    assert [call[1] for call in api.calls if call[0] == "GET"] == [LISTING + "1", LISTING + "2", LISTING + "3"]
    assert api.writes[1][2] == {"state": "closed", "state_reason": "duplicate"}
    api = run("opened", opened(number=500), FakeGitHub([filler] * (verify_bot.MAX_PAGES + 2)))
    assert len(api.calls) == verify_bot.MAX_PAGES and api.writes == []


def test_reopened_checks_the_fixed_part_of_the_title_not_the_body():
    """A body edited after the opening never decides: reopened reads the title alone."""
    event = opened(f"Verify: x {OTHER}", action="reopened")
    api = run("reopened", event, FakeGitHub([[item(11, f"Verify: y {OTHER}"), item(12)]]))
    assert api.writes == close(11)
    api = run("reopened", opened("Verify: SONOFF ZBMicro", action="reopened"), FakeGitHub([[item(12)]]))
    assert api.calls == []
    api = run("reopened", opened(action="reopened"), FakeGitHub([[item(21), item(25)]]))
    assert api.writes == []


# edited


def test_edited_title_that_drops_the_fixed_part_gets_it_back_with_the_new_name():
    api = run("edited", EDITED, FakeGitHub())
    assert api.calls == [("PATCH", "/issues/21", {"title": f"Verify: SONOFF ZBMicro plug {FIXED}"})]


def test_edited_title_with_another_fixed_part_gets_the_old_one_back():
    api = run("edited", edited(TITLE, f"Verify: Better name {OTHER}"), FakeGitHub())
    assert api.calls == [("PATCH", "/issues/21", {"title": f"Verify: Better name {FIXED}"})]
    api = run("edited", edited(TITLE, f"Better name {OTHER}"), FakeGitHub())
    assert api.calls == [("PATCH", "/issues/21", {"title": f"Verify: Better name {FIXED}"})]
    api = run("edited", edited(TITLE, ""), FakeGitHub())
    assert api.calls == [("PATCH", "/issues/21", {"title": f"Verify: {FIXED}"})]


@pytest.mark.parametrize("new", [
    f"Verify: x {FIXED} extra",
    "Verify: a | b",
    "Verify: [x](https://example.com)",
    "Verify: line\nbreak",
    "Verify: \u00e4rger",
    "Verify: " + "y" * 101,
    f"Verify: {OTHER} {OTHER}",
])
def test_edited_title_with_an_unclean_name_gets_the_old_title_back(new):
    api = run("edited", edited(TITLE, new), FakeGitHub())
    assert api.calls == [("PATCH", "/issues/21", {"title": TITLE})]


def test_edited_title_that_keeps_the_fixed_part_changes_nothing():
    api = run("edited", edited(TITLE, f"Verify: **Another** name | x {FIXED}"), FakeGitHub())
    assert api.calls == []


def test_edited_title_may_not_gain_a_fixed_part():
    """An old issue opened without one never becomes the oldest issue of a variant."""
    api = run("edited", edited("Verify: SONOFF ZBMicro", TITLE), FakeGitHub())
    assert api.calls == [("PATCH", "/issues/21", {"title": "Verify: SONOFF ZBMicro"})]
    api = run("edited", edited("Verify: SONOFF ZBMicro", "Verify: SONOFF ZBMicro plug"), FakeGitHub())
    assert api.calls == []


def test_edited_closed_issue_is_restored_too():
    api = run("edited", edited(TITLE, f"Verify: x {OTHER}", state="closed"), FakeGitHub())
    assert api.calls == [("PATCH", "/issues/21", {"title": f"Verify: x {FIXED}"})]


@pytest.mark.parametrize("changes", [
    {"body": {"from": "### Device variant\n\n" + OTHER}},
    {},
    {"title": {}},
    {"title": {"from": None}},
    {"title": "x"},
    None,
])
def test_edited_body_only_or_no_old_title_changes_nothing(changes):
    event = edited(None, f"Verify: x {OTHER}")
    event["issue"]["body"] = body_with(OTHER)
    if changes is None:
        del event["changes"]
    else:
        event["changes"] = changes
    api = run("edited", event, FakeGitHub())
    assert api.calls == []


# who and what is touched


@pytest.mark.parametrize("labels", [(), ("bug",), ("verify-ish",)])
@pytest.mark.parametrize("name", ["opened", "reopened", "edited"])
def test_issue_without_the_label_is_never_touched(name, labels):
    event = opened("Verify: SONOFF ZBMicro", labels=labels) if name != "edited" else edited(TITLE, "x")
    event["issue"]["labels"] = [{"name": label} for label in labels]
    api = run(name, event, FakeGitHub([[item(5)]]))
    assert api.calls == []


@pytest.mark.parametrize("issue", [
    {"labels": "verify"},
    {"labels": [{"name": "verify"}], "number": "21"},
    {"labels": [{"name": "verify"}], "number": True},
    {"labels": [{"name": "verify"}], "number": 0},
    {"labels": [{"name": "verify"}], "title": None},
])
def test_malformed_issue_is_never_touched(issue):
    event = opened("Verify: SONOFF ZBMicro")
    event["issue"].update(issue)
    api = run("opened", event, FakeGitHub([[item(5)]]))
    assert api.calls == []
    api = run("labeled", opened("Verify: SONOFF ZBMicro"), FakeGitHub([[item(5)]]))
    assert api.calls == []


@pytest.mark.parametrize("title", [
    "Verify: **bold** ![x](https://example.com/a.png) | ] [ `code` <script>",
    "Verify: a\nb\r\nc\u2028d\u00a0e",
    "Verify: " + "\u00e4" * 300,
    "[" * 200,
    f"Verify: x {FIXED[:-1]}",
])
def test_hostile_titles_become_a_clean_name_with_the_fixed_part(title):
    api = run("opened", opened(title), FakeGitHub())
    (new,) = [call[2]["title"] for call in api.writes]
    assert new.endswith(" " + FIXED) and len(new) <= confirm.MAX_TITLE and confirm.parse(new) == confirm.parse(FIXED)
    assert "\n" not in new and new.count("[") == 1 and new.count("]") == 1 and new.count("|") == 3


def test_comment_holds_only_the_number_and_fixed_text():
    """Nothing of the older issue's title or body reaches the comment."""
    api = run("opened", opened(), FakeGitHub([[item(12, f"Verify: <img src=x onerror=alert(1)> @everyone {FIXED}")]]))
    assert api.writes == close(12)
    text = api.writes[0][2]["body"]
    assert all(mark not in text for mark in (";", ", and", ", or", "->", "\u2192"))


# API errors


def test_listing_error_leaves_the_issue_untouched():
    api = FakeGitHub(fail="GET")
    with pytest.raises(urllib.error.HTTPError):
        run("opened", opened("Verify: SONOFF ZBMicro"), api)
    assert api.writes == []


def test_listing_that_answers_no_list_is_an_error():
    for answer in ({"message": "x"}, None, "x"):
        api = FakeGitHub([answer])
        with pytest.raises(verify_bot.ApiError):
            run("opened", opened("Verify: SONOFF ZBMicro"), api)
        assert api.writes == []


def test_comment_error_means_no_close():
    api = FakeGitHub([[item(12)]], fail="POST")
    with pytest.raises(urllib.error.HTTPError):
        run("opened", opened("Verify: SONOFF ZBMicro"), api)
    assert [call[0] for call in api.calls] == ["GET", "POST"]


def write_event(tmp_path: Path, event: object) -> Path:
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event))
    return path


def environ(path: Path, **extra) -> dict:
    return {"GITHUB_EVENT_PATH": str(path), "GITHUB_EVENT_NAME": "issues", "GITHUB_REPOSITORY": "jabla/xquirks", "GITHUB_TOKEN": "t", **extra}


def test_main_exits_non_zero_on_an_api_error(tmp_path, capsys):
    path = write_event(tmp_path, opened("Verify: SONOFF ZBMicro"))
    assert verify_bot.main(environ(path), FakeGitHub(fail="GET")) == 1
    assert verify_bot.main(environ(path), FakeGitHub([[item(12)]], fail="PATCH")) == 1
    assert verify_bot.main(environ(path), FakeGitHub([[item(12)]])) == 0
    # no title or body of an issue in the log: it could hold a workflow command
    assert "Verify" not in capsys.readouterr().out


def test_main_refuses_a_bad_environment(tmp_path):
    path = write_event(tmp_path, opened("Verify: SONOFF ZBMicro"))
    api = FakeGitHub()
    assert verify_bot.main(environ(path, GITHUB_REPOSITORY="jabla/x quirks?"), api) == 1
    assert verify_bot.main(environ(path, GITHUB_EVENT_NAME="issue_comment"), api) == 0
    assert verify_bot.main(environ(tmp_path / "missing.json"), api) == 1
    assert verify_bot.main(environ(write_event(tmp_path, ["x"])), api) == 1
    assert api.calls == []


def test_github_client_without_token_refuses():
    with pytest.raises(verify_bot.ApiError):
        verify_bot.GitHub("jabla/xquirks", "")("GET", LISTING + "1")


def test_script_runs_isolated_without_network_when_nothing_to_do(tmp_path):
    """python3 -I as the workflow runs it: loads confirm.py by its path, no token needed for an issue it leaves alone."""
    for event in (opened(labels=("bug",)), opened("Verify: x", body_with("_No response_")), edited(TITLE, f"Verify: y {FIXED}")):
        path = write_event(tmp_path, event)
        env = {"PATH": os.environ["PATH"], "GITHUB_EVENT_PATH": str(path), "GITHUB_EVENT_NAME": "issues", "GITHUB_REPOSITORY": "jabla/xquirks"}
        done = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "tools/verify_bot.py")], env=env, capture_output=True, text=True, timeout=60)
        assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    path = write_event(tmp_path, opened("Verify: x"))
    env = {"PATH": os.environ["PATH"], "GITHUB_EVENT_PATH": str(path), "GITHUB_EVENT_NAME": "issues", "GITHUB_REPOSITORY": "jabla/xquirks"}
    done = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "tools/verify_bot.py")], env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 1 and "Verify" not in done.stdout + done.stderr
