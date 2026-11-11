#!/usr/bin/env python3
"""midna -> Hermes + hyperactive migration as installer steps. Stdlib only; every command is
idempotent and safe to re-run.

    midna_hermes.py install [--option value ...]  P1: back up Hermes, install hyperactive at a pinned
                                                  commit from a local git copy, set config, add the
                                                  helper cron jobs and skill, import midna as a quiet
                                                  mirror, and start the nightly mirror timer
    midna_hermes.py mirror                        P1, nightly (the timer): snapshot midna.db, re-import
                                                  it into Hermes. Does nothing once activated
    midna_hermes.py activate                      P3, the writer flip: final import, quiet flip, cron
                                                  plan, brief on, then pause midna's wakeups
    midna_hermes.py deactivate                    P3 rollback: midna:* jobs off, mirror mode back,
                                                  midna's paused wakeups back to pending
    midna_hermes.py uninstall                     P1 rollback: undo the import, restore config.yaml and
                                                  SOUL.md, put back the previous hyperactive
    midna_hermes.py status

It runs as the midna user (it reads midna.db). Everything that touches Hermes runs as Hermes's
user through `as_hermes` (default `sudo -n -u hermes -H ...`), using this same file from a copy
staged into <HERMES_HOME>/midna/code/<sha> (the `hermes-side` subcommands). Settings live in
--config (default ~/.config/midna/hermes-migration.json); `install` writes the options it is
given there, so later commands and the timer need none.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import pwd
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]  # this file is <repo>/deploy/midna/midna_hermes.py
STAGED = ("plugin", "dev/import_midna.py", "deploy")  # what Hermes's user needs from the repo
DEFAULT_CONFIG = Path("~/.config/midna/hermes-migration.json").expanduser()
CATALOG_PIN = "justin06lee/hyperactive#plugin@2f147d3e6a1a91389d4d50991b46a546b5ea804b"  # hyperactive 0.3.0

DEFAULTS = {
    "hermes_home": "/home/hermes/.hermes",
    "as_hermes": "sudo -n -u hermes -H env XDG_RUNTIME_DIR=/run/user/{uid}",
    "hermes_user": "hermes",
    "hermes_bin": "hermes",
    "hermes_python": "python3",
    "hermes_group": "hermes",  # the snapshot is made group-readable for it; "" = leave it 600
    "midna_db": "~/.local/share/midna/midna.db",
    "repo": str(REPO),
    "ref": "HEAD",
    "local_home": "~/.local/share/midna/hermes",  # midna's staged copy (what the timer runs)
    "state_dir": "~/.local/state/midna-hermes",
    "tz": "Asia/Seoul",
    "deliver": "telegram",
    "features": "notes,calendar",
    "pinned_chars": 14000,
    "brief_at": "08:00",  # Hermes's brief, off (empty) while mirroring, this from activate on
    "mirror_at": "03:40",
    "sweep_at": "04:10",
    "jobwatch_every": "every 2m",
    "ledger_every": "every 10m",
    "unit_dir": "~/.config/systemd/user",
    "systemctl": True,
    "gateway_restart": True,
    "rollback_plugin": CATALOG_PIN,
    "persona": True,  # SOUL.md = deploy/midna/persona/SOUL.md (off: the pre-midna SOUL.md is put back)
    "body": True,  # midna-body MCP (P2): midna's systemd user service + Hermes mcp_servers entry
    "mac": "huiyunlee@100.98.21.63",  # the body's ssh target (Tailscale)
    "body_port": 8791,
    "api_server": True,  # Hermes api_server on 127.0.0.1 (P4's app path; also the health check)
    "api_port": 8642,
    "api_key_file": "~/.local/share/midna/secrets/hermes_api_server_key",
    "app": True,  # P4: Hermes's own messages also go into the midna app (the `midna` platform plugin)
    "app_url": "http://127.0.0.1:7717",  # midna serve
    "app_token_file": "~/.local/share/midna/secrets/hermes_app_token",  # written by `midna hermes setup`
    "backups_keep": 20,
}
TIMER = "midna-hermes-mirror"
APP = "midna"  # the platform name in deliver targets ("telegram,midna")
APP_PLUGIN = "midna-app"
BODY = "midna-body"
BODY_TOKEN_ENV = "MIDNA_BODY_TOKEN"
SRC = "midna"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def stamp() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S")


def log(msg: str) -> None:
    print(msg, flush=True)


def _run(cmd: list[str], *, check: bool = True, env: dict | None = None, input: str | None = None,
         cwd: str | None = None) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, input=input, cwd=cwd, timeout=900)
    if check and r.returncode != 0:
        raise SystemExit(f"failed ({r.returncode}): {shlex.join(cmd)}\n{(r.stderr or r.stdout).strip()[-2000:]}")
    return r


# --- settings and state (the midna side) ------------------------------------------------------

def load_config(path: Path, overrides: dict | None = None, *, save: bool = False) -> dict:
    saved = json.loads(path.read_text()) if path.exists() else {}
    cfg = {**DEFAULTS, **saved, **{k: v for k, v in (overrides or {}).items() if v is not None}}
    if save:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({k: cfg[k] for k in DEFAULTS}, indent=2) + "\n")
    cfg["_config"] = str(path)
    return cfg


def as_hermes(cfg: dict) -> list[str]:
    spec = cfg["as_hermes"]
    if "{uid}" in spec:
        spec = spec.replace("{uid}", str(pwd.getpwnam(cfg["hermes_user"]).pw_uid))
    return shlex.split(spec)


def state_path(cfg: dict) -> Path:
    return Path(cfg["state_dir"]).expanduser() / "state.json"


def load_state(cfg: dict) -> dict:
    p = state_path(cfg)
    return json.loads(p.read_text()) if p.exists() else {"phase": "none"}


def save_state(cfg: dict, **changes) -> dict:
    st = {**load_state(cfg), **changes}
    p = state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, indent=2) + "\n")
    return st


def hermes_run(cfg: dict, args: list[str], *, check: bool = True, input: str | None = None) -> subprocess.CompletedProcess:
    """Run a command as Hermes's user, from /tmp (a directory it can read)."""
    prefix = as_hermes(cfg)
    if not prefix:  # scratch: the same user, HERMES_HOME from the environment
        return _run(args, check=check, input=input, cwd="/tmp",
                    env={**os.environ, "HERMES_HOME": cfg["hermes_home"]})
    return _run(prefix + ["bash", "-lc", "cd /tmp && exec " + shlex.join(args)], check=check, input=input)


def hermes_side(cfg: dict, op: str, opts: dict | None = None, *, check: bool = True) -> dict:
    """This file's hermes-side subcommand, from the staged copy; returns its JSON result."""
    script = f"{cfg['hermes_home']}/midna/code/current/deploy/midna/midna_hermes.py"
    payload = {"hermes_home": cfg["hermes_home"], "hermes_bin": cfg["hermes_bin"], **(opts or {})}
    r = hermes_run(cfg, [cfg["hermes_python"], "-I", script, "hermes-side", op], input=json.dumps(payload), check=False)
    for line in r.stderr.splitlines():
        log(f"  [hermes] {line}")
    if r.returncode != 0:
        if check:
            raise SystemExit(f"hermes-side {op} failed ({r.returncode})")
        return {"ok": False}
    return json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {}


# --- staging the code: midna's copy and Hermes's copy, both at one commit ---------------------

def resolve_sha(cfg: dict) -> str:
    return _run(["git", "-C", cfg["repo"], "rev-parse", "--verify", cfg["ref"] + "^{commit}"]).stdout.strip()


def stage_local(cfg: dict, sha: str) -> Path:
    """<local_home>/code/<sha>, from `git archive`, and code/current pointing at it."""
    code = Path(cfg["local_home"]).expanduser() / "code"
    target = code / sha
    if not target.exists():
        tmp = Path(tempfile.mkdtemp(prefix=f".{sha[:8]}-", dir=_mkdir(code)))
        archive = _run_bytes(["git", "-C", cfg["repo"], "archive", "--format=tar", sha, *STAGED])
        subprocess.run(["tar", "-x", "-C", str(tmp)], input=archive, check=True)
        tmp.rename(target)
    _symlink(code / "current", sha)
    return target


def _run_bytes(cmd: list[str]) -> bytes:
    r = subprocess.run(cmd, capture_output=True, timeout=300)
    if r.returncode != 0:
        raise SystemExit(f"failed: {shlex.join(cmd)}\n{r.stderr.decode(errors='replace')}")
    return r.stdout


def _mkdir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def _symlink(link: Path, target: str) -> None:
    if link.is_symlink() and os.readlink(link) == target:
        return
    tmp = link.with_name(f".{link.name}.new")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(target)
    os.replace(tmp, link)


def stage_hermes(cfg: dict, sha: str) -> bool:
    """Hermes's copy: a bare git repo (what `hermes plugins install file://...` installs from, at the
    exact sha) and code/<sha> extracted from it. Returns True if anything new was staged."""
    h = cfg["hermes_home"]
    bare = f"{h}/midna/hyperactive.git"
    have = hermes_run(cfg, ["bash", "-c", f"test -d {shlex.quote(f'{h}/midna/code/{sha}')} && "
                                         f"git --git-dir={shlex.quote(bare)} cat-file -e {sha}^{{commit}}"], check=False)
    if have.returncode != 0:
        tmp = Path(tempfile.mkdtemp(prefix="midna-stage-"))
        try:
            tmp.chmod(0o755)
            bundle = tmp / "hyperactive.bundle"
            _run(["git", "-C", cfg["repo"], "branch", "-f", "midna-hermes-stage", sha])
            try:
                _run(["git", "-C", cfg["repo"], "bundle", "create", str(bundle), "midna-hermes-stage"])
            finally:
                _run(["git", "-C", cfg["repo"], "branch", "-D", "midna-hermes-stage"], check=False)
            bundle.chmod(0o644)
            q = shlex.quote
            hermes_run(cfg, ["bash", "-c", "set -e; umask 077; "
                             f"mkdir -p {q(h + '/midna/code')}; "
                             f"[ -d {q(bare)} ] || git init -q --bare {q(bare)}; "
                             f"git --git-dir={q(bare)} fetch -q {q(str(bundle))} "
                             f"+refs/heads/midna-hermes-stage:refs/heads/stage-{sha[:12]}; "
                             f"t=$(mktemp -d {q(h + '/midna/code/.stage-XXXXXX')}); "
                             f"git --git-dir={q(bare)} archive {sha} {' '.join(STAGED)} | tar -x -C \"$t\"; "
                             f"rm -rf {q(h + '/midna/code/' + sha)}; mv \"$t\" {q(h + '/midna/code/' + sha)}"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    hermes_run(cfg, ["ln", "-sfn", sha, f"{h}/midna/code/current"])
    return have.returncode != 0


# --- P1 ----------------------------------------------------------------------------------------

def cmd_install(cfg: dict, args) -> int:
    sha = resolve_sha(cfg)
    st = load_state(cfg)
    log(f"midna -> Hermes install (P1) at {sha[:12]}; phase now: {st['phase']}")
    local = stage_local(cfg, sha)
    log(f"- midna's copy: {local}")
    log(f"- Hermes's copy: {'staged' if stage_hermes(cfg, sha) else 'already staged'} "
        f"({cfg['hermes_home']}/midna/code/{sha[:12]})")
    log(f"- backup: {backup(cfg)}")
    log("- " + body_install(cfg, sha))
    if cfg["app"] and not app_token(cfg):
        log(f"- midna app delivery: waiting for {cfg['app_token_file']} (`midna hermes setup` pairs Hermes)")
    activated = st["phase"] == "activated"
    res = hermes_side(cfg, "p1", {
        "persona": cfg["persona"], "body": cfg["body"], "api_server": cfg["api_server"],
        "body_url": f"http://127.0.0.1:{cfg['body_port']}/mcp", "body_token": body_token(cfg) if cfg["body"] else "",
        "api_port": cfg["api_port"], "api_key": api_key(cfg) if cfg["api_server"] else "",
        "app_url": cfg["app_url"], "app_token": app_token(cfg),
        "sha": sha, "tz": cfg["tz"], "deliver": deliver_to(cfg), "features": cfg["features"],
        "pinned_chars": cfg["pinned_chars"], "brief_at": cfg["brief_at"] if activated else "",
        "quiet_sources": "" if activated else SRC, "sweep_at": cfg["sweep_at"],
        "jobwatch_every": cfg["jobwatch_every"], "ledger_every": cfg["ledger_every"]})
    for line in res.get("steps", []):
        log(f"- {line}")
    save_state(cfg, installed_sha=sha, installed_at=now_utc().isoformat(timespec="seconds"),
               phase=st["phase"] if st["phase"] in ("mirror", "activated") else "mirror")
    if not activated:
        mirror(cfg)
    log("- " + timer_install(cfg, enable=not activated))
    if res.get("changed") and cfg["gateway_restart"] and not args.no_gateway_restart:
        log("- " + restart_gateway(cfg))
    elif res.get("changed"):
        log("- gateway NOT restarted (--no-gateway-restart); it runs the old plugin code until it is")
    log("done.")
    return 0


def backup(cfg: dict) -> str:
    """Timestamped copy of Hermes's config.yaml, SOUL.md, .env and hyperactive.db, taken on every
    install (and by `backup`), before anything changes. Separate from the one-time pre-midna copy."""
    return hermes_side(cfg, "backup", {"keep": cfg["backups_keep"]}).get("dir", "?")


def _http(url: str, *, data: dict | None = None, token: str = "", timeout: float = 10) -> tuple[int, dict | str]:
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            code = r.status
    except urllib.error.HTTPError as e:
        raw, code = e.read().decode(errors="replace"), e.code
    except OSError as e:
        return 0, str(e)
    try:
        return code, json.loads(raw)
    except ValueError:
        return code, raw


def cmd_check(cfg: dict, args) -> int:
    """Is the live setup healthy? Gateway unit active, api_server /health, midna-body health and
    `hermes mcp test midna-body`; with --turn, one real agent turn through /v1/responses (local
    only: the reply comes back here, nothing is sent to Telegram)."""
    bad = []
    r = hermes_run(cfg, ["systemctl", "--user", "is-active", "hermes-gateway.service"], check=False)
    log(f"- gateway: {r.stdout.strip() or r.stderr.strip()}")
    if r.stdout.strip() != "active":
        bad.append("gateway")
    if cfg["api_server"]:
        code, body = _http(f"http://127.0.0.1:{cfg['api_port']}/health")
        log(f"- api_server /health: {code} {body}")
        if code != 200:
            bad.append("api_server")
    if cfg["body"]:
        ok = body_health(cfg, 3)
        log(f"- midna-body /health: {'ok' if ok else 'NOT ok'}")
        r = hermes_run(cfg, [cfg["hermes_bin"], "mcp", "test", BODY], check=False)
        out = (r.stdout + r.stderr).strip()
        log("- hermes mcp test midna-body:\n    " + "\n    ".join(out.splitlines()[-12:]))
        if not ok or r.returncode != 0:
            bad.append("midna-body")
    if args.turn and cfg["api_server"]:
        code, body = _http(f"http://127.0.0.1:{cfg['api_port']}/v1/responses", token=api_key(cfg), timeout=600,
                           data={"model": "hermes-agent", "input": args.turn, "store": False})
        text, tools = "", []
        if isinstance(body, dict):
            for item in body.get("output", []):
                if item.get("type") == "function_call":
                    tools.append(item.get("name"))
                if item.get("type") == "message":
                    text += "".join(c.get("text", "") for c in item.get("content", []))
        log(f"- test turn: HTTP {code}, status {body.get('status') if isinstance(body, dict) else '?'}, "
            f"tools {tools}\n    reply: {(text or str(body))[:1500]}")
        if code != 200 or not text:
            bad.append("turn")
    log("ok" if not bad else f"NOT ok: {', '.join(bad)}")
    return 1 if bad else 0


def cmd_backup(cfg: dict, args) -> int:
    log(f"backup: {backup(cfg)}")
    return 0


# --- the body (P2): midna-body MCP as midna's systemd user service ------------------------------

def _secret(path: str) -> str:
    """A random secret kept in a 600 file (created once)."""
    p = Path(path).expanduser()
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(__import__("secrets").token_urlsafe(32) + "\n")
    return p.read_text().strip()


def body_token(cfg: dict) -> str:
    return _secret(str(Path(cfg["state_dir"]).expanduser() / f"{BODY}.token"))


def api_key(cfg: dict) -> str:
    return _secret(cfg["api_key_file"])


def app_token(cfg: dict) -> str:
    """midna's device token for Hermes (a bridge), or "" while `midna hermes setup` hasn't paired it."""
    p = Path(cfg["app_token_file"]).expanduser()
    return p.read_text().strip() if cfg["app"] and p.exists() else ""


def deliver_to(cfg: dict) -> str:
    """Where Hermes's own messages go: the configured target, plus the midna app once it is paired."""
    targets = [t for t in cfg["deliver"].split(",") if t and t != APP]
    return ",".join(targets + ([APP] if app_token(cfg) else []))


def body_unit(cfg: dict) -> str:
    code = Path(cfg["local_home"]).expanduser() / "code" / "current" / "deploy" / "midna" / "midna_body.py"
    state = Path(cfg["state_dir"]).expanduser()
    args = ["--port", str(cfg["body_port"]), "--mac", cfg["mac"], "--midna-db", str(Path(cfg["midna_db"]).expanduser()),
            "--token-file", str(state / f"{BODY}.token"), "--log", str(state / f"{BODY}.jsonl"), "--tz", cfg["tz"]]
    return ("[Unit]\nDescription=midna-body: MCP server for Hermes (his Mac over ssh, midna's chat archive)\n"
            "After=network-online.target\n\n[Service]\nType=simple\n"
            f"ExecStart=/usr/bin/python3 -I {code} serve {shlex.join(args)}\n"
            "Restart=always\nRestartSec=5\n\n[Install]\nWantedBy=default.target\n")


def body_install(cfg: dict, sha: str) -> str:
    unit = Path(cfg["unit_dir"]).expanduser() / f"{BODY}.service"
    if not cfg["body"]:
        if not unit.exists():
            return "midna-body: off"
        if cfg["systemctl"]:
            _run(["systemctl", "--user", "disable", "--now", f"{BODY}.service"], check=False)
        unit.unlink()
        if cfg["systemctl"]:
            _run(["systemctl", "--user", "daemon-reload"], check=False)
        return "midna-body: service stopped and removed"
    body_token(cfg)
    text = body_unit(cfg)
    changed = not unit.exists() or unit.read_text() != text
    if changed:
        _mkdir(unit.parent)
        unit.write_text(text)
    if not cfg["systemctl"]:
        return f"midna-body: unit {'written' if changed else 'unchanged'} (systemctl off)"
    st = load_state(cfg)
    if changed:
        _run(["systemctl", "--user", "daemon-reload"])
    _run(["systemctl", "--user", "enable", f"{BODY}.service"], check=False)
    active = _run(["systemctl", "--user", "is-active", f"{BODY}.service"], check=False).stdout.strip() == "active"
    if changed or not active or st.get("body_sha") != sha:
        _run(["systemctl", "--user", "restart", f"{BODY}.service"])
        save_state(cfg, body_sha=sha)
        how = "restarted"
    else:
        how = "running"
    ok = body_health(cfg)
    return f"midna-body: {how} on 127.0.0.1:{cfg['body_port']} (mac {cfg['mac']}), health {'ok' if ok else 'NOT ok'}"


def body_health(cfg: dict, wait: float = 10.0) -> bool:
    import time
    import urllib.request
    end = time.monotonic() + wait
    while True:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{cfg['body_port']}/health", timeout=3) as r:
                return r.status == 200
        except OSError:
            if time.monotonic() > end:
                return False
            time.sleep(0.5)


def restart_gateway(cfg: dict) -> str:
    r = hermes_run(cfg, [cfg["hermes_bin"], "gateway", "restart"], check=False)
    if r.returncode != 0:
        return f"gateway restart FAILED: {(r.stderr or r.stdout).strip()[-300:]}"
    return "gateway restarted"


def timer_units(cfg: dict) -> dict[str, str]:
    code = Path(cfg["local_home"]).expanduser() / "code" / "current" / "deploy" / "midna" / "midna_hermes.py"
    hh, mm = cfg["mirror_at"].split(":")
    return {
        f"{TIMER}.service": (
            "[Unit]\nDescription=midna -> Hermes: nightly mirror import of midna.db (P1-P3)\n"
            "After=network.target\n\n[Service]\nType=oneshot\n"
            f"ExecStart=/usr/bin/python3 -I {code} mirror --config {cfg['_config']}\n"),
        f"{TIMER}.timer": (
            "[Unit]\nDescription=midna -> Hermes: nightly mirror import\n\n[Timer]\n"
            f"OnCalendar=*-*-* {int(hh):02d}:{int(mm):02d}:00 {cfg['tz']}\nPersistent=true\nRandomizedDelaySec=120\n\n"
            "[Install]\nWantedBy=timers.target\n"),
    }


def timer_install(cfg: dict, *, enable: bool) -> str:
    unit_dir = _mkdir(Path(cfg["unit_dir"]).expanduser())
    changed = False
    for name, text in timer_units(cfg).items():
        f = unit_dir / name
        if not f.exists() or f.read_text() != text:
            f.write_text(text)
            changed = True
    if not cfg["systemctl"]:
        return f"timer units {'written' if changed else 'unchanged'} in {unit_dir} (systemctl off)"
    if changed:
        _run(["systemctl", "--user", "daemon-reload"])
    _run(["systemctl", "--user", "enable" if enable else "disable", "--now", f"{TIMER}.timer"], check=False)
    return f"timer {TIMER}.timer {'enabled' if enable else 'disabled'} ({cfg['mirror_at']} {cfg['tz']})"


# --- the mirror (P1 nightly) -----------------------------------------------------------------

def _import_midna():
    sys.path.insert(0, str(REPO / "dev"))
    sys.path.insert(0, str(REPO / "plugin"))
    import import_midna  # noqa: PLC0415
    return import_midna


def mirror(cfg: dict) -> dict:
    """Snapshot midna.db (read-only, through the backup API) and import it into Hermes."""
    if load_state(cfg)["phase"] == "activated":  # re-importing now would overwrite Hermes's newer data
        log("mirror: skipped, Hermes is the writer since activation")
        return {"skipped": True}
    lock_dir = _mkdir(Path(cfg["state_dir"]).expanduser())
    with open(lock_dir / "mirror.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit("mirror: another mirror run holds the lock")
        tmp = Path(tempfile.mkdtemp(prefix="midna-snap-"))
        try:
            tmp.chmod(0o711)  # Hermes's user reaches the file by name, can't list the dir
            snap = tmp / "midna-snap.db"
            _import_midna().snapshot(str(Path(cfg["midna_db"]).expanduser()), str(snap),
                                     cfg["hermes_group"] if as_hermes(cfg) else "")
            res = hermes_side(cfg, "apply-mirror", {"snapshot": str(snap), "tz": cfg["tz"],
                                                    "deliver": cfg["deliver"]})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    if res.get("skipped"):
        log(f"mirror: skipped by Hermes's side ({res.get('why')})")
        return res
    log(f"mirror: imported into {cfg['hermes_home']} -> {res.get('summary')}; report {res.get('out')}")
    save_state(cfg, last_mirror={"at": now_utc().isoformat(timespec="seconds"), "summary": res.get("summary"),
                                 "out": res.get("out")})
    return res


# --- P3 ----------------------------------------------------------------------------------------

def sidecar(cfg: dict) -> Path:
    return Path(cfg["state_dir"]).expanduser() / "paused-wakeups.json"


def pause_midna(cfg: dict) -> int:
    """midna's pending wakeups -> 'cancelled' (its schema has no 'paused'), ids kept in a sidecar so
    `deactivate` can put exactly these back. The sidecar is written before the update."""
    conn = sqlite3.connect(Path(cfg["midna_db"]).expanduser(), timeout=30)
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        ids = [r[0] for r in conn.execute("SELECT id FROM wakeups WHERE status='pending'")]
        f = sidecar(cfg)
        f.parent.mkdir(parents=True, exist_ok=True)
        known = json.loads(f.read_text()) if f.exists() else {"ids": []}
        known["ids"] = sorted(set(known["ids"]) | set(ids))
        known.setdefault("paused_at", now_utc().isoformat(timespec="seconds"))
        f.write_text(json.dumps(known, indent=2) + "\n")
        if ids:
            conn.execute(f"UPDATE wakeups SET status='cancelled' WHERE status='pending' AND id IN "
                         f"({','.join('?' * len(ids))})", ids)
        conn.execute("COMMIT")
        return len(ids)
    finally:
        conn.close()


def unpause_midna(cfg: dict) -> int:
    f = sidecar(cfg)
    if not f.exists():
        return 0
    ids = json.loads(f.read_text())["ids"]
    conn = sqlite3.connect(Path(cfg["midna_db"]).expanduser(), timeout=30)
    try:
        n = conn.execute(f"UPDATE wakeups SET status='pending' WHERE status='cancelled' AND id IN "
                         f"({','.join('?' * len(ids))})", ids).rowcount if ids else 0
        conn.commit()
    finally:
        conn.close()
    f.rename(f.with_name(f"paused-wakeups.restored.{stamp()}.json"))
    return n


def cmd_activate(cfg: dict, args) -> int:
    st = load_state(cfg)
    if st["phase"] not in ("mirror", "activated"):
        raise SystemExit(f"activate: run install first (phase is {st['phase']})")
    log("activate (P3 writer flip)" + (" again: re-checking every step" if st["phase"] == "activated" else ""))
    mirror(cfg)
    res = hermes_side(cfg, "activate", {"brief_at": cfg["brief_at"], "deliver": deliver_to(cfg)})
    for line in res.get("steps", []):
        log(f"- {line}")
    n = pause_midna(cfg)
    log(f"- midna: {n} pending wakeups paused (ids in {sidecar(cfg)})")
    save_state(cfg, phase="activated", activated_at=st.get("activated_at") or now_utc().isoformat(timespec="seconds"))
    log("- " + timer_install(cfg, enable=False))
    log("done. Hermes is the writer; midna's wakeups are paused.")
    return 0


def cmd_deactivate(cfg: dict, args) -> int:
    res = hermes_side(cfg, "deactivate", {})
    for line in res.get("steps", []):
        log(f"- {line}")
    log(f"- midna: {unpause_midna(cfg)} wakeups back to pending")
    save_state(cfg, phase="mirror", activated_at=None)
    log("- " + timer_install(cfg, enable=True))
    log("done. midna is the writer again; Hermes mirrors it. Rows created in Hermes meanwhile stay in "
        "Hermes only.")
    return 0


def cmd_uninstall(cfg: dict, args) -> int:
    if load_state(cfg)["phase"] == "activated":
        log("- deactivating first")
        cmd_deactivate(cfg, args)
    log("- " + body_install({**cfg, "body": False}, ""))
    res = hermes_side(cfg, "uninstall", {"rollback_plugin": cfg["rollback_plugin"]})
    for line in res.get("steps", []):
        log(f"- {line}")
    log("- " + timer_install(cfg, enable=False))
    unit_dir = Path(cfg["unit_dir"]).expanduser()
    for name in timer_units(cfg):
        (unit_dir / name).unlink(missing_ok=True)
    if cfg["systemctl"]:
        _run(["systemctl", "--user", "daemon-reload"], check=False)
    save_state(cfg, phase="uninstalled")
    if cfg["gateway_restart"] and not args.no_gateway_restart:
        log("- " + restart_gateway(cfg))
    log("done.")
    return 0


def cmd_status(cfg: dict, args) -> int:
    st = load_state(cfg)
    log(json.dumps({"config": cfg["_config"], "state": st}, indent=2))
    if cfg["body"]:
        log(f"midna-body: health {'ok' if body_health(cfg, 2) else 'NOT ok'} on 127.0.0.1:{cfg['body_port']}")
    log(json.dumps(hermes_side(cfg, "status", {}, check=False), indent=2))
    return 0


# === the hermes side: runs as Hermes's user, from <HERMES_HOME>/midna/code/<sha> ================

CRON_SCRIPTS = {
    "midna_jobwatch.py": ("jobwatch", ["check"]),
    "midna_oneshot_record.py": ("oneshot_sweep", ["record"]),
    "midna_oneshot_sweep.py": ("oneshot_sweep", ["sweep"]),
}
SHIM = '''# Written by deploy/midna/midna_hermes.py install; re-written on every install.
import sys
sys.path.insert(0, {code!r})
import {module}
sys.exit({module}.main({argv!r}))
'''
JOBWATCH_PROMPT = (
    "A background job you started has finished, or is overdue; the job watcher's report is below. Read "
    "the result file if the excerpt isn't enough, then tell the user briefly what came out of it. If "
    "the note says what to do next and you can do it, do it. Reply [SILENT] only if nothing is worth "
    "saying.")


class HermesSide:
    def __init__(self, opts: dict):
        self.o = opts
        self.home = Path(opts["hermes_home"])
        self.bin = opts.get("hermes_bin") or "hermes"
        self.mdir = self.home / "midna"
        self.code = self.mdir / "code" / "current"
        self.db = self.home / "plugin-data" / "hyperactive" / "hyperactive.db"
        self.steps: list[str] = []
        self.changed = False
        sys.path.insert(0, str(HERE))
        sys.path.insert(0, str(REPO / "dev"))
        sys.path.insert(0, str(REPO / "plugin"))

    def say(self, msg: str) -> None:
        self.steps.append(msg)

    def hermes(self, *args, check: bool = True) -> subprocess.CompletedProcess:
        return _run([self.bin, *args], check=check, env={**os.environ, "HERMES_HOME": str(self.home)}, cwd="/tmp")

    def state(self) -> dict:
        f = self.mdir / "state.json"
        return json.loads(f.read_text()) if f.exists() else {}

    def set_state(self, **kv) -> None:
        f = self.mdir / "state.json"
        f.write_text(json.dumps({**self.state(), **kv}, indent=2) + "\n")

    # --- config ---
    def config_set(self, key: str, value) -> None:
        cur = self.config_get(key)
        if cur is not None and str(cur) == str(value):
            return
        self.hermes("config", "set", key, str(value))
        self.changed = True
        self.say(f"config {key} = {value!r}")

    def config_set_masked(self, key: str, value: str) -> None:
        """For keys `hermes config get` masks (headers.Authorization prints as `Bear...KEN}`)."""
        cur = self.config_get(key)
        if cur in (value, f"{value[:4]}...{value[-4:]}"):
            return
        self.hermes("config", "set", key, value)
        self.changed = True
        self.say(f"config {key} set")

    def config_unset(self, key: str) -> None:
        if self.config_get(key) in (None, ""):
            return
        self.hermes("config", "unset", key)
        self.changed = True
        self.say(f"config {key} removed")

    # --- .env (Hermes reads it at start; secrets never go into config.yaml) ---
    def env_set(self, key: str, value: str | None) -> None:
        """KEY=value in <HERMES_HOME>/.env; None removes the line."""
        f = self.home / ".env"
        lines = [ln for ln in f.read_text().splitlines()] if f.exists() else []
        while lines and not lines[-1].strip():
            lines.pop()
        kept = [ln for ln in lines if not re.match(rf"\s*(export\s+)?{re.escape(key)}=", ln)]
        new = kept + ([f"{key}={value}"] if value is not None else [])
        if new == lines:
            return
        tmp = f.with_name(".env.midna-new")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write("".join(ln + "\n" for ln in new))
        os.replace(tmp, f)
        self.changed = True
        self.say(f".env {key} {'set' if value is not None else 'removed'}")

    def config_get(self, key: str):
        """The configured value as `hermes config get` prints it; None when it isn't set."""
        r = self.hermes("config", "get", key, check=False)
        return r.stdout.strip() if r.returncode == 0 else None

    def ha_setup(self) -> None:
        r = self.hermes("hyperactive", "setup", check=False)
        self.say("hermes hyperactive setup: " + ("ok" if r.returncode == 0 else
                                                  f"FAILED {(r.stderr or r.stdout).strip()[-300:]}"))
        if r.returncode != 0:
            raise SystemExit(1)

    # --- cron ---
    def jobs(self) -> list[dict]:
        import oneshot_sweep  # noqa: PLC0415
        return oneshot_sweep.cron_jobs(self.home)

    def ensure_job(self, name: str, schedule: str, prompt: str | None, *, script: str, no_agent: bool,
                   deliver: str) -> None:
        """Exactly one job called `name` with this spec. The spec's hash is kept with the job id, so
        an unchanged spec is left alone and a changed one is replaced."""
        spec = [schedule, prompt or "", script, no_agent, deliver]
        digest = hashlib.sha1(json.dumps(spec).encode()).hexdigest()[:12]
        specs_file = self.mdir / "cron-specs.json"
        specs = json.loads(specs_file.read_text()) if specs_file.exists() else {}
        same = [j for j in self.jobs() if j.get("name") == name]
        known = specs.get(name, {})
        if len(same) == 1 and same[0].get("id") == known.get("id") and known.get("hash") == digest:
            return
        for j in same:
            self.hermes("cron", "remove", j["id"])
        args = ["cron", "create", schedule] + ([prompt] if prompt else [])
        args += ["--name", name, "--script", script, "--deliver", deliver] + (["--no-agent"] if no_agent else [])
        self.hermes(*args)
        new = [j for j in self.jobs() if j.get("name") == name]
        specs[name] = {"id": new[-1]["id"] if new else None, "hash": digest}
        specs_file.write_text(json.dumps(specs, indent=2) + "\n")
        self.say(f"cron job {name} {'replaced' if same else 'created'} ({schedule}, deliver {deliver})")

    def write_shims(self) -> None:
        scripts = self.home / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        code = str(self.code / "deploy" / "midna")
        for name, (module, argv) in CRON_SCRIPTS.items():
            text = SHIM.format(code=code, module=module,
                               argv=["--hermes-home", str(self.home), *argv] if module == "jobwatch"
                               else [*argv, "--hermes-home", str(self.home), "--hermes", self._abs_bin()])
            f = scripts / name
            if not f.exists() or f.read_text() != text:
                f.write_text(text)
                self.say(f"cron script {f}")

    def _abs_bin(self) -> str:
        return shutil.which(self.bin) or self.bin

    def install_skill(self) -> None:
        src = (self.code / "deploy" / "midna" / "skills" / "midna-jobs" / "SKILL.md").read_text()
        text = src.replace("{{CODE}}", str(self.code)).replace("{{HERMES_HOME}}", str(self.home))
        f = self.home / "skills" / "midna" / "midna-jobs" / "SKILL.md"
        if not f.exists() or f.read_text() != text:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
            self.changed = True
            self.say(f"skill {f.parent}")

    # --- the plugin ---
    @staticmethod
    def _tree_hash(d: Path) -> str:
        h = hashlib.sha1()
        for f in sorted(p for p in d.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
            h.update(str(f.relative_to(d)).encode())
            h.update(f.read_bytes())
        return h.hexdigest()

    def install_plugin(self, identifier: str, ref: str, wanted: Path | None, name: str = "hyperactive") -> None:
        installed = self.home / "plugins" / name
        if wanted is not None and installed.is_dir() and self._tree_hash(installed.resolve()) == self._tree_hash(wanted):
            return
        if installed.is_symlink():  # a dev checkout linked in by hand; the installer owns this from now on
            installed.unlink()
        self.hermes("plugins", "install", identifier, "--ref", ref, "--force", "--enable", "--yes-deps")
        self.changed = True
        self.say(f"plugin {name} installed from {identifier} @ {ref[:12]}")

    # --- persona, body (P2), api_server ---
    def install_soul(self, on: bool) -> None:
        f = self.home / "SOUL.md"
        src = (self.code / "deploy" / "midna" / "persona" / "SOUL.md") if on else (self.home / "backups" / "pre-midna" / "SOUL.md")
        if not src.exists():
            return
        text = src.read_text()
        if f.exists() and f.read_text() == text:
            return
        f.write_text(text)
        f.chmod(0o600)
        self.changed = True
        self.say("SOUL.md = " + ("midna persona" if on else "the pre-midna SOUL.md"))

    def body_config(self, on: bool, url: str, token: str) -> None:
        k = f"mcp_servers.{BODY}."
        if not on:
            self.config_unset(f"mcp_servers.{BODY}")
            self.env_set(BODY_TOKEN_ENV, None)
            return
        self.env_set(BODY_TOKEN_ENV, token)
        self.config_set(k + "url", url)
        self.config_set_masked(k + "headers.Authorization", "Bearer ${%s}" % BODY_TOKEN_ENV)
        self.config_set(k + "trust", "untrusted")  # write-capable device tools ask through the approval buttons
        self.config_set(k + "timeout", 600)
        self.config_set(k + "connect_timeout", 30)
        self.config_set(k + "enabled", "true")

    def api_config(self, on: bool, port: int, key: str) -> None:
        self.env_set("API_SERVER_ENABLED", "true" if on else None)
        self.env_set("API_SERVER_HOST", "127.0.0.1" if on else None)
        self.env_set("API_SERVER_PORT", str(port) if on else None)
        self.env_set("API_SERVER_KEY", key if on else None)

    def app_config(self, url: str, token: str, sha: str | None) -> None:
        """P4: the `midna` platform (deploy/midna/app_platform), send-only into midna serve's outbox.
        With no token (not paired, or turned off) the plugin and its env are taken out again."""
        installed = self.home / "plugins" / APP_PLUGIN
        if token and sha:
            self.install_plugin(f"file://{self.mdir}/hyperactive.git#deploy/midna/app_platform", sha,
                                self.mdir / "code" / sha / "deploy" / "midna" / "app_platform", name=APP_PLUGIN)
            self.env_set("MIDNA_APP_URL", url)
            self.env_set("MIDNA_APP_TOKEN", token)
            self.env_set("MIDNA_APP_HOME_CHANNEL", "main")
            return
        for key in ("MIDNA_APP_URL", "MIDNA_APP_TOKEN", "MIDNA_APP_HOME_CHANNEL"):
            self.env_set(key, None)
        if installed.exists():
            self.hermes("plugins", "remove", APP_PLUGIN, check=False)
            shutil.rmtree(installed, ignore_errors=True)
            self.changed = True
            self.say(f"plugin {APP_PLUGIN} removed")

    def retarget(self, deliver: str) -> None:
        """midna's cron jobs that deliver to the user follow `deliver` (Telegram, plus the midna app once it
        is paired). Jobs delivering elsewhere (`local`, a chat id) are left alone; hyperactive's own
        pulse and brief follow its `deliver` setting through `hermes hyperactive setup`."""
        base = ",".join(t for t in deliver.split(",") if t != APP)
        same = {base, f"{base},{APP}"}
        for j in self.jobs():
            name, cur = str(j.get("name", "")), str(j.get("deliver") or "")
            if not name.startswith(f"{SRC}:"):  # the helper jobs follow it through ensure_job
                continue
            if cur in same and cur != deliver:
                self.hermes("cron", "edit", j["id"], "--deliver", deliver)
                self.changed = True
                self.say(f"cron job {name}: deliver {cur} -> {deliver}")

    def op_backup(self) -> dict:
        root = self.home / "backups" / "midna"
        dest = root / stamp()
        while dest.exists():
            dest = dest.with_name(dest.name + "x")
        dest.mkdir(parents=True, mode=0o700)
        for name in ("config.yaml", "SOUL.md", ".env"):
            if (self.home / name).exists():
                shutil.copy2(self.home / name, dest / name.lstrip("."))
        if self.db.exists():
            s, d = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True), sqlite3.connect(dest / "hyperactive.db")
            s.backup(d)
            d.close()
            s.close()
        for f in dest.iterdir():
            f.chmod(0o600)
        for old in sorted(p for p in root.iterdir() if p.is_dir())[:-int(self.o.get("keep") or 20)]:
            shutil.rmtree(old)
        return {"dir": str(dest)}

    # --- the import ---
    def backup_db(self) -> Path | None:
        """hyperactive.db.pre-midna.<stamp> next to it (what the importer's live guard wants);
        keeps the 3 newest."""
        if not self.db.exists():
            return None
        dest = self.db.with_name(f"{self.db.name}.pre-midna.{stamp()}")
        s = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        d = sqlite3.connect(dest)
        s.backup(d)
        d.close()
        s.close()
        dest.chmod(0o600)
        for old in sorted(self.db.parent.glob(f"{self.db.name}.pre-midna.*"))[:-3]:
            old.unlink()
        return dest

    def run_import(self, snapshot: str, tz: str, deliver: str) -> dict:
        import import_midna  # noqa: PLC0415
        out_root = self.mdir / "mirror"
        out = out_root / stamp()
        while out.exists():  # two runs within a second
            out = out.with_name(out.name + "x")
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self.backup_db()
        args = ["apply", snapshot, "--target", str(self.db), "--out", str(out), "--tz", tz, "--deliver", deliver]
        args += ["--live"] if self.db.exists() else []
        with _quiet_stdout():
            rc = import_midna.main(args)
        plan = json.loads((out / "plan.json").read_text())
        for old in sorted(p for p in out_root.iterdir() if p.is_dir() and p.name != "last")[:-14]:
            shutil.rmtree(old)
        _symlink(out_root / "last", out.name)
        counts = plan["counts"]
        ins = sum(c.get("insert", 0) for c in counts.values())
        upd = sum(c.get("update", 0) for c in counts.values())
        closed = sum(c.get("closed", 0) for c in counts.values())
        return {"rc": rc, "out": str(out), "summary": f"{ins} new, {upd} updated, {closed} closed, "
                f"{len(plan['cron'])} cron jobs planned, problems: {len(plan['problems'])}"}

    # --- ops ---
    def op_p1(self) -> dict:
        o = self.o
        sha = o["sha"]
        bak = self.home / "backups" / "pre-midna"
        if not bak.exists():  # the first install's copy is the rollback point; never overwritten
            tmp = Path(tempfile.mkdtemp(prefix=".pre-midna-", dir=_mkdir(self.home / "backups")))
            for name in ("config.yaml", "SOUL.md"):
                if (self.home / name).exists():
                    shutil.copy2(self.home / name, tmp / name)
            if self.db.exists():
                s, d = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True), sqlite3.connect(tmp / "hyperactive.db")
                s.backup(d)
                d.close()
                s.close()
            meta = self.home / "plugins" / ".install-metadata.json"
            if meta.exists():
                shutil.copy2(meta, tmp / "plugins-install-metadata.json")
            ver = self.hermes("--version", check=False).stdout.strip()
            (tmp / "README").write_text(f"Taken {stamp()} by midna_hermes.py install, before any midna change.\n"
                                        f"hermes --version: {ver}\n")
            os.replace(tmp, bak)
            self.say(f"backup of config.yaml, SOUL.md, hyperactive.db -> {bak}")
        else:
            self.say(f"backup kept from the first install: {bak}")
        self.install_plugin(f"file://{self.mdir}/hyperactive.git#plugin", sha, self.mdir / "code" / sha / "plugin")
        p = "plugins.entries.hyperactive.settings."
        self.config_set("timezone", o["tz"])
        self.config_set("memory.provider", "hyperactive")
        self.config_set(p + "features", o["features"])
        self.config_set(p + "memory_pinned_chars", o["pinned_chars"])
        self.config_set(p + "quiet_sources", o["quiet_sources"])
        self.config_set(p + "brief_at", o["brief_at"])
        self.config_set(p + "deliver", o["deliver"])
        self.ha_setup()
        self.install_skill()
        self.install_soul(bool(o.get("persona", True)))
        self.body_config(bool(o.get("body")), o.get("body_url", ""), o.get("body_token", ""))
        self.api_config(bool(o.get("api_server")), int(o.get("api_port") or 8642), o.get("api_key", ""))
        self.app_config(o.get("app_url", ""), o.get("app_token", ""), sha)
        self.retarget(o["deliver"])
        self.write_shims()
        hh, mm = o["sweep_at"].split(":")
        self.ensure_job("midna-jobwatch", o["jobwatch_every"], JOBWATCH_PROMPT, script="midna_jobwatch.py",
                        no_agent=False, deliver=o["deliver"])
        self.ensure_job("midna-oneshot-ledger", o["ledger_every"], None, script="midna_oneshot_record.py",
                        no_agent=True, deliver="local")
        self.ensure_job("midna-oneshot-sweep", f"{int(mm)} {int(hh)} * * *", None, script="midna_oneshot_sweep.py",
                        no_agent=True, deliver=o["deliver"])
        self.set_state(installed_sha=sha)
        return {"steps": self.steps, "changed": self.changed}

    def op_apply_mirror(self) -> dict:
        o = self.o
        if self.state().get("phase") == "activated":  # authoritative, even if midna's side lost track
            return {"skipped": True, "why": "Hermes is the writer since activation"}
        res = self.run_import(o["snapshot"], o["tz"], o["deliver"])
        if res["rc"] != 0:
            raise SystemExit(f"import reported problems; see {res['out']}/report.txt")
        self.set_state(last_mirror=res["out"])
        return res

    def quiet_flip(self, at: datetime | None = None) -> dict:
        """What midna already raised (or will no longer raise) must not all arrive from Hermes at once:
        mark past-due imported decision reviews and commitments as notified, stale imported goals as
        nudged, and past-due imported reminders as handled. The decision-sweep cron job picks the
        decisions up from there."""
        import pulse  # noqa: PLC0415
        import store  # noqa: PLC0415
        at = at or now_utc()
        cfg = pulse.load_settings(self.db.parent)
        conn = store.connect(self.db)
        now = store.iso(at)
        like = f"{SRC}:%"
        try:
            n = {
                "decisions": conn.execute(
                    "UPDATE decisions SET notified_at=? WHERE status='open' AND review_at IS NOT NULL AND "
                    "review_at<=? AND notified_at IS NULL AND ext_ref LIKE ?", (now, now, like)).rowcount,
                "commitments": conn.execute(
                    "UPDATE commitments SET notified_at=? WHERE status='open' AND due_at IS NOT NULL AND "
                    "due_at<=? AND notified_at IS NULL AND ext_ref LIKE ?", (now, now, like)).rowcount,
                "reminders": conn.execute(
                    "UPDATE followups SET status='skipped', sent_at=? WHERE status='pending' AND due_at<=? AND "
                    "ext_ref LIKE ?", (now, now, like)).rowcount,
            }
            stale = [g["id"] for g in store.stale_goals(conn, at, store.parse_duration(cfg["stale_after"]))
                     if (g["ext_ref"] or "").startswith(f"{SRC}:")]
            for gid in stale:
                conn.execute("UPDATE goals SET nudged_at=? WHERE id=?", (now, gid))
            n["goals"] = len(stale)
            if any(n.values()):
                store.log(conn, "migration.activate", None,
                          "marked as already raised by midna: " + ", ".join(f"{k} {v}" for k, v in n.items()), "",
                          at=at)
            conn.commit()
        finally:
            conn.close()
        return n

    def preview_pulse(self) -> str:
        """What the next pulse would send, unmuted, run on a copy (changes nothing)."""
        import pulse  # noqa: PLC0415
        tmp = Path(tempfile.mkdtemp(prefix="ha-preview-"))
        try:
            s, d = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True), sqlite3.connect(tmp / "hyperactive.db")
            s.backup(d)
            d.close()
            s.close()
            settings = json.loads((self.db.parent / "pulse.json").read_text())
            settings["quiet_sources"] = ""
            settings["quiet_hours"] = ""
            (tmp / "pulse.json").write_text(json.dumps(settings))
            return pulse.run(tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def apply_cron_plan(self) -> None:
        """midna's scheduled wakeups (the last mirror's plan) as `midna:<id>` cron jobs; ones that
        already exist (by name, any state) are left alone, one-shots already past are skipped."""
        plan = json.loads((self.mdir / "mirror" / "last" / "plan.json").read_text())
        names = {j.get("name") for j in self.jobs()}
        made, kept, past = 0, 0, []
        soon = now_utc() + timedelta(minutes=2)
        for c in plan["cron"]:
            if c["name"] in names:
                kept += 1
                continue
            if c["one_shot"] and datetime.fromisoformat(c["schedule"]) <= soon:
                past.append(c["name"])
                continue
            self.hermes("cron", "create", c["schedule"], c["prompt"], "--name", c["name"],
                        "--deliver", self.o["deliver"])
            made += 1
        self.say(f"cron plan: {made} midna:* jobs created, {kept} already there"
                 + (f", {len(past)} one-shots already past skipped ({', '.join(past)})" if past else ""))

    def op_activate(self) -> dict:
        n = self.quiet_flip()
        self.say("quiet flip: marked as already raised by midna: " + ", ".join(f"{k} {v}" for k, v in n.items()))
        p = "plugins.entries.hyperactive.settings."
        self.config_set(p + "quiet_sources", "")
        self.config_set(p + "brief_at", self.o["brief_at"])
        self.ha_setup()
        self.apply_cron_plan()
        import oneshot_sweep  # noqa: PLC0415
        oneshot_sweep.record(self.home)
        preview = self.preview_pulse()
        items = len([b for b in preview.split("\n\n") if b.strip()]) if preview else 0
        self.say(f"next pulse would send {items} item(s)" + (f": {preview[:300]!r}" if items else ""))
        self.set_state(phase="activated", activated_at=now_utc().isoformat(timespec="seconds"))
        return {"steps": self.steps, "pulse_items": items}

    def op_deactivate(self) -> dict:
        removed = 0
        for j in self.jobs():
            if str(j.get("name", "")).startswith(f"{SRC}:"):
                self.hermes("cron", "remove", j["id"], check=False)
                removed += 1
        self.say(f"removed {removed} midna:* cron jobs")
        p = "plugins.entries.hyperactive.settings."
        self.config_set(p + "quiet_sources", SRC)
        self.config_set(p + "brief_at", "")
        self.ha_setup()
        self.set_state(phase="mirror", activated_at=None)
        return {"steps": self.steps}

    def op_uninstall(self) -> dict:
        for j in self.jobs():
            name = str(j.get("name", ""))
            if name.startswith(f"{SRC}:") or name in ("midna-jobwatch", "midna-oneshot-ledger", "midna-oneshot-sweep"):
                self.hermes("cron", "remove", j["id"], check=False)
                self.say(f"removed cron job {name}")
        for name in CRON_SCRIPTS:
            (self.home / "scripts" / name).unlink(missing_ok=True)
        (self.mdir / "cron-specs.json").unlink(missing_ok=True)
        shutil.rmtree(self.home / "skills" / "midna" / "midna-jobs", ignore_errors=True)
        if self.db.exists():
            import import_midna  # noqa: PLC0415
            self.backup_db()
            with _quiet_stdout():
                import_midna.main(["undo", "--target", str(self.db), "--apply", "--live"])
            self.say("imported midna rows deleted (undo); a backup of the DB before that is next to it")
        self.body_config(False, "", "")
        self.api_config(False, 0, "")
        self.app_config("", "", None)
        bak = self.home / "backups" / "pre-midna"
        for name in ("config.yaml", "SOUL.md"):
            if (bak / name).exists():
                shutil.copy2(bak / name, self.home / name)
                self.say(f"{name} restored from {bak}")
        ident, _, ref = self.o["rollback_plugin"].rpartition("@")
        self.install_plugin(ident, ref, None)
        self.ha_setup()
        self.set_state(phase="uninstalled")
        return {"steps": self.steps}

    def op_status(self) -> dict:
        names = sorted(str(j.get("name")) for j in self.jobs() if str(j.get("name", "")).startswith("midna"))
        soul = self.home / "SOUL.md"
        persona = self.code / "deploy" / "midna" / "persona" / "SOUL.md"
        env = (self.home / ".env").read_text() if (self.home / ".env").exists() else ""
        return {"state": self.state(), "jobs": names, "ledger": str(self.mdir / "oneshot-ledger.json"),
                "timezone": self.config_get("timezone"), "memory_provider": self.config_get("memory.provider"),
                "soul": "midna persona" if soul.exists() and persona.exists() and soul.read_text() == persona.read_text()
                else "other", "mcp_midna_body": self.config_get(f"mcp_servers.{BODY}.url"),
                "api_server": "on" if re.search(r"(?m)^API_SERVER_ENABLED=true", env) else "off",
                "midna_app": "on" if re.search(r"(?m)^MIDNA_APP_TOKEN=.", env) else "off"}


class _quiet_stdout:
    """The importer prints its report (also in report.txt); keep stdout for the one JSON result line."""
    def __enter__(self):
        self._old = sys.stdout
        sys.stdout = open(os.devnull, "w")

    def __exit__(self, *exc):
        sys.stdout.close()
        sys.stdout = self._old


def hermes_side_main(op: str) -> int:
    side = HermesSide(json.loads(sys.stdin.read() or "{}"))
    side.mdir.mkdir(parents=True, exist_ok=True)
    result = getattr(side, "op_" + op.replace("-", "_"))()
    print(json.dumps(result))
    return 0


# --- cli -------------------------------------------------------------------------------------

def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["hermes-side"]:
        return hermes_side_main(argv[1])
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=("install", "mirror", "activate", "deactivate", "uninstall", "status", "backup", "check"))
    ap.add_argument("--turn", help="check: also run one agent turn with this input through the api_server")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--no-gateway-restart", action="store_true")
    for key, default in DEFAULTS.items():  # install saves these; the other commands may override them once
        flag = "--" + key.replace("_", "-")
        if isinstance(default, bool):
            ap.add_argument(flag, type=lambda s: s.lower() in ("1", "true", "yes", "on"), default=None)
        else:
            ap.add_argument(flag, type=type(default), default=None)
    args = ap.parse_args(argv)
    overrides = {k: getattr(args, k) for k in DEFAULTS}
    cfg = load_config(Path(args.config).expanduser(), overrides, save=args.command == "install")
    if args.command == "mirror":
        mirror(cfg)
        return 0
    return {"install": cmd_install, "activate": cmd_activate, "deactivate": cmd_deactivate,
            "uninstall": cmd_uninstall, "status": cmd_status, "backup": cmd_backup, "check": cmd_check}[args.command](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
