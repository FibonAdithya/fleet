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
    lines += ["", _HEADER]
    for row in payload["rows"]:
        lines.append(
            f"{'#' + str(row['task']):>5}  {row['state']:<12} {row['area'] or '-':<8} "
            f"{row['agent'] or '-':<8} {row['attempt']:>3} {row['rebases']:>3} "
            f"{row['cost_usd']:>7.2f}  {row['title'][:48]}"
        )
    return lines
