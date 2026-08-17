"""The GitHub seam. RealGh shells out; FakeGh is in memory and used by tests."""

from __future__ import annotations

import json
import subprocess
from typing import Protocol

from fleet import labels as labels_module


class GhError(RuntimeError):
    """Anything the GitHub client could not do.

    Subclasses RuntimeError so that code catching RealGh's subprocess failures
    also catches FakeGh's simulated ones -- the two clients must be
    indistinguishable to a caller's except clause.
    """


class GhClient(Protocol):
    def create_issue(self, title: str, body: str, labels: list[str]) -> int: ...
    def comment(self, issue: int, body: str) -> None: ...
    def add_labels(self, issue: int, labels: list[str]) -> None: ...
    def remove_labels(self, issue: int, labels: list[str]) -> None: ...
    def get_issue(self, issue: int) -> dict: ...
    def list_issues(self, state: str = "open") -> list[dict]: ...
    def search_titles(self, text: str) -> list[dict]: ...
    def ensure_label(self, name: str, colour: str = "ededed") -> None: ...


class RealGh:
    """Shells out to the `gh` CLI. Never used in tests."""

    def __init__(self, repo_slug: str) -> None:
        self.repo = repo_slug

    def _run(self, *args: str) -> str:
        try:
            return subprocess.run(
                ["gh", *args, "--repo", self.repo], capture_output=True, text=True, check=True
            ).stdout
        except subprocess.CalledProcessError as exc:
            # CalledProcessError's message carries the argv and exit code but not
            # stderr, which is where gh puts the actual reason. Task 19 is the only
            # live exercise of this path; an opaque failure there is undiagnosable.
            raise GhError(f"gh {' '.join(args)} failed: {exc.stderr.strip()}") from exc

    def create_issue(self, title: str, body: str, labels: list[str]) -> int:
        args = ["issue", "create", "--title", title, "--body", body]
        if labels:
            # `--label ""` is not "no labels" to gh -- it looks up a label named ""
            # and fails the whole create.
            args += ["--label", ",".join(labels)]
        url = self._run(*args).strip().splitlines()[-1]
        return int(url.rstrip("/").rsplit("/", 1)[-1])

    def comment(self, issue: int, body: str) -> None:
        self._run("issue", "comment", str(issue), "--body", body)

    def add_labels(self, issue: int, labels: list[str]) -> None:
        if not labels:
            # Mirror create_issue's guard: `--add-label ""` is not "no labels" to
            # gh -- it looks up a label named "" and fails the edit.
            return
        self._run("issue", "edit", str(issue), "--add-label", ",".join(labels))

    def remove_labels(self, issue: int, labels: list[str]) -> None:
        if not labels:
            return
        self._run("issue", "edit", str(issue), "--remove-label", ",".join(labels))

    def get_issue(self, issue: int) -> dict:
        raw = json.loads(
            self._run(
                "issue", "view", str(issue), "--json", "number,title,body,labels,state,comments"
            )
        )
        raw["labels"] = [label["name"] for label in raw.get("labels", [])]
        raw["comments"] = [c["body"] for c in raw.get("comments", [])]
        return raw

    def list_issues(self, state: str = "open") -> list[dict]:
        raw = json.loads(
            self._run(
                "issue",
                "list",
                "--state",
                state,
                "--limit",
                "200",
                "--json",
                "number,title,body,labels,state",
            )
        )
        for row in raw:
            row["labels"] = [label["name"] for label in row.get("labels", [])]
        return raw

    def search_titles(self, text: str) -> list[dict]:
        """Case-insensitive substring match over the most recent 200 issues.

        Client-side filtering, not GitHub's search API, and capped by the
        `--limit 200` in `list_issues`. Good enough as a best-effort duplicate
        check; not an exhaustive one.
        """
        return [i for i in self.list_issues("all") if text.lower() in i["title"].lower()]

    def ensure_label(self, name: str, colour: str = "ededed") -> None:
        """Create the label if it does not exist.

        `gh issue create --label X` fails with "could not add label: 'X' not
        found" rather than creating X, and creates no issue at all. Any label
        generated at runtime -- `discovered-by:<n>` above all -- must be created
        before it is applied.
        """
        try:
            self._run("label", "create", name, "--color", colour)
        except GhError as exc:
            # --force would "update the label color and description if it already
            # exists", silently repainting a curated label. Tolerate the collision
            # instead: the label existing is the outcome we wanted.
            if "already exists" not in str(exc):
                raise


class FakeGh:
    """In-memory ledger with the same surface. The only client tests use."""

    def __init__(
        self,
        max_attempts: int = labels_module.DEFAULT_MAX_ATTEMPTS,
        max_fix_passes: int = labels_module.DEFAULT_MAX_FIX_PASSES,
    ) -> None:
        self.issues: dict[int, dict] = {}
        self._next = 1
        self.ensured_labels: set[str] = set()
        # A fake standing in for a repo seeded under a different policy needs the
        # same counters that repo was seeded with, or its idea of "this label
        # exists" diverges from the real one. See labels.static_vocabulary.
        self.vocabulary = labels_module.static_vocabulary(max_attempts, max_fix_passes)

    def _check_labels(self, names: list[str]) -> None:
        """Mirror gh: naming a label that does not exist fails the whole edit and
        changes nothing. Without this the fake is more permissive than the real
        client, and a missing ensure_label call passes every test here while
        breaking against GitHub.

        Applies to removal too: `gh issue edit --remove-label X` resolves X to an
        id before it can remove it, so an X the repo has never had fails. (An X
        the repo has but the issue does not is a no-op -- that is a different
        case, and remove_labels still tolerates it.)"""
        known = self.vocabulary | self.ensured_labels
        unknown = [n for n in names if n not in known]
        if unknown:
            raise GhError(f"could not add label: {unknown[0]!r} not found")

    def _get(self, issue: int) -> dict:
        """The live, mutable record. Internal only -- callers that need to write
        (comment/add_labels/remove_labels) go through this; get_issue, the public
        read path, must not hand out this same object. See get_issue."""
        if issue not in self.issues:
            raise GhError(f"no such issue: {issue}")
        return self.issues[issue]

    def create_issue(self, title: str, body: str, labels: list[str]) -> int:
        self._check_labels(labels)
        number = self._next
        self._next += 1
        self.issues[number] = {
            "number": number,
            "title": title,
            "body": body,
            "labels": list(labels),
            "state": "OPEN",
            "comments": [],
        }
        return number

    def comment(self, issue: int, body: str) -> None:
        self._get(issue)["comments"].append(body)

    def add_labels(self, issue: int, labels: list[str]) -> None:
        if not labels:
            # Mirror RealGh: no labels to add means no edit at all, not an edit
            # naming a label called "".
            return
        self._check_labels(labels)
        current = self._get(issue)["labels"]
        current.extend(label for label in labels if label not in current)

    def remove_labels(self, issue: int, labels: list[str]) -> None:
        if not labels:
            return
        self._check_labels(labels)
        record = self._get(issue)
        record["labels"] = [label for label in record["labels"] if label not in labels]

    def get_issue(self, issue: int) -> dict:
        """A snapshot, not the live record. RealGh parses a fresh dict from gh's
        JSON every call, so a caller mutating what it gets back is a no-op
        against GitHub. Returning the stored record directly here would let that
        same mutation silently corrupt the fake's ledger -- passing every test
        while doing nothing live. Copy `labels` and `comments` too: they are
        lists, and a shallow dict copy would still share them with the store."""
        record = self._get(issue)
        copy = dict(record)
        copy["labels"] = list(record["labels"])
        copy["comments"] = list(record["comments"])
        return copy

    def close_issue(self, issue: int) -> None:
        self._get(issue)["state"] = "CLOSED"

    def list_issues(self, state: str = "open") -> list[dict]:
        """Summary view, deliberately WITHOUT `comments` -- RealGh's `issue list`
        does not fetch comment bodies, and a fake that returns more than the real
        client makes code pass every test here and KeyError against GitHub.
        Use `get_issue` when you need comments.

        `state` is matched case-insensitively: real gh's `--state` flag accepts
        "open", "OPEN", or any other casing identically, and the stored state
        here is "OPEN"/"CLOSED" (what gh's JSON actually returns -- see
        fixtures/ledger/issues.json), so a literal `==` would silently diverge
        from both callers and gh.

        Each row is a snapshot, for the same reason get_issue's is: a dict
        comprehension is a shallow copy, so handing back the stored `labels`
        list would let a caller that builds a label list in place corrupt this
        ledger -- passing every test here and doing nothing against GitHub,
        which returns freshly-parsed JSON every call."""
        normalized = state.upper()
        return [
            {**{k: v for k, v in issue.items() if k != "comments"}, "labels": list(issue["labels"])}
            for issue in self.issues.values()
            if normalized == "ALL" or issue["state"] == normalized
        ]

    def search_titles(self, text: str) -> list[dict]:
        return [i for i in self.list_issues("all") if text.lower() in i["title"].lower()]

    def ensure_label(self, name: str, colour: str = "ededed") -> None:
        self.ensured_labels.add(name)
