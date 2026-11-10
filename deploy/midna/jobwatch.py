#!/usr/bin/env python3
"""Wake the agent when a background job's result file appears. Stdlib only.

    python3 jobwatch.py add <result file> --note "<what the job is, what to do with it>" [--id ID]
    python3 jobwatch.py list
    python3 jobwatch.py check        # the cron job's pre-run script

Hermes wakes the agent when a `terminal(background=true, notify=true)` process exits, but only on
push platforms (not the api_server the app uses) and not across a gateway restart. This covers the
rest: whoever starts a long job registers its result file here, and the `midna-jobwatch` cron job
(agent mode, every 2 minutes) runs `check` first. `check` prints each job whose result file now
exists, is non-empty and hasn't changed for --settle seconds, and retires it; no finished job means
empty output, which makes Hermes skip the model call. A job with no result after --expire-days is
reported once as overdue and retired too.

Watches live in <HERMES_HOME>/midna/jobwatch/<id>.json (retired ones in done/). The result file must
be readable by the user Hermes runs as.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HEAD_CHARS = 3000


def watch_dir(home: Path) -> Path:
    return home / "midna" / "jobwatch"


def add(home: Path, result: str, note: str = "", job_id: str = "") -> dict:
    path = str(Path(result).expanduser().resolve())
    job_id = job_id or hashlib.sha1(path.encode()).hexdigest()[:10]
    if not re.fullmatch(r"[\w.-]{1,64}", job_id):
        raise SystemExit(f"bad id {job_id!r}: letters, digits, . _ - only")
    d = watch_dir(home)
    d.mkdir(parents=True, exist_ok=True)
    entry = {"id": job_id, "result": path, "note": note,
             "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    f = d / f"{job_id}.json"
    if f.exists():  # re-registering the same job keeps its start time
        entry["added_at"] = json.loads(f.read_text()).get("added_at", entry["added_at"])
    f.write_text(json.dumps(entry, indent=2, ensure_ascii=False))
    return entry


def watches(home: Path) -> list[dict]:
    d = watch_dir(home)
    out = []
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            out.append({**json.loads(f.read_text()), "_file": f})
        except (OSError, ValueError):
            continue
    return out


def _retire(entry: dict, why: str) -> None:
    f: Path = entry["_file"]
    done = f.parent / "done"
    done.mkdir(exist_ok=True)
    data = {k: v for k, v in entry.items() if k != "_file"}
    data.update(retired=why, retired_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    (done / f.name).write_text(json.dumps(data, indent=2, ensure_ascii=False))
    f.unlink()


def check(home: Path, settle: int = 20, expire_days: float = 7, now: float | None = None) -> str:
    now = now or time.time()
    blocks = []
    for e in watches(home):
        p = Path(e["result"])
        try:
            st = p.stat()
        except PermissionError:
            st = None
            blocks.append(f"Background job {e['id']} ({e.get('note') or 'no note'}): its result file {p} exists "
                          "but can't be read by Hermes's user. Tell the user; the watch stays.")
            continue
        except OSError:
            st = None
        if st and st.st_size > 0 and now - st.st_mtime >= settle:
            try:
                head = p.read_text(errors="replace")[:HEAD_CHARS]
            except OSError as exc:
                head = f"(could not read it: {exc})"
            more = "" if st.st_size <= HEAD_CHARS else f"\n[... {st.st_size} bytes in all; read the file for the rest]"
            blocks.append(f"Background job {e['id']} finished.\nWhat it was: {e.get('note') or '(no note)'}\n"
                          f"Result file: {p}\n--- result ---\n{head}{more}")
            _retire(e, "finished")
            continue
        added = datetime.fromisoformat(e["added_at"]).timestamp()
        if now - added > expire_days * 86400:
            blocks.append(f"Background job {e['id']} has no result after {expire_days:g} days, so it is no "
                          f"longer watched.\nWhat it was: {e.get('note') or '(no note)'}\nExpected result file: {p}")
            _retire(e, "expired")
    return "\n\n".join(blocks)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("result")
    a.add_argument("--note", default="")
    a.add_argument("--id", default="")
    sub.add_parser("list")
    c = sub.add_parser("check")
    c.add_argument("--settle", type=int, default=20, help="seconds the file must be unchanged")
    c.add_argument("--expire-days", type=float, default=7)
    args = ap.parse_args(argv)
    home = Path(args.hermes_home)
    if args.cmd == "add":
        e = add(home, args.result, args.note, args.id)
        print(f"watching job {e['id']}: {e['result']}")
    elif args.cmd == "list":
        for e in watches(home):
            print(f"{e['id']}  {e['result']}  {e.get('note', '')}")
    else:
        text = check(home, args.settle, args.expire_days)
        if text:
            print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
