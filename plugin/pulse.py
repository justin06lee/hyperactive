"""The proactive pulse: run by a Hermes cron job (no_agent=True), prints what is due.

Hermes cron delivers this script's stdout verbatim to the job's `deliver` target; empty stdout
sends nothing. So the pulse is cheap (no LLM), deterministic, and keeps working when the model is
down. Anything that needs reasoning (the daily brief) belongs in a separate agent-mode cron job.

Every item it sends ends with the goal it serves, and is logged as a `pulse.*` event whose `ref`
names the row that caused it, which is what `/ha why` reads back.

Invoked through the shim the plugin installs at <HERMES_HOME>/scripts/hyperactive_pulse.py.
`brief()` is the other entry point: the data for the daily brief, printed by a second shim as the
pre-run script of an agent-mode cron job. It changes nothing; empty output skips the model call.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

try:
    from . import store
except ImportError:  # loaded as a top-level module by the cron shim
    import store  # type: ignore[no-redef]

DEFAULTS = {"quiet_hours": "", "card_timeout": "1h", "stale_after": "3d", "max_items": 5, "timezone": ""}
STAKES_REASON = {"money": "It involves money", "public": "It's public",
                 "irreversible": "It can't be undone"}


def load_settings(data_dir: Path) -> dict:
    f = data_dir / "pulse.json"
    try:
        saved = json.loads(f.read_text()) if f.exists() else {}
    except ValueError:
        saved = {}
    cfg = {**DEFAULTS, **{k: v for k, v in saved.items() if v not in (None, "")}}
    store.set_timezone(cfg["timezone"])  # Hermes's timezone, written by the plugin
    return cfg


def in_quiet_hours(spec: str, at: datetime) -> bool:
    """spec like '22:00-07:00' (local time, may wrap midnight). Empty = never quiet."""
    if not spec or "-" not in spec:
        return False
    start_s, end_s = (s.strip() for s in spec.split("-", 1))
    start, end = time.fromisoformat(start_s), time.fromisoformat(end_s)
    t = store.local(at).time()
    return start <= t < end if start < end else (t >= start or t < end)


def span(td: timedelta) -> str:
    """timedelta -> '1h', '4 days', '3 min'."""
    s = int(td.total_seconds())
    if s >= 2 * 86400:
        return f"{s // 86400} days"
    if s >= 3600:
        return f"{s // 3600}h" if s % 3600 == 0 else f"{s / 3600:.1f}h"
    return f"{max(1, s // 60)} min"


def either(options: list[str]) -> str:
    """['yes', 'no'] -> 'yes or no'; ['a', 'b', 'c'] -> 'a, b or c'."""
    return options[0] if len(options) == 1 else f"{', '.join(options[:-1])} or {options[-1]}"


def _card_text(card: dict, step: str, timeout: timedelta, result: str = "") -> str:
    q, rec, opts = card["question"], card["recommended"], " / ".join(card["options"])
    low = card["stakes"] == "low"
    reason = STAKES_REASON.get(card["stakes"], "")
    if step == "ask":
        tail = (f"I'd go with {rec}. If I don't hear back, I'll do that in {span(2 * timeout)}." if low
                else f"I'd go with {rec}, but it's yours to decide. {reason}.")
        return f"{q}  {opts}\n{tail}\nReply {either(card['options'])}."
    if step == "reask":
        tail = (f"Still no answer. I'll go with {rec} in {span(timeout)}." if low
                else f"{reason}, so it waits for you.")
        return f"Still open: {q}  {opts}\n{tail}"
    if step == "apply":
        return f"No reply on \"{q}\", so I went with {rec}" + (f": {result}." if result else ".")
    return (f"\"{q}\" is still open. {reason}, so I won't decide it for you.\n"
            f"Reply {either(card['options'])} when you're ready.")


def run(data_dir: Path, at: datetime | None = None) -> str:
    at = at or store.now()
    cfg = load_settings(data_dir)
    if in_quiet_hours(cfg["quiet_hours"], at):
        return ""  # deferred, not dropped: nothing changes until the window ends
    timeout = store.parse_duration(cfg["card_timeout"])
    stale_after = store.parse_duration(cfg["stale_after"])
    cap = max(1, int(cfg["max_items"]))
    stamp = store.iso(at)
    conn = store.connect(data_dir / "hyperactive.db")
    out: list[str] = []

    def emit(kind: str, goal_id, ref: str, text: str, extra: str = "") -> None:
        out.append(f"{text}\n{store.goal_line(conn, goal_id, extra)}")
        store.log(conn, f"pulse.{kind}", goal_id, text, ref, at=at)

    try:
        # 1. Tap cards: decisions waiting on the person come first.
        for card in store.open_cards(conn):
            if len(out) >= cap:
                break
            step = store.card_action(card, at, timeout)
            if step == "wait":
                continue
            result = ""
            if step == "ask":
                conn.execute("UPDATE cards SET asked_at=? WHERE id=?", (stamp, card["id"]))
            elif step == "reask":
                conn.execute("UPDATE cards SET reasked_at=? WHERE id=?", (stamp, card["id"]))
            elif step == "apply":
                result = store.answer_card(conn, card["id"], card["recommended"], by="timeout", at=at)["result"]
            elif step == "hold":
                conn.execute("UPDATE cards SET held_at=? WHERE id=?", (stamp, card["id"]))
            emit(f"card.{step}", card["goal_id"], f"card:{card['id']}",
                 _card_text(card, step, timeout, result), f"card {card['id']}")

        # 2. Commitments that came due, both directions.
        for c in store.commitments(conn):
            if len(out) >= cap:
                break
            if not c["due_at"] or c["notified_at"] or store.parse_iso(c["due_at"]) > at:
                continue
            due = store.local_time(c["due_at"], at=at)
            text = (f"You owe {c['person']}: {c['what']} (due {due}). Done already? Just tell me."
                    if c["direction"] == "i_owe" else
                    f"{c['person']} owes you: {c['what']} (due {due}). Want a short nudge drafted?")
            conn.execute("UPDATE commitments SET notified_at=? WHERE id=?", (stamp, c["id"]))
            emit("commit", c["goal_id"], f"commit:{c['id']}", text)

        # 3. Decisions whose review date arrived.
        for d in conn.execute("SELECT * FROM decisions WHERE status='open' AND review_at IS NOT NULL "
                              "AND review_at<=? AND notified_at IS NULL ORDER BY review_at", (stamp,)).fetchall():
            if len(out) >= cap:
                break
            why = f" because {d['rationale']}" if d["rationale"] else ""
            made = store.local_time(d["created_at"], at=at)
            text = (f"Review time. On {made} you decided \"{d['title']}\": {d['choice'] or '(no choice noted)'}{why}.\n"
                    f"Still right? /ha decision {d['id']} keep, or tell me what changed.")
            conn.execute("UPDATE decisions SET notified_at=? WHERE id=?", (stamp, d["id"]))
            emit("decision", d["goal_id"], f"decision:{d['id']}", text)

        # 4. Follow-ups, reminders and nudges the agent scheduled.
        for f in store.pending_followups(conn):
            if len(out) >= cap:
                break
            if store.parse_iso(f["due_at"]) > at:
                break  # ordered by due_at
            conn.execute("UPDATE followups SET status='sent', sent_at=? WHERE id=?", (stamp, f["id"]))
            emit(f["kind"], f["goal_id"], f"followup:{f['id']}", f["text"])

        # 5. At most one stale goal per pulse: a nudge, not a nag.
        for g in store.stale_goals(conn, at, stale_after)[:1]:
            if len(out) >= cap:
                break
            still = span(at - store.parse_iso(g["last_moved_at"]))
            conn.execute("UPDATE goals SET nudged_at=? WHERE id=?", (stamp, g["id"]))
            emit("stale", g["id"], f"goal:{g['id']}",
                 f"\"{g['title']}\" hasn't moved in {still}. Still on? Tell me the next step, or that it's done.")

        conn.commit()
        return "\n\n".join(out)
    finally:
        conn.close()


# --- /ha why --------------------------------------------------------------------------------

def _cause(conn, ref: str, kind: str) -> str:
    table, _, rid = (ref or "").partition(":")
    rid = int(rid) if rid.isdigit() else 0
    if table == "followup":
        f = store.get_followup(conn, rid)
        return (f"follow-up #{rid}, scheduled {store.local_time(f['created_at'])} for "
                f"{store.local_time(f['due_at'])}, came due" if f else f"follow-up #{rid}")
    if table == "card":
        c = store.get_card(conn, rid)
        if not c:
            return f"card #{rid}"
        step = kind.rsplit(".", 1)[-1]
        detail = {"ask": "was due to be asked", "reask": "got no answer, so I asked once more",
                  "apply": "got no answer twice and is low stakes, so I applied the recommendation",
                  "hold": f"got no answer twice, but it's a {c['stakes']} call, so it stays with you"}
        return f"card {rid} {detail.get(step, step)}"
    if table == "commit":
        c = conn.execute("SELECT c.*, p.name AS person FROM commitments c LEFT JOIN people p ON p.id=c.person_id "
                         "WHERE c.id=?", (rid,)).fetchone()
        return (f"commitment #{rid} ({store.commitment_text(dict(c))}) was due "
                f"{store.local_time(c['due_at'])}" if c else f"commitment #{rid}")
    if table == "decision":
        d = conn.execute("SELECT * FROM decisions WHERE id=?", (rid,)).fetchone()
        return (f"decision #{rid} \"{d['title']}\" reached its review date" if d else f"decision #{rid}")
    if table == "goal":
        g = store.get_goal(conn, rid)
        return (f"goal #{rid} had not moved since {store.local_time(g['last_moved_at'])}" if g else f"goal #{rid}")
    return kind


def explain(conn, event_id: int | None = None) -> str:
    """Why hyperactive sent something: the cause and the goal chain it serves.
    No id: the whole most recent pulse message."""
    if event_id is None:
        last = conn.execute("SELECT at FROM events WHERE kind LIKE 'pulse.%' ORDER BY id DESC LIMIT 1").fetchone()
        if last is None:
            return "hyperactive hasn't messaged you yet."
        rows = conn.execute("SELECT * FROM events WHERE kind LIKE 'pulse.%' AND at=? ORDER BY id",
                            (last["at"],)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchall()
        if not rows:
            return f"no event #{event_id}"
    blocks = []
    for e in rows:
        chain = " < ".join(f"{g['title']} ({g['horizon']})" for g in store.goal_chain(conn, e["goal_id"]))
        sent = (e["detail"] or "").splitlines()[0]
        blocks.append(f"{store.local_time(e['at'])} (event {e['id']})\n"
                      f"  sent: {sent[:90] + '...' if len(sent) > 90 else sent}\n"
                      f"  why:  {_cause(conn, e['ref'], e['kind'])}\n"
                      f"  goal: {chain or 'none (inbox)'}")
    return "\n\n".join(blocks)


# --- the daily brief's data ------------------------------------------------------------------

def brief(data_dir: Path, at: datetime | None = None) -> str:
    """Facts for the daily brief, read-only. The agent-mode brief job gets this as its script
    output and writes the prose (skill hyperactive:brief). Empty when nothing is active, which makes
    Hermes skip the model call entirely."""
    at = at or store.now()
    cfg = load_settings(data_dir)
    stale_after = store.parse_duration(cfg["stale_after"])
    local = store.local(at)
    end_of_day = local.replace(hour=23, minute=59, second=59, microsecond=0)
    conn = store.connect(data_dir / "hyperactive.db")
    try:
        def goal(gid) -> str:
            chain = store.goal_chain(conn, gid)
            return f"goal #{gid} {chain[0]['title']}" if chain else "inbox"

        sections: list[tuple[str, list[str]]] = []
        att = store.attention(conn, at)
        sections.append(("Waiting on the user (tap cards)", [
            f"- card {c['id']}: {c['question']} ({' / '.join(c['options'])}; recommended {c['recommended']}, "
            f"{c['stakes']} stakes) [{goal(c['goal_id'])}]" for c in att["cards"]]))
        sections.append(("Scheduled for today", [
            f"- {store.local_time(f['due_at'], at=at)} {f['kind']}: {f['text']} [{goal(f['goal_id'])}]"
            for f in store.pending_followups(conn) if store.parse_iso(f["due_at"]) <= end_of_day]))
        sections.append(("Promises due within a day or overdue", [
            f"- {store.commitment_text(c)}: "
            f"{'overdue since' if store.parse_iso(c['due_at']) <= at else 'due'} "
            f"{store.local_time(c['due_at'], at=at)} [{goal(c['goal_id'])}]" for c in att["commitments"]]))
        sections.append(("Decisions up for review", [
            f"- \"{d['title']}\": {d['choice']} [{goal(d['goal_id'])}]" for d in att["decisions"]]))
        sections.append(("Stalled goals", [
            f"- #{g['id']} {g['title']} ({g['horizon']}): no movement since "
            f"{store.local_time(g['last_moved_at'], at=at)}"
            for g in store.stale_goals(conn, at, stale_after, include_nudged=True)[:3]]))
        week = conn.execute("SELECT * FROM goals WHERE status='active' AND horizon='week' "
                            "ORDER BY last_moved_at DESC LIMIT 6").fetchall()
        sections.append(("Active week goals", [
            f"- #{g['id']} {g['title']}" + (f" (under {store.goal_chain(conn, g['id'])[1]['title']})"
                                             if len(store.goal_chain(conn, g["id"])) > 1 else "")
            for g in week]))
        if not any(lines for _, lines in sections):
            return ""
        body = [f"{title}:\n" + "\n".join(lines) for title, lines in sections if lines]
        return f"hyperactive data for {local.strftime('%A %b %-d, %H:%M')}\n\n" + "\n\n".join(body)
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    data_dir = Path(argv[1])
    text = brief(data_dir) if argv[0] == "brief" else run(data_dir)
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
