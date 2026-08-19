# Captured Claude Code transcript

Real `~/.claude/projects/<slug>/<session-uuid>.jsonl` output, reduced to the two
fields `src/fleet/cost.py` reads: `message.model` and `message.usage`. Everything
else — prompts, responses, tool calls — is stripped deliberately: the fixture
must not carry conversation content, and the pricer never reads it.

Lines with no `message` are kept on purpose. A transcript is mostly such lines,
and skipping them rather than crashing on them is behaviour worth pinning.

Re-capture with the command in `docs/superpowers/plans/2026-08-18-m3-daemon.md`,
Task 9 Step 1.
