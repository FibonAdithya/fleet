# Captured Claude Code transcript

Real `~/.claude/projects/<slug>/<session-uuid>.jsonl` output, reduced to the two
fields `src/fleet/cost.py` reads: `message.model` and `message.usage`. Everything
else — prompts, responses, tool calls — is stripped deliberately: the fixture
must not carry conversation content, and the pricer never reads it.

Lines with no `message` are kept on purpose. A transcript is mostly such lines,
and skipping them rather than crashing on them is behaviour worth pinning.

Re-capture with the command in `docs/ai/plans/2026-08-18-m3-daemon.md`,
Task 9 Step 1.

# Captured codex rollouts

`codex-breakdown.jsonl` and `codex-total-only.jsonl` are the two shapes a real
`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` comes in — component token
counts populated, and a cumulative `total_tokens` with every component zero.
`CodexProvider.read` prices the first and refuses the second; see the codex
gate in `docs/runbook.md` for why neither the date nor `codex --version`
tells you which one a launch will produce.

`codex-subagent-parent.jsonl` is three records lifted out of the one local
session that spawned threads — the session behind the 74% figure in that same
runbook section. A `turn_context` naming the model, the session's final
cumulative `token_count`, and the `world_state` announcing a live subagent
roster. The session's own prose is left out on the rule above; what remains of
`turn_context` is codex's own configuration (sandbox policy, personality,
collaboration mode), not conversation.

It is kept because everything else about this transcript is priceable — known
model, a breakdown that sums to its total, no cache-write field — so without
`_codex_spawned_subagents` it prices cleanly to a confident number covering
26% of the real session. A fixture that merely failed to parse would not have
pinned that.
