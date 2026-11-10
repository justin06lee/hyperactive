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

## Commands

```sh
python3 -I deploy/midna/midna_hermes.py install      # P1 (writes ~/.config/midna/hermes-migration.json)
python3 -I deploy/midna/midna_hermes.py mirror       # P1 nightly; the timer runs this
python3 -I deploy/midna/midna_hermes.py activate     # P3 writer flip
python3 -I deploy/midna/midna_hermes.py deactivate   # P3 rollback
python3 -I deploy/midna/midna_hermes.py uninstall    # P1 rollback
python3 -I deploy/midna/midna_hermes.py status
```

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
8. Restarts the gateway only if the plugin, config or skill changed.

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
