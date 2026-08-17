"""Create the fixture repo's label set and seeded backlog from issues.yaml.

Idempotent twice over: a label that already exists is tolerated without being
repainted, and an issue is skipped when its title is already present.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from fleet import labels as vocab
from fleet.gh import GhClient, RealGh

# The palette the fixture repo was first painted with. ensure_label tolerates an
# existing label rather than repainting it, so these take effect only on a repo
# that has never been set up.
COLOURS = {
    "fleet:triage": "ededed",
    "fleet:ready": "0e8a16",
    "fleet:claimed": "1d76db",
    "fleet:review": "5319e7",
    "fleet:integrating": "006b75",
    "fleet:blocked": "fbca04",
    "fleet:human": "b60205",
    "fleet:done": "0e8a16",
    vocab.AUTO_OK: "c2e0c6",
}
BY_PREFIX = (("area:", "bfd4f2"), ("class:", "f9d0c4"), ("fleet:", "d4c5f9"))


def colour(name: str) -> str:
    if name in COLOURS:
        return COLOURS[name]
    for prefix, value in BY_PREFIX:
        if name.startswith(prefix):
            return value
    return "ededed"


def ensure_vocabulary(gh: GhClient) -> None:
    """Create every label in the vocabulary before any issue names one.

    `gh issue create --label X` does not create X: it fails with "could not add
    label: 'X' not found" and creates no issue at all. Applying labels without
    this step aborted on the very first issue against any repo that had not been
    hand-labelled -- so "idempotent by title, safe to re-run" held only on the
    one repo somebody had already set up by hand. Sourcing the set from
    `static_vocabulary` is also what keeps the repo and FakeGh's idea of "this
    label exists" in step when `max_attempts` moves.
    """
    for name in sorted(vocab.static_vocabulary()):
        gh.ensure_label(name, colour(name))


def main(repo: str, spec_path: str, gh: GhClient | None = None) -> int:
    """`gh` is injectable so the seeding logic can be exercised against FakeGh,
    which refuses an uncreated label exactly as gh does."""
    specs = yaml.safe_load(Path(spec_path).read_text())
    gh = gh if gh is not None else RealGh(repo)
    ensure_vocabulary(gh)
    already = {row["title"] for row in gh.list_issues("all")}
    for spec in specs:
        if spec["title"] in already:
            print(f"skip (exists): {spec['title']}")
            continue
        # Building the names through the vocabulary rather than f-strings means a
        # typo in issues.yaml's `area` or `class` fails here, with the offending
        # value named, instead of as an opaque gh label error.
        names = [
            vocab.state_label("ready"),
            vocab.area_label(spec["area"]),
            vocab.class_label(spec["class"]),
        ]
        if spec.get("auto_ok"):
            names.append(vocab.AUTO_OK)
        number = gh.create_issue(spec["title"], spec["body"], names)
        print(f"created #{number}: {spec['title']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
