"""hyperactive as a Hermes memory provider (opt-in: `memory.provider: hyperactive` in config.yaml).

Hermes's built-in memory is two flat files with a small cap. This keeps memories as rows in the same
hyperactive.db: a kind, an importance (1-10), pins, supersede chains, archive, and FTS search.
- Pinned memories go into the system prompt (system_prompt_block), most important first, up to
  `memory_pinned_chars` (plugin setting).
- Each turn, the few unpinned memories that best match the user's message are recalled (prefetch).
- Tools: ha_remember (save / supersede / pin / archive) and ha_recall (search).
- Writes through Hermes's own memory tool are mirrored in, so switching providers loses nothing.

Without `memory.provider: hyperactive`, Hermes never activates this class and nothing here runs.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from agent.memory_provider import MemoryProvider, RecallStatus, is_trivial_prompt

try:
    from . import store
except ImportError:  # loaded as a top-level module
    import store  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

PINNED_CHARS = 12000
RECALL_LIMIT = 6

REMEMBER = {
    "name": "ha_remember",
    "description": (
        "Save something durable about the user to long-term memory: who they are, preferences, people, "
        "patterns, how to do recurring things. One fact per call, in plain words. If it replaces an older "
        "memory, pass supersedes (the old id, from ha_recall or the pinned list) and the old one is archived. "
        "pinned=true puts it in every conversation's system prompt: only for things that should always "
        "shape how you act. To change an existing memory: memory_id + pinned / importance / archive."),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The memory, self-contained."},
            "kind": {"type": "string", "enum": list(store.MEMORY_KINDS)},
            "importance": {"type": "integer", "minimum": 1, "maximum": 10, "description": "Default 5."},
            "pinned": {"type": "boolean"},
            "tags": {"type": "string", "description": "Optional comma-separated tags."},
            "supersedes": {"type": "integer", "description": "Id of the memory this one replaces."},
            "memory_id": {"type": "integer", "description": "To change an existing memory instead."},
            "archive": {"type": "string", "description": "With memory_id: archive it, giving the reason."},
        },
    },
}

RECALL = {
    "name": "ha_recall",
    "description": ("Search the user's long-term memory (full text). Use before asking the user something "
                    "they may have told you already. Returns ids for ha_remember supersedes."),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Words to look for."},
            "include_archived": {"type": "boolean", "description": "Also search superseded/archived memories."},
            "limit": {"type": "integer", "description": "Default 8."},
        },
        "required": ["query"],
    },
}


def memory_line(m: dict) -> str:
    return f"- [{m['kind']} #{m['id']}] {m['text']}"


class HyperactiveMemory(MemoryProvider):
    def __init__(self, pinned_chars: int = PINNED_CHARS):
        self._db_path: Path | None = None
        self._writes = True
        self._pinned_chars = pinned_chars
        self._last_recall = 0

    @property
    def name(self) -> str:
        return "hyperactive"

    def is_available(self) -> bool:
        return True  # local SQLite, nothing to configure

    def initialize(self, session_id: str, **kwargs) -> None:
        home = Path(kwargs.get("hermes_home") or "")
        data = home / "plugin-data" / "hyperactive"
        data.mkdir(parents=True, exist_ok=True)
        self._db_path = data / "hyperactive.db"
        # Subagents, cron runs and flushes read memory but don't mirror writes into it.
        self._writes = kwargs.get("agent_context", "primary") == "primary"

    def _db(self):
        if self._db_path is None:
            raise RuntimeError("hyperactive memory used before initialize()")
        return store.connect(self._db_path)

    # -- prompt ------------------------------------------------------------------------------

    def system_prompt_block(self) -> str:
        try:
            conn = self._db()
            try:
                pins = store.pinned_memories(conn)
            finally:
                conn.close()
        except Exception:
            logger.exception("hyperactive memory: pinned block failed")
            return ""
        if not pins:
            return ""
        lines, used = [], 0
        for m in pins:  # most important first, so the cap drops the least important
            line = memory_line(m)
            if used + len(line) > self._pinned_chars:
                lines.append(f"- ({len(pins) - len(lines)} more pinned memories; ha_recall finds them)")
                break
            lines.append(line)
            used += len(line) + 1
        return ("## What you know about the user (pinned memories)\n"
                "Durable facts and standing instructions. Keep them current with ha_remember "
                "(supersedes=<id> when one changes).\n" + "\n".join(lines))

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        self._last_recall = 0
        if is_trivial_prompt(query):
            return ""
        try:
            conn = self._db()
            try:
                found = [m for m in store.search_memories(conn, query, RECALL_LIMIT + 4) if not m["pinned"]]
            finally:
                conn.close()
        except Exception:
            logger.exception("hyperactive memory: recall failed")
            return ""
        found = found[:RECALL_LIMIT]
        self._last_recall = len(found)
        if not found:
            return ""
        return "## Possibly relevant memories (hyperactive)\n" + "\n".join(memory_line(m) for m in found)

    def recall_status(self):
        return RecallStatus(provider_label="hyperactive", count=self._last_recall) if self._last_recall else None

    # -- tools ---------------------------------------------------------------------------------

    def get_tool_schemas(self) -> list[dict]:
        return [REMEMBER, RECALL]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        args = args or {}
        try:
            conn = self._db()
            try:
                if tool_name == "ha_remember":
                    return json.dumps({"success": True, **self._remember(conn, args)})
                if tool_name == "ha_recall":
                    rows = store.search_memories(conn, args.get("query", ""), int(args.get("limit") or 8),
                                                 include_archived=bool(args.get("include_archived")))
                    return json.dumps({"success": True, "memories": [
                        {k: m[k] for k in ("id", "text", "kind", "importance", "pinned", "archived_at", "superseded_by")}
                        for m in rows]})
            finally:
                conn.close()
        except store.StoreError as exc:
            return json.dumps({"success": False, "error": str(exc)})
        except Exception as exc:
            logger.exception("hyperactive memory tool failed")
            return json.dumps({"success": False, "error": f"hyperactive: {exc}"})
        return json.dumps({"success": False, "error": f"unknown tool {tool_name}"})

    @staticmethod
    def _remember(conn, a: dict) -> dict:
        if a.get("memory_id"):
            row = store.update_memory(conn, a["memory_id"], pinned=a.get("pinned"), importance=a.get("importance"),
                                      archive_reason=a.get("archive") or None)
            return {"id": row["id"], "pinned": bool(row["pinned"]), "importance": row["importance"],
                    "archived": bool(row["archived_at"])}
        mid = store.add_memory(conn, a.get("text", ""), a.get("tags", ""), kind=a.get("kind") or "context",
                               importance=a.get("importance") or 5, pinned=bool(a.get("pinned")),
                               supersedes=a.get("supersedes"), source="tool")
        return {"id": mid, "superseded": a.get("supersedes")}

    # -- mirror Hermes's built-in memory tool ----------------------------------------------------

    def on_memory_write(self, action: str, target: str, content: str, metadata=None) -> None:
        if not self._writes:
            return
        try:
            conn = self._db()
            try:
                previous = (metadata or {}).get("previous_content") or ""
                old = conn.execute("SELECT id FROM memories WHERE text=? AND archived_at IS NULL "
                                   "ORDER BY id DESC LIMIT 1", (previous.strip(),)).fetchone() if previous else None
                kind = "preference" if target == "user" else "context"
                if action == "add" and content.strip():
                    store.add_memory(conn, content, kind=kind, source=f"hermes:{target}")
                elif action == "replace" and content.strip():
                    store.add_memory(conn, content, kind=kind, source=f"hermes:{target}",
                                     supersedes=old["id"] if old else None)
                elif action == "remove" and old:
                    store.update_memory(conn, old["id"], archive_reason="removed with the memory tool")
            finally:
                conn.close()
        except Exception:
            logger.exception("hyperactive memory: mirroring a memory-tool write failed")
