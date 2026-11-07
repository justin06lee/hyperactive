"""hyperactive: a proactive layer for Hermes.

Keeps an objective tree + follow-ups in a plugin-owned SQLite store and wakes itself through a
Hermes cron "pulse" job to deliver what is due. Registration only wires things up; the pulse job
is created lazily (first scheduled follow-up) or by `hermes hyperactive setup`, never at import.
"""

from __future__ import annotations

import argparse
import json
import logging
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from hermes_constants import get_hermes_home
from plugins.plugin_storage import plugin_data_dir

from . import store

logger = logging.getLogger(__name__)

PLUGIN = "hyperactive"
PULSE_JOB_NAME = "hyperactive-pulse"
PULSE_SCRIPT = "hyperactive_pulse.py"
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


# --- the pulse cron job ------------------------------------------------------------------------

def ensure_pulse(ctx) -> dict:
    """Install the pulse shim script and make sure exactly one pulse cron job exists."""
    data_dir = _data_dir()
    settings_file = data_dir / "pulse.json"
    settings = json.loads(settings_file.read_text()) if settings_file.exists() else {}
    settings["quiet_hours"] = ctx.get_config("quiet_hours", default="") or ""
    settings_file.write_text(json.dumps(settings, indent=2))

    scripts = get_hermes_home() / "scripts"  # the only place Hermes cron runs scripts from
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / PULSE_SCRIPT).write_text(_SHIM.format(plugin_dir=str(_HERE), data_dir=str(data_dir)))

    every = ctx.get_config("pulse_every", default="every 5m")
    deliver = ctx.get_config("deliver", default="origin")
    listed = json.loads(ctx.dispatch_tool("cronjob_manage", {"action": "list"}) or "{}")
    if listed.get("error", "").startswith("Unknown tool"):
        # `hermes hyperactive ...` runs outside a session, where the cron tool isn't loaded.
        # Fall back to the user-facing `hermes cron` CLI against the same HERMES_HOME.
        return _ensure_pulse_cli(every, deliver)
    for job in listed.get("jobs", []):
        if job.get("name") == PULSE_JOB_NAME:
            return {"job_id": job.get("job_id") or job.get("id"), "created": False}
    created = json.loads(ctx.dispatch_tool("cronjob_manage", {
        "action": "create", "name": PULSE_JOB_NAME, "schedule": every, "script": PULSE_SCRIPT,
        "no_agent": True, "deliver": deliver, "catch_up": True,
    }) or "{}")
    if not created.get("success", True) or created.get("error"):
        raise RuntimeError(f"could not create pulse job: {created}")
    return {"job_id": created.get("job_id") or created.get("id"), "created": True}


def _ensure_pulse_cli(every: str, deliver: str) -> dict:
    hermes = shutil.which("hermes") or sys.argv[0]
    listed = subprocess.run([hermes, "cron", "list"], capture_output=True, text=True, timeout=120)
    if PULSE_JOB_NAME in listed.stdout:
        return {"job": PULSE_JOB_NAME, "created": False}
    made = subprocess.run([hermes, "cron", "create", every, "--name", PULSE_JOB_NAME, "--script", PULSE_SCRIPT,
                           "--no-agent", "--deliver", deliver, "--catch-up", "on"],
                          capture_output=True, text=True, timeout=120)
    if made.returncode != 0:
        raise RuntimeError(f"could not create pulse job: {made.stderr.strip() or made.stdout.strip()}")
    return {"job": PULSE_JOB_NAME, "created": True, "detail": made.stdout.strip().splitlines()[:3]}


# --- tools -------------------------------------------------------------------------------------

GOAL_ADD = {
    "name": "ha_goal_add",
    "description": ("Record one of the user's goals in their objective tree (life > year > quarter > week). "
                    "Use when the user states something they want to achieve. Link it to a parent goal when one "
                    "fits (see the goal tree in the system prompt). Optionally schedule a proactive follow-up."),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "The goal, short and concrete."},
            "horizon": {"type": "string", "enum": list(store.HORIZONS), "description": "Time horizon."},
            "parent_id": {"type": "integer", "description": "Parent goal id, if any."},
            "why": {"type": "string", "description": "Why it matters to the user, in their words."},
            "follow_up_in": {"type": "string", "description": "Optional: when to check in, e.g. 'in 2h', '3d', or ISO time."},
            "follow_up_text": {"type": "string", "description": "What to ask or say at the check-in."},
        },
        "required": ["title", "horizon"],
    },
}

FOLLOWUP = {
    "name": "ha_followup",
    "description": ("Schedule something hyperactive will say on its own later: a follow-up, reminder or nudge. "
                    "Every follow-up should serve a goal; pass goal_id. Delivered by the pulse even if the user "
                    "never messages again. Quiet hours defer delivery, they never drop it."),
    "parameters": {
        "type": "object",
        "properties": {
            "goal_id": {"type": "integer"},
            "text": {"type": "string", "description": "The message to deliver, self-contained."},
            "when": {"type": "string", "description": "'in 30m', '2h', '1d', or ISO timestamp."},
            "kind": {"type": "string", "enum": ["followup", "reminder", "nudge"]},
        },
        "required": ["text", "when"],
    },
}

STATUS = {
    "name": "ha_status",
    "description": "Show the user's goal tree and everything hyperactive has scheduled.",
    "parameters": {"type": "object", "properties": {}},
}


def _status_text() -> str:
    conn = _db()
    try:
        lines = ["Goals:", store.render_tree(conn), "", "Scheduled:"]
        pend = store.pending_followups(conn)
        lines += [f"- #{f['id']} {f['kind']} (goal #{f['goal_id']}) at {f['due_at']}: {f['text']}" for f in pend] or ["(nothing)"]
        return "\n".join(lines)
    finally:
        conn.close()


def _make_handlers(ctx):
    def schedule(goal_id, text, when, kind="followup") -> dict:
        conn = _db()
        try:
            row = store.add_followup(conn, goal_id, text, when, kind)
        finally:
            conn.close()
        row["pulse"] = ensure_pulse(ctx)
        return row

    def goal_add(args, **_kw):
        try:
            conn = _db()
            try:
                gid = store.add_goal(conn, args["title"], args.get("horizon", "week"),
                                     args.get("parent_id"), args.get("why", ""))
            finally:
                conn.close()
            out = {"goal_id": gid}
            if args.get("follow_up_in"):
                text = args.get("follow_up_text") or f"How is \"{args['title']}\" going? What moved since we set it?"
                out["followup"] = schedule(gid, text, args["follow_up_in"])
            return _ok(**out)
        except Exception as exc:  # tool handlers must never raise
            return _err(str(exc))

    def followup(args, **_kw):
        try:
            return _ok(**schedule(args.get("goal_id"), args["text"], args["when"], args.get("kind", "followup")))
        except Exception as exc:
            return _err(str(exc))

    def status(args, **_kw):
        try:
            return _ok(status=_status_text())
        except Exception as exc:
            return _err(str(exc))

    return goal_add, followup, status, schedule


# --- slash + CLI commands ----------------------------------------------------------------------

USAGE = ("/ha status | /ha goal <horizon> <title> [--parent N] [--follow-up 'in 2m'] | "
         "/ha followup <goal_id> <when> <text> | /ha setup")


def _command(ctx, schedule, raw: str) -> str:
    try:
        argv = shlex.split(raw or "")
    except ValueError as exc:
        return f"hyperactive: {exc}"
    if not argv or argv[0] == "status":
        return _status_text()
    if argv[0] == "setup":
        return f"hyperactive pulse: {ensure_pulse(ctx)}"
    if argv[0] == "goal" and len(argv) >= 3:
        horizon, rest = argv[1], argv[2:]
        parent = follow = None
        if "--parent" in rest:
            i = rest.index("--parent"); parent = int(rest[i + 1]); del rest[i:i + 2]
        if "--follow-up" in rest:
            i = rest.index("--follow-up"); follow = rest[i + 1]; del rest[i:i + 2]
        title = " ".join(rest)
        conn = _db()
        try:
            gid = store.add_goal(conn, title, horizon, parent)
        finally:
            conn.close()
        msg = f"goal #{gid} [{horizon}] {title}"
        if follow:
            f = schedule(gid, f"Check-in on \"{title}\": what moved, and what's the next step?", follow)
            msg += f"; follow-up #{f['id']} due {f['due_at']}"
        return msg
    if argv[0] == "followup" and len(argv) >= 4:
        f = schedule(int(argv[1]), " ".join(argv[3:]), argv[2])
        return f"follow-up #{f['id']} due {f['due_at']}"
    return USAGE


def _setup_cli(subparser):
    subparser.add_argument("words", nargs=argparse.REMAINDER, help="same grammar as the /ha slash command")


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
            "You are proactive. Tie what you do to one of these goals; record new goals with ha_goal_add "
            "and schedule check-ins with ha_followup instead of waiting to be asked.\n" + tree)


# --- registration ------------------------------------------------------------------------------

def register(ctx):
    goal_add, followup, status, schedule = _make_handlers(ctx)
    ctx.register_tool(name="ha_goal_add", toolset=PLUGIN, schema=GOAL_ADD, handler=goal_add)
    ctx.register_tool(name="ha_followup", toolset=PLUGIN, schema=FOLLOWUP, handler=followup)
    ctx.register_tool(name="ha_status", toolset=PLUGIN, schema=STATUS, handler=status)

    ctx.register_command("ha", lambda raw: _command(ctx, schedule, raw),
                         description="hyperactive: goals and proactive follow-ups", args_hint="<subcommand>")

    def _cli(args):
        print(_command(ctx, schedule, shlex.join(getattr(args, "words", []) or [])))

    ctx.register_cli_command(name=PLUGIN, help="hyperactive goals and proactive follow-ups",
                             setup_fn=_setup_cli, handler_fn=_cli)

    ctx.register_system_prompt_section("hyperactive.goals", _system_prompt_section, max_chars=4000)

    for child in sorted((_HERE / "skills").iterdir()):
        if (child / "SKILL.md").exists():
            ctx.register_skill(child.name, child / "SKILL.md")
