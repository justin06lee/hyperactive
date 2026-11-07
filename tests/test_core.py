"""Hermes-free tests for the store, the pulse and the tap-card rule: python -m pytest tests"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

import pulse  # noqa: E402
import store  # noqa: E402

T0 = datetime(2026, 11, 7, 17, 0, tzinfo=timezone.utc)
M = timedelta(minutes=1)
H = timedelta(hours=1)


@pytest.fixture
def db(tmp_path):
    conn = store.connect(tmp_path / "hyperactive.db")
    yield conn
    conn.close()


def later(minutes):
    return store.now() + timedelta(minutes=minutes)


def test_goal_tree_and_followup_delivery(tmp_path, db):
    year = store.add_goal(db, "Win a hackathon prize", "year")
    week = store.add_goal(db, "Open the catalog PR", "week", parent_id=year)
    assert "  - #2 [week] Open the catalog PR" in store.render_tree(db)
    store.add_followup(db, week, "Is the PR open?", "in 1m")

    assert pulse.run(tmp_path, at=store.now()) == ""  # not due yet
    out = pulse.run(tmp_path, at=later(2))
    assert out == "Is the PR open?\ngoal: Open the catalog PR (week)"
    assert pulse.run(tmp_path, at=later(3)) == ""  # delivered once


def test_followup_without_goal_goes_to_inbox(tmp_path, db):
    f = store.add_followup(db, None, "ping", "in 1m")
    assert store.get_goal(db, f["goal_id"])["title"] == store.INBOX
    assert pulse.run(tmp_path, at=later(2)).endswith("goal: none yet (inbox)")
    assert store.render_tree(db).splitlines()[-1].endswith("[week] Inbox")


def test_quiet_hours_defer_not_drop(tmp_path, db):
    (tmp_path / "pulse.json").write_text('{"quiet_hours": "00:00-23:59"}')
    store.add_followup(db, None, "ping", "in 1m")
    assert pulse.run(tmp_path, at=later(5)) == ""
    (tmp_path / "pulse.json").write_text('{"quiet_hours": ""}')
    assert "ping" in pulse.run(tmp_path, at=later(5))


def test_quiet_window_wraps_midnight():
    local = datetime(2026, 11, 7, 23, 30).astimezone()
    assert pulse.in_quiet_hours("22:00-07:00", local)
    assert not pulse.in_quiet_hours("22:00-07:00", local.replace(hour=12))


def card(stakes, asked, reasked=None, held=None):
    return {"status": "open", "stakes": stakes, "asked_at": asked.isoformat(),
            "reasked_at": reasked.isoformat() if reasked else None, "held_at": held}


def test_card_reasks_after_an_hour_then_applies_low_stakes_only():
    assert store.card_action(card("low", T0), T0 + H / 2) == "wait"
    assert store.card_action(card("low", T0), T0 + H) == "reask"
    assert store.card_action(card("low", T0, T0 + H), T0 + H * 1.5) == "wait"
    assert store.card_action(card("low", T0, T0 + H), T0 + 2 * H) == "apply"
    for stakes in ("money", "public", "irreversible"):
        assert store.card_action(card(stakes, T0, T0 + H), T0 + 10 * H) == "hold"
        assert store.card_action(card(stakes, T0, T0 + H, held="x"), T0 + 10 * H) == "wait"  # one notice


def test_low_card_flow_applies_typed_action(tmp_path, db):
    fit = store.add_goal(db, "Stay fit", "quarter")
    gym = store.add_followup(db, fit, "Gym at 6am", "in 1d", kind="reminder")
    target = (store.now() + timedelta(days=1, hours=1)).replace(microsecond=0)
    c = store.add_card(db, "Move gym to 7am?", "yes", goal_id=fit,
                       action={"type": "reschedule", "followup_id": gym["id"], "when": target.isoformat()})
    (tmp_path / "pulse.json").write_text('{"card_timeout": "1m"}')

    ask = pulse.run(tmp_path, at=later(0))
    assert ask.startswith("Move gym to 7am?  yes / no\nI'd go with yes.")
    assert ask.endswith("goal: Stay fit (quarter) · card 1")
    assert pulse.run(tmp_path, at=later(0.5)) == ""
    assert pulse.run(tmp_path, at=later(1.1)).startswith("Still open: Move gym to 7am?")
    applied = pulse.run(tmp_path, at=later(2.2))
    assert applied.startswith("No reply on \"Move gym to 7am?\", so I went with yes: moved \"Gym at 6am\" to")
    assert store.get_followup(db, gym["id"])["due_at"] == store.iso(target)
    assert store.get_card(db, c["id"])["answered_by"] == "timeout"
    assert pulse.run(tmp_path, at=later(10)) == ""


def test_money_card_is_held_never_applied(tmp_path, db):
    site = store.add_goal(db, "Keep the site online", "quarter")
    store.add_card(db, "Pay the $40 domain renewal?", "pay", options=["pay", "skip"], stakes="money",
                   goal_id=site, action={"type": "schedule", "text": "Renew the domain", "when": "in 1h"})
    (tmp_path / "pulse.json").write_text('{"card_timeout": "1m"}')
    assert "it's yours to decide. It involves money." in pulse.run(tmp_path, at=later(0))
    assert "It involves money, so it waits for you." in pulse.run(tmp_path, at=later(1.1))
    held = pulse.run(tmp_path, at=later(2.2))
    assert "I won't decide it for you" in held
    assert pulse.run(tmp_path, at=later(60)) == ""  # one hold notice, then silence
    assert store.get_card(db, 1)["status"] == "open"
    assert store.pending_followups(db) == []  # nothing applied
    r = store.answer_card(db, 1, "Pay.")
    assert r["answer"] == "pay" and r["result"].startswith("I'll remind you at")
    with pytest.raises(store.StoreError):
        store.answer_card(db, 1, "skip")  # already answered


def test_answer_that_is_not_recommended_records_only(db):
    fit = store.add_goal(db, "Stay fit", "quarter")
    store.add_card(db, "Drop the gym goal?", "yes", goal_id=fit,
                   action={"type": "goal_status", "goal_id": fit, "status": "dropped"})
    assert store.answer_card(db, 1, "2") == {"id": 1, "answer": "no", "by": "user", "result": ""}
    assert store.get_goal(db, fit)["status"] == "active"


def test_card_validation(db):
    with pytest.raises(store.StoreError):
        store.add_card(db, "Q?", "maybe")  # not an option
    with pytest.raises(store.StoreError):
        store.add_card(db, "Q?", "yes", action={"type": "run_shell", "cmd": "rm -rf /"})
    with pytest.raises(store.StoreError):
        store.add_card(db, "Q?", "yes", action={"type": "reschedule", "followup_id": 99, "when": "1h"})
    with pytest.raises(store.StoreError):
        store.add_card(db, "Q?", "yes", stakes="cheap")


def test_commitments_both_ways(tmp_path, db):
    pr = store.add_goal(db, "Ship catalog PR", "week")
    store.add_commitment(db, "i_owe", "Sam", "the deck", "in 1m", pr)
    store.add_commitment(db, "they_owe", "Lee", "the contract", "in 2m", pr)
    store.add_commitment(db, "they_owe", "Ana", "feedback")  # no due date: never pings
    out = pulse.run(tmp_path, at=later(3))
    assert "You owe Sam: the deck (due" in out and "Lee owes you: the contract (due" in out
    assert out.count("goal: Ship catalog PR (week)") == 2
    assert pulse.run(tmp_path, at=later(4)) == ""
    store.close_commitment(db, 1)
    assert [c["person"] for c in store.commitments(db)] == ["Lee", "Ana"]


def test_decision_review(tmp_path, db):
    store.add_decision(db, "Database", "SQLite", "one file, no server", "in 1m")
    out = pulse.run(tmp_path, at=later(2))
    assert "you decided \"Database\": SQLite because one file, no server." in out
    assert "/ha decision 1 keep" in out
    assert pulse.run(tmp_path, at=later(3)) == ""
    store.review_decision(db, 1, "keep", review_in="in 1m")  # look again later
    assert "Review time." in pulse.run(tmp_path, at=later(2))


def test_stale_goal_nudged_once_until_it_moves(tmp_path, db):
    (tmp_path / "pulse.json").write_text('{"stale_after": "2m"}')
    g = store.add_goal(db, "Ship catalog PR", "week")
    assert pulse.run(tmp_path, at=later(1)) == ""
    out = pulse.run(tmp_path, at=later(3))
    assert out.startswith("\"Ship catalog PR\" hasn't moved in 3 min.")
    assert pulse.run(tmp_path, at=later(4)) == ""  # nudged once
    store.touch(db, g, at=store.now() + timedelta(minutes=4))
    db.commit()
    assert pulse.run(tmp_path, at=later(5)) == ""
    assert "hasn't moved" in pulse.run(tmp_path, at=later(7))


def test_moving_a_week_goal_moves_its_parent(db):
    q = store.add_goal(db, "Win a prize", "quarter")
    w = store.add_goal(db, "Ship PR", "week", parent_id=q)
    db.execute("UPDATE goals SET last_moved_at='2026-01-01T00:00:00+00:00'")
    store.add_followup(db, w, "x", "1h")
    assert store.get_goal(db, q)["last_moved_at"] > "2026-11"


def test_max_items_caps_one_message(tmp_path, db):
    (tmp_path / "pulse.json").write_text('{"max_items": 2}')
    for i in range(3):
        store.add_followup(db, None, f"ping {i}", "in 1m")
    assert pulse.run(tmp_path, at=later(2)).count("ping") == 2
    assert pulse.run(tmp_path, at=later(3)).count("ping") == 1


def test_why_explains_last_message_with_goal_chain(tmp_path, db):
    year = store.add_goal(db, "Win a hackathon prize", "year")
    week = store.add_goal(db, "Ship catalog PR", "week", parent_id=year)
    store.add_followup(db, week, "Is the PR open?", "in 1m")
    pulse.run(tmp_path, at=later(2))
    why = pulse.explain(db)
    assert "sent: Is the PR open?" in why and "why:  follow-up #1" in why
    assert "goal: Ship catalog PR (week) < Win a hackathon prize (year)" in why


def test_parse_when():
    assert store.parse_when("in 2h", base=T0) == T0 + timedelta(hours=2)
    assert store.parse_when("3d", base=T0) == T0 + timedelta(days=3)
    assert store.parse_when("now", base=T0) == T0
    assert store.parse_when("2026-11-08T09:00:00+00:00") == datetime(2026, 11, 8, 9, tzinfo=timezone.utc)
    with pytest.raises(store.StoreError):
        store.parse_when("next tuesday-ish")


def test_match_option():
    assert store.match_option(["yes", "no"], " Yes! ") == "yes"
    assert store.match_option(["pay", "skip"], "2") == "skip"
    assert store.match_option(["yes", "no"], "yes please move it") is None
