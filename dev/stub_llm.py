"""A scripted stand-in for an LLM, so the scratch Hermes runs real agent turns with no API key.

OpenAI-compatible /v1/chat/completions on 127.0.0.1. It is NOT a model: it pattern-matches.
  - "I want <goal> by <when>" or "goal: <title>" -> ha_goal_add(week goal, filed under the first
    [year] goal in the system prompt's objective tree, check-in in 1m)
  - lines "tool: <name> <json args>"            -> calls those tools (dev-only, for seeding demos
                                                   through the real tool path)
  - a [hyperactive] note saying a card was answered -> confirms it in one line
  - after tool results                          -> a short reply built from the result
  - anything else                               -> echoes whether the hyperactive section is present
Usage: python dev/stub_llm.py [port]   (default 18766)
"""

import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _text(content) -> str:
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def _calls(calls: list, tools: list) -> dict:
    """Tool calls, through Hermes Tool Search's tool_call bridge when plugin tools are deferred."""
    direct = {t.get("function", {}).get("name") for t in tools}
    if not all(name in direct for name, _ in calls):
        calls = [("tool_call", {"calls": [{"name": n, "arguments": a} for n, a in calls]})]
    stamp = int(time.time() * 1000)
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": f"call_{stamp}_{i}", "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)}} for i, (name, args) in enumerate(calls)]}


def _reply_to_tool(messages: list) -> str:
    raw = _text(messages[-1].get("content"))
    try:
        result = json.loads(raw)
    except ValueError:
        return f"Done. {raw[:200]}"
    if isinstance(result, dict) and isinstance(result.get("results"), list):  # tool_call bridge
        result = result["results"][-1] if result["results"] else {}
        result = result.get("result", result)
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except ValueError:
                pass
    if not isinstance(result, dict) or not result.get("success", True):
        return f"That didn't work: {(result or {}).get('error', raw[:200])}"
    if "goal_id" in result and "followup" in result:
        under = result.get("filed_under") or []
        where = f"Filed under \"{under[-1]}\"." if under else "Noted."
        return f"{where} I'll check in at {result['followup']['due_local']}."
    return "Done."


def decide(messages: list, tools: list) -> dict:
    if not messages:
        return {"role": "assistant", "content": "ok"}
    if messages[-1].get("role") == "tool":
        return {"role": "assistant", "content": _reply_to_tool(messages)}
    user = _text(messages[-1].get("content"))
    system = " ".join(_text(x.get("content")) for x in messages if x.get("role") == "system")

    answered = re.search(r'answers card (\d+) \("([^"]*)"\): ([^.]+)\. It is recorded(?: and done: ([^\n]*?))?\. Confirm', user)
    if answered:
        n, _q, choice, result = answered.groups()
        return {"role": "assistant", "content": f"Got it, {choice}." + (f" Done: {result}." if result else "")}

    directives = re.findall(r"^tool:\s*(\w+)\s+(\{.*\})\s*$", user, re.M)
    if directives:
        return _calls([(name, json.loads(args)) for name, args in directives], tools)

    said = user.split("[hyperactive]")[0]
    m = re.search(r"I want (.+?) by (\w+)", said, re.I) or re.search(r"goal:\s*(.+)", said, re.I)
    if m:
        title = m.group(1).strip().split("\n")[0][:120]
        title = ("Get " + title) if title.lower().startswith("the ") else title[0].upper() + title[1:]
        args = {"title": title, "horizon": "week", "follow_up_in": "in 1m",
                "follow_up_text": f"{title}: done yet? If not, what's in the way?"}
        year = re.search(r"#(\d+) \[year\]", system)
        if year:
            args["parent_id"] = int(year.group(1))
        return _calls([("ha_goal_add", args)], tools)

    seen = "hyperactive: the user's objective tree" in system
    return {"role": "assistant", "content": f"(stub model) hyperactive section in system prompt: {seen}"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._json({"object": "list", "data": [{"id": "stub", "object": "model", "owned_by": "local"}]})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        msg = decide(req.get("messages", []), req.get("tools") or [])
        finish = "tool_calls" if msg.get("tool_calls") else "stop"
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        if not req.get("stream"):
            return self._json({"id": "stub", "object": "chat.completion", "created": int(time.time()),
                               "model": "stub", "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                               "usage": usage})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        delta = {"role": "assistant", "content": msg.get("content")}
        if msg.get("tool_calls"):
            delta["tool_calls"] = [{**tc, "index": i} for i, tc in enumerate(msg["tool_calls"])]
        for chunk in ({"delta": delta, "finish_reason": None}, {"delta": {}, "finish_reason": finish}):
            payload = {"id": "stub", "object": "chat.completion.chunk", "created": int(time.time()),
                       "model": "stub", "choices": [{"index": 0, **chunk}]}
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
        self.wfile.write(b"data: " + json.dumps({"id": "stub", "object": "chat.completion.chunk", "model": "stub",
                                                  "choices": [], "usage": usage}).encode() + b"\n\ndata: [DONE]\n\n")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18766
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
