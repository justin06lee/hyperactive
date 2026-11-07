"""hyperactive's structured store: one SQLite file under <HERMES_HOME>/plugin-data/hyperactive/.

Pure stdlib on purpose: the cron pulse script opens the same file from a subprocess without
importing Hermes. Every row that makes the agent act carries a goal_id so actions trace to a goal.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

HORIZONS = ("life", "year", "quarter", "week")
STAKES = ("low", "money", "public", "irreversible")

SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
  id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES goals(id),
  horizon TEXT NOT NULL CHECK (horizon IN ('life','year','quarter','week')),
  title TEXT NOT NULL, why TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL, last_moved_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY, goal_id INTEGER REFERENCES goals(id), title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open', due_at TEXT, created_at TEXT NOT NULL, done_at TEXT);
CREATE TABLE IF NOT EXISTS people (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, notes TEXT DEFAULT '', last_contact_at TEXT);
CREATE TABLE IF NOT EXISTS commitments (
  id INTEGER PRIMARY KEY, goal_id INTEGER REFERENCES goals(id),
  direction TEXT NOT NULL CHECK (direction IN ('i_owe','they_owe')),
  person_id INTEGER REFERENCES people(id), what TEXT NOT NULL, due_at TEXT,
  status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY, goal_id INTEGER REFERENCES goals(id), title TEXT NOT NULL,
  choice TEXT DEFAULT '', rationale TEXT DEFAULT '', review_at TEXT,
  status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY, text TEXT NOT NULL, tags TEXT DEFAULT '', created_at TEXT NOT NULL);
-- Anything hyperactive will say on its own later. The pulse delivers rows whose due_at passed.
CREATE TABLE IF NOT EXISTS followups (
  id INTEGER PRIMARY KEY, goal_id INTEGER REFERENCES goals(id),
  kind TEXT NOT NULL DEFAULT 'followup', text TEXT NOT NULL, due_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, sent_at TEXT);
-- Tap cards: a question with a recommended answer and a timeout rule (see card_action()).
CREATE TABLE IF NOT EXISTS cards (
  id INTEGER PRIMARY KEY, goal_id INTEGER REFERENCES goals(id), question TEXT NOT NULL,
  options TEXT NOT NULL DEFAULT '[]', recommended TEXT NOT NULL,
  stakes TEXT NOT NULL DEFAULT 'low' CHECK (stakes IN ('low','money','public','irreversible')),
  status TEXT NOT NULL DEFAULT 'open', asked_at TEXT, reasked_at TEXT, answer TEXT,
  created_at TEXT NOT NULL);
-- Append-only trail of everything hyperactive did on its own, with the goal it served.
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, goal_id INTEGER, detail TEXT);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_when(when: str, *, base: datetime | None = None) -> datetime:
    """'in 2m' / '2h' / '3d' / ISO timestamp -> aware UTC datetime. Raises ValueError."""
    base = base or now()
    text = (when or "").strip().lower()
    m = re.fullmatch(r"(?:in\s+)?(\d+)\s*(m|min|mins|minutes?|h|hours?|d|days?)", text)
    if m:
        n, unit = int(m.group(1)), m.group(2)[0]
        return base + {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]
    dt = datetime.fromisoformat(when.strip())
    return dt if dt.tzinfo else dt.replace(tzinfo=base.astimezone().tzinfo)


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def log(conn, kind: str, goal_id: int | None, detail: str) -> None:
    conn.execute("INSERT INTO events (at, kind, goal_id, detail) VALUES (?,?,?,?)",
                 (iso(now()), kind, goal_id, detail))


# --- goals ---------------------------------------------------------------------------------

def add_goal(conn, title: str, horizon: str = "week", parent_id: int | None = None, why: str = "") -> int:
    if horizon not in HORIZONS:
        raise ValueError(f"horizon must be one of {', '.join(HORIZONS)}")
    if parent_id is not None and conn.execute("SELECT 1 FROM goals WHERE id=?", (parent_id,)).fetchone() is None:
        raise ValueError(f"no goal #{parent_id}")
    t = iso(now())
    cur = conn.execute("INSERT INTO goals (parent_id, horizon, title, why, created_at, last_moved_at) "
                       "VALUES (?,?,?,?,?,?)", (parent_id, horizon, title.strip(), why, t, t))
    log(conn, "goal.add", cur.lastrowid, title)
    conn.commit()
    return cur.lastrowid


def get_goal(conn, goal_id: int):
    return conn.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()


def goal_tree(conn) -> list[dict]:
    """Active goals as nested dicts, roots first (life -> week)."""
    rows = [dict(r) for r in conn.execute("SELECT * FROM goals WHERE status='active' ORDER BY id")]
    by_id = {r["id"]: {**r, "children": []} for r in rows}
    roots = []
    for r in by_id.values():
        parent = by_id.get(r["parent_id"])
        (parent["children"] if parent else roots).append(r)
    return roots


def render_tree(conn, *, max_lines: int = 40) -> str:
    lines: list[str] = []

    def walk(node, depth):
        lines.append(f"{'  ' * depth}- #{node['id']} [{node['horizon']}] {node['title']}")
        for child in node["children"]:
            walk(child, depth + 1)

    for root in goal_tree(conn):
        walk(root, 0)
    if not lines:
        return "(no goals yet)"
    return "\n".join(lines[:max_lines] + (["  ..."] if len(lines) > max_lines else []))


# --- follow-ups ----------------------------------------------------------------------------

def add_followup(conn, goal_id: int | None, text: str, when: str, kind: str = "followup") -> dict:
    if goal_id is not None and get_goal(conn, goal_id) is None:
        raise ValueError(f"no goal #{goal_id}")
    due = parse_when(when)
    cur = conn.execute("INSERT INTO followups (goal_id, kind, text, due_at, created_at) VALUES (?,?,?,?,?)",
                       (goal_id, kind, text.strip(), iso(due), iso(now())))
    log(conn, f"{kind}.schedule", goal_id, f"#{cur.lastrowid} due {iso(due)}: {text}")
    conn.commit()
    return {"id": cur.lastrowid, "goal_id": goal_id, "due_at": iso(due)}


def pending_followups(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM followups WHERE status='pending' ORDER BY due_at")]


def add_memory(conn, text: str, tags: str = "") -> int:
    cur = conn.execute("INSERT INTO memories (text, tags, created_at) VALUES (?,?,?)",
                       (text.strip(), tags, iso(now())))
    conn.commit()
    return cur.lastrowid


# --- tap cards -----------------------------------------------------------------------------

REASK_AFTER = timedelta(hours=1)


def card_action(card: dict, at: datetime) -> str:
    """What the pulse should do with an open card right now: 'wait', 'reask', 'apply' or 'hold'.

    Rule: ask; if unanswered after 1h re-ask once; if still unanswered 1h after the re-ask,
    apply the recommendation ONLY for low-stakes cards. Money, public and irreversible calls are
    never decided for the person: they stay held until answered.
    """
    if card["status"] != "open" or not card.get("asked_at"):
        return "wait"
    asked = datetime.fromisoformat(card["asked_at"])
    if not card.get("reasked_at"):
        return "reask" if at - asked >= REASK_AFTER else "wait"
    if at - datetime.fromisoformat(card["reasked_at"]) < REASK_AFTER:
        return "wait"
    return "apply" if card["stakes"] == "low" else "hold"


def options_json(options) -> str:
    return json.dumps(list(options or []))
