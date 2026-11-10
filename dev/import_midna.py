#!/usr/bin/env python3
"""Import a midna.db into hyperactive's store. Stdlib only, idempotent, dry run by default.

    python3 -I dev/import_midna.py snapshot <midna.db> <snap.db> [--group hermes]  # read-only copy
    python3 -I dev/import_midna.py plan  <snap.db> --target <hyperactive.db> --out <dir>
    python3 -I dev/import_midna.py apply <snap.db> --target <hyperactive.db> --out <dir> [--live]
    python3 -I dev/import_midna.py undo  --target <hyperactive.db> [--apply] [--live]

`plan` (the dry run) applies everything to a throwaway copy of the target, so its counts, integrity
check and reconciliation are exactly what `apply` would do. It writes plan.json, report.txt
and cron-plan.sh (the agent-mode cron jobs for midna's scheduled wakeups) to --out and never
touches the target. `apply` does the same in one transaction on the target itself.

Every imported row carries ext_ref = 'midna:<table>:<id>' (unique per table), so a re-run updates
rows instead of duplicating them, and `undo` deletes exactly what was imported. Rows Hermes created
itself (no ext_ref) are never touched.

The source is only ever opened read-only (mode=ro). Times become UTC ISO, as hyperactive stores them
(rendered in Hermes's timezone, so set it to midna's, Asia/Seoul, before going live).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))
import store  # noqa: E402

SRC = "midna"
LIVE_MARKERS = ("/home/hermes/",)
BACKUP_MAX_AGE = timedelta(minutes=10)

GOAL_STATUS = {"active": "active", "paused": "paused", "done": "done", "abandoned": "dropped"}
TASK_STATUS = {"todo": "open", "doing": "doing", "waiting": "waiting", "done": "done", "dropped": "dropped"}
# midna's built-in rituals, as midna words them (src: midna's wakeup prompts, gW). The morning
# brief folds into hyperactive-brief; memory maintenance is the pulse's nightly fade.
RITUAL_PROMPTS = {
    "evening review": (
        "Evening shutdown. Look at what happened today (session_search, ha_status, tasks, commitments). "
        "Close what got done, carry over what didn't, and record anything worth remembering. If there's "
        "something the user should see tonight or a question worth reflecting on, say it briefly. If there's "
        "nothing worth saying, reply with exactly [SILENT]."),
    "weekly review": (
        "Weekly review. For each active goal: what moved, what didn't, and whether the week's time went where "
        "the user says their priorities are. Find stale tasks, slipping commitments, decisions to review, "
        "people to reconnect with, goals due for review. Propose concrete adjustments for next week; goals "
        "themselves change only by renegotiating with the user. Put questions that need an answer as ha_card."),
}
FOLDED = {"morning brief": "folded into hyperactive-brief (brief_at 08:00)",
          "memory maintenance": "replaced by the pulse's nightly memory fade (no model call)"}
WAKEUP_PREAMBLE = ("[Wakeup: the user did not send this; you are running on your own (migrated from midna "
                   "wakeup #{id}).] ")
WAKEUP_TAIL = (" Your final reply is delivered to the user; reply with exactly [SILENT] if there is nothing "
               "worth sending. Act with your tools and update your records. (Written for midna's tools: "
               "message_user/ask_user = your final reply or an ha_card; schedule_wakeup = ha_followup (kind "
               "reminder) for anything that must arrive on time, since it needs no model and is delivered late "
               "rather than lost if the gateway is down, or a one-shot cron job only when the moment needs "
               "thinking; history_search = session_search for new chats, midna-body history_search for older ones; "
               "recall/remember = ha_recall/ha_remember; notes and tasks are in hyperactive.)")
# midna ids inside wakeup text ("note #5") -> the imported hyperactive row's id
ID_MENTION = re.compile(r"\b(note|task|goal|decision|commitment|fact)(\s*#)(\d+)", re.I)
MENTION_TABLE = {"note": ("notes", "notes"), "task": ("tasks", "tasks"), "goal": ("goals", "goals"),
                 "decision": ("decisions", "decisions"), "commitment": ("commitments", "commitments"),
                 "fact": ("memories", "facts")}
NOT_IMPORTED = {
    "messages": "chat history stays in the midna archive (searched via midna-body history_search)",
    "episodes": "run log; Hermes usage telemetry covers this going forward",
    "actions": "tool-call log, archive only",
    "attachments": "475 MB of blobs stay in the archive",
    "device_commands": "device audit log stays with midna serve",
    "outbox": "delivery log", "push_sends": "delivery log", "inbound_events": "event log",
    "jobs": "finished background jobs; nothing to resume",
    "conversations": "app conversations stay with midna serve",
    "approvals": "device approvals stay with midna serve", "devices": "body pairing stays with midna serve",
    "hands_devices": "body pairing", "push_tokens": "APNs, stays with midna serve",
    "pairing_codes": "body pairing", "settings": "see report: timezone/mode map to Hermes config",
}


def ms(v) -> str | None:
    return None if v is None else store.iso(datetime.fromtimestamp(v / 1000, tz=timezone.utc))


def ref(table: str, rid) -> str:
    return f"{SRC}:{table}:{rid}"


# --- read midna --------------------------------------------------------------------------------

def open_source(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def snapshot(src: str, dest: str, group: str = "") -> None:
    """WAL-consistent copy through the backup API; the source is opened read-only. Mode 600, or 640
    for `group` (the hermes user applies the import, so it needs to read the copy)."""
    if Path(dest).exists():
        raise SystemExit(f"refusing: {dest} exists")
    s = open_source(src)
    d = sqlite3.connect(dest)
    os.chmod(dest, 0o600)
    s.backup(d)
    d.execute("PRAGMA journal_mode=DELETE")  # a WAL copy needs -shm/-wal files the reader can't create
    d.close()
    s.close()
    if group:
        try:
            shutil.chown(dest, group=group)
            os.chmod(dest, 0o640)
        except PermissionError:
            # not a member of that group (no root to add us): grant its same-named user read via an ACL
            subprocess.run(["setfacl", "-m", f"u:{group}:r", dest], check=True)


class Plan:
    """What the import wants: hyperactive rows keyed by ext_ref, in dependency order."""

    def __init__(self):
        self.items: list[dict] = []
        self.skips: Counter = Counter()
        self.skip_rows: list[dict] = []
        self.warnings: list[str] = []
        self.cron: list[dict] = []
        self.closed: dict[str, str] = {}  # ext_ref -> status, for imported rows midna has since closed

    def add(self, table, ext_ref, values, refs=None, source=None):
        self.items.append({"table": table, "ext_ref": ext_ref, "values": values, "refs": refs or {},
                           "source": source or ext_ref})

    def skip(self, source_table, rid, reason, preview=""):
        self.skips[(source_table, reason)] += 1
        self.skip_rows.append({"source": ref(source_table, rid), "reason": reason, "preview": preview[:100]})


def build(src: sqlite3.Connection, tz: str) -> Plan:
    p = Plan()
    zone = ZoneInfo(tz)
    q = lambda sql: src.execute(sql).fetchall()  # noqa: E731

    # goals, parents first
    goals = {g["id"]: g for g in q("SELECT * FROM goals")}
    done: set[int] = set()

    def add_goal(g):
        if g["id"] in done:
            return
        if g["parent_id"] in goals:
            add_goal(goals[g["parent_id"]])
        elif g["parent_id"] is not None:
            p.warnings.append(f"goal {g['id']}: parent {g['parent_id']} missing, imported as a root")
        done.add(g["id"])
        horizon = store.SPANS.get(g["horizon"], g["horizon"])
        if g["horizon"] in store.SPANS:
            p.warnings.append(f"goal {g['id']}: horizon '{g['horizon']}' stored as {horizon} + span "
                              f"'{g['horizon']}' (shown as [{g['horizon']}])")
        if g["status"] == "abandoned":
            p.warnings.append(f"goal {g['id']}: status abandoned -> dropped")
        p.add("goals", ref("goals", g["id"]), {
            "horizon": horizon, "span": g["horizon"] if g["horizon"] in store.SPANS else None,
            "title": g["title"], "why": g["why"] or "", "status": GOAL_STATUS.get(g["status"], g["status"]),
            "success": g["success"] or "", "target_at": ms(g["target_at"]),
            "review_every_days": g["review_every_days"], "last_reviewed_at": ms(g["last_reviewed_at"]),
            "created_at": ms(g["created_at"]), "last_moved_at": ms(g["updated_at"])},
            {"parent_id": ("goals", ref("goals", g["parent_id"])) if g["parent_id"] in goals else None})

    for g in goals.values():
        add_goal(g)

    for c in q("SELECT * FROM goal_changes ORDER BY id"):
        p.add("events", ref("goal_changes", c["id"]), {
            "at": ms(c["at"]), "kind": "goal.change", "detail": f"{c['change']} (why: {c['reason']})"},
            {"goal_id": ("goals", ref("goals", c["goal_id"])), "ref": ("goals", ref("goals", c["goal_id"]), "goal:{}")})

    for t in q("SELECT * FROM tasks ORDER BY id"):
        p.add("tasks", ref("tasks", t["id"]), {
            "title": t["title"], "status": TASK_STATUS[t["status"]], "notes": t["notes"] or "",
            "owner": "agent" if t["owner"] == "midna" else "user", "priority": t["priority"],
            "estimate_min": t["estimate_min"], "due_at": ms(t["due_at"]), "created_at": ms(t["created_at"]),
            "updated_at": ms(t["updated_at"]), "done_at": ms(t["done_at"])},
            {"goal_id": ("goals", ref("goals", t["goal_id"])) if t["goal_id"] else None})

    for c in q("SELECT c.*, p.name AS pname FROM commitments c LEFT JOIN people p ON p.id=c.person_id ORDER BY c.id"):
        who = (c["pname"] or c["counterparty"] or "").strip()
        if c["direction"] == "mine" and who.lower() in ("user", "the user", ""):
            # midna's own promise to the user: an agent task in hyperactive, not a commitment to a person.
            p.warnings.append(f"commitment {c['id']}: midna's promise to the user -> task owned by the agent")
            p.add("tasks", ref("commitments", c["id"]), {
                "title": c["what"], "status": {"open": "open"}.get(c["status"], c["status"]),
                "notes": f"(was midna commitment #{c['id']})", "owner": "agent", "priority": None,
                "estimate_min": None, "due_at": ms(c["due_at"]), "created_at": ms(c["created_at"]),
                "updated_at": ms(c["updated_at"]), "done_at": ms(c["closed_at"])},
                {"goal_id": ("goals", ref("goals", c["goal_id"])) if c["goal_id"] else None})
            continue
        p.add("commitments", ref("commitments", c["id"]), {
            "direction": "i_owe" if c["direction"] == "mine" else "they_owe", "what": c["what"],
            "due_at": ms(c["due_at"]), "status": c["status"], "created_at": ms(c["created_at"]),
            "closed_at": ms(c["closed_at"])},
            {"goal_id": ("goals", ref("goals", c["goal_id"])) if c["goal_id"] else "inbox",
             "person_id": ("person", who)})

    for d in q("SELECT * FROM decisions ORDER BY id"):
        status = "reviewed" if (d["outcome"] or d["reviewed_at"]) else "open"
        p.add("decisions", ref("decisions", d["id"]), {
            "title": d["title"], "choice": d["choice"] or "", "rationale": d["reasoning"] or "",
            "context": d["context"] or "", "options": d["options"] or "", "expected": d["expected"] or "",
            "confidence": d["confidence"], "outcome": d["outcome"] or "", "lesson": d["lesson"] or "",
            "review_at": ms(d["review_at"]), "reviewed_at": ms(d["reviewed_at"]), "status": status,
            "created_at": ms(d["decided_at"] or d["created_at"])},
            {"goal_id": ("goals", ref("goals", d["goal_id"])) if d["goal_id"] else "inbox"})

    for table in ("commitments", "decisions"):
        n = sum(1 for i in p.items if i["table"] == table and i["refs"].get("goal_id") == "inbox")
        if n:
            p.warnings.append(f"{n} {table} without a goal -> filed under the Inbox goal")
    n = src.execute("SELECT COUNT(*) FROM wakeups WHERE kind='reminder' AND cron IS NULL "
                    "AND status IN ('pending','running')").fetchone()[0]
    if n:
        p.warnings.append(f"{n} pending reminders -> followups under the Inbox goal")

    for f in q("SELECT * FROM facts ORDER BY id"):
        p.add("memories", ref("facts", f["id"]), {
            "text": f["content"], "tags": "", "kind": f["kind"], "importance": f["importance"],
            "pinned": f["pinned"], "source": f["source"] or "", "created_at": ms(f["created_at"]),
            "updated_at": ms(f["created_at"]), "last_used_at": ms(f["last_used_at"]), "use_count": f["use_count"],
            "archived_at": ms(f["archived_at"]), "archive_reason": f["archive_reason"]},
            {"superseded_by": ("memories", ref("facts", f["superseded_by"])) if f["superseded_by"] else None})

    entity_table = {"goal": "goals", "task": "tasks", "commitment": "commitments", "decision": "decisions"}
    for l in q("SELECT * FROM fact_links"):
        if l["entity"] not in entity_table:
            p.skip("fact_links", f"{l['fact_id']}/{l['entity']}/{l['entity_id']}", f"links to a {l['entity']}")
            continue
        p.add("memory_links", ref("fact_links", f"{l['fact_id']}:{l['entity']}:{l['entity_id']}"),
              {"entity": l["entity"]},
              {"memory_id": ("memories", ref("facts", l["fact_id"])),
               "entity_id": (entity_table[l["entity"]], ref(entity_table[l["entity"]], l["entity_id"]))})

    for n in q("SELECT * FROM notes ORDER BY id"):
        p.add("notes", ref("notes", n["id"]), {"title": n["title"], "body": n["body"], "tags": n["tags"] or "",
                                              "created_at": ms(n["created_at"]), "updated_at": ms(n["updated_at"])})

    for e in q("SELECT * FROM events ORDER BY id"):
        if e["source"] != "midna" or e["external_id"]:
            p.warnings.append(f"event {e['id']}: from {e['source']} ({e['external_id']}); imported as a local entry")
        p.add("calendar", ref("events", e["id"]), {
            "title": e["title"], "starts_at": ms(e["starts_at"]), "ends_at": ms(e["ends_at"]),
            "all_day": e["all_day"], "location": e["location"] or "", "notes": e["notes"] or "",
            "status": e["status"], "created_at": ms(e["created_at"]), "updated_at": ms(e["updated_at"])},
            {"goal_id": ("goals", ref("goals", e["goal_id"])) if e["goal_id"] else None})

    for w in q("SELECT * FROM wakeups ORDER BY id"):
        due_local = datetime.fromtimestamp(w["due_at"] / 1000, tz=zone)
        if w["status"] not in ("pending", "running"):
            p.skip("wakeups", w["id"], f"{w['status']} (history)")
            if w["kind"] == "reminder" and not w["cron"]:  # sent or cancelled since an earlier import
                p.closed[ref("wakeups", w["id"])] = "sent" if w["status"] == "done" else "cancelled"
            continue
        if w["kind"] == "reminder" and not w["cron"]:
            p.add("followups", ref("wakeups", w["id"]), {
                "kind": "reminder", "text": w["reason"], "due_at": ms(w["due_at"]), "status": "pending",
                "created_at": ms(w["created_at"])}, {"goal_id": "inbox"})
            continue
        if w["reason"] in FOLDED:
            p.skip("wakeups", w["id"], FOLDED[w["reason"]], w["reason"])
            continue
        if not w["cron"] and due_local < datetime.now(zone):
            p.warnings.append(f"wakeup {w['id']}: one-shot was due {due_local:%b %-d %H:%M} (past); not planned, "
                              "midna runs it")
            p.skip("wakeups", w["id"], "one-shot already past due", w["reason"])
            continue
        when = (f"for {due_local:%a %b %-d %H:%M}" if not w["cron"] else f"recurring, cron '{w['cron']}'")
        prompt = RITUAL_PROMPTS.get(w["reason"]) or (
            WAKEUP_PREAMBLE.format(id=w["id"]) + "You scheduled this for yourself "
            f"(on {datetime.fromtimestamp(w['created_at'] / 1000, tz=zone):%b %-d}, {when}): "
            f"\"{w['reason']}\" Do what it calls for." + WAKEUP_TAIL)
        p.cron.append({"name": f"midna:{w['id']}", "wakeup_id": w["id"], "kind": w["kind"],
                       "schedule": w["cron"] or due_local.isoformat(timespec="minutes"),
                       "one_shot": not w["cron"], "due_local": due_local.strftime("%Y-%m-%d %H:%M %Z"),
                       "prompt": prompt, "reason": w["reason"]})
        p.skip("wakeups", w["id"], "agent cron job (cron-plan.sh)" + (" one-shot" if not w["cron"] else " recurring"),
               w["reason"])

    for table, why in NOT_IMPORTED.items():
        try:
            n = src.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            continue
        if n:
            p.skips[(table, why)] += n
    people = src.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    if people:
        p.warnings.append(f"{people} people rows: only commitment counterparties are imported")
    settings = {r["key"]: r["value"] for r in q("SELECT key, value FROM settings")}
    if json.loads(settings.get("timezone", "null") or "null") != tz:
        p.warnings.append(f"midna timezone is {settings.get('timezone')}, importing as {tz}")
    return p


# --- write hyperactive -------------------------------------------------------------------------

IMPORT_TABLES = list(store.COLUMNS_V3) + ["notes", "calendar"]  # the tables with ext_ref
COMPARE_SKIP = {"last_used_at", "use_count"}  # recall touches these on both sides; not a content change


def sync(conn: sqlite3.Connection, plan: Plan) -> dict:
    """Upsert every planned row by ext_ref inside the caller's transaction. Returns counts + diffs."""
    counts: dict[str, Counter] = defaultdict(Counter)
    diffs: list[str] = []
    ids: dict[tuple[str, str], int] = {}
    deferred: list[tuple[str, int, str, tuple]] = []

    def lookup(table, ext):
        if (table, ext) in ids:
            return ids[(table, ext)]
        if table == "memory_links":
            return None
        r = conn.execute(f"SELECT id FROM {table} WHERE ext_ref=?", (ext,)).fetchone()
        return r[0] if r else None

    def resolve(col, spec, table, ext):
        if spec is None:
            return None
        if spec == "inbox":
            return store.inbox_goal(conn)
        if spec[0] == "person":
            return store.person_id(conn, spec[1] or "someone")
        rid = lookup(spec[0], spec[1])
        if rid is None and col == "superseded_by":
            deferred.append((table, ext, col, spec))
            return None
        if rid is None:
            counts[table]["orphan_ref"] += 1
            return None
        return spec[2].format(rid) if len(spec) > 2 else rid

    for item in plan.items:
        table, ext = item["table"], item["ext_ref"]
        values = dict(item["values"])
        for col, spec in item["refs"].items():
            values[col] = resolve(col, spec, table, ext)
        if table == "memory_links":
            if values["memory_id"] is None or values["entity_id"] is None:
                counts[table]["skip_orphan"] += 1
                continue
            cur = conn.execute("INSERT OR IGNORE INTO memory_links (memory_id, entity, entity_id) VALUES (?,?,?)",
                               (values["memory_id"], values["entity"], values["entity_id"]))
            counts[table]["insert" if cur.rowcount else "unchanged"] += 1
            continue
        existing = conn.execute(f"SELECT * FROM {table} WHERE ext_ref=?", (ext,)).fetchone()
        if existing is None:
            cols = list(values) + ["ext_ref"]
            cur = conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                               (*values.values(), ext))
            ids[(table, ext)] = cur.lastrowid
            counts[table]["insert"] += 1
            continue
        ids[(table, ext)] = existing["id"]
        changed = {k: v for k, v in values.items()
                   if k not in COMPARE_SKIP and existing[k] != v and not (k == "superseded_by" and v is None)}
        if not changed:
            counts[table]["unchanged"] += 1
            continue
        conn.execute(f"UPDATE {table} SET {', '.join(f'{k}=?' for k in values)} WHERE id=?",
                     (*values.values(), existing["id"]))
        counts[table]["update"] += 1
        for k, v in changed.items():
            diffs.append(f"{ext} {table}.{k}: {str(existing[k])[:80]!r} -> {str(v)[:80]!r}")

    for table, ext, col, spec in deferred:
        rid = lookup(spec[0], spec[1])
        if rid is None:
            counts[table]["orphan_ref"] += 1
            continue
        conn.execute(f"UPDATE {table} SET {col}=? WHERE ext_ref=?", (rid, ext))

    # Reminders midna delivered (or cancelled) since the last run: close them here too, or the pulse
    # would send them again once mirroring ends.
    for ext, status in plan.closed.items():
        cur = conn.execute("UPDATE followups SET status=?, sent_at=COALESCE(sent_at, ?) WHERE ext_ref=? "
                           "AND status='pending'", (status, store.iso(store.now()), ext))
        if cur.rowcount:
            counts["followups"]["closed"] += 1
            diffs.append(f"{ext} followups.status: 'pending' -> {status!r} (closed in midna)")
    return {"counts": {t: dict(c) for t, c in counts.items()}, "diffs": diffs}


def remap_ids(conn: sqlite3.Connection, plan: Plan) -> None:
    """'note #5' in a wakeup's text means midna's note 5; point it at the imported row's id."""
    def sub(m):
        table, src_table = MENTION_TABLE[m.group(1).lower()]
        row = conn.execute(f"SELECT id FROM {table} WHERE ext_ref=?", (ref(src_table, m.group(3)),)).fetchone()
        if row is None:
            plan.warnings.append(f"cron prompt mentions {m.group(0)}, which wasn't imported; left as is")
            return m.group(0)
        word = "memory" if src_table == "facts" else m.group(1)
        return f"{word}{m.group(2)}{row[0]}"
    for c in plan.cron:
        c["prompt"] = ID_MENTION.sub(sub, c["prompt"])


def check(conn: sqlite3.Connection, plan: Plan) -> list[str]:
    """Integrity, foreign keys and planned-vs-present counts. Empty list = all good."""
    problems = []
    ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if ok != "ok":
        problems.append(f"integrity_check: {ok}")
    for row in conn.execute("PRAGMA foreign_key_check"):
        problems.append(f"foreign key: {tuple(row)}")
    want: dict[str, list[str]] = defaultdict(list)
    for i in plan.items:
        if i["table"] != "memory_links":
            want[i["table"]].append(i["ext_ref"])
    for table in IMPORT_TABLES:
        refs = want.get(table, [])
        have = {r[0] for r in conn.execute(f"SELECT ext_ref FROM {table} WHERE ext_ref LIKE ?", (f"{SRC}:%",))}
        missing = [r for r in refs if r not in have]
        if missing:
            problems.append(f"{table}: {len(missing)} planned rows missing, e.g. {missing[0]}")
        gone = len(have - set(refs))
        if gone:  # imported by an earlier run, no longer in midna (deleted, or a reminder now closed): kept
            plan.warnings.append(f"{table}: {gone} rows imported earlier are no longer in midna's live set; kept")
    return problems


def guard_target(target: Path, live: bool) -> None:
    path = str(target.resolve())
    if any(m in path for m in LIVE_MARKERS) or path.endswith("/.hermes/plugin-data/hyperactive/hyperactive.db"):
        if not live:
            raise SystemExit(f"refusing: {path} looks live; pass --live (after backing it up)")
        backups = sorted(target.parent.glob(f"{target.name}.pre-midna.*"), key=lambda b: b.stat().st_mtime)
        if not backups or datetime.now().timestamp() - backups[-1].stat().st_mtime > BACKUP_MAX_AGE.total_seconds():
            raise SystemExit(f"refusing --live: no {target.name}.pre-midna.<stamp> backup from the last 10 minutes")


def cron_script(plan: Plan, deliver: str) -> str:
    lines = ["#!/usr/bin/env bash",
             "# Generated by dev/import_midna.py. NOT run by the importer: review, then run as the hermes user",
             "# at the writer flip (P3), after Hermes's timezone is Asia/Seoul (midna's cron times are KST).",
             "# Each job is named midna:<wakeup id>; undo: hermes cron list | grep midna: , then hermes cron remove <id>.",
             "# One-shots (ISO time) fire once and are then kept disabled (hermes cron list --all). Hermes 0.22.5 deletes",
             "# a one-shot unrun if the gateway is down for more than 120 s past its time, and rejects one already past.",
             "# Agent jobs only fire while the gateway runs (hermes cron tick alone skips them).",
             "set -euo pipefail", f"DELIVER=\"${{DELIVER:-{deliver}}}\"", 'PAUSED="${PAUSED:-}"  # set to --paused to stage them', ""]
    for c in plan.cron:
        lines.append(f"# wakeup {c['wakeup_id']} ({c['kind']}, {'one-shot ' + c['due_local'] if c['one_shot'] else c['schedule']})")
        lines.append(f"hermes cron create {shlex.quote(c['schedule'])} {shlex.quote(c['prompt'])} "
                     f"--name {shlex.quote(c['name'])} --deliver \"$DELIVER\" $PAUSED")
        lines.append("")
    return "\n".join(lines)


def report(plan: Plan, result: dict, problems: list[str], mode: str, target: Path) -> str:
    out = [f"midna -> hyperactive import ({mode}) into {target}", ""]
    out.append("Imported rows (hyperactive table: insert / update / unchanged):")
    for table, c in sorted(result["counts"].items()):
        extra = "".join(f", {k} {v}" for k, v in c.items() if k not in ("insert", "update", "unchanged"))
        out.append(f"  {table:13} {c.get('insert', 0):4} / {c.get('update', 0):3} / {c.get('unchanged', 0):4}{extra}")
    out += ["", "Not imported (midna table: rows, reason):"]
    for (table, why), n in sorted(plan.skips.items()):
        out.append(f"  {table:16} {n:5}  {why}")
    out += ["", f"Agent cron jobs planned: {len(plan.cron)} "
                f"({sum(c['one_shot'] for c in plan.cron)} one-shot, {sum(not c['one_shot'] for c in plan.cron)} recurring)"]
    out += [f"  {c['name']:12} {c['schedule']:24} {c['reason'][:70]}" for c in plan.cron]
    out += ["", "Remaps and warnings:"] + [f"  - {w}" for w in plan.warnings]
    if result["diffs"]:
        out += ["", f"Changed since the last run ({len(result['diffs'])}):"] + [f"  {d}" for d in result["diffs"][:200]]
    out += ["", "Checks: " + ("integrity ok, foreign keys ok, counts reconcile" if not problems else "PROBLEMS")]
    out += [f"  ! {p}" for p in problems]
    return "\n".join(out)


def run_import(args, apply: bool) -> int:
    target = Path(args.target)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    src = open_source(args.source)
    store.set_timezone(args.tz)
    plan = build(src, args.tz)
    src.close()
    if apply:
        guard_target(target, args.live)
        work = target
    else:  # dry run on a throwaway copy, so the numbers are the real ones
        tmp = Path(tempfile.mkdtemp(prefix="ha-import-"))
        work = tmp / "hyperactive.db"
        if target.exists():
            s, d = sqlite3.connect(f"file:{target.resolve()}?mode=ro", uri=True), sqlite3.connect(work)
            s.backup(d)
            s.close()
            d.close()
    conn = store.connect(work)
    conn.execute("PRAGMA foreign_keys=ON")
    remap_mentions = lambda: remap_ids(conn, plan)  # noqa: E731
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = sync(conn, plan)
        remap_mentions()
        problems = check(conn, plan)
        if problems and apply:
            raise RuntimeError("; ".join(problems))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
        if not apply:
            shutil.rmtree(work.parent, ignore_errors=True)
    text = report(plan, result, problems, "apply" if apply else "dry run", target)
    (out / "report.txt").write_text(text + "\n")
    (out / "plan.json").write_text(json.dumps({
        "mode": "apply" if apply else "dry-run", "target": str(target), "at": store.iso(store.now()),
        "counts": result["counts"], "skipped": [{"table": t, "reason": r, "rows": n} for (t, r), n in plan.skips.items()],
        "skipped_rows": plan.skip_rows, "warnings": plan.warnings, "cron": plan.cron, "diffs": result["diffs"],
        "problems": problems}, indent=2, ensure_ascii=False))
    (out / "cron-plan.sh").write_text(cron_script(plan, args.deliver) + "\n")
    os.chmod(out / "cron-plan.sh", 0o700)
    print(text)
    return 1 if problems else 0


def undo(args) -> int:
    target = Path(args.target)
    if args.apply:
        guard_target(target, args.live)
    conn = store.connect(target) if args.apply else sqlite3.connect(f"file:{target.resolve()}?mode=ro", uri=True)
    tables = IMPORT_TABLES
    like = f"{SRC}:%"
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE ext_ref LIKE ?", (like,)).fetchone()[0] for t in tables}
    links = conn.execute("SELECT COUNT(*) FROM memory_links WHERE memory_id IN "
                         "(SELECT id FROM memories WHERE ext_ref LIKE ?)", (like,)).fetchone()[0]
    if args.apply:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM memory_links WHERE memory_id IN (SELECT id FROM memories WHERE ext_ref LIKE ?)", (like,))
        conn.execute("UPDATE memories SET superseded_by=NULL WHERE superseded_by IN "
                     "(SELECT id FROM memories WHERE ext_ref LIKE ?)", (like,))
        for t in ("events", "followups", "memories", "notes", "calendar", "decisions", "commitments", "tasks", "goals"):
            if t == "goals":  # children first: imported goals may parent each other
                conn.execute("UPDATE goals SET parent_id=NULL WHERE parent_id IN (SELECT id FROM goals WHERE ext_ref LIKE ?)",
                             (like,))
            conn.execute(f"DELETE FROM {t} WHERE ext_ref LIKE ?", (like,))
        conn.execute("COMMIT")
    conn.close()
    print(("deleted" if args.apply else "would delete") + ": " +
          ", ".join(f"{t} {n}" for t, n in counts.items() if n) + f", memory_links {links}")
    print("cron jobs: hermes cron list | grep 'midna:' -> hermes cron remove <id> for each")
    print("people created for commitments are kept (they may be referenced by new rows).")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("source")
    s.add_argument("dest")
    s.add_argument("--group", default="", help="make the copy group-readable by this group (e.g. hermes)")
    for name in ("plan", "apply"):
        s = sub.add_parser(name)
        s.add_argument("source", help="a midna.db snapshot (opened read-only)")
        s.add_argument("--target", required=True, help="hyperactive.db")
        s.add_argument("--out", required=True, help="where plan.json, report.txt and cron-plan.sh go")
        s.add_argument("--tz", default="Asia/Seoul")
        s.add_argument("--deliver", default="telegram", help="--deliver for the generated cron jobs")
        s.add_argument("--live", action="store_true")
    s = sub.add_parser("undo")
    s.add_argument("--target", required=True)
    s.add_argument("--apply", action="store_true", help="without it, only count")
    s.add_argument("--live", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "snapshot":
        snapshot(args.source, args.dest, args.group)
        return 0
    if args.cmd == "undo":
        return undo(args)
    return run_import(args, apply=args.cmd == "apply")


if __name__ == "__main__":
    sys.exit(main())
