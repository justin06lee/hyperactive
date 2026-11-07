"""The proactive pulse: run by a Hermes cron job (no_agent=True), prints what is due.

Hermes cron delivers this script's stdout verbatim to the job's `deliver` target; empty stdout
sends nothing. So the pulse is cheap (no LLM), deterministic, and keeps working when the model is
down. Anything that needs reasoning (daily brief, nudges) is a separate agent-mode cron job.

Invoked through the shim the plugin installs at <HERMES_HOME>/scripts/hyperactive_pulse.py.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, time
from pathlib import Path

try:
    from . import store
except ImportError:  # loaded as a top-level module by the cron shim
    import store  # type: ignore[no-redef]


def in_quiet_hours(spec: str, at: datetime) -> bool:
    """spec like '22:00-07:00' (local time, may wrap midnight). Empty = never quiet."""
    if not spec or "-" not in spec:
        return False
    start_s, end_s = (s.strip() for s in spec.split("-", 1))
    start, end = time.fromisoformat(start_s), time.fromisoformat(end_s)
    t = at.astimezone().time()
    return start <= t < end if start < end else (t >= start or t < end)


def run(data_dir: Path, at: datetime | None = None) -> str:
    at = at or store.now()
    settings_file = data_dir / "pulse.json"
    settings = json.loads(settings_file.read_text()) if settings_file.exists() else {}
    conn = store.connect(data_dir / "hyperactive.db")
    try:
        if in_quiet_hours(settings.get("quiet_hours", ""), at):
            return ""  # deferred, not dropped: rows stay pending until the window ends
        due = [f for f in store.pending_followups(conn) if datetime.fromisoformat(f["due_at"]) <= at]
        out = []
        for f in due:
            goal = store.get_goal(conn, f["goal_id"]) if f["goal_id"] else None
            head = f"[hyperactive] {f['kind']}"
            if goal:
                head += f" for goal #{goal['id']} ({goal['horizon']}): {goal['title']}"
            out.append(f"{head}\n{f['text']}")
            conn.execute("UPDATE followups SET status='sent', sent_at=? WHERE id=?", (store.iso(at), f["id"]))
            store.log(conn, f"{f['kind']}.sent", f["goal_id"], f"#{f['id']}")
        conn.commit()
        return "\n\n".join(out)
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    data_dir = Path(argv[1])
    text = run(data_dir)
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
