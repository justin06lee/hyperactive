"""hyperactive's structured store: one SQLite file under <HERMES_HOME>/plugin-data/hyperactive/.

Pure stdlib on purpose: the cron pulse script opens the same file from a subprocess without
importing Hermes. Every row that makes the agent act carries a goal_id (an "Inbox" goal when the
caller had none) so every proactive message traces to a goal. Times are stored in UTC and
rendered in local time.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HORIZONS = ("life", "year", "quarter", "week")
# Finer horizons kept in goals.span; the CHECK on goals.horizon predates them, so each also stores
# the nearest coarse horizon (that's what stale nudges and the brief go by).
SPANS = {"years": "life", "month": "quarter"}
MEMORY_KINDS = ("identity", "preference", "relationship", "pattern", "context", "procedure", "world")
EVENT_STATUSES = ("confirmed", "tentative", "cancelled")
TASK_STATUSES = ("open", "doing", "waiting", "done", "dropped")
STAKES = ("low", "money", "public", "irreversible")
GOAL_STATUSES = ("active", "done", "paused", "dropped")
INBOX = "Inbox"
SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
  id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES goals(id),
  horizon TEXT NOT NULL CHECK (horizon IN ('life','year','quarter','week')),
  title TEXT NOT NULL, why TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL, last_moved_at TEXT NOT NULL, nudged_at TEXT);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY, goal_id INTEGER REFERENCES goals(id), title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open', due_at TEXT, created_at TEXT NOT NULL, done_at TEXT);
CREATE TABLE IF NOT EXISTS people (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, notes TEXT DEFAULT '',
  last_contact_at TEXT);
-- Promises in both directions: i_owe (the user promised someone) and they_owe (someone promised them).
CREATE TABLE IF NOT EXISTS commitments (
  id INTEGER PRIMARY KEY, goal_id INTEGER REFERENCES goals(id),
  direction TEXT NOT NULL CHECK (direction IN ('i_owe','they_owe')),
  person_id INTEGER REFERENCES people(id), what TEXT NOT NULL, due_at TEXT,
  status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL, notified_at TEXT, closed_at TEXT);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY, goal_id INTEGER REFERENCES goals(id), title TEXT NOT NULL,
  choice TEXT DEFAULT '', rationale TEXT DEFAULT '', review_at TEXT,
  status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL, notified_at TEXT);
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY, text TEXT NOT NULL, tags TEXT DEFAULT '', created_at TEXT NOT NULL);
-- Anything hyperactive will say on its own later. The pulse delivers rows whose due_at passed.
CREATE TABLE IF NOT EXISTS followups (
  id INTEGER PRIMARY KEY, goal_id INTEGER REFERENCES goals(id),
  kind TEXT NOT NULL DEFAULT 'followup', text TEXT NOT NULL, due_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, sent_at TEXT);
-- Tap cards: a question with a recommended answer and a timeout rule (see card_action()).
-- `action` is a typed action (ACTIONS) the plugin performs itself when the recommended answer wins.
CREATE TABLE IF NOT EXISTS cards (
  id INTEGER PRIMARY KEY, goal_id INTEGER REFERENCES goals(id), question TEXT NOT NULL,
  options TEXT NOT NULL DEFAULT '[]', recommended TEXT NOT NULL,
  stakes TEXT NOT NULL DEFAULT 'low' CHECK (stakes IN ('low','money','public','irreversible')),
  action TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'open', due_at TEXT NOT NULL,
  asked_at TEXT, reasked_at TEXT, held_at TEXT, answer TEXT, answered_by TEXT, answered_at TEXT,
  result TEXT, created_at TEXT NOT NULL);
-- Append-only trail of everything hyperactive did, with the goal it served and what caused it.
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, goal_id INTEGER, ref TEXT,
  detail TEXT);
"""


class StoreError(ValueError):
    """A caller mistake worth showing to the model or the user verbatim."""


_TZ: ZoneInfo | None = None  # Hermes's configured timezone; None = this process's local time


def set_timezone(name: str) -> None:
    """Use Hermes's timezone setting (an IANA name) for local times; '' or invalid = system local."""
    global _TZ
    try:
        _TZ = ZoneInfo(name) if name else None
    except Exception:
        _TZ = None


def local(dt: datetime) -> datetime:
    return dt.astimezone(_TZ)


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


_DURATION = re.compile(r"(?:in\s+)?(\d+)\s*(m|min|mins|minutes?|h|hrs?|hours?|d|days?|w|wks?|weeks?)")


def parse_duration(text: str) -> timedelta:
    m = _DURATION.fullmatch((text or "").strip().lower())
    if not m:
        raise StoreError(f"not a duration: {text!r} (try '30m', '2h', '3d')")
    n, unit = int(m.group(1)), m.group(2)[0]
    return {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n),
            "w": timedelta(weeks=n)}[unit]


_CLOCK = re.compile(r"(?:(today|tomorrow|tmrw|mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?\s+(?:at\s+)?)?"
                    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?")
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _parse_clock(text: str, base: datetime) -> datetime | None:
    """'07:00', '7am', 'tomorrow 7:30', 'Sun 07:00' (as local_time prints it) -> next such local time."""
    m = _CLOCK.fullmatch(text.lower())
    if not m or (m.group(3) is None and m.group(4) is None):
        return None
    day, hour, minute, ampm = m.group(1), int(m.group(2)), int(m.group(3) or 0), m.group(4)
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    here = local(base)
    dt = here.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if day in ("tomorrow", "tmrw"):
        dt += timedelta(days=1)
    elif day in _DAYS:
        dt += timedelta(days=(_DAYS.index(day) - here.weekday()) % 7)
        if dt <= here:
            dt += timedelta(days=7)
    elif day is None and dt <= here:
        dt += timedelta(days=1)
    return dt.astimezone(timezone.utc)


def parse_when(when: str, *, base: datetime | None = None) -> datetime:
    """'now' / 'in 2m' / '2h' / '3d' / '7am' / 'tomorrow 07:00' / 'Sun 07:00' / 'Nov 14 07:00' /
    ISO timestamp (local if naive) -> aware UTC datetime."""
    base = base or now()
    text = (when or "").strip()
    if text.lower() in ("", "now"):
        return base
    if _DURATION.fullmatch(text.lower()):
        return base + parse_duration(text)
    clock = _parse_clock(text, base)
    if clock:
        return clock
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:  # local_time's own far-out format, 'Nov 14 07:00'
            dt = datetime.strptime(f"{local(base).year} {text}", "%Y %b %d %H:%M")
        except ValueError:
            raise StoreError(f"can't read time {when!r} (try 'in 2h', '1d', 'tomorrow 07:00' "
                             "or 2026-11-08T09:00)") from None
    return (dt if dt.tzinfo else dt.replace(tzinfo=_TZ) if _TZ else dt.astimezone()).astimezone(timezone.utc)


def local_time(ts: str | datetime, *, at: datetime | None = None) -> str:
    """'18:00' today, 'Sat 07:00' within a week, 'Nov 14 07:00' beyond. Local time."""
    dt = local(parse_iso(ts) if isinstance(ts, str) else ts)
    ref = local(at or now())
    if dt.date() == ref.date():
        return dt.strftime("%H:%M")
    if abs((dt.date() - ref.date()).days) < 7:
        return dt.strftime("%a %H:%M")
    return dt.strftime("%b %-d %H:%M")


# Schema v3: additive only (new columns with defaults, new tables, indexes), so a v2 build can still
# open and write the file. Every column is checked by name, not by user_version, because a v2 build
# resets user_version to 2 when it opens the file.
COLUMNS_V3 = {
    "goals": ("span TEXT", "success TEXT DEFAULT ''", "target_at TEXT", "review_every_days INTEGER",
              "last_reviewed_at TEXT", "ext_ref TEXT"),
    "tasks": ("notes TEXT DEFAULT ''", "owner TEXT NOT NULL DEFAULT 'user'", "priority INTEGER",
              "estimate_min INTEGER", "updated_at TEXT", "ext_ref TEXT"),
    "people": ("ext_ref TEXT",),
    "commitments": ("ext_ref TEXT",),
    "decisions": ("context TEXT DEFAULT ''", "options TEXT DEFAULT ''", "expected TEXT DEFAULT ''",
                  "confidence REAL", "outcome TEXT DEFAULT ''", "lesson TEXT DEFAULT ''", "reviewed_at TEXT",
                  "ext_ref TEXT"),
    "memories": ("kind TEXT NOT NULL DEFAULT 'context'", "importance INTEGER NOT NULL DEFAULT 5",
                 "pinned INTEGER NOT NULL DEFAULT 0", "source TEXT DEFAULT ''", "superseded_by INTEGER",
                 "archived_at TEXT", "archive_reason TEXT", "updated_at TEXT", "last_used_at TEXT",
                 "use_count INTEGER NOT NULL DEFAULT 0", "ext_ref TEXT"),
    "followups": ("ext_ref TEXT",),
    "events": ("ext_ref TEXT",),
}
SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', tags TEXT DEFAULT '',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, ext_ref TEXT);
-- The user's calendar ('events' is the audit trail): local entries, no sync with a real calendar.
CREATE TABLE IF NOT EXISTS calendar (
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT,
  all_day INTEGER NOT NULL DEFAULT 0, location TEXT DEFAULT '', notes TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'confirmed' CHECK (status IN ('confirmed','tentative','cancelled')),
  goal_id INTEGER REFERENCES goals(id), created_at TEXT NOT NULL, updated_at TEXT NOT NULL, ext_ref TEXT);
CREATE INDEX IF NOT EXISTS calendar_starts ON calendar(starts_at);
CREATE TABLE IF NOT EXISTS memory_links (
  memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  entity TEXT NOT NULL, entity_id INTEGER NOT NULL, PRIMARY KEY (memory_id, entity, entity_id));
"""
# External-content FTS5 indexes kept in sync by triggers. Optional: without FTS5, search uses LIKE.
FTS = {"memories": ("text", "tags"), "notes": ("title", "body", "tags")}


def _has_fts5(conn) -> bool:
    try:
        return bool(conn.execute("SELECT sqlite_compileoption_used('ENABLE_FTS5')").fetchone()[0]) or \
            "fts5" in {r[0] for r in conn.execute("PRAGMA module_list")}
    except sqlite3.Error:
        return False


def migrate(conn) -> list[str]:
    """Bring any v1/v2 file to v3. Idempotent; returns what it changed."""
    changed = []
    conn.execute("BEGIN IMMEDIATE")  # two processes opening an old file at once: one migrates
    try:
        for table, cols in COLUMNS_V3.items():
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for col in cols:
                if col.split()[0] not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
                    changed.append(f"{table}.{col.split()[0]}")
        for stmt in SCHEMA_V3.split(";"):
            if stmt.strip():
                conn.execute(stmt)
        for table in list(COLUMNS_V3) + ["notes", "calendar"]:
            conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {table}_ext_ref ON {table}(ext_ref)")
        if _has_fts5(conn):
            for table, cols in FTS.items():
                fts = f"{table}_fts"
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (fts,)).fetchone():
                    continue
                c, new = ", ".join(cols), ", ".join(f"new.{x}" for x in cols)
                old = ", ".join(f"old.{x}" for x in cols)
                conn.execute(f"CREATE VIRTUAL TABLE {fts} USING fts5({c}, content='{table}', content_rowid='id')")
                conn.execute(f"CREATE TRIGGER {table}_fts_ai AFTER INSERT ON {table} BEGIN "
                             f"INSERT INTO {fts}(rowid, {c}) VALUES (new.id, {new}); END")
                conn.execute(f"CREATE TRIGGER {table}_fts_ad AFTER DELETE ON {table} BEGIN "
                             f"INSERT INTO {fts}({fts}, rowid, {c}) VALUES ('delete', old.id, {old}); END")
                conn.execute(f"CREATE TRIGGER {table}_fts_au AFTER UPDATE ON {table} BEGIN "
                             f"INSERT INTO {fts}({fts}, rowid, {c}) VALUES ('delete', old.id, {old}); "
                             f"INSERT INTO {fts}(rowid, {c}) VALUES (new.id, {new}); END")
                conn.execute(f"INSERT INTO {fts}({fts}) VALUES ('rebuild')")
                changed.append(fts)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return changed


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        migrate(conn)
    return conn


def log(conn, kind: str, goal_id: int | None, detail: str, ref: str = "", *, at: datetime | None = None) -> int:
    cur = conn.execute("INSERT INTO events (at, kind, goal_id, ref, detail) VALUES (?,?,?,?,?)",
                       (iso(at or now()), kind, goal_id, ref, detail))
    return cur.lastrowid


# --- goals ---------------------------------------------------------------------------------

def add_goal(conn, title: str, horizon: str = "week", parent_id: int | None = None, why: str = "") -> int:
    span = horizon if horizon in SPANS else None
    horizon = SPANS.get(horizon, horizon)
    if horizon not in HORIZONS:
        raise StoreError(f"horizon must be one of {', '.join(HORIZONS + tuple(SPANS))}")
    if not (title or "").strip():
        raise StoreError("a goal needs a title")
    if parent_id is not None and get_goal(conn, parent_id) is None:
        raise StoreError(f"no goal #{parent_id}")
    t = iso(now())
    cur = conn.execute("INSERT INTO goals (parent_id, horizon, span, title, why, created_at, last_moved_at) "
                       "VALUES (?,?,?,?,?,?,?)", (parent_id, horizon, span, title.strip(), why or "", t, t))
    log(conn, "goal.add", cur.lastrowid, title.strip(), f"goal:{cur.lastrowid}")
    if parent_id is not None:
        touch(conn, parent_id)
    conn.commit()
    return cur.lastrowid


def get_goal(conn, goal_id: int | None):
    if goal_id is None:
        return None
    return conn.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()


def inbox_goal(conn) -> int:
    """The goal that catches anything scheduled without one, so nothing proactive is goal-less."""
    row = conn.execute("SELECT id FROM goals WHERE title=? AND parent_id IS NULL AND status='active'",
                       (INBOX,)).fetchone()
    if row:
        return row["id"]
    t = iso(now())
    cur = conn.execute("INSERT INTO goals (horizon, title, why, created_at, last_moved_at) VALUES (?,?,?,?,?)",
                       ("week", INBOX, "Things not tied to a goal yet.", t, t))
    return cur.lastrowid


def resolve_goal(conn, goal_id) -> int:
    """A valid goal id for a new row: the one given, or the Inbox."""
    if goal_id in (None, "", 0):
        return inbox_goal(conn)
    goal_id = int(goal_id)
    if get_goal(conn, goal_id) is None:
        raise StoreError(f"no goal #{goal_id}")
    return goal_id


def touch(conn, goal_id: int | None, at: datetime | None = None) -> None:
    """Something on this goal moved: bump it and every ancestor (a week moving moves its quarter)."""
    seen = set()
    while goal_id is not None and goal_id not in seen:
        seen.add(goal_id)
        conn.execute("UPDATE goals SET last_moved_at=? WHERE id=?", (iso(at or now()), goal_id))
        row = get_goal(conn, goal_id)
        goal_id = row["parent_id"] if row else None


def set_goal_status(conn, goal_id: int, status: str) -> None:
    if status not in GOAL_STATUSES:
        raise StoreError(f"status must be one of {', '.join(GOAL_STATUSES)}")
    if get_goal(conn, goal_id) is None:
        raise StoreError(f"no goal #{goal_id}")
    conn.execute("UPDATE goals SET status=? WHERE id=?", (status, goal_id))
    touch(conn, goal_id)
    log(conn, f"goal.{status}", goal_id, "", f"goal:{goal_id}")
    conn.commit()


def goal_chain(conn, goal_id: int | None) -> list:
    """The goal and its ancestors, nearest first."""
    out, seen = [], set()
    while goal_id is not None and goal_id not in seen:
        seen.add(goal_id)
        row = get_goal(conn, goal_id)
        if row is None:
            break
        out.append(row)
        goal_id = row["parent_id"]
    return out


def horizon_of(goal) -> str:
    """The goal's own horizon: its span ('years', 'month') when it has one."""
    return (goal["span"] if "span" in goal.keys() else None) or goal["horizon"]


def goal_line(conn, goal_id: int | None, extra: str = "") -> str:
    """The calm last line of every proactive message: which goal it serves."""
    g = get_goal(conn, goal_id)
    if g is None or (g["title"] == INBOX and g["parent_id"] is None):
        base = "goal: none yet (inbox)"
    else:
        base = f"goal: {g['title']} ({horizon_of(g)})"
    return f"{base} · {extra}" if extra else base


def goal_tree(conn) -> list[dict]:
    """Active goals as nested dicts, roots first; the Inbox sorts last."""
    rows = [dict(r) for r in conn.execute("SELECT * FROM goals WHERE status='active' ORDER BY id")]
    by_id = {r["id"]: {**r, "children": []} for r in rows}
    roots = []
    for r in by_id.values():
        parent = by_id.get(r["parent_id"])
        (parent["children"] if parent else roots).append(r)
    return sorted(roots, key=lambda r: r["title"] == INBOX)


def render_tree(conn, *, max_lines: int = 40) -> str:
    lines: list[str] = []

    def walk(node, depth):
        lines.append(f"{'  ' * depth}- #{node['id']} [{horizon_of(node)}] {node['title']}")
        for child in node["children"]:
            walk(child, depth + 1)

    for root in goal_tree(conn):
        walk(root, 0)
    if not lines:
        return "(no goals yet)"
    return "\n".join(lines[:max_lines] + (["  ..."] if len(lines) > max_lines else []))


def stale_goals(conn, at: datetime, stale_after: timedelta, *, include_nudged: bool = False) -> list[dict]:
    """Week goals still for stale_after (quarter goals: 4x), not nudged since they last moved
    (include_nudged: those too, for the brief)."""
    out = []
    for g in conn.execute("SELECT * FROM goals WHERE status='active' AND horizon IN ('week','quarter') "
                          "AND NOT (title=? AND parent_id IS NULL) ORDER BY last_moved_at", (INBOX,)):
        limit = stale_after * (4 if g["horizon"] == "quarter" else 1)
        moved = parse_iso(g["last_moved_at"])
        if at - moved < limit:
            continue
        if (not include_nudged and g["nudged_at"] and parse_iso(g["nudged_at"]) > moved
                and at - parse_iso(g["nudged_at"]) < limit):
            continue  # already nudged for this stretch of stillness
        out.append(dict(g))
    return out


# --- follow-ups ----------------------------------------------------------------------------

def add_followup(conn, goal_id: int | None, text: str, when: str, kind: str = "followup") -> dict:
    if kind not in ("followup", "reminder", "nudge"):
        raise StoreError("kind must be followup, reminder or nudge")
    if not (text or "").strip():
        raise StoreError("a follow-up needs text")
    goal_id = resolve_goal(conn, goal_id)
    due = parse_when(when)
    cur = conn.execute("INSERT INTO followups (goal_id, kind, text, due_at, created_at) VALUES (?,?,?,?,?)",
                       (goal_id, kind, text.strip(), iso(due), iso(now())))
    log(conn, f"{kind}.schedule", goal_id, text.strip(), f"followup:{cur.lastrowid}")
    touch(conn, goal_id)
    conn.commit()
    return {"id": cur.lastrowid, "goal_id": goal_id, "due_at": iso(due), "due_local": local_time(due)}


def get_followup(conn, followup_id: int):
    return conn.execute("SELECT * FROM followups WHERE id=?", (followup_id,)).fetchone()


def pending_followups(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM followups WHERE status='pending' ORDER BY due_at")]


# --- memory (opt-in: the memory provider in memory.py), notes, calendar ---------------------

def _fts_query(query: str) -> str:
    """Free text -> an FTS5 query that can't be a syntax error: any word, best matches first."""
    words = re.findall(r"\w+", query or "")
    return " OR ".join(f'"{w}"' for w in words[:12])


def _fts_ids(conn, table: str, query: str, limit: int) -> list[int] | None:
    """Row ids matching `query`, best first; None when this SQLite has no FTS index (use LIKE)."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (f"{table}_fts",)).fetchone():
        return None
    q = _fts_query(query)
    if not q:
        return []
    return [r[0] for r in conn.execute(f"SELECT rowid FROM {table}_fts WHERE {table}_fts MATCH ? "
                                       f"ORDER BY rank LIMIT ?", (q, limit))]


def _like_ids(conn, table: str, cols: tuple[str, ...], query: str, limit: int) -> list[int]:
    words = re.findall(r"\w+", query or "")[:6]
    if not words:
        return []
    where = " OR ".join(f"{c} LIKE ?" for c in cols for _ in words)
    args = [f"%{w}%" for _ in cols for w in words]
    return [r[0] for r in conn.execute(f"SELECT id FROM {table} WHERE {where} ORDER BY id DESC LIMIT ?",
                                       (*args, limit))]


def add_memory(conn, text: str, tags: str = "", *, kind: str = "context", importance: int = 5,
               pinned: bool = False, supersedes: int | None = None, source: str = "") -> int:
    """Remember something durable about the user. `supersedes` archives the memory it replaces."""
    if not (text or "").strip():
        raise StoreError("a memory needs text")
    if kind not in MEMORY_KINDS:
        raise StoreError(f"kind must be one of {', '.join(MEMORY_KINDS)}")
    importance = int(importance)
    if not 1 <= importance <= 10:
        raise StoreError("importance is 1-10")
    old = None
    if supersedes:
        old = conn.execute("SELECT * FROM memories WHERE id=?", (int(supersedes),)).fetchone()
        if old is None:
            raise StoreError(f"no memory #{supersedes}")
    t = iso(now())
    cur = conn.execute("INSERT INTO memories (text, tags, kind, importance, pinned, source, created_at, updated_at, "
                       "last_used_at) VALUES (?,?,?,?,?,?,?,?,?)",
                       (text.strip(), tags or "", kind, importance, int(bool(pinned)), source, t, t, t))
    if old is not None:
        conn.execute("UPDATE memories SET superseded_by=?, archived_at=?, archive_reason='superseded', updated_at=? "
                     "WHERE id=?", (cur.lastrowid, t, t, old["id"]))
    log(conn, "memory.add", None, text.strip()[:200], f"memory:{cur.lastrowid}")
    conn.commit()
    return cur.lastrowid


def update_memory(conn, memory_id: int, *, pinned=None, importance=None, archive_reason: str | None = None) -> dict:
    row = conn.execute("SELECT * FROM memories WHERE id=?", (int(memory_id),)).fetchone()
    if row is None:
        raise StoreError(f"no memory #{memory_id}")
    t = iso(now())
    sets, args = ["updated_at=?"], [t]
    if pinned is not None:
        sets.append("pinned=?"); args.append(int(bool(pinned)))
    if importance is not None:
        if not 1 <= int(importance) <= 10:
            raise StoreError("importance is 1-10")
        sets.append("importance=?"); args.append(int(importance))
    if archive_reason:
        sets += ["archived_at=?", "archive_reason=?"]; args += [t, archive_reason]
    conn.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id=?", (*args, row["id"]))
    log(conn, "memory.update", None, ", ".join(s.split("=")[0] for s in sets[1:]), f"memory:{row['id']}")
    conn.commit()
    return dict(conn.execute("SELECT * FROM memories WHERE id=?", (row["id"],)).fetchone())


# Memory strength: importance/10, halved every HALF_LIFE[importance]
# days since last use, boosted by use count and by a link to something still open. Unpinned
# memories weaker than FADE_BELOW are archived by the nightly fade.
HALF_LIFE = (0, 3, 7, 10, 14, 30, 45, 90, 180, 365, 730)
FADE_BELOW = 0.05


def _memory_alive(conn, memory_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM memory_links l WHERE l.memory_id=? AND (l.entity='person' "
        "OR (l.entity='goal' AND EXISTS (SELECT 1 FROM goals g WHERE g.id=l.entity_id AND g.status IN ('active','paused'))) "
        "OR (l.entity='task' AND EXISTS (SELECT 1 FROM tasks t WHERE t.id=l.entity_id AND t.status IN ('open','doing','waiting'))) "
        "OR (l.entity='commitment' AND EXISTS (SELECT 1 FROM commitments c WHERE c.id=l.entity_id AND c.status='open')) "
        "OR (l.entity='decision' AND EXISTS (SELECT 1 FROM decisions d WHERE d.id=l.entity_id AND d.status='open'))) "
        "LIMIT 1", (memory_id,)).fetchone() is not None


def memory_strength(conn, m, at: datetime | None = None) -> float:
    if m["pinned"]:
        return float("inf")
    at = at or now()
    used = parse_iso(m["last_used_at"] or m["created_at"])
    days = max(0.0, (at - used).total_seconds() / 86400)
    imp = min(10, max(1, int(m["importance"])))
    decay = 0.5 ** (days / HALF_LIFE[imp])
    boost = 1 + 0.25 * math.log2(1 + int(m["use_count"] or 0))
    return imp / 10 * decay * boost * (1.5 if _memory_alive(conn, m["id"]) else 1)


def fade_memories(conn, at: datetime | None = None) -> int:
    """Archive unpinned memories that have faded below FADE_BELOW. Returns how many."""
    at = at or now()
    faded = [m["id"] for m in conn.execute("SELECT * FROM memories WHERE archived_at IS NULL AND pinned=0")
             if memory_strength(conn, m, at) < FADE_BELOW]
    conn.executemany("UPDATE memories SET archived_at=?, archive_reason='faded' WHERE id=?",
                     [(iso(at), mid) for mid in faded])
    if faded:
        log(conn, "memory.fade", None, f"{len(faded)} faded", "", at=at)
    conn.commit()
    return len(faded)


def search_memories(conn, query: str, limit: int = 8, *, include_archived: bool = False,
                    touch_used: bool = True) -> list[dict]:
    """Best matches, ranked by text relevance weighted by memory strength."""
    q = _fts_query(query)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='memories_fts'").fetchone():
        hits = [(r[0], -r[1]) for r in conn.execute(
            "SELECT rowid, bm25(memories_fts) FROM memories_fts WHERE memories_fts MATCH ? ORDER BY rank LIMIT 50",
            (q,))] if q else []
    else:
        hits = [(mid, 1.0) for mid in _like_ids(conn, "memories", ("text", "tags"), query, 50)]
    at = now()
    scored = []
    for mid, rel in hits:
        r = conn.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
        if r and (include_archived or not r["archived_at"]):
            scored.append((rel * (0.5 + min(memory_strength(conn, r, at), 3)), dict(r)))
    rows = [r for _, r in sorted(scored, key=lambda x: -x[0])[:limit]]
    if touch_used and rows:
        conn.executemany("UPDATE memories SET last_used_at=?, use_count=use_count+1 WHERE id=?",
                         [(iso(now()), r["id"]) for r in rows])
        conn.commit()
    return rows


def pinned_memories(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM memories WHERE pinned=1 AND archived_at IS NULL ORDER BY importance DESC, kind, id")]


def save_note(conn, title: str, body: str = "", tags: str = "", note_id: int | None = None) -> dict:
    """New note, or (note_id) replace an existing note's title/body/tags; empty fields stay as they were."""
    t = iso(now())
    if note_id:
        row = conn.execute("SELECT * FROM notes WHERE id=?", (int(note_id),)).fetchone()
        if row is None:
            raise StoreError(f"no note #{note_id}")
        conn.execute("UPDATE notes SET title=?, body=?, tags=?, updated_at=? WHERE id=?",
                     ((title or "").strip() or row["title"], body if body else row["body"],
                      tags if tags else row["tags"], t, row["id"]))
        conn.commit()
        return {"id": row["id"], "updated": True}
    if not (title or "").strip():
        raise StoreError("a note needs a title")
    cur = conn.execute("INSERT INTO notes (title, body, tags, created_at, updated_at) VALUES (?,?,?,?,?)",
                       (title.strip(), body or "", tags or "", t, t))
    log(conn, "note.add", None, title.strip(), f"note:{cur.lastrowid}")
    conn.commit()
    return {"id": cur.lastrowid, "updated": False}


def get_note(conn, note_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM notes WHERE id=?", (int(note_id),)).fetchone()
    return dict(row) if row else None


def search_notes(conn, query: str = "", limit: int = 10) -> list[dict]:
    """Best matches for `query` (recent notes when empty), with a short preview of each body."""
    if (query or "").strip():
        ids = _fts_ids(conn, "notes", query, limit)
        if ids is None:
            ids = _like_ids(conn, "notes", ("title", "body", "tags"), query, limit)
    else:
        ids = [r[0] for r in conn.execute("SELECT id FROM notes ORDER BY updated_at DESC LIMIT ?", (limit,))]
    out = []
    for nid in ids:
        n = get_note(conn, nid)
        if n:
            body = " ".join(n["body"].split())
            out.append({"id": n["id"], "title": n["title"], "tags": n["tags"],
                        "updated": local_time(n["updated_at"]), "preview": body[:160] + ("..." if len(body) > 160 else "")})
    return out


def _event_time(text: str, all_day: bool) -> datetime:
    """'2026-11-20' (all-day: local midnight), or anything parse_when reads."""
    if all_day and re.fullmatch(r"\d{4}-\d{2}-\d{2}", (text or "").strip()):
        return parse_when(f"{text.strip()}T00:00")
    return parse_when(text)


def add_event(conn, title: str, starts: str, ends: str | None = None, *, all_day: bool = False,
              location: str = "", notes: str = "", status: str = "confirmed", goal_id: int | None = None) -> dict:
    if not (title or "").strip():
        raise StoreError("an event needs a title")
    if status not in EVENT_STATUSES:
        raise StoreError(f"status must be one of {', '.join(EVENT_STATUSES)}")
    start = _event_time(starts, all_day)
    end = _event_time(ends, all_day) if ends else None
    if end and end < start:
        raise StoreError("the event ends before it starts")
    if goal_id not in (None, "", 0) and get_goal(conn, int(goal_id)) is None:
        raise StoreError(f"no goal #{goal_id}")
    t = iso(now())
    cur = conn.execute("INSERT INTO calendar (title, starts_at, ends_at, all_day, location, notes, status, goal_id, "
                       "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (title.strip(), iso(start), iso(end) if end else None, int(bool(all_day)), location or "",
                        notes or "", status, int(goal_id) if goal_id else None, t, t))
    log(conn, "calendar.add", int(goal_id) if goal_id else None, title.strip(), f"calendar:{cur.lastrowid}")
    conn.commit()
    return {"id": cur.lastrowid, "starts_local": local_time(start), "all_day": bool(all_day)}


def update_event(conn, event_id: int, **fields) -> dict:
    row = conn.execute("SELECT * FROM calendar WHERE id=?", (int(event_id),)).fetchone()
    if row is None:
        raise StoreError(f"no event #{event_id}")
    sets, args = [], []
    for key in ("title", "location", "notes", "status"):
        if fields.get(key) not in (None, ""):
            if key == "status" and fields[key] not in EVENT_STATUSES:
                raise StoreError(f"status must be one of {', '.join(EVENT_STATUSES)}")
            sets.append(f"{key}=?"); args.append(fields[key])
    for key in ("starts", "ends"):
        if fields.get(key):
            sets.append(f"{key}_at=?"); args.append(iso(_event_time(fields[key], bool(row["all_day"]))))
    if not sets:
        raise StoreError("nothing to change")
    conn.execute(f"UPDATE calendar SET {', '.join(sets)}, updated_at=? WHERE id=?", (*args, iso(now()), row["id"]))
    log(conn, "calendar.update", row["goal_id"], ", ".join(s.split("=")[0] for s in sets), f"calendar:{row['id']}")
    conn.commit()
    return {"id": row["id"], "changed": [s.split("=")[0] for s in sets]}


def events_between(conn, start: datetime, end: datetime, *, include_cancelled: bool = False) -> list[dict]:
    """Events overlapping [start, end), soonest first."""
    sql = ("SELECT * FROM calendar WHERE starts_at < ? AND COALESCE(ends_at, starts_at) >= ?"
           + ("" if include_cancelled else " AND status != 'cancelled'") + " ORDER BY starts_at")
    return [dict(r) for r in conn.execute(sql, (iso(end), iso(start)))]


def event_text(e: dict, *, at: datetime | None = None) -> str:
    when = (local(parse_iso(e["starts_at"])).strftime("%a %b %-d") + " (all day)" if e["all_day"]
            else local_time(e["starts_at"], at=at))
    extra = "".join(f", {x}" for x in (e["location"], e["status"] if e["status"] != "confirmed" else "") if x)
    return f"#{e['id']} {when} {e['title']}{extra}"


# --- people, commitments, decisions --------------------------------------------------------

def person_id(conn, name: str) -> int:
    name = (name or "").strip()
    if not name:
        raise StoreError("a commitment needs a person")
    row = conn.execute("SELECT id FROM people WHERE name=?", (name,)).fetchone()
    if row:
        return row["id"]
    return conn.execute("INSERT INTO people (name) VALUES (?)", (name,)).lastrowid


def add_commitment(conn, direction: str, person: str, what: str, due: str | None = None,
                   goal_id: int | None = None) -> dict:
    if direction not in ("i_owe", "they_owe"):
        raise StoreError("direction must be i_owe or they_owe")
    if not (what or "").strip():
        raise StoreError("a commitment needs 'what'")
    goal_id = resolve_goal(conn, goal_id)
    pid = person_id(conn, person)
    due_at = iso(parse_when(due)) if due else None
    cur = conn.execute("INSERT INTO commitments (goal_id, direction, person_id, what, due_at, created_at) "
                       "VALUES (?,?,?,?,?,?)", (goal_id, direction, pid, what.strip(), due_at, iso(now())))
    log(conn, "commit.add", goal_id, f"{direction} {person}: {what}", f"commit:{cur.lastrowid}")
    touch(conn, goal_id)
    conn.commit()
    return {"id": cur.lastrowid, "goal_id": goal_id, "due_at": due_at,
            "due_local": local_time(due_at) if due_at else None}


def commitments(conn, status: str = "open") -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT c.*, p.name AS person FROM commitments c LEFT JOIN people p ON p.id=c.person_id "
        "WHERE c.status=? ORDER BY c.due_at IS NULL, c.due_at", (status,))]


def close_commitment(conn, commitment_id: int, status: str = "done") -> dict:
    if status not in ("done", "dropped"):
        raise StoreError("status must be done or dropped")
    row = conn.execute("SELECT * FROM commitments WHERE id=?", (commitment_id,)).fetchone()
    if row is None:
        raise StoreError(f"no commitment #{commitment_id}")
    conn.execute("UPDATE commitments SET status=?, closed_at=? WHERE id=?", (status, iso(now()), commitment_id))
    log(conn, f"commit.{status}", row["goal_id"], row["what"], f"commit:{commitment_id}")
    touch(conn, row["goal_id"])
    conn.commit()
    return {"id": commitment_id, "status": status}


def commitment_text(c: dict) -> str:
    if c["direction"] == "i_owe":
        return f"you owe {c['person']}: {c['what']}"
    return f"{c['person']} owes you: {c['what']}"


def add_decision(conn, title: str, choice: str = "", rationale: str = "", review_in: str | None = None,
                 goal_id: int | None = None) -> dict:
    if not (title or "").strip():
        raise StoreError("a decision needs a title")
    goal_id = resolve_goal(conn, goal_id)
    review_at = iso(parse_when(review_in)) if review_in else None
    cur = conn.execute("INSERT INTO decisions (goal_id, title, choice, rationale, review_at, created_at) "
                       "VALUES (?,?,?,?,?,?)", (goal_id, title.strip(), choice or "", rationale or "",
                                                review_at, iso(now())))
    log(conn, "decision.add", goal_id, f"{title}: {choice}", f"decision:{cur.lastrowid}")
    touch(conn, goal_id)
    conn.commit()
    return {"id": cur.lastrowid, "goal_id": goal_id, "review_at": review_at,
            "review_local": local_time(review_at) if review_at else None}


def review_decision(conn, decision_id: int, verdict: str, choice: str = "", review_in: str | None = None) -> dict:
    """keep (optionally with a next review date) or revise (with the new choice)."""
    if verdict not in ("keep", "revise"):
        raise StoreError("verdict must be keep or revise")
    row = conn.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
    if row is None:
        raise StoreError(f"no decision #{decision_id}")
    review_at = iso(parse_when(review_in)) if review_in else None
    new_choice = choice or row["choice"]
    conn.execute("UPDATE decisions SET status=?, choice=?, review_at=?, notified_at=NULL WHERE id=?",
                 ("open" if review_at else ("kept" if verdict == "keep" else "revised"),
                  new_choice, review_at, decision_id))
    log(conn, f"decision.{verdict}", row["goal_id"], new_choice, f"decision:{decision_id}")
    touch(conn, row["goal_id"])
    conn.commit()
    return {"id": decision_id, "verdict": verdict, "choice": new_choice, "review_at": review_at}


# --- tap cards -----------------------------------------------------------------------------

# Typed actions a card may carry. The plugin performs these itself; never an arbitrary tool call.
ACTIONS = {
    "reschedule": "move a pending follow-up/reminder: {type, followup_id, when, text?}",
    "schedule": "schedule a new follow-up under the card's goal: {type, text, when}",
    "goal_status": "set a goal's status: {type, goal_id, status: done|paused|dropped|active}",
}
REASK_AFTER = timedelta(hours=1)


def _options(options) -> list[str]:
    if isinstance(options, str):
        options = [o.strip() for o in re.split(r"[,/|]", options)]
    opts = [str(o).strip() for o in (options or []) if str(o).strip()]
    return opts or ["yes", "no"]


def _action_when(when: str) -> str:
    """Durations ('in 1m') count from when the action runs; clock times ('tomorrow 07:00') are fixed
    now, so a card applied after midnight doesn't slide a day."""
    parse_when(when)
    return when if _DURATION.fullmatch((when or "").strip().lower()) else iso(parse_when(when))


def validate_action(conn, action) -> dict:
    if not action:
        return {}
    if isinstance(action, str):
        try:
            action = json.loads(action)
        except ValueError:
            raise StoreError("action must be an object like {\"type\": \"reschedule\", ...}") from None
    kind = action.get("type")
    if kind not in ACTIONS:
        raise StoreError(f"action type must be one of {', '.join(ACTIONS)} (or no action)")
    if kind == "reschedule":
        f = get_followup(conn, int(action.get("followup_id") or 0))
        if f is None or f["status"] != "pending":
            raise StoreError("reschedule needs the id of a pending follow-up (see ha_status)")
        out = {"type": kind, "followup_id": f["id"], "when": _action_when(action.get("when", ""))}
        if (action.get("text") or "").strip():
            out["text"] = action["text"].strip()
        return out
    if kind == "schedule":
        if not (action.get("text") or "").strip():
            raise StoreError("schedule needs text")
        return {"type": kind, "text": action["text"].strip(), "when": _action_when(action.get("when", ""))}
    if get_goal(conn, int(action.get("goal_id") or 0)) is None:
        raise StoreError("goal_status needs a valid goal_id")
    if action.get("status") not in GOAL_STATUSES:
        raise StoreError(f"goal_status needs status: {', '.join(GOAL_STATUSES)}")
    return {"type": kind, "goal_id": int(action["goal_id"]), "status": action["status"]}


def add_card(conn, question: str, recommended: str, *, options=None, stakes: str = "low",
             goal_id: int | None = None, action=None, when: str = "now") -> dict:
    if not (question or "").strip():
        raise StoreError("a card needs a question")
    if stakes not in STAKES:
        raise StoreError(f"stakes must be one of {', '.join(STAKES)}")
    opts = _options(options)
    rec = match_option(opts, recommended)
    if rec is None:
        raise StoreError(f"recommended must be one of the options: {' / '.join(opts)}")
    goal_id = resolve_goal(conn, goal_id)
    act = validate_action(conn, action)
    due = parse_when(when)
    cur = conn.execute("INSERT INTO cards (goal_id, question, options, recommended, stakes, action, due_at, "
                       "created_at) VALUES (?,?,?,?,?,?,?,?)",
                       (goal_id, question.strip(), json.dumps(opts), rec, stakes, json.dumps(act),
                        iso(due), iso(now())))
    log(conn, "card.add", goal_id, question.strip(), f"card:{cur.lastrowid}")
    touch(conn, goal_id)
    conn.commit()
    return {"id": cur.lastrowid, "goal_id": goal_id, "options": opts, "recommended": rec,
            "stakes": stakes, "ask_at": local_time(due)}


def get_card(conn, card_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
    if row is None:
        return None
    card = dict(row)
    card["options"] = json.loads(card["options"] or "[]")
    card["action"] = json.loads(card["action"] or "{}")
    return card


def open_cards(conn, *, asked_only: bool = False) -> list[dict]:
    sql = "SELECT id FROM cards WHERE status='open'" + (" AND asked_at IS NOT NULL" if asked_only else "")
    return [get_card(conn, r["id"]) for r in conn.execute(sql + " ORDER BY COALESCE(reasked_at, asked_at, due_at)")]


def match_option(options: list[str], text: str) -> str | None:
    """'Yes', 'yes.', '1' -> the option it names; None if it names none."""
    t = re.sub(r"[\s.!]+$", "", (text or "").strip().lower())
    for o in options:
        if t == o.lower():
            return o
    if t.isdigit() and 1 <= int(t) <= len(options):
        return options[int(t) - 1]
    return None


def card_action(card: dict, at: datetime, timeout: timedelta = REASK_AFTER) -> str:
    """What the pulse should do with an open card right now: 'ask', 'wait', 'reask', 'apply' or 'hold'.

    Rule: ask; if unanswered after `timeout` (1h) re-ask once; if still unanswered `timeout` after
    the re-ask, apply the recommendation ONLY for low-stakes cards. Money, public and irreversible
    calls are never decided for the person: they stay held (one notice) until answered.
    """
    if card["status"] != "open":
        return "wait"
    if not card.get("asked_at"):
        due = card.get("due_at")
        return "ask" if not due or parse_iso(due) <= at else "wait"
    if not card.get("reasked_at"):
        return "reask" if at - parse_iso(card["asked_at"]) >= timeout else "wait"
    if at - parse_iso(card["reasked_at"]) < timeout:
        return "wait"
    if card["stakes"] == "low":
        return "apply"
    return "wait" if card.get("held_at") else "hold"


def apply_action(conn, card: dict) -> str:
    """Perform the card's typed action. Returns a short human result ('' when there is none)."""
    act = card.get("action") or {}
    kind = act.get("type")
    if kind == "reschedule":
        f = get_followup(conn, act["followup_id"])
        if f is None or f["status"] != "pending":
            return f"follow-up #{act['followup_id']} was no longer pending, so nothing moved"
        due = parse_when(act["when"])
        text = act.get("text") or f["text"]
        conn.execute("UPDATE followups SET due_at=?, text=? WHERE id=?", (iso(due), text, f["id"]))
        return f"moved \"{text}\" to {local_time(due)}"
    if kind == "schedule":
        f = add_followup(conn, card["goal_id"], act["text"], act["when"])
        return f"I'll remind you at {f['due_local']}"
    if kind == "goal_status":
        set_goal_status(conn, act["goal_id"], act["status"])
        g = get_goal(conn, act["goal_id"])
        return f"marked \"{g['title']}\" {act['status']}"
    return ""


def answer_card(conn, card_id: int, answer: str, by: str = "user", at: datetime | None = None) -> dict:
    """Record an answer (user, reaction or timeout). The action runs only if the answer is the
    recommended option. Raises StoreError for unknown cards, closed cards and non-options."""
    card = get_card(conn, int(card_id))
    if card is None:
        raise StoreError(f"no card #{card_id}")
    if card["status"] != "open":
        raise StoreError(f"card #{card_id} is already {card['status']} ({card['answer']})")
    choice = match_option(card["options"], answer)
    if choice is None:
        raise StoreError(f"card #{card_id} options are: {' / '.join(card['options'])}")
    at = at or now()
    result = apply_action(conn, card) if choice == card["recommended"] else ""
    status = "applied" if by == "timeout" else "answered"
    conn.execute("UPDATE cards SET status=?, answer=?, answered_by=?, answered_at=?, result=? WHERE id=?",
                 (status, choice, by, iso(at), result, card["id"]))
    log(conn, f"card.{status}", card["goal_id"], f"{choice} by {by}" + (f": {result}" if result else ""),
        f"card:{card['id']}", at=at)
    touch(conn, card["goal_id"], at)
    conn.commit()
    return {"id": card["id"], "answer": choice, "by": by, "result": result}


def options_json(options) -> str:
    return json.dumps(_options(options))


# --- what needs the user's attention (pre_llm_call today, the daily brief later) -------------

def attention(conn, at: datetime | None = None, horizon: timedelta = timedelta(hours=24)) -> dict:
    at = at or now()
    soon = at + horizon
    cards = open_cards(conn, asked_only=True)
    commits = [c for c in commitments(conn) if c["due_at"] and parse_iso(c["due_at"]) <= soon]
    decisions = [dict(r) for r in conn.execute(
        "SELECT * FROM decisions WHERE status='open' AND review_at IS NOT NULL AND review_at<=? ORDER BY review_at",
        (iso(soon),))]
    return {"cards": cards, "commitments": commits, "decisions": decisions}
