# Multi-provider cost accounting, and per-role agent/model routing

M3 shipped a budget ceiling that reads Claude Code transcripts and nothing else.
`cost.py`'s docstring is explicit that this is the part most likely to be quietly
wrong, which is why it is a pure function. Extending it to a second CLI is
therefore not a matter of parameterising field names: the two transcript formats
disagree about what a usage record *means*, and every disagreement fails in the
direction of a wrong ceiling.

This design adds a provider seam to `cost.py`, moves agent-and-model selection
into the policy layer per role, and turns two classes of silent mispricing into
loud failures.

## 1. What was measured

Everything in this section was read off real transcripts on this box, not
inferred from documentation. Claude Code figures come from
`~/.claude/projects/**/*.jsonl`; codex figures from 81 sessions under
`~/.codex/sessions/**/*.jsonl` (1,358 `token_count` records, cli 0.146.0 and
0.147.0).

### 1.1 The two formats disagree about the fold

| | Claude Code | codex |
|---|---|---|
| Path | `~/.claude/projects/*/<uuid>.jsonl` | `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl` |
| Model | `message.model`, beside each usage block | `turn_context.payload.model`, a separate record |
| Usage | `message.usage` | `event_msg.payload.token_count.info.total_token_usage` |
| **Semantics** | **per-message delta** | **cumulative running total** |
| **Correct fold** | **sum every record** | **take the last record** |

This is the load-bearing difference. A 25-turn codex session summed as if it
were Claude Code overcounts by roughly 25x. No field-mapping table can express
a fold, which is why the seam is a protocol rather than configuration.

Verified across all 81 sessions: `total_token_usage` never decreases, including
across `compacted` events. "Take the last" is therefore safe and needs no
reset handling.

### 1.2 The counters do not mean the same thing

Claude Code's `input_tokens` **excludes** cached tokens; codex's `input_tokens`
**includes** them. Verified over 1,358 records: `cached_input_tokens >
input_tokens` never occurs, and `total_tokens == input_tokens + output_tokens`
holds wherever a breakdown is present.

So codex's decomposition is:

```
billable_input = input_tokens - cached_input_tokens
cache_read     = cached_input_tokens
cache_write    = cache_write_input_tokens
output         = output_tokens
```

`reasoning_output_tokens` is a **subset** of `output_tokens` (verified: it never
exceeds it). Adding it double-counts.

Pricing codex's `input_tokens` at the base rate, the way `price_lines` does
today, charges cache-read tokens at up to 10x their real cost.

### 1.3 Some codex sessions carry no breakdown at all

50 of 80 sessions with token records emit `total_tokens` only, with every
component field zero. These are not sessions whose final record is degraded —
they contain **no** breakdown record anywhere.

The discriminator is `session_meta.payload.thread_source`:

| `thread_source` | sessions | breakdown present |
|---|---|---|
| `user` | 13 | yes |
| `subagent` | 17 | yes |
| absent | 50 | **no** |

A component-based adapter prices those sessions at **$0**. That is precisely the
failure `UnpricedModel` exists to prevent — "pricing it at zero would disable
the budget ceiling instead" — arriving through a different door.

**Decision: refuse, do not guess.** A transcript with usage records but no
usable breakdown raises `UnpricedTranscript`, is collected exactly like
`UnpricedModel`, and drains the fleet. The alternatives were both rejected:
pricing `total_tokens` at the output rate cannot under-bill but over-bills a
read-heavy session several-fold and drains the night almost immediately;
pricing it at the input rate under-bills every output token. Inventing a blend
puts a fabricated number inside a spending ceiling.

**This makes codex conditionally routable, and §6 makes that a gate.**

## 2. The provider seam

`cost.py` gains a protocol with two responsibilities, kept on one object because
the same fact — which CLI ran — determines both, and splitting them invites a
mismatched pair:

```python
class Provider(Protocol):
    name: str                                                    # "claude" | "codex"
    def transcript(self, session_id: str) -> Path | None: ...    # filesystem
    def price(self, lines: list[str], prices: Prices) -> float:  # pure
```

`price` stays a pure function of lines, preserving the property the module
docstring calls out as deliberate. Only `transcript` does I/O.

- `price_lines` becomes `ClaudeProvider.price`; `transcript_path` becomes
  `ClaudeProvider.transcript`. Behaviour is unchanged — this is a move, and the
  existing tests should pass untouched.
- `CodexProvider` is new: last-record fold, the §1.2 decomposition, and the
  §1.3 refusal.
- `[prices]` is **unchanged**. It is already keyed by arbitrary model string,
  so `gpt-5.6-sol` is simply another row.

Provider selection uses a key the fleet already has: the herdr snapshot carries
`"agent": "claude"` per agent (`fixtures/herdr/agent-fleet-worktree.json`). No
new plumbing is needed to know which CLI produced a transcript.

## 3. Per-role routing

Split along the line the codebase already draws between the target repo's
policy and the fleet's own mechanics.

### 3.1 Policy decides routing

A second table in `docs/agent/task-classes.md`, read by the existing
header-matching `_rows(text, ("role", "agent", "model"))`. `_rows` compares
`cells[:len(header)]`, so a three-column header needs no parser change, and
`ownership.md` already carries two tables this way.

```markdown
| Role | Agent | Model |
|---|---|---|
| `implementer` | `codex` | `gpt-5.6-sol` |
| `reviewer` | `claude` | `claude-opus-5` |
| `integrator` | `claude` | `claude-opus-5` |
```

Validated on load, in the style of `parse_routes` (which raises on an unknown
class rather than skipping the row):

1. every role is in `known_roles()`
2. every agent has a registered `Provider`
3. **every model has a `[prices]` row**

Check 3 is the one that earns its place. Today an unpriced model is discovered
only when a dispatched agent's transcript is read, mid-run, after spending —
`docs/m3-inputs.md` §1.2 describes exactly that: "dispatch its first tick's
agents and then drain on the second." Declaring the model moves it to a startup
error, before anything is dispatched.

The table is **required**, not defaulted to all-claude, on the same reasoning
the `Policy` docstring gives for `integration_branch` having no default.
Nothing is deployed yet, so there is no compatibility cost.

### 3.2 `fleet.toml` decides mechanics

Launch flags are operational, not policy, and they are CLI-specific —
`--permission-mode acceptEdits` means nothing to codex. Both CLIs accept a model
flag but spell it differently (`claude --model`, `codex -m/--model`), so the
flag name is configuration:

```toml
[agents.claude]
model_flag = "--model"
args = ["--permission-mode", "acceptEdits"]

[agents.codex]
model_flag = "--model"
args = ["--full-auto"]
```

`model_flag` is a separate key rather than a `{model}` placeholder inside `args`
deliberately: the code always appends `[model_flag, model]`, so it cannot be
silently omitted. A placeholder someone forgets fails by running the operator's
default model with no error — the silent-success class this codebase keeps
designing out.

`RealHerdr` takes a `role -> (kind, model, args)` mapping in place of today's
single `agent_kind` default (`src/fleet/herdr.py:376`). Dispatch already holds
the role at `herdr_executor.py:177`, so nothing new needs threading through.

## 4. Declared versus observed model

Declaring the model turns the transcript's model from the only source of truth
into a **check**.

This catches what `UnpricedModel` structurally cannot. If `claude-opus-5` is
silently served as `claude-opus-4-8`, both are priced, no exception fires, and
the night bills at the wrong rate against a model nobody chose. `UnpricedModel`
only fires on a model with *no* row, so substitution among priced models is
invisible to it — and that becomes the likelier failure once the ceiling is
correctly configured. The `UnpricedModel` docstring already names the hazard: "a
model changing under an agent happens without anyone editing fleet.toml."

**Observed model** is defined as *the set of models carrying nonzero tokens*.
This excludes `<synthetic>` for free — its usage block is all zeros — so the
pseudo-model needs no special case. A mismatch is that set not being exactly
`{declared}`.

**Where the comparison happens.** Not in `parse_snapshot`: herdr identifies
agents by name (`fleet-<task>`), so it knows the task but not the role, and the
role lives on the ledger. So:

- **herdr observes.** `AgentState` gains `model: str | None`. This follows the
  field's own precedent — "`task` and `cost_usd` are supplied by the shell: the
  herdr snapshot carries neither."
- **`decide` compares.** It already holds both halves: the ledger (task → role,
  via `_role_for`) and the policy (role → declared model).

herdr stays ignorant of policy, matching its current shape — it takes `prices`,
a flat lookup table, never a `Policy`.

**Consequence: escalate the task, do not drain the fleet.** `REAPING = (Advance,
Release, Rebase, Escalate, Retire, Close)` at `decide.py:44` means `Escalate`
already stops a live agent, and `_reap` already escalates on a per-task cost
condition read off `AgentState` (`over_budget`, `decide.py:371`). The new branch
sits beside it:

```python
Escalate(task=task.number, reason="model_mismatch")
```

Cost is always computed from the **observed** model. Pricing by the declared
model would be fiction — you bill what actually ran.

`AgentState.model` is independently useful: `fleet status` can show what each
agent is really running, which is currently invisible.

## 5. Unknown provider

An agent kind with no registered `Provider` drains the fleet through the
existing `unpriced` collector rather than escalating a task — it is a
configuration fault, not a task fault. §3.1's load-time validation makes it
close to unreachable, but the runtime path costs nothing to keep closed.

## 6. Verification gate before routing a role to codex

§1.3 leaves one question unanswered by the available evidence: the 50
breakdown-less sessions all have an absent `thread_source`, while all 30
sessions with `thread_source` set (`user` or `subagent`) carry breakdowns.
herdr launches codex as a fresh interactive session in a pane, which resembles
the `thread_source=user` case — but that is inference, not measurement.

**Gate:** before any graded run routes a role to codex, launch one codex agent
through herdr and confirm its transcript emits component breakdowns. If it does
not, codex is unusable under a budget ceiling and the design's refusal in §1.3
will drain the run on the first tick — correctly and loudly, but the night is
lost either way.

This is cheap to check and must not be assumed.

## 7. Testing

Fixtures are **captured from real sessions**, not invented, alongside the
existing `fixtures/herdr/` captures — the parent spec's rule is that the pure
core is tested against real shapes. New: `fixtures/cost/codex-*.jsonl`.

Each test names the mutation it catches. A test that catches nothing does not
ship.

| Test | Mutation caught |
|---|---|
| codex, 3 turns, increasing cumulative totals | `CodexProvider.price` folding with `sum` instead of `last` (~3x) |
| codex, `cached_input_tokens > 0` | charging `input_tokens` at base rate without subtracting cached |
| codex, `reasoning_output_tokens > 0` | adding reasoning to output (subset — double-counts) |
| codex, total-only transcript | returning 0.0 instead of raising `UnpricedTranscript` |
| Claude, >= 2 assistant messages | `ClaudeProvider.price` folding with `last` instead of `sum` |
| Claude, `<synthetic>` present | observed-model set including a zero-token pseudo-model |
| policy naming a model with no `[prices]` row | load-time validation passing silently |
| policy naming an agent with no `Provider` | same |
| live agent, observed != declared | the `_reap` branch not firing |
| live agent, observed == declared | the `_reap` branch firing spuriously |

Existing `cost.py` tests must pass unchanged through the `ClaudeProvider` move;
any that need editing indicate the move changed behaviour.

## 8. Out of scope

- **The 5m/1h cache-write split.** `cost.py` carries one `cache_write` rate, but
  the API bills 1.25x base for a 5-minute TTL and 2x for 1-hour, and
  `cache_creation_input_tokens` is the blended total. Measured locally: 74% of
  cache-creation tokens are 1h. The interim mitigation is to configure
  `cache_write` at the 1h rate, which over-counts by ~11% instead of
  under-counting by ~31%. Pricing the two TTLs separately is a real `cost.py`
  change and belongs with this work's successor, not inside it.
- **Providers beyond claude and codex.** herdr supports roughly 21 agent kinds.
  Only these two are installed here, and an adapter written against a format
  nobody has read is exactly the speculative work that produces a wrong ceiling.
  The seam makes adding a third cheap; adding it blind is not.
- **Per-task or per-class agent selection.** Per-role was chosen; the other
  granularities are a routing dimension M4 does not have yet.
