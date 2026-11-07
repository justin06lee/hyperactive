"""hyperactive: a proactive layer for Hermes.

Keeps an objective tree, commitments, decisions and tap cards in a plugin-owned SQLite store and
wakes itself through one Hermes cron "pulse" job to deliver what is due. Registration only wires
things up; the pulse job is created lazily (first thing scheduled) or by `/ha setup`, never at
import, and healed on session start once it has been set up.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from hermes_constants import get_hermes_home
from plugins.plugin_storage import plugin_data_dir

from . import pulse, store

logger = logging.getLogger(__name__)

PLUGIN = "hyperactive"
PULSE_JOB_NAME = "hyperactive-pulse"
PULSE_SCRIPT = "hyperactive_pulse.py"
PULSE_SETTINGS = ("quiet_hours", "card_timeout", "stale_after", "max_items")
_HERE = Path(__file__).resolve().parent

_SHIM = '''# Installed by the hyperactive Hermes plugin. Re-created by `hermes hyperactive setup`.
import sys
sys.path.insert(0, {plugin_dir!r})
import pulse
sys.exit(pulse.main(["pulse", {data_dir!r}]))
'''


def _data_dir() -> Path:
    return plugin_data_dir(PLUGIN)


def _db():
    return store.connect(_data_dir() / "hyperactive.db")


def _ok(**payload) -> str:
    return json.dumps({"success": True, **payload})


def _err(msg: str) -> str:
    return json.dumps({"success": False, "error": msg})


def _tool(fn):
    """Tool handlers return JSON and never raise. StoreError text goes back to the model as-is."""
    def handler(args, **_kw):
        try:
            conn = _db()
            try:
                return _ok(**fn(conn, args or {}))
            finally:
                conn.close()
        except store.StoreError as exc:
            return _err(str(exc))
        except Exception as exc:
            logger.exception("hyperactive tool failed")
            return _err(f"hyperactive: {exc}")
    return handler


# --- the pulse cron job ------------------------------------------------------------------------

def _settings(ctx) -> dict:
    return {
        "every": ctx.get_config("pulse_every", default="every 5m") or "every 5m",
        "deliver": ctx.get_config("deliver", default="origin") or "origin",
        **{k: ctx.get_config(k, default=pulse.DEFAULTS[k]) for k in PULSE_SETTINGS},
    }


def ensure_pulse(ctx) -> dict:
    """Write pulse.json and the shim, and make sure exactly one pulse job exists with the
    configured schedule and delivery target. Safe to call often."""
    cfg = _settings(ctx)
    data_dir = _data_dir()
    (data_dir / "pulse.json").write_text(json.dumps({k: cfg[k] for k in PULSE_SETTINGS}, indent=2))
    scripts = get_hermes_home() / "scripts"  # the only place Hermes cron runs scripts from
    scripts.mkdir(parents=True, exist_ok=True)
    shim = _SHIM.format(plugin_dir=str(_HERE), data_dir=str(data_dir))
    if not (scripts / PULSE_SCRIPT).exists() or (scripts / PULSE_SCRIPT).read_text() != shim:
        (scripts / PULSE_SCRIPT).write_text(shim)

    listed = json.loads(ctx.dispatch_tool("cronjob_manage", {"action": "list"}) or "{}")
    if str(listed.get("error", "")).startswith("Unknown tool"):
        # `hermes hyperactive ...` runs outside a session, where the cron tool isn't loaded.
        return _ensure_pulse_cli(cfg["every"], cfg["deliver"])
    for job in listed.get("jobs", []):
        if job.get("name") != PULSE_JOB_NAME:
            continue
        job_id = job.get("job_id") or job.get("id")
        drift = {k: cfg[v] for k, v in (("schedule", "every"), ("deliver", "deliver"))
                 if job.get(k) != cfg[v]}
        if drift:
            ctx.dispatch_tool("cronjob_manage", {"action": "update", "job_id": job_id, **drift})
        return {"job_id": job_id, "created": False, "updated": sorted(drift)}
    created = json.loads(ctx.dispatch_tool("cronjob_manage", {
        "action": "create", "name": PULSE_JOB_NAME, "schedule": cfg["every"], "script": PULSE_SCRIPT,
        "no_agent": True, "deliver": cfg["deliver"], "catch_up": True,
    }) or "{}")
    if not created.get("success", True) or created.get("error"):
        raise RuntimeError(f"could not create pulse job: {created}")
    return {"job_id": created.get("job_id") or created.get("id"), "created": True}


def _hermes_cli(*args) -> subprocess.CompletedProcess:
    hermes = shutil.which("hermes") or sys.argv[0]
    return subprocess.run([hermes, *args], capture_output=True, text=True, timeout=120)


def _cli_pulse_job() -> dict | None:
    """Find the pulse job in `hermes cron list` output: {'id', 'schedule', 'deliver'}."""
    job = None
    for line in _hermes_cli("cron", "list").stdout.splitlines():
        head = re.match(r"\s+([0-9a-f]{8,})\s+\[", line)
        if head:
            job = {"id": head.group(1)}
            continue
        field = re.match(r"\s+(Name|Schedule|Deliver):\s+(.*\S)", line)
        if job is not None and field:
            job[field.group(1).lower()] = field.group(2)
            if job.get("name") == PULSE_JOB_NAME and "deliver" in job:
                return job
    return None


def _ensure_pulse_cli(every: str, deliver: str) -> dict:
    job = _cli_pulse_job()
    if job:
        edits = []
        if job.get("schedule") != every:
            edits += ["--schedule", every]
        if job.get("deliver") != deliver:
            edits += ["--deliver", deliver]
        if edits:
            _hermes_cli("cron", "edit", job["id"], *edits)
        return {"job_id": job["id"], "created": False, "updated": edits[::2]}
    made = _hermes_cli("cron", "create", every, "--name", PULSE_JOB_NAME, "--script", PULSE_SCRIPT,
                       "--no-agent", "--deliver", deliver, "--catch-up", "on")
    if made.returncode != 0:
        raise RuntimeError(f"could not create pulse job: {made.stderr.strip() or made.stdout.strip()}")
    return {"job": PULSE_JOB_NAME, "created": True}


def remove_pulse(ctx) -> str:
    """Remove the pulse job and shim. The data (goals, history) stays unless deleted by hand."""
    removed = []
    listed = json.loads(ctx.dispatch_tool("cronjob_manage", {"action": "list"}) or "{}")
    if str(listed.get("error", "")).startswith("Unknown tool"):
        job = _cli_pulse_job()
        if job and _hermes_cli("cron", "remove", job["id"]).returncode == 0:
            removed.append(f"cron job {job['id']}")
    else:
        for job in listed.get("jobs", []):
            if job.get("name") == PULSE_JOB_NAME:
                ctx.dispatch_tool("cronjob_manage", {"action": "remove", "job_id": job.get("job_id")})
                removed.append(f"cron job {job.get('job_id')}")
    shim = get_hermes_home() / "scripts" / PULSE_SCRIPT
    if shim.exists():
        shim.unlink()
        removed.append(str(shim))
    settings = _data_dir() / "pulse.json"
    if settings.exists():
        settings.unlink()  # also stops the session-start self-heal from recreating the job
    kept = _data_dir() / "hyperactive.db"
    return (f"removed: {', '.join(removed) or 'nothing'}. "
            f"Your goals and history are kept in {kept}; delete that file to erase them.")


# --- tools -------------------------------------------------------------------------------------

_GOAL_ID = {"type": "integer", "description": "Goal this serves (see the goal tree). Omit only if none fits: it goes to the Inbox."}

GOAL_ADD = {
    "name": "ha_goal_add",
    "description": ("Record one of the user's goals in their objective tree (life > year > quarter > week). "
                    "Use when the user states something they want to achieve. Link it to a parent goal when one "
                    "fits (see the goal tree in the system prompt). Optionally schedule a proactive check-in."),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "The goal, short and concrete."},
            "horizon": {"type": "string", "enum": list(store.HORIZONS), "description": "Time horizon."},
            "parent_id": {"type": "integer", "description": "Parent goal id, if any."},
            "why": {"type": "string", "description": "Why it matters to the user, in their words."},
            "follow_up_in": {"type": "string", "description": "Optional: when to check in, e.g. 'in 2h', '3d', or ISO time."},
            "follow_up_text": {"type": "string", "description": "What to ask at the check-in, self-contained."},
        },
        "required": ["title", "horizon"],
    },
}

FOLLOWUP = {
    "name": "ha_followup",
    "description": ("Schedule something to say to the user on your own later: a follow-up, reminder or nudge. "
                    "Delivered even if the user never messages again. Quiet hours defer it, never drop it."),
    "parameters": {
        "type": "object",
        "properties": {
            "goal_id": _GOAL_ID,
            "text": {"type": "string", "description": "The message, self-contained: it may be read hours later."},
            "when": {"type": "string", "description": "'in 30m', '2h', '1d', or ISO timestamp (local if no zone)."},
            "kind": {"type": "string", "enum": ["followup", "reminder", "nudge"]},
        },
        "required": ["text", "when"],
    },
}

CARD = {
    "name": "ha_card",
    "description": (
        "Ask the user a quick question with a recommended answer (a tap card), or record their answer. "
        "Ask: question + options + recommended + stakes. If unanswered it is re-asked once after an hour; "
        "an hour later the recommendation is applied ONLY when stakes is 'low'. Use stakes money, public or "
        "irreversible for anything that spends money, is visible to others, or can't be undone: those wait "
        "for the user forever. `action` is what the plugin itself does if the recommended answer wins: "
        "{type: reschedule, followup_id, when} | {type: schedule, text, when} | "
        "{type: goal_status, goal_id, status}. Answer: card_id + answer (when the user replies to a card)."),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "Short, phone-sized question."},
            "options": {"type": "array", "items": {"type": "string"}, "description": "2-4 one-word options. Default yes/no."},
            "recommended": {"type": "string", "description": "The option you recommend."},
            "stakes": {"type": "string", "enum": list(store.STAKES)},
            "goal_id": _GOAL_ID,
            "action": {"type": "object", "description": "Optional typed action, see above."},
            "when": {"type": "string", "description": "When to ask. Default now (next pulse)."},
            "card_id": {"type": "integer", "description": "To answer: the card's id."},
            "answer": {"type": "string", "description": "To answer: the option the user chose."},
        },
    },
}

COMMIT = {
    "name": "ha_commit",
    "description": ("Track a promise in either direction: i_owe (the user promised someone something) or "
                    "they_owe (someone promised the user). hyperactive brings it up when it's due. "
                    "To close one: commitment_id + status done|dropped."),
    "parameters": {
        "type": "object",
        "properties": {
            "direction": {"type": "string", "enum": ["i_owe", "they_owe"]},
            "person": {"type": "string", "description": "Who it's with, by first name."},
            "what": {"type": "string", "description": "The thing promised, e.g. 'the deck'."},
            "due": {"type": "string", "description": "When it's due: 'in 2d', ISO time. Optional."},
            "goal_id": _GOAL_ID,
            "commitment_id": {"type": "integer"},
            "status": {"type": "string", "enum": ["done", "dropped"]},
        },
    },
}

DECIDE = {
    "name": "ha_decide",
    "description": ("Record a decision the user made, with its rationale and a date to review it; hyperactive "
                    "asks 'still right?' on that date. To review one: decision_id + verdict keep|revise "
                    "(+ new choice, + review_in to look again later)."),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "What was decided about, e.g. 'Database'."},
            "choice": {"type": "string", "description": "What they chose, e.g. 'SQLite'."},
            "rationale": {"type": "string", "description": "Why, in their words."},
            "review_in": {"type": "string", "description": "When to revisit: '2w', '30d', ISO time."},
            "goal_id": _GOAL_ID,
            "decision_id": {"type": "integer"},
            "verdict": {"type": "string", "enum": ["keep", "revise"]},
        },
    },
}

STATUS = {
    "name": "ha_status",
    "description": "Show the user's goal tree and everything hyperactive is tracking or has scheduled.",
    "parameters": {"type": "object", "properties": {}},
}

TOOLS = (GOAL_ADD, FOLLOWUP, CARD, COMMIT, DECIDE, STATUS)


def _status_text(conn) -> str:
    lines = ["Goals:", store.render_tree(conn)]
    cards = store.open_cards(conn)
    if cards:
        lines += ["", "Open cards:"] + [
            f"- card {c['id']}: {c['question']}  {' / '.join(c['options'])} (rec. {c['recommended']}, {c['stakes']})"
            for c in cards]
    commits = store.commitments(conn)
    if commits:
        lines += ["", "Commitments:"] + [
            f"- #{c['id']} {store.commitment_text(c)}" + (f", due {store.local_time(c['due_at'])}" if c["due_at"] else "")
            for c in commits]
    decisions = conn.execute("SELECT * FROM decisions WHERE status='open' ORDER BY review_at").fetchall()
    if decisions:
        lines += ["", "Decisions:"] + [
            f"- #{d['id']} {d['title']}: {d['choice']}" + (f", review {store.local_time(d['review_at'])}" if d["review_at"] else "")
            for d in decisions]
    pend = store.pending_followups(conn)
    lines += ["", "Scheduled:"] + ([f"- #{f['id']} {f['kind']} {store.local_time(f['due_at'])} (goal #{f['goal_id']}): {f['text']}"
                                    for f in pend] or ["(nothing)"])
    return "\n".join(lines)


def _make_handlers(ctx):
    def scheduled(row: dict) -> dict:
        row["pulse"] = ensure_pulse(ctx)
        return row

    def goal_add(conn, a):
        gid = store.add_goal(conn, a.get("title", ""), a.get("horizon", "week"), a.get("parent_id"), a.get("why", ""))
        out = {"goal_id": gid, "filed_under": [g["title"] for g in store.goal_chain(conn, gid)[1:]]}
        if a.get("follow_up_in"):
            text = a.get("follow_up_text") or f"How is \"{a['title']}\" going? What moved, and what's next?"
            out["followup"] = scheduled(store.add_followup(conn, gid, text, a["follow_up_in"]))
        return out

    def followup(conn, a):
        return scheduled(store.add_followup(conn, a.get("goal_id"), a.get("text", ""), a.get("when", ""),
                                            a.get("kind", "followup")))

    def card(conn, a):
        if a.get("card_id"):
            return store.answer_card(conn, a["card_id"], a.get("answer", ""))
        return scheduled(store.add_card(conn, a.get("question", ""), a.get("recommended", ""),
                                        options=a.get("options"), stakes=a.get("stakes", "low"),
                                        goal_id=a.get("goal_id"), action=a.get("action"), when=a.get("when", "now")))

    def commit(conn, a):
        if a.get("commitment_id"):
            return store.close_commitment(conn, a["commitment_id"], a.get("status", "done"))
        row = store.add_commitment(conn, a.get("direction", ""), a.get("person", ""), a.get("what", ""),
                                   a.get("due"), a.get("goal_id"))
        return scheduled(row) if row["due_at"] else row

    def decide(conn, a):
        if a.get("decision_id"):
            return store.review_decision(conn, a["decision_id"], a.get("verdict", "keep"), a.get("choice", ""),
                                         a.get("review_in"))
        row = store.add_decision(conn, a.get("title", ""), a.get("choice", ""), a.get("rationale", ""),
                                 a.get("review_in"), a.get("goal_id"))
        return scheduled(row) if row["review_at"] else row

    def status(conn, _a):
        return {"status": _status_text(conn)}

    return {"ha_goal_add": goal_add, "ha_followup": followup, "ha_card": card, "ha_commit": commit,
            "ha_decide": decide, "ha_status": status}


# --- slash + CLI commands ----------------------------------------------------------------------

USAGE = """/ha                          goals, cards, commitments, schedule
/ha answer <card> <option>   answer a tap card
/ha why [event]              why hyperactive sent its last message
/ha decision <id> keep|revise [new choice]
/ha done <commitment>        close a commitment
/ha goal <horizon> <title> [--parent N] [--follow-up 'in 2h']
/ha followup <goal> <when> <text>
/ha setup | /ha uninstall"""


def _command(ctx, handlers, raw: str) -> str:
    try:
        argv = shlex.split(raw or "")
    except ValueError as exc:
        return f"hyperactive: {exc}"
    cmd, args = (argv[0], argv[1:]) if argv else ("status", [])
    try:
        if cmd == "setup":
            return f"hyperactive pulse: {ensure_pulse(ctx)}"
        if cmd == "uninstall":
            return remove_pulse(ctx)
        conn = _db()
        try:
            if cmd == "status":
                return _status_text(conn)
            if cmd == "why":
                return pulse.explain(conn, int(args[0]) if args else None)
            if cmd == "answer" and len(args) >= 2:
                r = store.answer_card(conn, int(args[0]), " ".join(args[1:]))
                return f"card {r['id']}: {r['answer']}." + (f" Done: {r['result']}." if r["result"] else "")
            if cmd == "decision" and len(args) >= 2:
                r = store.review_decision(conn, int(args[0]), args[1], " ".join(args[2:]))
                return f"decision {r['id']}: {r['verdict']} ({r['choice']})."
            if cmd == "done" and args:
                r = store.close_commitment(conn, int(args[0]))
                return f"commitment {r['id']}: done."
            if cmd == "goal" and len(args) >= 2:
                horizon, rest = args[0], args[1:]
                parent = follow = None
                if "--parent" in rest:
                    i = rest.index("--parent"); parent = int(rest[i + 1]); del rest[i:i + 2]
                if "--follow-up" in rest:
                    i = rest.index("--follow-up"); follow = rest[i + 1]; del rest[i:i + 2]
                out = handlers["ha_goal_add"](conn, {"title": " ".join(rest), "horizon": horizon,
                                                     "parent_id": parent, "follow_up_in": follow})
                msg = f"goal {out['goal_id']}: {' '.join(rest)}"
                return msg + (f"; check-in at {out['followup']['due_local']}" if follow else "")
            if cmd == "followup" and len(args) >= 3:
                f = handlers["ha_followup"](conn, {"goal_id": int(args[0]), "when": args[1], "text": " ".join(args[2:])})
                return f"follow-up {f['id']} at {f['due_local']}"
        finally:
            conn.close()
    except (store.StoreError, ValueError, IndexError) as exc:
        return f"hyperactive: {exc}"
    return USAGE


def _setup_cli(subparser):
    subparser.add_argument("words", nargs=argparse.REMAINDER, help="same grammar as the /ha slash command")


# --- prompt context ----------------------------------------------------------------------------

def _system_prompt_section(_info) -> str:
    try:
        conn = _db()
        try:
            tree = store.render_tree(conn, max_lines=25)
        finally:
            conn.close()
    except Exception:
        return ""
    return ("## hyperactive: the user's objective tree\n"
            "You are proactive. Tie what you do to one of these goals. Record goals (ha_goal_add), promises "
            "both ways (ha_commit) and decisions (ha_decide); schedule check-ins yourself (ha_followup) and "
            "ask quick calls as tap cards (ha_card) instead of waiting to be asked.\n" + tree)


def _attention_lines(conn, at) -> list[str]:
    att = store.attention(conn, at)
    lines = [f"- card {c['id']} awaits an answer: {c['question']} ({' / '.join(c['options'])}; "
             f"recommended {c['recommended']}, {c['stakes']} stakes)" for c in att["cards"][:3]]
    for c in att["commitments"][:3]:
        late = store.parse_iso(c["due_at"]) <= at
        lines.append(f"- commitment {c['id']}, {store.commitment_text(c)}: "
                     f"{'overdue since' if late else 'due'} {store.local_time(c['due_at'], at=at)}")
    lines += [f"- decision {d['id']} \"{d['title']}\" ({d['choice']}) is up for review"
              for d in att["decisions"][:2]]
    return lines


def pre_llm_call(user_message="", **_kw):
    """Before each turn: (1) if the user's whole message is one option of the only card awaiting an
    answer ("skip"), or names a card and an option ("2 skip"), answer it here, deterministically;
    (2) inject what needs attention, a few lines max."""
    try:
        text = user_message if isinstance(user_message, str) else ""
        conn = _db()
        try:
            at = store.now()
            notes = []
            awaiting = store.open_cards(conn, asked_only=True)
            targeted = re.fullmatch(r"\s*(?:card\s*)?(\d+)[\s:,]+(\S+)\s*", text, re.I)
            if targeted:  # "2 skip" / "card 2: skip" picks the card when several are waiting
                awaiting = [c for c in awaiting if c["id"] == int(targeted.group(1))]
                text = targeted.group(2)
            if len(awaiting) == 1 and len(text) <= 40:
                card = awaiting[0]
                choice = store.match_option(card["options"], text)
                if choice:
                    r = store.answer_card(conn, card["id"], choice, by="reply")
                    notes.append(f"The user's message answers card {card['id']} (\"{card['question']}\"): "
                                 f"{r['answer']}. It is recorded"
                                 + (f" and done: {r['result']}" if r["result"] else "")
                                 + ". Confirm in one short line.")
            lines = _attention_lines(conn, at)
        finally:
            conn.close()
    except Exception:
        logger.exception("hyperactive pre_llm_call failed")
        return None
    if lines:
        notes.append("Needs the user's attention (bring up only if relevant; answer cards with ha_card, "
                     "close commitments with ha_commit):\n" + "\n".join(lines))
    return {"context": "[hyperactive]\n" + "\n\n".join(notes)} if notes else None


# --- Telegram reactions (UNTESTED: written against hooks.md, no test bot yet) -----------------

YES_EMOJI = {"👍", "✅", "👌", "🙏", "❤", "❤️", "🔥"}
NO_EMOJI = {"👎", "❌"}


def on_platform_event(platform="", event_type="", payload=None, **_kw):
    """A reaction answers the most recently asked card: thumbs up = recommended, thumbs down = the
    other option of a two-option card. Hermes doesn't tell the pulse script which Telegram message
    id it delivered, so we can't match the exact message; we only act when one card was (re)asked
    in the last two card timeouts, and never on anything else."""
    if platform != "telegram" or event_type != "reaction" or not payload:
        return
    emojis = set(payload.get("emojis") or [])
    try:
        conn = _db()
        try:
            cfg = pulse.load_settings(_data_dir())
            window = 2 * store.parse_duration(cfg["card_timeout"])
            at = store.now()
            recent = [c for c in store.open_cards(conn, asked_only=True)
                      if at - store.parse_iso(c["reasked_at"] or c["asked_at"]) <= window]
            if len(recent) != 1:
                return
            card = recent[0]
            if emojis & YES_EMOJI:
                choice = card["recommended"]
            elif emojis & NO_EMOJI and len(card["options"]) == 2:
                choice = next(o for o in card["options"] if o != card["recommended"])
            else:
                return
            store.answer_card(conn, card["id"], choice, by="reaction")
        finally:
            conn.close()
    except Exception:
        logger.exception("hyperactive reaction handling failed")


# --- registration ------------------------------------------------------------------------------

def register(ctx):
    handlers = _make_handlers(ctx)
    for schema in TOOLS:
        ctx.register_tool(name=schema["name"], toolset=PLUGIN, schema=schema, handler=_tool(handlers[schema["name"]]))

    def command(raw):
        return _command(ctx, handlers, raw)

    ctx.register_command("ha", command, description="hyperactive: goals, cards and proactive follow-ups",
                         args_hint="[answer|why|decision|done|goal|followup|setup]")
    ctx.register_cli_command(name=PLUGIN, help="hyperactive goals, cards and proactive follow-ups",
                             setup_fn=_setup_cli,
                             handler_fn=lambda args: print(command(shlex.join(getattr(args, "words", []) or []))))

    ctx.register_system_prompt_section("hyperactive.goals", _system_prompt_section, max_chars=4000)
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("gateway_platform_event", on_platform_event)

    def heal(**_kw):
        # Only once the user has set hyperactive up (pulse.json exists): re-create a deleted shim or
        # job, and push changed settings to the job. Never creates a job for someone who never used it.
        if (_data_dir() / "pulse.json").exists():
            try:
                ensure_pulse(ctx)
            except Exception:
                logger.exception("hyperactive pulse self-heal failed")

    ctx.register_hook("on_session_start", heal)

    for child in sorted((_HERE / "skills").iterdir()):
        if (child / "SKILL.md").exists():
            ctx.register_skill(child.name, child / "SKILL.md")
