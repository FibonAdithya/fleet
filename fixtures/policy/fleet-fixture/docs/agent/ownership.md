# Ownership map

One live task per area. This is the fleet's primary collision-avoidance
mechanism, and it is coarse on purpose.

| Glob | Area |
|---|---|
| `src/eval/**` | eval |
| `src/train/**` | train |
| `src/models/**` | models |
| `src/data/**` | data |
| `configs/**` | configs |
| `gates/**` | configs |
| `docs/**` | docs |
| `tests/eval/**` | eval |
| `tests/train/**` | train |
| `tests/models/**` | models |
| `tests/data/**` | data |

Tests live in the area of the code they cover, so an implementer can always add
a binding test without crossing an area boundary.

## Non-dispatchable paths

No task touching these is ever labelled `fleet:auto-ok`, regardless of area.

| Path | Why |
|---|---|
| `gates/**` | setting a band is a human judgement |
| `docs/agent/**` | this file governs the fleet; an agent editing it widens its own authority |
| `.agents/**` | role prompts and schemas, same reason |
| `Makefile` | the gate itself |
| `pytest.ini` | the gate itself |
