"""hyperactive's structured store: one SQLite file under <HERMES_HOME>/plugin-data/hyperactive/.

Pure stdlib on purpose: the cron pulse script opens the same file from a subprocess without
importing Hermes. Every row that makes the agent act carries a goal_id (an "Inbox" goal when the
caller had none) so every proactive message traces to a goal. Times are stored in UTC and
rendered in local time.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HORIZONS = ("life", "year", "quarter", "week")
STAKES = ("low", "money", "public", "irreversible")
GOAL_STATUSES = ("active", "done", "paused", "dropped")
INBOX = "Inbox"
SCHEMA_VERSION = 2

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


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return conn


def log(conn, kind: str, goal_id: int | None, detail: str, ref: str = "", *, at: datetime | None = None) -> int:
    cur = conn.execute("INSERT INTO events (at, kind, goal_id, ref, detail) VALUES (?,?,?,?,?)",
                       (iso(at or now()), kind, goal_id, ref, detail))
    return cur.lastrowid


# --- goals ---------------------------------------------------------------------------------

def add_goal(conn, title: str, horizon: str = "week", parent_id: int | None = None, why: str = "") -> int:
    if horizon not in HORIZONS:
        raise StoreError(f"horizon must be one of {', '.join(HORIZONS)}")
    if not (title or "").strip():
        raise StoreError("a goal needs a title")
    if parent_id is not None and get_goal(conn, parent_id) is None:
        raise StoreError(f"no goal #{parent_id}")
    t = iso(now())
    cur = conn.execute("INSERT INTO goals (parent_id, horizon, title, why, created_at, last_moved_at) "
                       "VALUES (?,?,?,?,?,?)", (parent_id, horizon, title.strip(), why or "", t, t))
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


def goal_line(conn, goal_id: int | None, extra: str = "") -> str:
    """The calm last line of every proactive message: which goal it serves."""
    g = get_goal(conn, goal_id)
    if g is None or (g["title"] == INBOX and g["parent_id"] is None):
        base = "goal: none yet (inbox)"
    else:
        base = f"goal: {g['title']} ({g['horizon']})"
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
        lines.append(f"{'  ' * depth}- #{node['id']} [{node['horizon']}] {node['title']}")
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


def add_memory(conn, text: str, tags: str = "") -> int:
    cur = conn.execute("INSERT INTO memories (text, tags, created_at) VALUES (?,?,?)",
                       (text.strip(), tags, iso(now())))
    conn.commit()
    return cur.lastrowid


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
