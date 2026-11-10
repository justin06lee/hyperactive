"""Schema v3, memory, notes, calendar, quiet sources and the midna importer. Hermes-free except the
memory-provider tests, which skip when Hermes isn't importable: python -m pytest tests"""

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugin"))
sys.path.insert(0, str(ROOT / "dev"))

import import_midna  # noqa: E402
import pulse  # noqa: E402
import store  # noqa: E402

V2_SCHEMA_VERSION = 2


@pytest.fixture
def db(tmp_path):
    conn = store.connect(tmp_path / "hyperactive.db")
    yield conn
    conn.close()


# --- schema v3 -------------------------------------------------------------------------------

def test_v2_file_migrates_additively_and_v2_code_can_still_write(tmp_path):
    path = tmp_path / "hyperactive.db"
    old = sqlite3.connect(path)
    old.executescript(store.SCHEMA)  # what a v0.3.0 build creates
    old.execute(f"PRAGMA user_version={V2_SCHEMA_VERSION}")
    old.execute("INSERT INTO goals (horizon, title, created_at, last_moved_at) VALUES ('week','Ship it','2026-11-01T00:00:00+00:00','2026-11-01T00:00:00+00:00')")
    old.execute("INSERT INTO memories (text, created_at) VALUES ('likes tea', '2026-11-01T00:00:00+00:00')")
    old.commit()
    old.close()

    conn = store.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
    assert conn.execute("SELECT title, span, ext_ref FROM goals").fetchone()[:] == ("Ship it", None, None)
    m = conn.execute("SELECT kind, importance, pinned FROM memories").fetchone()
    assert m[:] == ("context", 5, 0)
    assert [r["id"] for r in store.search_memories(conn, "tea")] == [1]  # FTS rebuilt over old rows
    conn.close()

    # A v0.3.0 build opening it again: its inserts still work, and it resets user_version...
    old = sqlite3.connect(path)
    old.executescript(store.SCHEMA)
    old.execute(f"PRAGMA user_version={V2_SCHEMA_VERSION}")
    old.execute("INSERT INTO memories (text, created_at) VALUES ('likes coffee', '2026-11-01T00:00:00+00:00')")
    old.execute("INSERT INTO tasks (title, created_at) VALUES ('call', 't')")
    old.commit()
    old.close()
    # ...and v3 re-checks by column, idempotently, keeping the FTS index in sync.
    conn = store.connect(path)
    assert store.migrate(conn) == []
    assert [r["text"] for r in store.search_memories(conn, "coffee")] == ["likes coffee"]
    assert conn.execute("SELECT owner, status FROM tasks").fetchone()[:] == ("user", "open")


def test_years_and_month_horizons_keep_their_span(db):
    gid = store.add_goal(db, "Five-year foundation", "years")
    store.add_goal(db, "November push", "month", parent_id=gid)
    tree = store.render_tree(db)
    assert "[years] Five-year foundation" in tree and "[month] November push" in tree
    assert store.get_goal(db, gid)["horizon"] == "life"


# --- memory ----------------------------------------------------------------------------------

def test_memory_supersede_pin_search_and_fade(db):
    old = store.add_memory(db, "Works from Seoul", kind="context", importance=6)
    new = store.add_memory(db, "Works from Seoul at his uncle's company", kind="context", supersedes=old)
    assert db.execute("SELECT superseded_by, archive_reason FROM memories WHERE id=?", (old,)).fetchone()[:] == (new, "superseded")
    assert [m["id"] for m in store.search_memories(db, "Seoul uncle")] == [new]
    assert {m["id"] for m in store.search_memories(db, "Seoul", include_archived=True)} == {old, new}

    pin = store.add_memory(db, "Plain everyday words, no jargon", kind="preference", importance=9, pinned=True)
    assert [m["id"] for m in store.pinned_memories(db)] == [pin]

    weak = store.add_memory(db, "Had a sandwich", kind="context", importance=1)
    long_ago = store.now() + timedelta(days=60)
    assert store.fade_memories(db, long_ago) >= 1
    assert db.execute("SELECT archive_reason FROM memories WHERE id=?", (weak,)).fetchone()[0] == "faded"
    assert db.execute("SELECT archived_at FROM memories WHERE id=?", (pin,)).fetchone()[0] is None


def test_search_survives_fts_syntax(db):
    store.add_memory(db, 'He said "ship it" (NOT later)')
    assert store.search_memories(db, 'ship "it" AND (') != []
    assert store.search_memories(db, "!!!") == []


def test_quiet_sources_mute_imported_rows_only(tmp_path, db):
    gid = store.add_goal(db, "Mine", "week")
    store.add_followup(db, gid, "native", "in 1m")
    store.add_followup(db, gid, "imported", "in 1m")
    db.execute("UPDATE followups SET ext_ref='midna:wakeups:1' WHERE text='imported'")
    db.commit()
    (tmp_path / "pulse.json").write_text(json.dumps({"quiet_sources": "midna"}))
    out = pulse.run(tmp_path, at=store.now() + timedelta(minutes=2))
    assert "native" in out and "imported" not in out


# --- notes and calendar ----------------------------------------------------------------------

def test_notes_save_edit_search(db):
    n = store.save_note(db, "Gusteau lane charter", "Keep improving gusteau.\nOnly agents read it.", "gusteau")
    store.save_note(db, "Groceries", "eggs")
    store.save_note(db, "", "Keep improving gusteau. Weekly check.", note_id=n["id"])
    hits = store.search_notes(db, "gusteau charter")
    assert hits[0]["id"] == n["id"] and "Weekly check" in hits[0]["preview"]
    assert store.get_note(db, n["id"])["title"] == "Gusteau lane charter"
    assert [h["title"] for h in store.search_notes(db)][0] == "Gusteau lane charter"  # most recent first


def test_calendar_add_list_move_cancel(db):
    store.set_timezone("Asia/Seoul")
    try:
        e = store.add_event(db, "GSA meeting", "2026-11-12T09:30", "2026-11-12T10:30", location="Gangnam")
        store.add_event(db, "Workshop", "2026-11-13", all_day=True, status="tentative")
        day = lambda d: datetime(2026, 11, d, tzinfo=store._TZ)  # noqa: E731
        assert [x["title"] for x in store.events_between(db, day(12), day(14))] == ["GSA meeting", "Workshop"]
        assert "all day" in store.event_text(store.events_between(db, day(13), day(14))[0])
        store.update_event(db, e["id"], starts="2026-11-12T11:00", status="cancelled")
        assert [x["title"] for x in store.events_between(db, day(12), day(14))] == ["Workshop"]
        with pytest.raises(store.StoreError):
            store.add_event(db, "Backwards", "2026-11-12T10:00", "2026-11-12T09:00")
    finally:
        store.set_timezone("")


def test_brief_lists_todays_calendar(tmp_path, db):
    store.add_event(db, "Dentist", "in 1h")
    assert "Calendar today:\n- #1" in pulse.brief(tmp_path)


# --- the midna importer ----------------------------------------------------------------------

MIDNA_SCHEMA = """
CREATE TABLE goals (id INTEGER PRIMARY KEY, parent_id INTEGER, title TEXT NOT NULL, why TEXT, horizon TEXT NOT NULL,
  success TEXT, status TEXT NOT NULL DEFAULT 'active', target_at INTEGER, review_every_days INTEGER,
  last_reviewed_at INTEGER, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE goal_changes (id INTEGER PRIMARY KEY, goal_id INTEGER NOT NULL, change TEXT NOT NULL, reason TEXT NOT NULL, at INTEGER NOT NULL);
CREATE TABLE tasks (id INTEGER PRIMARY KEY, goal_id INTEGER, title TEXT NOT NULL, notes TEXT, status TEXT NOT NULL,
  owner TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 3, due_at INTEGER, estimate_min INTEGER,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, done_at INTEGER);
CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE commitments (id INTEGER PRIMARY KEY, direction TEXT NOT NULL, person_id INTEGER, counterparty TEXT,
  what TEXT NOT NULL, due_at INTEGER, goal_id INTEGER, source TEXT, status TEXT NOT NULL DEFAULT 'open',
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, closed_at INTEGER);
CREATE TABLE decisions (id INTEGER PRIMARY KEY, title TEXT NOT NULL, context TEXT, options TEXT, choice TEXT,
  reasoning TEXT, expected TEXT, confidence REAL, goal_id INTEGER, review_at INTEGER, outcome TEXT, lesson TEXT,
  decided_at INTEGER, reviewed_at INTEGER, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE facts (id INTEGER PRIMARY KEY, content TEXT NOT NULL, kind TEXT NOT NULL, importance INTEGER NOT NULL,
  pinned INTEGER NOT NULL DEFAULT 0, source TEXT, superseded_by INTEGER, created_at INTEGER NOT NULL,
  last_used_at INTEGER NOT NULL, use_count INTEGER NOT NULL DEFAULT 0, archived_at INTEGER, archive_reason TEXT);
CREATE TABLE fact_links (fact_id INTEGER NOT NULL, entity TEXT NOT NULL, entity_id INTEGER NOT NULL);
CREATE TABLE notes (id INTEGER PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL, tags TEXT,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE events (id INTEGER PRIMARY KEY, title TEXT NOT NULL, starts_at INTEGER NOT NULL, ends_at INTEGER,
  all_day INTEGER NOT NULL DEFAULT 0, location TEXT, notes TEXT, source TEXT NOT NULL DEFAULT 'midna',
  external_id TEXT, goal_id INTEGER, status TEXT NOT NULL DEFAULT 'confirmed', created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL);
CREATE TABLE wakeups (id INTEGER PRIMARY KEY, due_at INTEGER NOT NULL, reason TEXT NOT NULL, cron TEXT,
  kind TEXT NOT NULL, status TEXT NOT NULL, created_by TEXT NOT NULL, created_at INTEGER NOT NULL,
  fired_at INTEGER, joined_to INTEGER);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE messages (id INTEGER PRIMARY KEY, text TEXT);
"""


@pytest.fixture
def midna(tmp_path):
    path = tmp_path / "midna.db"
    c = sqlite3.connect(path)
    c.executescript(MIDNA_SCHEMA)
    t = 1793000000000  # ms
    future = int((datetime.now(timezone.utc) + timedelta(days=3)).timestamp() * 1000)
    c.executescript(f"""
    INSERT INTO settings VALUES ('timezone', '"Asia/Seoul"');
    INSERT INTO goals VALUES (1, NULL, 'Life', 'why', 'life', 's', 'active', NULL, NULL, NULL, {t}, {t});
    INSERT INTO goals VALUES (3, 2, 'Year one', '', 'year', '', 'abandoned', {t}, 30, NULL, {t}, {t});
    INSERT INTO goals VALUES (2, 1, '5 years', '', 'years', '', 'active', NULL, 90, NULL, {t}, {t});
    INSERT INTO goal_changes VALUES (1, 2, 'title: a -> b', 'renegotiated', {t});
    INSERT INTO tasks VALUES (1, 3, 'Pull-ups', NULL, 'doing', 'user', 2, NULL, 30, {t}, {t}, NULL);
    INSERT INTO tasks VALUES (2, NULL, 'Scrape', NULL, 'waiting', 'midna', 3, NULL, NULL, {t}, {t}, NULL);
    INSERT INTO commitments VALUES (1, 'mine', NULL, 'user', 'Daily digest', NULL, NULL, NULL, 'open', {t}, {t}, NULL);
    INSERT INTO commitments VALUES (2, 'theirs', NULL, 'Uncle', 'Company profile', {t}, 3, NULL, 'dropped', {t}, {t}, {t});
    INSERT INTO decisions VALUES (1, 'DB', 'ctx', 'a|b', 'SQLite', 'simple', 'fine', 0.8, NULL, {t}, NULL, NULL, {t}, NULL, {t}, {t});
    INSERT INTO facts VALUES (1, 'Lives in Seoul', 'context', 5, 0, NULL, 2, {t}, {t}, 1, {t}, 'superseded');
    INSERT INTO facts VALUES (2, 'Lives in Seoul, Gangnam', 'context', 6, 1, 'chat', NULL, {t}, {t}, 3, NULL, NULL);
    INSERT INTO fact_links VALUES (2, 'goal', 2), (2, 'task', 1), (2, 'person', 9);
    INSERT INTO notes VALUES (5, 'Five-year system', 'Ramadan plan', 'deen', {t}, {t});
    INSERT INTO events VALUES (1, 'GSA', {t}, NULL, 0, 'Seoul', NULL, 'midna', NULL, NULL, 'confirmed', {t}, {t});
    INSERT INTO wakeups VALUES (1, {t}, 'morning brief', '0 8 * * *', 'ritual', 'pending', 'system', {t}, NULL, NULL);
    INSERT INTO wakeups VALUES (2, {t}, 'evening review', '0 21 * * *', 'ritual', 'pending', 'system', {t}, NULL, NULL);
    INSERT INTO wakeups VALUES (3, {future}, 'Draft the Ramadan schedule from note #5', NULL, 'followup', 'pending', 'midna', {t}, NULL, NULL);
    INSERT INTO wakeups VALUES (4, {future}, 'Dhuhr 12:16', NULL, 'reminder', 'pending', 'midna', {t}, NULL, NULL);
    INSERT INTO wakeups VALUES (5, {t}, 'old', NULL, 'followup', 'done', 'midna', {t}, {t}, NULL);
    INSERT INTO wakeups VALUES (6, {t}, 'Past check', NULL, 'followup', 'pending', 'midna', {t}, NULL, NULL);
    INSERT INTO messages VALUES (1, 'hi');
    """)
    c.commit()
    c.close()
    return path


def run(argv) -> int:
    return import_midna.main([str(a) for a in argv])


def test_import_dry_run_writes_nothing_then_apply_is_idempotent(tmp_path, midna, capsys):
    target = tmp_path / "home" / "hyperactive.db"
    target.parent.mkdir()
    out = tmp_path / "out"
    assert run(["plan", midna, "--target", target, "--out", out]) == 0
    assert not target.exists()
    planned = json.loads((out / "plan.json").read_text())
    assert planned["problems"] == []
    assert planned["counts"]["goals"] == {"insert": 3}
    assert planned["counts"]["tasks"] == {"insert": 3}  # 2 tasks + midna's own promise to the user

    assert run(["apply", midna, "--target", target, "--out", out]) == 0
    conn = store.connect(target)
    g = {r["ext_ref"]: r for r in conn.execute("SELECT * FROM goals")}
    assert g["midna:goals:2"]["span"] == "years" and g["midna:goals:2"]["horizon"] == "life"
    assert g["midna:goals:3"]["parent_id"] == g["midna:goals:2"]["id"]  # parent imported first
    assert g["midna:goals:3"]["status"] == "dropped"
    assert conn.execute("SELECT owner FROM tasks WHERE ext_ref='midna:commitments:1'").fetchone()[0] == "agent"
    c = conn.execute("SELECT c.direction, p.name FROM commitments c JOIN people p ON p.id=c.person_id").fetchone()
    assert c[:] == ("they_owe", "Uncle")
    d = conn.execute("SELECT goal_id, confidence, status FROM decisions").fetchone()
    assert store.get_goal(conn, d["goal_id"])["title"] == store.INBOX and d["confidence"] == 0.8
    old, new = conn.execute("SELECT id, superseded_by, pinned FROM memories ORDER BY ext_ref").fetchall()
    assert old["superseded_by"] == new["id"] and new["pinned"] == 1
    assert conn.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0] == 2  # person link has no table
    assert conn.execute("SELECT kind, due_at IS NOT NULL FROM followups").fetchone()[:] == ("reminder", 1)
    conn.close()

    cron = json.loads((out / "plan.json").read_text())["cron"]
    assert [c["name"] for c in cron] == ["midna:2", "midna:3"]  # brief folded, past one-shot skipped
    note_id = store.connect(target).execute("SELECT id FROM notes").fetchone()[0]
    assert f"note #{note_id}" in cron[1]["prompt"] and cron[1]["one_shot"]
    script = (out / "cron-plan.sh").read_text()
    assert "--name midna:3" in script and "120 s" in script

    capsys.readouterr()
    assert run(["plan", midna, "--target", target, "--out", out]) == 0
    again = json.loads((out / "plan.json").read_text())["counts"]
    assert all(set(c) == {"unchanged"} for c in again.values()), again


def test_import_updates_changed_rows_and_keeps_hermes_rows(tmp_path, midna):
    target = tmp_path / "hyperactive.db"
    out = tmp_path / "out"
    run(["apply", midna, "--target", target, "--out", out])
    conn = store.connect(target)
    own = store.save_note(conn, "Written in Hermes", "x")["id"]
    conn.close()
    src = sqlite3.connect(midna)
    src.execute("UPDATE notes SET body='Ramadan plan v2' WHERE id=5")
    src.commit()
    src.close()

    run(["apply", midna, "--target", target, "--out", out])
    planned = json.loads((out / "plan.json").read_text())
    assert planned["counts"]["notes"] == {"update": 1}
    assert any("notes.body" in d and "v2" in d for d in planned["diffs"])

    assert run(["undo", "--target", target, "--apply"]) == 0
    conn = store.connect(target)
    assert [r[0] for r in conn.execute("SELECT id FROM notes")] == [own]
    assert conn.execute("SELECT COUNT(*) FROM goals WHERE ext_ref IS NOT NULL").fetchone()[0] == 0


def test_import_refuses_live_target(tmp_path, midna):
    live = tmp_path / ".hermes" / "plugin-data" / "hyperactive" / "hyperactive.db"
    live.parent.mkdir(parents=True)
    with pytest.raises(SystemExit, match="looks live"):
        run(["apply", midna, "--target", live, "--out", tmp_path / "out"])
    with pytest.raises(SystemExit, match="backup"):
        run(["apply", midna, "--target", live, "--out", tmp_path / "out", "--live"])


def test_snapshot_reads_source_read_only(tmp_path, midna):
    midna.chmod(0o400)
    dest = tmp_path / "snap.db"
    assert run(["snapshot", midna, dest]) == 0
    assert sqlite3.connect(dest).execute("SELECT COUNT(*) FROM goals").fetchone()[0] == 3


# --- the memory provider (needs Hermes) ------------------------------------------------------

def test_memory_provider(tmp_path):
    pytest.importorskip("agent.memory_provider")
    import memory

    home = tmp_path / "home"
    p = memory.HyperactiveMemory(pinned_chars=120)
    p.initialize("s1", hermes_home=str(home), platform="cli")
    conn = store.connect(home / "plugin-data" / "hyperactive" / "hyperactive.db")
    for i in range(4):
        store.add_memory(conn, f"Pinned rule number {i} about plain words", pinned=True, importance=9 - i)
    store.add_memory(conn, "Training for a muscle-up this year", kind="context")
    conn.close()

    block = p.system_prompt_block()
    assert "Pinned rule number 0" in block and "more pinned memories" in block  # capped, most important first
    assert "muscle-up" in p.prefetch("how is my muscle-up going?")
    assert p.recall_status().count == 1
    assert p.prefetch("ok") == ""

    saved = json.loads(p.handle_tool_call("ha_remember", {"text": "Prefers Korean BBQ", "kind": "preference"}))
    assert saved["success"]
    found = json.loads(p.handle_tool_call("ha_recall", {"query": "korean bbq"}))["memories"]
    assert found[0]["id"] == saved["id"]
    assert not json.loads(p.handle_tool_call("ha_remember", {"text": "x", "kind": "nope"}))["success"]

    p.on_memory_write("add", "user", "Wakes for Fajr")
    p.on_memory_write("replace", "user", "Wakes for Fajr at 5:37", {"previous_content": "Wakes for Fajr"})
    found = json.loads(p.handle_tool_call("ha_recall", {"query": "Fajr", "include_archived": True}))["memories"]
    assert {(m["archived_at"] is None) for m in found} == {True, False}
