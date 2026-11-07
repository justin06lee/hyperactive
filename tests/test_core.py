"""Hermes-free tests for the store, the pulse and the tap-card rule: python -m pytest tests"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

import pulse  # noqa: E402
import store  # noqa: E402

T0 = datetime(2026, 11, 7, 17, 0, tzinfo=timezone.utc)


def test_goal_tree_and_followup_delivery(tmp_path):
    conn = store.connect(tmp_path / "hyperactive.db")
    year = store.add_goal(conn, "Win a hackathon prize", "year")
    week = store.add_goal(conn, "Open the catalog PR", "week", parent_id=year)
    assert "  - #2 [week] Open the catalog PR" in store.render_tree(conn)
    store.add_followup(conn, week, "Is the PR open?", "in 1m")
    conn.close()

    assert pulse.run(tmp_path, at=store.now()) == ""  # not due yet
    out = pulse.run(tmp_path, at=store.now() + timedelta(minutes=2))
    assert "goal #2 (week): Open the catalog PR" in out and "Is the PR open?" in out
    assert pulse.run(tmp_path, at=store.now() + timedelta(minutes=3)) == ""  # delivered once


def test_quiet_hours_defer_not_drop(tmp_path):
    (tmp_path / "pulse.json").write_text('{"quiet_hours": "00:00-23:59"}')
    conn = store.connect(tmp_path / "hyperactive.db")
    store.add_followup(conn, None, "ping", "in 1m")
    conn.close()
    later = store.now() + timedelta(minutes=5)
    assert pulse.run(tmp_path, at=later) == ""
    (tmp_path / "pulse.json").write_text('{"quiet_hours": ""}')
    assert "ping" in pulse.run(tmp_path, at=later)


def test_quiet_window_wraps_midnight():
    local = datetime(2026, 11, 7, 23, 30).astimezone()
    assert pulse.in_quiet_hours("22:00-07:00", local)
    assert not pulse.in_quiet_hours("22:00-07:00", local.replace(hour=12))


def card(stakes, asked, reasked=None):
    return {"status": "open", "stakes": stakes, "asked_at": asked.isoformat(),
            "reasked_at": reasked.isoformat() if reasked else None}


def test_card_reasks_after_an_hour_then_applies_low_stakes_only():
    h = timedelta(hours=1)
    assert store.card_action(card("low", T0), T0 + h / 2) == "wait"
    assert store.card_action(card("low", T0), T0 + h) == "reask"
    assert store.card_action(card("low", T0, T0 + h), T0 + h * 1.5) == "wait"
    assert store.card_action(card("low", T0, T0 + h), T0 + 2 * h) == "apply"
    for stakes in ("money", "public", "irreversible"):
        assert store.card_action(card(stakes, T0, T0 + h), T0 + 10 * h) == "hold"


def test_parse_when():
    assert store.parse_when("in 2h", base=T0) == T0 + timedelta(hours=2)
    assert store.parse_when("3d", base=T0) == T0 + timedelta(days=3)
    assert store.parse_when("2026-11-08T09:00:00+00:00") == datetime(2026, 11, 8, 9, tzinfo=timezone.utc)
