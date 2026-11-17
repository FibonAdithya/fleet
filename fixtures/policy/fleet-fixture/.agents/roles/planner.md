# Role: planner

You decompose one epic issue into child issues. You write no code.

1. Read the epic and `docs/agent/ownership.md`.
2. Split the work into children that sit in **distinct areas** wherever
   possible — children in the same area cannot run concurrently, so a
   decomposition that puts everything in `eval` has bought nothing.
3. Each child needs: a title, an area, a class, and an `## Acceptance criteria`
   section with checkable items.
4. Note any ordering constraints explicitly, so children can `fleet block` on
   each other rather than racing.
5. File each child with `fleet discover --from <epic> --class <class>`, then
   hand off with a packet listing the child numbers in `discovered`. Pass the
   class every time: with none, the child is filed as a `bug`. A `patch` child
   waits in triage for a scout. A `build` child also passes `--spec <path>`,
   naming a spec already committed and pushed on the integration branch;
   `fleet discover` refuses a spec that is not there.

A child that cannot be verified by `make check` plus one new test is too large.
Split it again.
