"""The GitHub seam. RealGh shells out; FakeGh is in memory and used by tests."""

from __future__ import annotations

import json
import subprocess
from typing import Protocol


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
            raise RuntimeError(f"gh {' '.join(args)} failed: {exc.stderr.strip()}") from exc

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
        self._run("issue", "edit", str(issue), "--add-label", ",".join(labels))

    def remove_labels(self, issue: int, labels: list[str]) -> None:
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
        self._run("label", "create", name, "--color", colour, "--force")


class FakeGh:
    """In-memory ledger with the same surface. The only client tests use."""

    def __init__(self) -> None:
        self.issues: dict[int, dict] = {}
        self._next = 1
        self.ensured_labels: set[str] = set()

    def create_issue(self, title: str, body: str, labels: list[str]) -> int:
        number = self._next
        self._next += 1
        self.issues[number] = {
            "number": number,
            "title": title,
            "body": body,
            "labels": list(labels),
            "state": "open",
            "comments": [],
        }
        return number

    def comment(self, issue: int, body: str) -> None:
        self.get_issue(issue)["comments"].append(body)

    def add_labels(self, issue: int, labels: list[str]) -> None:
        current = self.get_issue(issue)["labels"]
        current.extend(label for label in labels if label not in current)

    def remove_labels(self, issue: int, labels: list[str]) -> None:
        record = self.get_issue(issue)
        record["labels"] = [label for label in record["labels"] if label not in labels]

    def get_issue(self, issue: int) -> dict:
        if issue not in self.issues:
            raise KeyError(f"no such issue: {issue}")
        return self.issues[issue]

    def close_issue(self, issue: int) -> None:
        self.get_issue(issue)["state"] = "closed"

    def list_issues(self, state: str = "open") -> list[dict]:
        """Summary view, deliberately WITHOUT `comments` -- RealGh's `issue list`
        does not fetch comment bodies, and a fake that returns more than the real
        client makes code pass every test here and KeyError against GitHub.
        Use `get_issue` when you need comments."""
        return [
            {k: v for k, v in issue.items() if k != "comments"}
            for issue in self.issues.values()
            if state == "all" or issue["state"] == state
        ]

    def search_titles(self, text: str) -> list[dict]:
        return [i for i in self.list_issues("all") if text.lower() in i["title"].lower()]

    def ensure_label(self, name: str, colour: str = "ededed") -> None:
        self.ensured_labels.add(name)
