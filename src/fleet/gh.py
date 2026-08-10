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


class RealGh:
    """Shells out to the `gh` CLI. Never used in tests."""

    def __init__(self, repo_slug: str) -> None:
        self.repo = repo_slug

    def _run(self, *args: str) -> str:
        return subprocess.run(
            ["gh", *args, "--repo", self.repo], capture_output=True, text=True, check=True
        ).stdout

    def create_issue(self, title: str, body: str, labels: list[str]) -> int:
        url = self._run(
            "issue", "create", "--title", title, "--body", body, "--label", ",".join(labels)
        ).strip()
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
        return [i for i in self.list_issues("all") if text.lower() in i["title"].lower()]


class FakeGh:
    """In-memory ledger with the same surface. The only client tests use."""

    def __init__(self) -> None:
        self.issues: dict[int, dict] = {}
        self._next = 1

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
        return [i for i in self.issues.values() if state == "all" or i["state"] == state]

    def search_titles(self, text: str) -> list[dict]:
        return [i for i in self.issues.values() if text.lower() in i["title"].lower()]
