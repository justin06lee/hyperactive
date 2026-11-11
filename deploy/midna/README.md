# deploy/midna: midna → Hermes + hyperactive, as installer steps

Justin-specific deployment code. It is not part of the public plugin (`plugin/`) and not in the catalog.
Everything here is stdlib Python, idempotent and safe to re-run. It is written so the midna repo's
installer (`midna install`) can vendor this directory and call it unchanged.

| file | runs as | what |
|---|---|---|
| `midna_hermes.py` | midna (Hermes's side through `sudo -n -u hermes`) | the phase commands below |
| `oneshot_sweep.py` | hermes, as 2 no-agent cron jobs | ledger of pending one-shots (every 10m); nightly re-creation of one-shots Hermes dropped while its gateway was down |
| `jobwatch.py` | hermes, as the `midna-jobwatch` cron job's pre-run script | wakes the agent when a registered background job's result file appears |
| `skills/midna-jobs/SKILL.md` | — | tells the agent to register long jobs and to use `ha_followup` for anything time-critical |
| `persona/SOUL.md` | — | the midna persona, installed as Hermes's `SOUL.md` |
| `midna_body.py` | midna, as the `midna-body.service` user unit | the midna-body MCP server (P2): his Mac over Tailscale ssh, and search over midna's chat archive |

## Commands

```sh
python3 -I deploy/midna/midna_hermes.py install      # P1 (writes ~/.config/midna/hermes-migration.json)
python3 -I deploy/midna/midna_hermes.py mirror       # P1 nightly; the timer runs this
python3 -I deploy/midna/midna_hermes.py activate     # P3 writer flip
python3 -I deploy/midna/midna_hermes.py deactivate   # P3 rollback
python3 -I deploy/midna/midna_hermes.py uninstall    # P1 rollback
python3 -I deploy/midna/midna_hermes.py status
python3 -I deploy/midna/midna_hermes.py backup       # timestamped copy of Hermes's config/SOUL/.env/hyperactive.db
```

**Rollback of one step** is re-running `install` with that step's toggle off (saved, so later
runs keep it off): `--persona false` (pre-midna SOUL.md back), `--body false` (P2: the
`mcp_servers` entry, the token and the service go), `--api-server false`. Turning it back on is
the same with `true`. A gateway restart happens only if something changed.

`install` takes every setting as an option (`--hermes-home`, `--midna-db`, `--ref`, `--deliver`,
`--mirror-at`, ...; see `DEFAULTS` in `midna_hermes.py`) and saves them; the other commands read
them back. The defaults are the live Jetson layout.

### install (P1)
1. Stages the repo at `--ref` (resolved to a sha) twice: for midna in `~/.local/share/midna/hermes/code/<sha>`
   (what the timer runs) and for Hermes as a bare repo `~hermes/.hermes/midna/hyperactive.git` plus
   `~hermes/.hermes/midna/code/<sha>`.
2. Hermes's side, once: copies `config.yaml`, `SOUL.md`, `hyperactive.db` and the plugin install
   record to `~hermes/.hermes/backups/pre-midna/`. Later runs never overwrite this (it is the P1
   rollback point).
3. Installs hyperactive from `file://~hermes/.hermes/midna/hyperactive.git#plugin --ref <sha>`
   (`--force --enable`), only if the installed files differ. The public catalog is not involved.
4. Sets `timezone`, `memory.provider: hyperactive`, and the plugin settings: `features`,
   `memory_pinned_chars`, `quiet_sources: midna`, `brief_at: ""`, `deliver`. Only changed keys are
   written. Then it runs `hermes hyperactive setup`.
5. Installs the `midna-jobs` skill, the cron shims in `~hermes/.hermes/scripts/`, and the jobs
   `midna-jobwatch` (every 2m, agent), `midna-oneshot-ledger` (every 10m, no agent), and
   `midna-oneshot-sweep` (04:10, no agent). A job is replaced only when its spec changes.
6. Mirror import (below).
7. Writes and enables `midna-hermes-mirror.{service,timer}` (midna's systemd user units, 03:40 Asia/Seoul,
   `Persistent=true`).
8. Restarts the gateway only if the plugin, config, skill, SOUL or .env changed.

Also, on every run (each one a toggle, default on):
- **Backup first:** `~hermes/.hermes/backups/midna/<stamp>/` gets `config.yaml`, `SOUL.md`, `env`
  (the `.env`) and `hyperactive.db`; the 20 newest are kept. `backups/pre-midna/` stays the P1 rollback point.
- **Persona:** `SOUL.md` = `persona/SOUL.md` (voice and values only; facts come from the memory provider's pinned memories).
- **midna-body (P2):** midna's user unit `midna-body.service` serves MCP on `127.0.0.1:8791/mcp`
  (bearer token in `~/.local/state/midna-hermes/midna-body.token`, mode 600; restarted when the code
  sha or unit changes). Hermes gets `MIDNA_BODY_TOKEN` in its `.env` and
  `mcp_servers.midna-body: {url, headers.Authorization: "Bearer ${MIDNA_BODY_TOKEN}", trust: untrusted, timeout: 600}`.
  `untrusted` means Hermes asks Justin (approval buttons) before any tool without `readOnlyHint`:
  `device_shell`, `device_write_file`, `device_applescript`, `device_clipboard_write`. Status, list, read,
  screenshot, clipboard read and the archive tools run on their own (rec 3). Every device call is logged
  to `~/.local/state/midna-hermes/midna-body.jsonl`.
- **api_server:** `API_SERVER_ENABLED/HOST/PORT/KEY` in Hermes's `.env` (127.0.0.1:8642). The key lives
  in midna's secrets (`~/.local/share/midna/secrets/hermes_api_server_key`), which is what P4's brain
  adapter in `midna serve` will use.

### mirror (P1 nightly)
- Snapshot of midna.db through the backup API (read-only) into a private temp dir, group-readable by `hermes`.
- Then, as hermes: a `hyperactive.db.pre-midna.<stamp>` backup (the 3 newest are kept), and `import_midna apply --live`.
  Reports go to `~hermes/.hermes/midna/mirror/<stamp>/`, the 14 newest are kept, and `last` points to the newest.
- Reminders midna has since sent or cancelled are closed in Hermes too. Rows deleted in midna are kept and listed.
- Does nothing once activated, checked on both sides.

### activate (P3)
1. Final mirror import.
2. Quiet flip. Imported decision reviews and commitments that are past due are marked notified,
   stale imported goals are marked nudged, and past-due imported reminders are marked skipped. This
   prevents a flood of old items; the decision-sweep cron job handles the decisions from there.
3. `quiet_sources: ""`, `brief_at: 08:00`, `hermes hyperactive setup`.
4. Creates the `midna:<id>` cron jobs from the last plan. It skips existing names and one-shots that are already past.
5. Previews the next unmuted pulse on a copy and prints the item count. On the Nov 10 data it is 0.
6. Pauses midna's pending wakeups. They are set to `cancelled` (midna's schema has no `paused`), and
   the ids go to `~/.local/state/midna-hermes/paused-wakeups.json`.
7. Disables the mirror timer.

### deactivate / uninstall
- `deactivate`:
  - removes the `midna:*` jobs, sets `quiet_sources: midna` and `brief_at: ""`;
  - puts exactly the sidecar's wakeups back to `pending`;
  - re-enables the timer.
  - Rows created in Hermes while it was the writer stay in Hermes (`--export-new` is still unwritten).
- `uninstall`:
  - deactivates first, if needed;
  - removes the helper jobs, shims and skill;
  - `import_midna undo` (after a DB backup);
  - restores `config.yaml` and `SOUL.md` from `backups/pre-midna`;
  - reinstalls `--rollback-plugin` (default: the catalog's 0.3.0 pin);
  - removes the timer and restarts the gateway.

## Missed one-shots (Hermes 0.22.5)
- **Agent one-shot crons:** Hermes deletes one unrun if its gateway is down for more than 120 s past its time.
  It writes a `# Cron job removed before firing` file into `cron/output/<job id>/`, but that file has no prompt.
  - The ledger job copies every pending one-shot every 10 minutes, so the prompt is kept.
  - The sweep re-creates each dropped job from its diagnostic, 3 minutes ahead, marked as a late run.
  - Each dropped job is re-created at most 3 times.
  - Jobs the user removes leave no diagnostic and are never brought back.
- **Time-critical reminders:** these go through `ha_followup` (kind `reminder`). It needs no model,
  and the pulse delivers it late rather than dropping it.
  - The tool description, the skill and the migrated cron prompts all say so.

## Long jobs from the app
- Hermes's own completion wake (`notify=true`) doesn't reach the api_server, and a gateway restart loses it.
- The skill tells the agent to write a result file and register it:
  `jobwatch.py add <file> --note ...`.
- `midna-jobwatch` then wakes the agent within about 2 minutes of the file settling. Each job is reported once.
- A job with no result after 7 days is reported once as overdue.

## midna-body tools
| tool | approval | what |
|---|---|---|
| `device_status` | no | name, macOS, uptime, battery, frontmost app, idle seconds |
| `device_list`, `device_read_file` | no | `ls -la`; first `max_bytes` of a file |
| `device_screenshot` | no | `screencapture` + `sips` resize, returned as an image |
| `device_clipboard_read` | no | `pbpaste` |
| `device_shell` | yes | zsh login shell, command sent base64 on stdin, timeout ≤ 570 s |
| `device_write_file`, `device_applescript`, `device_clipboard_write` | yes | |
| `history_search` | no | FTS5 over midna.db `messages` (read-only), role/since/until filters, snippets with ids |
| `history_read` | no | a message with its neighbours in the same conversation |

Local check without Hermes: `python3 -I deploy/midna/midna_body.py call history_search '{"query":"fajr"}'`.

## P4: the midna app (`--app`, default on)
Chats from the app reach Hermes through the api_server (midna's `hermes` brain). The other direction,
what Hermes sends on its own (the pulse, the brief, `midna:*` and helper cron jobs), goes through
`app_platform/`: a send-only Hermes platform plugin, `midna-app`, registering the platform `midna`. Its
`send` POSTs to `midna serve`'s `/v1/deliver` (a device token of kind `bridge`), which puts the text in the
app's conversation and outbox as a notification, push included, without midna's quiet hours (the pulse
keeps its own). Hermes's cron wrapper ("Cronjob Response: …") is stripped.

- `install` with `secrets/hermes_app_token` present: installs the plugin from the staged bare repo, sets
  `MIDNA_APP_URL`/`MIDNA_APP_TOKEN`/`MIDNA_APP_HOME_CHANNEL` in Hermes's `.env`, and delivers to
  `<deliver>,midna`: hyperactive's `deliver`, the helper jobs, and every `midna:*` job (`hermes cron edit`).
  Without the token it delivers to `<deliver>` only and says what it waits for.
- Off: `install --app false` takes the plugin and env out and puts the jobs back on `<deliver>`.
- Agent-made cron jobs that deliver to `origin` (a Telegram chat) still go to Telegram only.

## What the midna repo's `midna install` must call
The end state ships from the midna repo (Justin, Nov 10). This directory is written to be vendored
there unchanged (e.g. as `vendor/hyperactive` at a pinned sha); `midna install` then needs only:

1. Prerequisites it already owns: the `midna` user with `midna.db`, ssh access to the Mac (key in
   `~midna/.ssh`, host in `--mac`), Hermes installed for the `hermes` user with its gateway unit, the
   hermes-patch bridge, and `sudo -n -u hermes` allowed for `midna`.
2. `python3 -I <vendor>/deploy/midna/midna_hermes.py install --repo <vendor> --ref <pinned sha> [--mac user@host]`.
   This is everything P1 + persona + P2 + api_server, idempotent, and safe to re-run on every `midna install`/update.
3. At the writer flip (P3), once: `midna_hermes.py activate`. `midna install` must not run it on its own;
   it should read `~/.local/state/midna-hermes/state.json` and keep whatever phase is recorded (install already does this).
4. P4: `midna hermes setup` (midna repo, `clients/cli/src/hermes.ts`; `midna host install` runs it) clones
   this repo at its pinned sha into `~/.local/share/midna/vendor/hyperactive`, pairs Hermes with
   `midna serve` as a bridge (token in `secrets/hermes_app_token`), runs step 2 with `--repo`/`--ref`/`--app-url`,
   and once the phase is `activated` sets `midna model hermes`: app chats become Hermes runs
   (`/v1/runs` + SSE, key from `hermes_api_server_key`).
5. `midna restore`: restoring `midna.db` then re-running `install` re-mirrors it while phase is `mirror`. After
   activation, Hermes's own data is the source of truth: restore `~hermes/.hermes/backups/midna/<stamp>/`
   (config, SOUL, env, hyperactive.db) and restart the gateway.

Hand steps that remain: none for P1/P2 on this Jetson. On a fresh machine, the Mac side (Tailscale, the
ssh key in `authorized_keys`, Remote Login on) is the only thing outside the installer.
