"""Render a state-socket payload as lines. Pure: no socket, no daemon.

Separate from the command so the table is testable without a running fleet --
the same reason `do_plan` is separate from `fleet plan`.
"""

from __future__ import annotations

_HEADER = (
    f"{'task':>5}  {'state':<12} {'area':<8} {'agent':<8} {'att':>3} {'reb':>3} {'cost':>7}  title"
)


def render(payload: dict) -> list[str]:
    if not payload:
        return ["no tick yet: the daemon is running but has not completed one."]
    lines = [
        f"run {payload['run_id']}  tick {payload['tick']}  at {payload['at']}",
        f"spent ${payload['spent_usd']} of ${payload['fleet_usd']}  "
        f"free slots {payload['free_slots']}  held {','.join(payload['held_areas']) or '-'}",
    ]
    if payload.get("draining"):
        lines.append(
            f"DRAINING ({payload.get('drain_reason') or 'unknown'}): "
            "in-flight agents finish, nothing new starts."
        )
    # `.get`, because a daemon older than the field serves a payload without it
    # and a KeyError here exits the client non-zero -- which reads as "no fleet
    # daemon", a different fact entirely.
    failures = list(payload.get("failures") or ())
    if failures:
        # The count here and the detail at the bottom, deliberately split. These
        # are the cleanup errors the executor swallowed rather than raised, and
        # `FAILURE_LIMIT` is 64: rendered only above the table they would push
        # every row off a `watch fleet status`, and rendered only below it they
        # are off the bottom of a fifty-row ledger. A glance has to be able to
        # notice, and a reader has to be able to scroll to the reason.
        lines.append(f"{len(failures)} swallowed failure(s) -- listed below")
    lines += ["", _HEADER]
    for row in payload["rows"]:
        lines.append(
            f"{'#' + str(row['task']):>5}  {row['state']:<12} {row['area'] or '-':<8} "
            f"{row['agent'] or '-':<8} {row['attempt']:>3} {row['rebases']:>3} "
            f"{row['cost_usd']:>7.2f}  {row['title'][:48]}"
        )
    if failures:
        # Every one, not the last few. They are already bounded at the source
        # (Daemon.FAILURE_LIMIT), and a fleet-wide herdr outage that appends one
        # per task per tick is exactly the shape a truncated tail would hide.
        lines += ["", f"failures ({len(failures)}), oldest first:"]
        lines += [f"  - {failure}" for failure in failures]
    return lines
