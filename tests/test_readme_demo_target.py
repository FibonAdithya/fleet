"""README's demo-target setup forks a repository this project does not own.

GitHub lets only users with access to a private repository fork it, so the
`gh repo fork` step works for other readers only once the fixture repository
is public. The README has to say so, and give the command that shows the
repository's current visibility, or the setup path fails with no explanation
for everyone but the owner.
"""

from pathlib import Path

README = Path(__file__).resolve().parent.parent / "README.md"
FIXTURE = "FibonAdithya/fleet-fixture"


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start:] if end == -1 else text[start:end]


def test_demo_target_section_says_the_fixture_must_be_public():
    section = _section(README.read_text(), "## Trying it on the demo target")
    assert f"gh repo fork {FIXTURE} --clone" in section
    fork_at = section.index(f"gh repo fork {FIXTURE} --clone")
    before_fork = section[:fork_at]
    assert f"gh repo view {FIXTURE} --json visibility" in before_fork
    assert "must be public" in before_fork
    assert "history" in before_fork
