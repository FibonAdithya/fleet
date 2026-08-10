"""Create the seeded backlog from issues.yaml. Idempotent by issue title."""

from __future__ import annotations

import json
import subprocess
import sys

import yaml


def existing_titles(repo: str) -> set[str]:
    out = subprocess.run(
        ["gh", "issue", "list", "--repo", repo, "--state", "all", "--limit", "200", "--json", "title"],
        capture_output=True, text=True, check=True,
    ).stdout
    return {row["title"] for row in json.loads(out)}


def main(repo: str, spec_path: str) -> int:
    specs = yaml.safe_load(open(spec_path))
    already = existing_titles(repo)
    for spec in specs:
        if spec["title"] in already:
            print(f"skip (exists): {spec['title']}")
            continue
        labels = [f"area:{spec['area']}", f"class:{spec['class']}", "fleet:ready"]
        if spec.get("auto_ok"):
            labels.append("fleet:auto-ok")
        subprocess.run(
            ["gh", "issue", "create", "--repo", repo, "--title", spec["title"],
             "--body", spec["body"], "--label", ",".join(labels)],
            check=True,
        )
        print(f"created: {spec['title']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
