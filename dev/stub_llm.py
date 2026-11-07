"""A scripted stand-in for an LLM, so the scratch Hermes runs real agent turns with no API key.

OpenAI-compatible /v1/chat/completions on 127.0.0.1. It is NOT a model: it pattern-matches.
  - "goal: <title>" in the user turn -> calls ha_goal_add(title, week, follow up in 1m)
  - after a tool result               -> a short text reply quoting the result
  - anything else                     -> echoes whether the hyperactive system section is present
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


def decide(messages: list, tools: list) -> dict:
    if not messages:
        return {"role": "assistant", "content": "ok"}
    last = messages[-1]
    if last.get("role") == "tool":
        return {"role": "assistant", "content": f"Done. {_text(last.get('content'))[:300]}"}
    user = _text(last.get("content"))
    m = re.search(r"goal:\s*(.+)", user, re.I)
    if m:
        args = {"title": m.group(1).strip().split("\n")[0][:120], "horizon": "week",
                "follow_up_in": "in 1m", "follow_up_text": "Checking in on this goal: what moved, what's next?"}
        name, arguments = "ha_goal_add", args
        if "ha_goal_add" not in {t.get("function", {}).get("name") for t in tools}:
            # Plugin tools are deferred behind Hermes Tool Search; go through its tool_call bridge.
            name, arguments = "tool_call", {"calls": [{"name": "ha_goal_add", "arguments": args}]}
        return {"role": "assistant", "content": None, "tool_calls": [{
            "id": f"call_{int(time.time() * 1000)}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}]}
    system = " ".join(_text(x.get("content")) for x in messages if x.get("role") == "system")
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
