#!/usr/bin/env python3
"""Bring back one-shot cron jobs that Hermes dropped while its gateway was down. Stdlib only.

    python3 oneshot_sweep.py record --hermes-home <dir>
    python3 oneshot_sweep.py sweep  --hermes-home <dir> [--hermes <hermes binary>]

Hermes 0.22.5 deletes a one-shot unrun when it is more than 120 s past its time at the first tick
after a gateway outage (cron/jobs.py `_retire_expired_oneshot`). It leaves a diagnostic in the job's
output dir ("# Cron job removed before firing", with the job id, name and scheduled time) but not
the prompt. So:

- `record` (every 10 minutes, a no-agent cron job) copies every one-shot that hasn't run yet from
  cron/jobs.json into a ledger (<HERMES_HOME>/midna/oneshot-ledger.json). It prints nothing.
- `sweep` (nightly, a no-agent cron job) records, then re-creates every job a diagnostic says was
  dropped, from the ledger, a few minutes from now and marked as late. Jobs the user removed on
  purpose leave no diagnostic, so they are never brought back. A job is re-created at most 3 times,
  and each diagnostic is handled once. Its stdout (what was brought back, or lost) is delivered;
  nothing to do prints nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

MISSED_MARK = "# Cron job removed before firing"
COPY = ("name", "prompt", "deliver", "skills", "script", "no_agent", "workdir", "model", "provider")
MAX_ATTEMPTS = 3
RECREATE_IN = timedelta(minutes=3)
KEEP_FOR = timedelta(days=30)
LATE = ("[Late run: this one-shot was due {due}, but Hermes was down then and dropped it; it was "
        "re-created by the one-shot sweep. Do what it calls for now if it still makes sense; if the "
        "moment has passed, say so briefly or reply [SILENT].] ")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def hermes_tz(home: Path):
    """Hermes's `timezone:` from config.yaml (top-level key), else this process's local zone."""
    try:
        m = re.search(r"(?m)^timezone:\s*['\"]?([\w/+-]+)", (home / "config.yaml").read_text())
        return ZoneInfo(m.group(1)) if m else None
    except Exception:
        return None


def ledger_path(home: Path) -> Path:
    return home / "midna" / "oneshot-ledger.json"


def load_ledger(home: Path) -> dict:
    try:
        data = json.loads(ledger_path(home).read_text())
    except (OSError, ValueError):
        data = {}
    data.setdefault("jobs", {})
    data.setdefault("handled", {})
    return data


def save_ledger(home: Path, data: dict) -> None:
    path = ledger_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".ledger-")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def cron_jobs(home: Path) -> list[dict]:
    try:
        data = json.loads((home / "cron" / "jobs.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    return [j for j in jobs if isinstance(j, dict)]


def _when(text: str | None) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(text))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def record(home: Path, at: datetime | None = None) -> dict:
    at = at or now_utc()
    data = load_ledger(home)
    for j in cron_jobs(home):
        sched = j.get("schedule") or {}
        if sched.get("kind") != "once" or j.get("last_run_at") or not j.get("id"):
            continue
        entry = data["jobs"].setdefault(j["id"], {})
        entry.update({k: j.get(k) for k in COPY})
        entry["run_at"] = sched.get("run_at")
        entry.setdefault("orig_prompt", j.get("prompt"))
        entry.setdefault("orig_due", sched.get("run_at"))
        entry.setdefault("attempts", 0)
        entry["seen_at"] = at.isoformat()
    for jid, e in list(data["jobs"].items()):  # forget what is long past
        due = _when(e.get("run_at"))
        if due and at - due > KEEP_FOR:
            del data["jobs"][jid]
    for key, stamp in list(data["handled"].items()):
        if at - (_when(stamp) or at) > KEEP_FOR:
            del data["handled"][key]
    save_ledger(home, data)
    return data


def missed(home: Path) -> list[dict]:
    """Every 'removed before firing' diagnostic Hermes wrote: {key, job_id, name, run_time}."""
    found = []
    out = home / "cron" / "output"
    for f in sorted(out.glob("*/*.md")) if out.is_dir() else []:
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        if not text.startswith(MISSED_MARK):
            continue
        field = dict(re.findall(r"(?m)^- ([a-z ]+): (.*)$", text))
        found.append({"key": f"{f.parent.name}/{f.name}", "job_id": field.get("job id") or f.parent.name,
                      "name": field.get("name", ""), "run_time": field.get("scheduled run time", "")})
    return found


def local_text(when: str | None, tz) -> str:
    dt = _when(when)
    return dt.astimezone(tz).strftime("%a %b %-d %H:%M") if dt else str(when)


def create_args(entry: dict, schedule: str, prompt: str) -> list[str]:
    args = ["cron", "create", schedule, prompt]
    if entry.get("name"):
        args += ["--name", entry["name"]]
    if entry.get("deliver"):
        args += ["--deliver", entry["deliver"]]
    for skill in entry.get("skills") or []:
        args += ["--skill", skill]
    if entry.get("script"):
        args += ["--script", entry["script"]]
    if entry.get("no_agent"):
        args += ["--no-agent"]
    if entry.get("workdir"):
        args += ["--workdir", entry["workdir"]]
    if entry.get("model"):
        args += ["--model", entry["model"]]
        if entry.get("provider"):
            args += ["--provider", entry["provider"]]
    return args


def sweep(home: Path, hermes: str = "hermes", at: datetime | None = None) -> str:
    at = at or now_utc()
    tz = hermes_tz(home)
    data = record(home, at)
    lines = []
    for d in missed(home):
        if d["key"] in data["handled"]:
            continue
        entry = data["jobs"].get(d["job_id"])
        label = d["name"] or entry and entry.get("name") or d["job_id"]
        if entry is None:
            lines.append(f"- {label} (due {local_text(d['run_time'], tz)}) was dropped while Hermes was down; "
                         "it was created and lost between sweeps, so its prompt is unknown. Re-create it by hand.")
        elif entry.get("attempts", 0) >= MAX_ATTEMPTS:
            lines.append(f"- {label} (first due {local_text(entry.get('orig_due'), tz)}) was dropped again; "
                         f"gave up after {MAX_ATTEMPTS} re-creations.")
        else:
            when = (at + RECREATE_IN).astimezone(tz).isoformat(timespec="seconds")
            prompt = LATE.format(due=local_text(entry.get("orig_due"), tz)) + (entry.get("orig_prompt") or "")
            made = subprocess.run([hermes, *create_args(entry, when, prompt)], capture_output=True, text=True,
                                  timeout=120, env={**os.environ, "HERMES_HOME": str(home)})
            if made.returncode != 0:
                lines.append(f"- {label}: could not re-create it ({(made.stderr or made.stdout).strip()[:200]}); "
                             "will retry next sweep.")
                continue  # not handled: the next sweep tries again
            new = next((j for j in reversed(cron_jobs(home)) if j.get("name") == entry.get("name")
                        and (j.get("schedule") or {}).get("kind") == "once" and not j.get("last_run_at")), None)
            if new and new.get("id") != d["job_id"]:
                data["jobs"][new["id"]] = {**entry, "run_at": (new.get("schedule") or {}).get("run_at"),
                                           "prompt": new.get("prompt"), "attempts": entry.get("attempts", 0) + 1}
            lines.append(f"- {label} (due {local_text(entry.get('orig_due'), tz)}) was dropped while Hermes was "
                         f"down; re-created for {local_text(when, tz)}.")
        data["handled"][d["key"]] = at.isoformat()
    save_ledger(home, data)
    if not lines:
        return ""
    return "One-shot sweep: scheduled jobs that came due while Hermes was down\n" + "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("mode", choices=("record", "sweep"))
    ap.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes"))
    ap.add_argument("--hermes", default="hermes", help="the hermes binary (cron create)")
    args = ap.parse_args(argv)
    home = Path(args.hermes_home)
    if args.mode == "record":
        record(home)
        return 0
    text = sweep(home, args.hermes)
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
