"""The bot of the "Device test" issues (label `verify`): .github/workflows/verify-bot.yml runs it on issue events.

Usage (the environment of a workflow run: GITHUB_EVENT_PATH, GITHUB_EVENT_NAME, GITHUB_REPOSITORY, GITHUB_TOKEN):
    python3 -I tools/verify_bot.py

opened: the form's "Device variant" field, when it holds exactly a fixed part as confirm.fixed_part() writes it,
becomes the end of the title (the readable part stays). Then, when an older open verify issue (a lower number) has
that fixed part, a comment links it and the new issue closes as a duplicate. Newer closes, older stays: two runs at
once do no harm. reopened: the same check with the fixed part the title has. The body is read on opened only, so an
edited body never moves an old issue to another variant.
edited: a title edit keeps the fixed part. A title that lost or changed it gets it back, with the new readable part
when confirm.title() keeps that unchanged, else the old title comes back. A title that had none gets none.

Issues without the label stay untouched. The event comes from its file, never through the shell. Every read comes
before the first write, the comment before the close. Nothing of an issue's text reaches the log (it could hold a
workflow command). An API error ends the run with exit 1. Standard library and confirm.py (loaded by its path).
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
API = "https://api.github.com"
LABEL = "verify"
HEADING = "### Device variant"
PER_PAGE = 100
MAX_PAGES = 10
MAX_BYTES = PER_PAGE * 512 * 1024
TIMEOUT = 20
REPOSITORY = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+")
COMMENT = ("Thanks for testing. Issue #{number} already collects the tests of this device variant. Please give it a"
           " thumbs-up there: that counts as your confirmation. I close this one as a duplicate.")

_spec = importlib.util.spec_from_file_location("verify_bot_confirm", ROOT / "custom_components/xquirks/confirm.py")
confirm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(confirm)


class ApiError(Exception):
    """GitHub answered something the bot cannot use (or it has no token)."""


class GitHub:
    """Calls of the REST API on one repository with the run's token: one try each, TIMEOUT seconds, MAX_BYTES."""

    def __init__(self, repository: str, token: str | None) -> None:
        self.repository, self.token = repository, token

    def __call__(self, method: str, path: str, data: dict | None = None):
        if not self.token:
            raise ApiError("no token")
        request = urllib.request.Request(f"{API}/repos/{self.repository}{path}", method=method, headers={
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "xquirks-verify-bot", "Authorization": f"Bearer {self.token}",
        }, data=None if data is None else json.dumps(data).encode())
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ApiError("answer too large")
        return json.loads(body) if body else None


def variant(body: object) -> str | None:
    """The fixed part in the form's "Device variant" field: its heading once, a blank line, the value (outer spaces
    stripped), a blank line or the end. None unless the value is exactly a fixed part."""
    lines = body.replace("\r\n", "\n").split("\n") if isinstance(body, str) else []
    at = [index for index, line in enumerate(lines) if line == HEADING]
    if len(at) != 1 or (at[0] and lines[at[0] - 1]) or lines[at[0] + 1:at[0] + 2] != [""] or len(lines) < at[0] + 3:
        return None
    value, after = lines[at[0] + 2].strip(), lines[at[0] + 3:at[0] + 4]
    fields = confirm.parse(value)
    return value if fields and confirm.fixed_part(*fields) == value and after in ([], [""]) else None


def readable(title: str) -> str:
    """The readable part of a title: without a fixed part at its end and without the "Verify:" prefix."""
    text = title.strip()
    if confirm.parse(text) is not None:
        text = text[:text.rfind("[")].rstrip()
    prefix = confirm.PREFIX.strip()
    return text[len(prefix):].strip() if text[:len(prefix)].lower() == prefix.lower() else text


def restored(old: object, new: str) -> str | None:
    """The title an edit from old to new gets back, None when it may stay."""
    if not isinstance(old, str):
        return None
    if (fields := confirm.parse(old)) is None:
        return old if confirm.parse(new) is not None else None
    if confirm.matches(new, fixed := confirm.fixed_part(*fields)):
        return None
    name = readable(new)
    title = confirm.title(name, *fields)
    return title if title == (f"{confirm.PREFIX}{name} {fixed}" if name else f"{confirm.PREFIX}{fixed}") else old


def older(api, number: int, fixed: str) -> int | None:
    """The number of the oldest open verify issue with the fixed part when it is older than number (pages oldest first)."""
    for page in range(1, MAX_PAGES + 1):
        items = api("GET", f"/issues?state=open&labels={LABEL}&sort=created&direction=asc&per_page={PER_PAGE}&page={page}")
        if not isinstance(items, list):
            raise ApiError("the listing is no list")
        if (found := confirm.oldest(confirm.read_issues(items), fixed)) is not None and found.number < number:
            return found.number
        if len(items) < PER_PAGE:
            return None
    return None


def handle(action: object, event: dict, api) -> None:
    """What the bot does on one issue event."""
    issue = event.get("issue")
    labels = issue.get("labels") if isinstance(issue, dict) else None
    if not isinstance(labels, list) or not any(isinstance(label, dict) and label.get("name") == LABEL for label in labels):
        return
    number, title = issue.get("number"), issue.get("title")
    if type(number) is not int or not 0 < number <= confirm.MAX_NUMBER or not isinstance(title, str):
        return
    if action == "edited":
        changes = event.get("changes")
        change = changes.get("title") if isinstance(changes, dict) else None
        if isinstance(change, dict) and (title := restored(change.get("from"), title)) is not None:
            api("PATCH", f"/issues/{number}", {"title": title})
        return
    if action == "opened" and (fixed := variant(issue.get("body"))) is not None:
        new = None if confirm.matches(title, fixed) else confirm.title(readable(title), *confirm.parse(fixed))
    elif action == "reopened" and (fields := confirm.parse(title)) is not None:
        fixed, new = confirm.fixed_part(*fields), None
    else:
        return
    if (first := older(api, number, fixed)) is not None:
        api("POST", f"/issues/{number}/comments", {"body": COMMENT.format(number=first)})
        api("PATCH", f"/issues/{number}", {"state": "closed", "state_reason": "duplicate"} | ({"title": new} if new else {}))
    elif new:
        api("PATCH", f"/issues/{number}", {"title": new})


def main(environ=os.environ, api=None) -> int:
    repository = environ.get("GITHUB_REPOSITORY", "")
    if not REPOSITORY.fullmatch(repository):
        print("GITHUB_REPOSITORY is no owner/name")
        return 1
    if environ.get("GITHUB_EVENT_NAME") != "issues":
        return 0
    try:
        event = json.loads(pathlib.Path(environ.get("GITHUB_EVENT_PATH", "")).read_text(encoding="utf-8"))
        if not isinstance(event, dict):
            raise ValueError("the event is no object")
        handle(event.get("action"), event, api or GitHub(repository, environ.get("GITHUB_TOKEN")))
    except urllib.error.HTTPError as error:
        print(f"the GitHub API answered HTTP {int(error.code)}")
        return 1
    except (ApiError, OSError, ValueError, http.client.HTTPException) as error:
        print(f"stopped: {type(error).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
