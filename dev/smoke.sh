#!/usr/bin/env bash
# End-to-end smoke test against an ISOLATED scratch Hermes (never ~/.hermes, never the live gateway).
# Prereq (once): see DESIGN.md "Dev setup" -> .scratch/venv with Hermes editable-installed (Python 3.14).
set -euo pipefail
cd "$(dirname "$0")/.."
. .scratch/env.sh
[[ "$HERMES_HOME" == *"/.scratch/home" ]] || { echo "refusing: HERMES_HOME is not the scratch home"; exit 1; }

mkdir -p "$HERMES_HOME/plugins"
ln -sfn "$PWD/plugin" "$HERMES_HOME/plugins/hyperactive"
hermes plugins validate plugin | tail -1
hermes plugins enable hyperactive >/dev/null
hermes config set plugins.entries.hyperactive.settings.pulse_every "every 1m" >/dev/null
hermes config set plugins.entries.hyperactive.settings.deliver local >/dev/null

# Stub model (no API key, no network): scripted OpenAI-compatible server.
hermes config set model.provider custom >/dev/null
hermes config set model.base_url http://127.0.0.1:18766/v1 >/dev/null
hermes config set model.default stub >/dev/null
python dev/stub_llm.py & STUB=$!
trap 'kill $STUB 2>/dev/null' EXIT
sleep 1

# 1. A chat turn: the "model" records a goal; the plugin schedules its own follow-up in 1m.
hermes chat -q "goal: Smoke-test hyperactive $(date +%H%M%S)" --oneshot | grep -A1 "Hermes"
hermes hyperactive status
# 2. Wait until due, then tick cron (a running scratch gateway would also fire it on its own).
sleep 70
hermes cron tick >/dev/null
latest=$(ls -t "$HERMES_HOME"/cron/output/*/*.md | head -1)
echo "--- delivered by cron ($latest):"
tail -n +8 "$latest"
