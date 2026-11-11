#!/usr/bin/env python3
"""midna-body: an MCP server (streamable HTTP, plain-JSON replies) that gives Hermes midna's body:
his Mac over Tailscale ssh, and search over midna's chat history archive. Stdlib only.

    midna_body.py serve [--host 127.0.0.1 --port 8791 --mac user@host --midna-db PATH ...]
    midna_body.py token [--token-file PATH]     create the bearer token once; print it

It runs as the midna user (it owns the ssh key and midna.db) as the systemd user service
`midna-body.service`, which `midna_hermes.py install` writes. Hermes connects to
http://127.0.0.1:<port>/mcp with `Authorization: Bearer <token>` and marks the server
`trust: untrusted`: tools annotated `readOnlyHint` (status, list, read, screenshot, clipboard
read, history) run on their own, every other tool (shell, file write, AppleScript, clipboard
write) asks the user first through Hermes's approval buttons. Every device call is appended to
<state>/midna-body.jsonl.
"""

from __future__ import annotations

import argparse
import base64
import hmac
import json
import os
import re
import secrets
import shlex
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

VERSION = "0.1.0"
PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
STATE = Path("~/.local/state/midna-hermes").expanduser()
DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8791,
    "mac": "huiyunlee@100.98.21.63",
    "midna_db": "~/.local/share/midna/midna.db",
    "token_file": str(STATE / "midna-body.token"),
    "log": str(STATE / "midna-body.jsonl"),
    "tz": "Asia/Seoul",
}
MAX_OUT = 30_000
INSTRUCTIONS = (
    "midna-body is Justin's own Mac (macOS, over Tailscale ssh) and the archive of his chats with "
    "midna before the move to Hermes (Oct 2 - Nov 2026). device_* tools act on the Mac: reads run "
    "directly, anything that changes the Mac asks him first. history_search finds what he or midna "
    "said in the archive; history_read shows the messages around a hit. New conversations are in "
    "Hermes's own session_search, not here.")


def log_call(cfg: dict, rec: dict) -> None:
    try:
        p = Path(cfg["log"]).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


# --- the Mac ----------------------------------------------------------------------------------

class MacError(Exception):
    pass


def ssh(cfg: dict, script: str, timeout: float) -> tuple[int, str, str]:
    """Run a zsh script on the Mac (login shell, so Homebrew's PATH is there); the script goes on
    stdin, so nothing needs quoting for ssh. Exit 255 = ssh itself failed (Mac unreachable)."""
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
           "-o", "ServerAliveCountMax=3", cfg["mac"], "exec /bin/zsh -l -s"]
    try:
        r = subprocess.run(cmd, input=script.encode(), capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise MacError(f"timed out after {timeout:.0f}s")
    out, err = r.stdout.decode(errors="replace"), r.stderr.decode(errors="replace")
    if r.returncode == 255 and not out:
        raise MacError("Mac unreachable over ssh: " + (err.strip()[-300:] or "exit 255"))
    return r.returncode, out, err


def _b64(s: str | bytes) -> str:
    return base64.b64encode(s.encode() if isinstance(s, str) else s).decode()


def _cap(s: str, n: int = MAX_OUT) -> str:
    return s if len(s) <= n else s[:n] + f"\n... [{len(s) - n} more characters cut]"


def _path(p: str) -> str:
    """A Mac path for the shell: ~ expands, everything else is quoted."""
    p = (p or "").strip()
    if not p:
        raise ValueError("path is required")
    if p == "~":
        return '"$HOME"'
    if p.startswith("~/"):
        return '"$HOME"/' + shlex.quote(p[2:])
    return shlex.quote(p)


def t_status(cfg, a):
    rc, out, err = ssh(cfg, "echo host: $(scutil --get ComputerName 2>/dev/null || hostname); "
                            "echo macOS: $(sw_vers -productVersion); echo up: $(uptime | sed 's/^ *//'); "
                            "echo power: $(pmset -g batt | tail -n +2 | head -1 | sed 's/^ *//'); "
                            "echo front: $(lsappinfo info -only name $(lsappinfo front) 2>/dev/null "
                            "| sed 's/.*=//'); echo idle_s: $(( $(ioreg -c IOHIDSystem | "
                            "awk '/HIDIdleTime/ {print $NF; exit}') / 1000000000 ))", 30)
    return out.strip() or err.strip()


def t_list(cfg, a):
    rc, out, err = ssh(cfg, f"ls -la {_path(a.get('path') or '~')}", 30)
    return _cap(out if rc == 0 else f"exit {rc}\n{out}{err}")


def t_read_file(cfg, a):
    n = max(1, min(int(a.get("max_bytes") or 100_000), 1_000_000))
    p = _path(a.get("path", ""))
    rc, out, err = ssh(cfg, f"test -f {p} || {{ echo 'not a file' >&2; exit 2; }}; "
                            f"echo size: $(stat -f %z {p}); head -c {n} {p}", 60)
    return _cap(out if rc == 0 else f"exit {rc}\n{err}{out}", n + 200)


def t_screenshot(cfg, a):
    width = max(400, min(int(a.get("max_width") or 1600), 3000))
    rc, out, err = ssh(cfg, "f=$(mktemp -t midna-shot).png; screencapture -x -t png \"$f\" && "
                            f"sips -Z {width} \"$f\" >/dev/null && base64 -i \"$f\"; r=$?; rm -f \"$f\"; exit $r", 60)
    if rc != 0 or not out.strip():
        raise MacError(f"screenshot failed (exit {rc}): {err.strip()[-300:]}")
    return {"image": out.strip().replace("\n", ""), "mime": "image/png",
            "text": "Screenshot of the Mac's main display (taken over ssh; if it shows only the wallpaper, "
                    "sshd lacks Screen Recording permission)."}


def t_clipboard_read(cfg, a):
    rc, out, err = ssh(cfg, "pbpaste", 20)
    return _cap(out) if rc == 0 else f"exit {rc}\n{err}"


def t_shell(cfg, a):
    cmd = a.get("command") or ""
    if not cmd.strip():
        raise ValueError("command is required")
    timeout = max(5, min(int(a.get("timeout") or 120), 570))
    cd = f"cd {_path(a['cwd'])} || exit 2\n" if a.get("cwd") else "cd \"$HOME\"\n"
    rc, out, err = ssh(cfg, cd + f"eval \"$(printf %s {_b64(cmd)} | base64 -D)\"", timeout + 5)
    return _cap(f"exit {rc}\n{out}" + (f"\n[stderr]\n{err}" if err.strip() else ""))


def t_write_file(cfg, a):
    p = _path(a.get("path", ""))
    op = ">>" if a.get("append") else ">"
    data = a.get("content", "")
    rc, out, err = ssh(cfg, f"mkdir -p \"$(dirname {p})\" && printf %s {_b64(data)} | base64 -D {op} {p} && "
                            f"echo wrote $(stat -f %z {p}) bytes", 60)
    return f"exit {rc}\n{out}{err}".strip()


def t_applescript(cfg, a):
    src = a.get("script") or ""
    if not src.strip():
        raise ValueError("script is required")
    rc, out, err = ssh(cfg, f"printf %s {_b64(src)} | base64 -D | osascript -", 120)
    return _cap(f"exit {rc}\n{out}" + (f"\n[stderr]\n{err}" if err.strip() else ""))


def t_clipboard_write(cfg, a):
    rc, out, err = ssh(cfg, f"printf %s {_b64(a.get('text', ''))} | base64 -D | pbcopy && echo copied", 20)
    return f"exit {rc}\n{out}{err}".strip()


# --- the chat archive (midna.db, read-only) -----------------------------------------------------

FTS_SYNTAX = re.compile(r'["*()]|\b(AND|OR|NOT|NEAR)\b')


def archive(cfg: dict) -> sqlite3.Connection:
    db = Path(cfg["midna_db"]).expanduser()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _when(cfg: dict, ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, ZoneInfo(cfg["tz"])).strftime("%Y-%m-%d %H:%M")


def _ms(day: str, cfg: dict, end: bool = False) -> int:
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ZoneInfo(cfg["tz"]))
    return int(d.timestamp() * 1000) + (86_400_000 if end else 0)


def fts_query(q: str) -> str:
    """Plain words become an AND of quoted terms; FTS5 syntax (quotes, OR, NEAR, *) passes as is."""
    if FTS_SYNTAX.search(q):
        return q
    return " ".join('"' + w.replace('"', "") + '"' for w in q.split())


def t_history_search(cfg, a):
    q = (a.get("query") or "").strip()
    if not q:
        raise ValueError("query is required")
    limit = max(1, min(int(a.get("limit") or 10), 50))
    where, params = ["messages_fts MATCH ?"], [fts_query(q)]
    role = {"justin": "user", "user": "user", "midna": "midna"}.get((a.get("role") or "").lower())
    if role:
        where.append("m.role = ?")
        params.append(role)
    if a.get("since"):
        where.append("m.created_at >= ?")
        params.append(_ms(a["since"], cfg))
    if a.get("until"):
        where.append("m.created_at < ?")
        params.append(_ms(a["until"], cfg, end=True))
    order = "m.created_at DESC" if a.get("newest_first") else "bm25(messages_fts)"
    sql = (f"SELECT m.id, m.role, m.created_at, m.conversation_id, "
           f"snippet(messages_fts, 0, '[', ']', ' … ', 40) AS snip FROM messages_fts "
           f"JOIN messages m ON m.id = messages_fts.rowid WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ?")
    with archive(cfg) as conn:
        try:
            rows = conn.execute(sql, [*params, limit]).fetchall()
        except sqlite3.OperationalError:  # bad FTS syntax from the model: fall back to plain words
            params[0] = fts_query(re.sub(r'["*()]', " ", q))
            rows = conn.execute(sql, [*params, limit]).fetchall()
    if not rows:
        return f"No archived messages match {q!r}."
    who = {"user": "Justin", "midna": "midna"}
    lines = [f"#{r['id']} {_when(cfg, r['created_at'])} {who.get(r['role'], r['role'])}: "
             f"{' '.join(r['snip'].split())}" for r in rows]
    return f"{len(rows)} match(es) ({cfg['tz']} times; history_read <id> for context):\n" + "\n".join(lines)


def t_history_read(cfg, a):
    mid = int(a.get("message_id") or 0)
    before = max(0, min(int(a.get("before") if a.get("before") is not None else 3), 30))
    after = max(0, min(int(a.get("after") if a.get("after") is not None else 3), 30))
    with archive(cfg) as conn:
        hit = conn.execute("SELECT conversation_id FROM messages WHERE id = ?", (mid,)).fetchone()
        if not hit:
            return f"No archived message #{mid}."
        conv = hit["conversation_id"]
        rows = conn.execute(
            "SELECT * FROM (SELECT id, role, created_at, text FROM messages WHERE conversation_id = ? AND id < ? "
            "ORDER BY id DESC LIMIT ?) UNION ALL SELECT id, role, created_at, text FROM messages WHERE id = ? "
            "UNION ALL SELECT * FROM (SELECT id, role, created_at, text FROM messages WHERE conversation_id = ? "
            "AND id > ? ORDER BY id LIMIT ?) ORDER BY id", (conv, mid, before, mid, conv, mid, after)).fetchall()
    who = {"user": "Justin", "midna": "midna"}
    out = [f"{'>> ' if r['id'] == mid else ''}#{r['id']} {_when(cfg, r['created_at'])} "
           f"{who.get(r['role'], r['role'])}:\n{_cap(r['text'], 4000)}" for r in rows]
    return _cap("\n\n".join(out))


# --- tool table -----------------------------------------------------------------------------------

def _schema(props: dict, required: tuple = ()) -> dict:
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


S, I, B = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
TOOLS = {
    "device_status": (t_status, True, "Mac status: name, macOS version, uptime, battery/power, frontmost app, "
                      "seconds since last input. Cheap; use it to check the Mac is reachable.", _schema({})),
    "device_list": (t_list, True, "List a directory on the Mac (ls -la). ~ is his home.",
                    _schema({"path": {**S, "description": "Directory; default ~"}})),
    "device_read_file": (t_read_file, True, "Read a text file on the Mac (first max_bytes, default 100000).",
                         _schema({"path": S, "max_bytes": I}, ("path",))),
    "device_screenshot": (t_screenshot, True, "Screenshot of the Mac's main display, scaled to max_width px.",
                          _schema({"max_width": I})),
    "device_clipboard_read": (t_clipboard_read, True, "Read the Mac's clipboard (text).", _schema({})),
    "device_shell": (t_shell, False, "Run a zsh command on the Mac (login shell, Homebrew PATH) and return exit "
                     "code, stdout and stderr. Default timeout 120 s, max 570; for longer work start it in the "
                     "background on the Mac and check back. Asks Justin first.",
                     _schema({"command": S, "cwd": {**S, "description": "Working directory; default ~"},
                              "timeout": I}, ("command",))),
    "device_write_file": (t_write_file, False, "Write (or append to) a file on the Mac; parent dirs are created. "
                          "Asks Justin first. Never write under ~/.config (it is his dotfiles repo).",
                          _schema({"path": S, "content": S, "append": B}, ("path", "content"))),
    "device_applescript": (t_applescript, False, "Run AppleScript on the Mac (osascript). Asks Justin first.",
                           _schema({"script": S}, ("script",))),
    "device_clipboard_write": (t_clipboard_write, False, "Put text on the Mac's clipboard. Asks Justin first.",
                               _schema({"text": S}, ("text",))),
    "history_search": (t_history_search, True,
                       "Full-text search of Justin's chats with midna before Hermes (archive, Oct 2 2026 on). "
                       "Plain words are ANDed; FTS5 syntax (\"exact phrase\", OR, prefix*) also works. "
                       "role: justin|midna. since/until: YYYY-MM-DD (Asia/Seoul). Returns ids with snippets.",
                       _schema({"query": S, "role": S, "since": S, "until": S, "limit": I, "newest_first": B},
                               ("query",))),
    "history_read": (t_history_read, True, "Full text of an archived message and the messages around it "
                     "(before/after, default 3 each) in the same conversation.",
                     _schema({"message_id": I, "before": I, "after": I}, ("message_id",))),
}


def tools_list() -> list[dict]:
    return [{"name": name, "description": desc, "inputSchema": schema,
             "annotations": {"readOnlyHint": ro, "destructiveHint": not ro, "openWorldHint": name.startswith("device_")}}
            for name, (_, ro, desc, schema) in TOOLS.items()]


def call_tool(cfg: dict, name: str, args: dict) -> dict:
    if name not in TOOLS:
        raise KeyError(name)
    fn = TOOLS[name][0]
    t0 = time.monotonic()
    rec = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tool": name,
           "args": {k: (v[:300] if isinstance(v, str) else v) for k, v in args.items()}}
    try:
        res = fn(cfg, args)
        rec["ok"] = True
        if isinstance(res, dict):
            content = [{"type": "image", "data": res["image"], "mimeType": res["mime"]},
                       {"type": "text", "text": res["text"]}]
        else:
            content = [{"type": "text", "text": res}]
        return {"content": content, "isError": False}
    except (MacError, ValueError, sqlite3.Error) as e:
        rec["ok"], rec["error"] = False, str(e)[:500]
        return {"content": [{"type": "text", "text": f"{name} failed: {e}"}], "isError": True}
    finally:
        rec["ms"] = int((time.monotonic() - t0) * 1000)
        log_call(cfg, rec)


# --- MCP over HTTP ---------------------------------------------------------------------------------

def handle_rpc(cfg: dict, msg: dict) -> dict | None:
    """One JSON-RPC message -> its response (None for notifications)."""
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None
    try:
        if method == "initialize":
            want = params.get("protocolVersion")
            result = {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "midna-body", "version": VERSION}, "instructions": INSTRUCTIONS}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": tools_list()}
        elif method == "tools/call":
            try:
                result = call_tool(cfg, params.get("name", ""), params.get("arguments") or {})
            except KeyError:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool {params.get('name')!r}"}}
        elif method in ("resources/list", "prompts/list"):
            result = {method.split("/")[0]: []}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    except Exception as e:  # never kill the server over one call
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}


def make_handler(cfg: dict, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"midna-body/{VERSION}"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # journald gets errors only
            pass

        def _send(self, code: int, body: bytes = b"", ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _authed(self) -> bool:
            got = self.headers.get("Authorization", "")
            return hmac.compare_digest(got.encode(), f"Bearer {token}".encode())

        def do_GET(self):
            if self.path == "/health":
                return self._send(200, b"ok\n", "text/plain")
            self._send(405 if self._authed() else 401)  # no server-to-client SSE stream

        def do_DELETE(self):
            self._send(200 if self._authed() else 401)

        def do_POST(self):
            if self.path.rstrip("/") != "/mcp":
                return self._send(404)
            if not self._authed():
                return self._send(401, b'{"error":"unauthorized"}')
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"null")
            except (ValueError, json.JSONDecodeError):
                return self._send(400, json.dumps({"jsonrpc": "2.0", "id": None,
                                                   "error": {"code": -32700, "message": "parse error"}}).encode())
            msgs = body if isinstance(body, list) else [body]
            out = [r for r in (handle_rpc(cfg, m) for m in msgs if isinstance(m, dict)) if r is not None]
            if not out:
                return self._send(202)
            self._send(200, json.dumps(out if isinstance(body, list) else out[0], ensure_ascii=False).encode())

    return Handler


def ensure_token(path: Path) -> str:
    if path.exists():
        return path.read_text().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok + "\n")
    return tok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=("serve", "token", "call"))
    ap.add_argument("tool", nargs="?", help="call: tool name")
    ap.add_argument("arguments", nargs="?", default="{}", help="call: JSON arguments")
    for k, v in DEFAULTS.items():
        ap.add_argument("--" + k.replace("_", "-"), type=type(v), default=v)
    a = ap.parse_args(argv)
    cfg = {k: getattr(a, k) for k in DEFAULTS}
    if a.command == "token":
        print(ensure_token(Path(cfg["token_file"]).expanduser()))
        return 0
    if a.command == "call":  # local check without HTTP: midna_body.py call history_search '{"query":"x"}'
        res = call_tool(cfg, a.tool, json.loads(a.arguments))
        print("\n".join(c.get("text", f"<{c['type']}>") for c in res["content"]))
        return 1 if res["isError"] else 0
    token = ensure_token(Path(cfg["token_file"]).expanduser())
    srv = ThreadingHTTPServer((cfg["host"], cfg["port"]), make_handler(cfg, token))
    srv.daemon_threads = True
    print(f"midna-body {VERSION} on http://{cfg['host']}:{cfg['port']}/mcp (mac {cfg['mac']})", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
