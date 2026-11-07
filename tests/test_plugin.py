"""register() against a real Hermes PluginManager (pattern: hermes-agent
tests/hermes_cli/test_plugin_api_compat.py). Skipped when Hermes isn't importable."""

import shutil
from pathlib import Path

import pytest

hermes_plugins = pytest.importorskip("hermes_cli.plugins")
yaml = pytest.importorskip("hermes_yaml")

PLUGIN = Path(__file__).resolve().parents[1] / "plugin"


@pytest.fixture
def loaded(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    (home / "plugins").mkdir(parents=True)
    shutil.copytree(PLUGIN, home / "plugins" / "hyperactive", ignore=shutil.ignore_patterns("__pycache__"))
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": ["hyperactive"]}}))
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    manager = hermes_plugins.PluginManager()
    manager.discover_and_load()
    plugin = manager._plugins["hyperactive"]
    assert plugin.error is None and plugin.enabled
    return manager, plugin


def test_registrations_match_manifest(loaded):
    _manager, plugin = loaded
    manifest = yaml.safe_load((PLUGIN / "plugin.yaml").read_text())
    assert sorted(plugin.tools_registered) == sorted(manifest["provides_tools"])
    assert sorted(set(plugin.hooks_registered)) == sorted(manifest["provides_hooks"])
    assert "ha" in plugin.commands_registered


def test_plain_reply_answers_the_only_waiting_card(loaded):
    manager, plugin = loaded
    store = plugin.module.store
    conn = plugin.module._db()
    try:
        store.add_card(conn, "Pay the $40 domain renewal?", "pay", options=["pay", "skip"], stakes="money")
        conn.execute("UPDATE cards SET asked_at=?", (store.iso(store.now()),))
        conn.commit()
    finally:
        conn.close()

    [ctx] = [r for r in manager.invoke_hook("pre_llm_call", session_id="s", user_message="hello",
                                            conversation_history=[], is_first_turn=True, model="m",
                                            platform="cli") if r]
    assert "card 1 awaits an answer" in ctx["context"]

    [ctx] = [r for r in manager.invoke_hook("pre_llm_call", session_id="s", user_message="Skip.",
                                            conversation_history=[], is_first_turn=False, model="m",
                                            platform="cli") if r]
    assert "answers card 1" in ctx["context"] and ": skip." in ctx["context"]
    out = plugin.module._command(None, plugin.module._make_handlers(None), "why")
    assert out == "hyperactive hasn't messaged you yet."
    assert "card 1" not in plugin.module._command(None, plugin.module._make_handlers(None), "status")


def test_reaction_answers_recent_card(loaded):
    manager, plugin = loaded
    store = plugin.module.store
    conn = plugin.module._db()
    try:
        store.add_card(conn, "Move gym to 7am?", "yes")
        conn.execute("UPDATE cards SET asked_at=?", (store.iso(store.now()),))
        conn.commit()
    finally:
        conn.close()
    manager.invoke_hook("gateway_platform_event", platform="telegram", event_type="reaction",
                        payload={"emojis": ["👍"], "chat_id": "1", "message_id": "2", "thread_id": None})
    conn = plugin.module._db()
    try:
        card = store.get_card(conn, 1)
    finally:
        conn.close()
    assert (card["answer"], card["answered_by"]) == ("yes", "reaction")


def test_targeted_reply_picks_one_of_several_cards(loaded):
    manager, plugin = loaded
    store = plugin.module.store
    conn = plugin.module._db()
    try:
        store.add_card(conn, "Move gym to 7am?", "yes")
        store.add_card(conn, "Pay the $40 domain renewal?", "pay", options=["pay", "skip"], stakes="money")
        conn.execute("UPDATE cards SET asked_at=?", (store.iso(store.now()),))
        conn.commit()
    finally:
        conn.close()
    hook = dict(session_id="s", conversation_history=[], is_first_turn=False, model="m", platform="cli")
    [ctx] = [r for r in manager.invoke_hook("pre_llm_call", user_message="yes", **hook) if r]
    assert "answers card" not in ctx["context"]  # ambiguous: two cards wait, the model decides
    [ctx] = [r for r in manager.invoke_hook("pre_llm_call", user_message="card 2: skip", **hook) if r]
    assert "answers card 2" in ctx["context"]
