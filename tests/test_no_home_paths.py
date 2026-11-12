"""Guard: no tracked fixture, test or runbook names a real home directory."""

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

SLASH_HOME = re.compile(r"/home/([A-Za-z0-9_.-]+)")
ENCODED_HOME = re.compile(r"-home-([A-Za-z0-9_]+)-")


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z", "--", "fixtures", "tests", "docs/runbook.md"],
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8")
    return [f for f in out.split("\0") if f]


def text_of(rel: str) -> str:
    return (REPO / rel).read_bytes().decode("utf-8", errors="replace")


def foreign_homes(text: str) -> list[str]:
    """Home-directory references whose user name is not exactly `u`."""
    hits = [m.group(0) for m in SLASH_HOME.finditer(text) if m.group(1) != "u"]
    hits += [m.group(0) for m in ENCODED_HOME.finditer(text) if m.group(1) != "u"]
    return hits


FOREIGN = "/" + "home/" + "alice/proj"  # never a literal
ENCODED = "-" + "home-" + "alice-proj"


def test_a_foreign_home_is_found():
    assert foreign_homes(f"cwd: {FOREIGN}") == ["/" + "home/alice"]


def test_the_placeholder_home_is_allowed():
    assert foreign_homes("/home/u/TIG/fleet and /home/u") == []


def test_a_name_that_merely_starts_with_u_is_foreign():
    # catches `name.startswith("u")` or a regex `(?!u)` without a boundary
    assert foreign_homes("/" + "home/uma/x") == ["/" + "home/uma"]


def test_an_encoded_foreign_home_is_found():
    assert foreign_homes(f"/tmp/claude-1000/{ENCODED}/x") == ["-" + "home-alice-"]


def test_the_encoded_placeholder_is_allowed():
    assert foreign_homes("/tmp/claude-1000/-home-u-TIG-fleet/x") == []


def test_the_scan_covers_the_fixtures():
    files = tracked_files()
    assert "fixtures/herdr/snapshot-fleet.json" in files
    assert "docs/runbook.md" in files
    assert "tests/test_no_home_paths.py" in files


def test_no_tracked_file_names_a_foreign_home():
    hits = {f: foreign_homes(text_of(f)) for f in tracked_files()}
    hits = {f: h for f, h in hits.items() if h}
    assert not hits, hits
