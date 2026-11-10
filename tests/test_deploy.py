"""deploy/midna: the one-shot sweep, the job watcher, the mirror re-import, the P3 quiet flip and
midna pause, and the installer's settings and timer. Hermes-free, except the end-to-end test, which
runs every installer command against a throwaway HERMES_HOME when HA_TEST_HERMES names a hermes
binary (it installs plugins, so it takes a few minutes): python -m pytest tests"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy" / "midna"))
sys.path.insert(0, str(ROOT / "plugin"))
sys.path.insert(0, str(ROOT / "dev"))
sys.path.insert(0, str(ROOT / "tests"))

import import_midna  # noqa: E402
import jobwatch  # noqa: E402
import midna_hermes  # noqa: E402
import oneshot_sweep  # noqa: E402
import store  # noqa: E402
from test_p0 import midna  # noqa: E402,F401  (the fixture: a small midna.db)

NOW = datetime(2026, 11, 10, 3, 0, tzinfo=timezone.utc)


# --- the one-shot sweep --------------------------------------------------------------------

FAKE_HERMES = textwrap.dedent('''\
    #!/usr/bin/env python3
    # `hermes cron create <schedule> <prompt> --name N --deliver D ...`, appended to jobs.json
    import json, os, sys, uuid
    a = sys.argv[1:]
    assert a[:2] == ["cron", "create"], a
    if os.environ.get("FAKE_FAIL"):
        sys.exit("boom")
    f = os.path.join(os.environ["HERMES_HOME"], "cron", "jobs.json")
    data = json.load(open(f))
    opt = {a[i]: a[i + 1] for i in range(4, len(a) - 1) if a[i].startswith("--")}
    data["jobs"].append({"id": uuid.uuid4().hex[:12], "name": opt.get("--name"), "prompt": a[3],
                         "deliver": opt.get("--deliver"), "schedule": {"kind": "once", "run_at": a[2]},
                         "skills": [s for i, s in enumerate(a) if i and a[i - 1] == "--skill"],
                         "last_run_at": None, "argv": a})
    json.dump(data, open(f, "w"))
''')


@pytest.fixture
def hermes_home(tmp_path):
    home = tmp_path / "hermes"
    (home / "cron" / "output").mkdir(parents=True)
    (home / "config.yaml").write_text("model:\n  default: x\ntimezone: Asia/Seoul\n")
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": []}))
    fake = tmp_path / "hermes-bin"
    fake.write_text(FAKE_HERMES)
    fake.chmod(0o755)
    return home, str(fake)


def _job(home, jid, name, run_at, **kw):
    data = json.loads((home / "cron" / "jobs.json").read_text())
    data["jobs"].append({"id": jid, "name": name, "prompt": f"do {name}", "deliver": "telegram",
                         "skills": ["midna-jobs"], "schedule": {"kind": "once", "run_at": run_at},
                         "last_run_at": None, **kw})
    (home / "cron" / "jobs.json").write_text(json.dumps(data))


def _drop(home, jid, name, run_at):
    """What Hermes 0.22.5 does to a one-shot more than 120 s late: delete it, leave a diagnostic."""
    data = json.loads((home / "cron" / "jobs.json").read_text())
    data["jobs"] = [j for j in data["jobs"] if j["id"] != jid]
    (home / "cron" / "jobs.json").write_text(json.dumps(data))
    d = home / "cron" / "output" / jid
    d.mkdir()
    (d / "2026-11-10_12-00-00.md").write_text(
        "# Cron job removed before firing (run time outside grace window)\n\n"
        f"- job id: {jid}\n- name: {name}\n- scheduled run time: {run_at}\n- grace window: 120s\n"
        "- removed at: 2026-11-10T12:00:00+09:00\n\nThis one-shot's run time is ...\n")


def test_sweep_recreates_dropped_oneshots_once(hermes_home):
    home, fake = hermes_home
    _job(home, "aaa", "midna:1435", "2026-11-10T11:30:00+09:00")  # dropped while the gateway was down
    _job(home, "bbb", "removed-by-user", "2026-11-10T11:00:00+09:00")
    _job(home, "ccc", "recurring", None, schedule={"kind": "cron", "expr": "0 9 * * *"})
    oneshot_sweep.record(home, NOW - timedelta(hours=2))
    _drop(home, "aaa", "midna:1435", "2026-11-10T11:30:00+09:00")
    data = json.loads((home / "cron" / "jobs.json").read_text())  # the user removes bbb: no diagnostic
    data["jobs"] = [j for j in data["jobs"] if j["id"] != "bbb"]
    (home / "cron" / "jobs.json").write_text(json.dumps(data))

    text = oneshot_sweep.sweep(home, fake, NOW)
    assert "midna:1435 (due Tue Nov 10 11:30) was dropped" in text and "re-created for Tue Nov 10 12:03" in text
    assert "removed-by-user" not in text
    jobs = [j for j in json.loads((home / "cron" / "jobs.json").read_text())["jobs"] if j["name"] == "midna:1435"]
    assert len(jobs) == 1
    new = jobs[0]
    assert new["schedule"]["run_at"] == "2026-11-10T12:03:00+09:00"
    assert new["prompt"].startswith("[Late run: this one-shot was due Tue Nov 10 11:30") and new["prompt"].endswith("do midna:1435")
    assert new["deliver"] == "telegram" and new["skills"] == ["midna-jobs"]
    assert "recurring" not in json.dumps(oneshot_sweep.load_ledger(home)["jobs"])

    assert oneshot_sweep.sweep(home, fake, NOW + timedelta(minutes=1)) == ""  # each diagnostic once
    ledger = oneshot_sweep.load_ledger(home)
    assert ledger["jobs"][new["id"]]["attempts"] == 1
    assert ledger["jobs"][new["id"]]["orig_prompt"] == "do midna:1435"  # no "[Late run" piling up


def test_sweep_reports_unknown_and_retries_failures(hermes_home, monkeypatch):
    home, fake = hermes_home
    _drop(home, "zzz", "made-and-lost", "2026-11-10T11:00:00+09:00")  # never seen by `record`
    text = oneshot_sweep.sweep(home, fake, NOW)
    assert "made-and-lost" in text and "prompt is unknown" in text
    assert oneshot_sweep.sweep(home, fake, NOW) == ""

    _job(home, "yyy", "midna:9", "2026-11-10T11:00:00+09:00")
    oneshot_sweep.record(home, NOW)
    _drop(home, "yyy", "midna:9", "2026-11-10T11:00:00+09:00")
    monkeypatch.setenv("FAKE_FAIL", "1")
    assert "could not re-create" in oneshot_sweep.sweep(home, fake, NOW)
    monkeypatch.delenv("FAKE_FAIL")
    assert "re-created" in oneshot_sweep.sweep(home, fake, NOW)  # retried, not marked handled on failure


def test_sweep_gives_up_after_three(hermes_home):
    home, fake = hermes_home
    _job(home, "j0", "flaky", "2026-11-10T11:00:00+09:00")
    oneshot_sweep.record(home, NOW)
    jid = "j0"
    for i in range(4):
        _drop(home, jid, "flaky", "x")
        text = oneshot_sweep.sweep(home, fake, NOW + timedelta(hours=i))
        if i < 3:
            assert "re-created" in text
            jid = [j for j in json.loads((home / "cron" / "jobs.json").read_text())["jobs"] if j["name"] == "flaky"][-1]["id"]
        else:
            assert "gave up after 3" in text


def test_sweep_cli_is_silent_with_nothing_to_do(hermes_home):
    home, fake = hermes_home
    r = subprocess.run([sys.executable, "-I", str(ROOT / "deploy/midna/oneshot_sweep.py"), "sweep",
                        "--hermes-home", str(home), "--hermes", fake], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == ""


# --- the job watcher -----------------------------------------------------------------------

def test_jobwatch_wakes_once_when_the_result_settles(tmp_path):
    home = tmp_path / "hermes"
    result = tmp_path / "job" / "result.md"
    e = jobwatch.add(home, str(result), "scrape the listings; send the top 5")
    assert jobwatch.add(home, str(result), "new note")["id"] == e["id"]  # same file, same watch
    assert jobwatch.check(home) == ""  # no result yet: empty, so Hermes skips the model call
    result.parent.mkdir()
    result.write_text("x" * 5000)
    assert jobwatch.check(home, settle=20) == ""  # still being written
    out = jobwatch.check(home, settle=20, now=result.stat().st_mtime + 30)
    assert f"Background job {e['id']} finished" in out and "new note" in out and "5000 bytes" in out
    assert jobwatch.check(home, now=result.stat().st_mtime + 60) == ""  # retired: once only
    assert json.loads((home / "midna/jobwatch/done" / f"{e['id']}.json").read_text())["retired"] == "finished"


def test_jobwatch_expires_and_cli(tmp_path):
    home = tmp_path / "hermes"
    jobwatch.add(home, str(tmp_path / "never.md"), "never finishes", "slow-1")
    out = jobwatch.check(home, now=datetime.now().timestamp() + 8 * 86400)
    assert "slow-1 has no result after 7 days" in out
    assert jobwatch.watches(home) == []
    r = subprocess.run([sys.executable, "-I", str(ROOT / "deploy/midna/jobwatch.py"), "--hermes-home", str(home),
                        "add", str(tmp_path / "r.md"), "--note", "n", "--id", "bad id!"], capture_output=True, text=True)
    assert r.returncode != 0 and "bad id" in r.stderr


# --- the mirror re-import ------------------------------------------------------------------

def test_mirror_rerun_closes_sent_reminders_and_tolerates_deletions(tmp_path, midna):
    target = tmp_path / "hyperactive.db"
    out = tmp_path / "out"
    assert import_midna.main(["apply", str(midna), "--target", str(target), "--out", str(out)]) == 0
    src = sqlite3.connect(midna)
    src.execute("UPDATE wakeups SET status='done' WHERE id=4")  # midna sent the Dhuhr reminder
    src.execute("DELETE FROM notes WHERE id=5")
    src.commit()
    src.close()
    assert import_midna.main(["apply", str(midna), "--target", str(target), "--out", str(out)]) == 0
    plan = json.loads((out / "plan.json").read_text())
    assert plan["problems"] == [] and plan["counts"]["followups"] == {"closed": 1}
    assert any("notes: 1 rows imported earlier" in w for w in plan["warnings"])
    conn = store.connect(target)
    assert conn.execute("SELECT status FROM followups WHERE ext_ref='midna:wakeups:4'").fetchone()[0] == "sent"
    assert conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 1  # kept, not deleted
    conn.close()
    assert import_midna.main(["apply", str(midna), "--target", str(target), "--out", str(out)]) == 0
    again = json.loads((out / "plan.json").read_text())
    assert again["problems"] == [] and "followups" not in again["counts"]  # closed once, then left alone


def test_cron_prompts_send_time_critical_reminders_model_free(tmp_path, midna):
    import_midna.main(["plan", str(midna), "--target", str(tmp_path / "t.db"), "--out", str(tmp_path / "o")])
    prompt = json.loads((tmp_path / "o" / "plan.json").read_text())["cron"][1]["prompt"]
    assert "ha_followup (kind reminder) for anything that must arrive on time" in prompt


# --- P3: quiet flip, pause midna --------------------------------------------------------------

def test_quiet_flip_marks_only_past_due_imported_rows(tmp_path, midna):
    home = tmp_path / "hermes"
    db = home / "plugin-data" / "hyperactive" / "hyperactive.db"
    db.parent.mkdir(parents=True)
    (db.parent / "pulse.json").write_text(json.dumps({"timezone": "Asia/Seoul", "quiet_sources": "midna"}))
    import_midna.main(["apply", str(midna), "--target", str(db), "--out", str(tmp_path / "o")])
    conn = store.connect(db)
    own = store.add_decision(conn, "Hermes's own", "x", review_in="1m")["id"]  # created in Hermes: untouched
    conn.execute("UPDATE decisions SET review_at=? WHERE id=?", (store.iso(NOW - timedelta(days=1)), own))
    conn.execute("UPDATE followups SET due_at=? WHERE ext_ref='midna:wakeups:4'", (store.iso(NOW - timedelta(hours=1)),))
    conn.commit()
    conn.close()

    side = midna_hermes.HermesSide({"hermes_home": str(home)})
    n = side.quiet_flip(at=datetime.now(timezone.utc))
    assert n == {"decisions": 1, "commitments": 0, "reminders": 1, "goals": n["goals"]}
    assert side.quiet_flip() == {"decisions": 0, "commitments": 0, "reminders": 0, "goals": 0}  # idempotent
    conn = store.connect(db)
    assert conn.execute("SELECT notified_at FROM decisions WHERE id=?", (own,)).fetchone()[0] is None
    conn.close()
    # unmuted, the next pulse sends Hermes's own decision and nothing midna already raised
    (db.parent / "pulse.json").write_text(json.dumps({"timezone": "Asia/Seoul"}))
    assert side.preview_pulse().count("Review time") == 1 and "Hermes's own" in side.preview_pulse()


def test_pause_and_unpause_midna_wakeups(tmp_path, midna):
    cfg = {**midna_hermes.DEFAULTS, "midna_db": str(midna), "state_dir": str(tmp_path / "state")}
    assert midna_hermes.pause_midna(cfg) == 5
    assert midna_hermes.pause_midna(cfg) == 0  # idempotent; the sidecar keeps all five
    c = sqlite3.connect(midna)
    c.execute("INSERT INTO wakeups VALUES (7, 1, 'later', NULL, 'followup', 'cancelled', 'midna', 1, NULL, NULL)")
    c.commit()
    assert midna_hermes.unpause_midna(cfg) == 5
    st = dict(c.execute("SELECT id, status FROM wakeups").fetchall())
    assert [st[i] for i in (1, 2, 3, 4, 5, 6, 7)] == ["pending"] * 4 + ["done", "pending", "cancelled"]
    assert midna_hermes.unpause_midna(cfg) == 0


# --- installer settings, timer, mirror guard -------------------------------------------------

def test_config_saves_install_options_and_timer_units(tmp_path):
    path = tmp_path / "cfg.json"
    cfg = midna_hermes.load_config(path, {"hermes_home": "/x", "mirror_at": "03:40", "systemctl": None}, save=True)
    assert json.loads(path.read_text())["hermes_home"] == "/x"
    assert midna_hermes.load_config(path)["hermes_home"] == "/x"  # later commands need no options
    units = midna_hermes.timer_units(cfg)
    assert "OnCalendar=*-*-* 03:40:00 Asia/Seoul" in units["midna-hermes-mirror.timer"]
    assert "Persistent=true" in units["midna-hermes-mirror.timer"]
    assert f"mirror --config {path}" in units["midna-hermes-mirror.service"]
    cfg = {**cfg, "unit_dir": str(tmp_path / "units"), "systemctl": False}
    assert "written" in midna_hermes.timer_install(cfg, enable=True)
    assert "unchanged" in midna_hermes.timer_install(cfg, enable=True)
    if shutil.which("systemd-analyze"):
        r = subprocess.run(["systemd-analyze", "calendar", "*-*-* 03:40:00 Asia/Seoul"], capture_output=True, text=True)
        assert r.returncode == 0


def test_as_hermes_fills_the_uid():
    cfg = {**midna_hermes.DEFAULTS, "hermes_user": "root"}
    assert midna_hermes.as_hermes(cfg)[-1] == "XDG_RUNTIME_DIR=/run/user/0"
    assert midna_hermes.as_hermes({**cfg, "as_hermes": ""}) == []


def test_mirror_does_nothing_after_activation(tmp_path, capsys):
    cfg = {**midna_hermes.DEFAULTS, "state_dir": str(tmp_path / "s")}
    midna_hermes.save_state(cfg, phase="activated")
    assert midna_hermes.mirror(cfg) == {"skipped": True}
    assert "Hermes is the writer" in capsys.readouterr().out


# --- end to end against a real Hermes (opt-in) -----------------------------------------------

@pytest.mark.skipif(not os.environ.get("HA_TEST_HERMES"), reason="set HA_TEST_HERMES=<hermes binary>")
def test_installer_end_to_end(tmp_path, midna):
    hermes = os.environ["HA_TEST_HERMES"]
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "config.yaml").write_text("model:\n  provider: custom\n  base_url: http://127.0.0.1:9/v1\n"
                                      "  default: x\ntimezone: America/Los_Angeles\n")
    (home / "SOUL.md").write_text("before\n")
    env = {**os.environ, "HERMES_HOME": str(home)}
    # the "previous" hyperactive, from a local bare repo at the 0.3.0 commit (no network)
    old = tmp_path / "old.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(ROOT), str(old)], check=True)
    pin = f"file://{old}#plugin@2f147d3e6a1a91389d4d50991b46a546b5ea804b"
    subprocess.run([hermes, "plugins", "install", pin.split("@")[0], "--ref", pin.split("@")[1], "--enable",
                    "--yes-deps"], env=env, check=True, capture_output=True, cwd="/tmp")
    head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    cfgf = tmp_path / "cfg.json"
    base = ["--config", str(cfgf), "--no-gateway-restart"]

    def run(*cmd):
        assert midna_hermes.main([*cmd, *base]) == 0

    run("install", "--hermes-home", str(home), "--as-hermes", "", "--hermes-bin", hermes,
        "--hermes-python", sys.executable, "--hermes-group", "", "--midna-db", str(midna),
        "--local-home", str(tmp_path / "local"), "--state-dir", str(tmp_path / "state"),
        "--unit-dir", str(tmp_path / "units"), "--systemctl", "false", "--deliver", "local",
        "--ref", head, "--rollback-plugin", pin)

    def jobs():
        return {j["name"]: j for j in oneshot_sweep.cron_jobs(home)}

    def get(key):
        return subprocess.run([hermes, "config", "get", key], env=env, capture_output=True, text=True, cwd="/tmp").stdout.strip()

    s = "plugins.entries.hyperactive.settings."
    assert "version: 0.4.0" in (home / "plugins/hyperactive/plugin.yaml").read_text()
    assert get("timezone") == "Asia/Seoul" and get(s + "quiet_sources") == "midna" and get("memory.provider") == "hyperactive"
    assert {"hyperactive-pulse", "midna-jobwatch", "midna-oneshot-ledger", "midna-oneshot-sweep"} <= set(jobs())
    assert "hyperactive-brief" not in jobs()  # off while mirroring
    assert "timezone: America/Los_Angeles" in (home / "backups/pre-midna/config.yaml").read_text()
    db = home / "plugin-data/hyperactive/hyperactive.db"
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM goals WHERE ext_ref LIKE 'midna:%'").fetchone()[0] == 3
    before = {n: j["id"] for n, j in jobs().items()}

    run("install")  # again: nothing changes
    assert {n: j["id"] for n, j in jobs().items()} == before
    run("mirror")

    run("activate")
    assert get(s + "quiet_sources") == "" and "hyperactive-brief" in jobs()
    assert {n for n in jobs() if n.startswith("midna:")} == {"midna:2", "midna:3"}
    assert sqlite3.connect(midna).execute("SELECT COUNT(*) FROM wakeups WHERE status='pending'").fetchone()[0] == 0
    run("activate")  # again: no duplicate jobs, no re-import
    assert sorted(j["name"] for j in oneshot_sweep.cron_jobs(home)).count("midna:2") == 1
    run("mirror")
    assert len(list((home / "midna/mirror").glob("2*"))) == 4  # install x2, mirror, activate; none after

    run("deactivate")
    assert not [n for n in jobs() if n.startswith("midna:")] and get(s + "quiet_sources") == "midna"
    assert sqlite3.connect(midna).execute("SELECT COUNT(*) FROM wakeups WHERE status='pending'").fetchone()[0] == 5

    run("uninstall")
    assert "version: 0.3.0" in (home / "plugins/hyperactive/plugin.yaml").read_text()
    assert "timezone: America/Los_Angeles" in (home / "config.yaml").read_text()
    assert not [n for n in jobs() if n.startswith("midna")]
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM goals WHERE ext_ref LIKE 'midna:%'").fetchone()[0] == 0
