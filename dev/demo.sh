#!/usr/bin/env bash
# The 3 demo moments from DESIGN.md, end to end on the ISOLATED scratch Hermes with the stub model
# and 1-minute timings (pulse every 1m, card timeout 1m). Prints what the user's phone would show.
# Takes about 8 minutes of real time. Wipes only the scratch plugin's data.
set -euo pipefail
cd "$(dirname "$0")/.."
. .scratch/env.sh
[[ "$HERMES_HOME" == *"/.scratch/home" ]] || { echo "refusing: HERMES_HOME is not the scratch home"; exit 1; }

DATA="$HERMES_HOME/plugin-data/hyperactive"
set_cfg() { hermes config set "plugins.entries.hyperactive.settings.$1" "$2" >/dev/null; }

# Stop only gateways whose HERMES_HOME is this scratch home (hermes chat may auto-start one).
stop_scratch_gateways() {
  for pid in $(pgrep -u "$(id -u)" -f "hermes_cli.main.*gateway run" || true); do
    if tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | grep -qx "HERMES_HOME=$HERMES_HOME"; then
      kill "$pid" 2>/dev/null || true
    fi
  done
}
cleanup() { kill "${STUB:-}" 2>/dev/null || true; set_cfg quiet_hours ""; stop_scratch_gateways; }
trap cleanup EXIT

say()   { printf '\n\033[1m%s\033[0m\n' "$*"; }
note()  { printf '  \033[2m%s\033[0m\n' "$*"; }
you()   { printf '  you     > %s\n' "$1"; }
chat()  {  # one real agent turn; prints the user's line and Hermes's reply
  [[ "${2:-}" == quiet ]] || you "$1"
  local reply
  reply=$(hermes chat -q "$1" --oneshot 2>&1 | grep -A1 '☤ Hermes' | tail -1)
  [[ "${2:-}" == quiet ]] || printf '  hermes  > %s\n' "$reply"
}

# --- the phone: every non-silent pulse delivery, framed ----------------------------------------
SEEN="$(mktemp)"; trap 'cleanup; rm -f "$SEEN"' EXIT
phone() {  # print new deliveries; returns 0 if any were printed
  local shown=1 f
  for f in $(ls -tr "$HERMES_HOME"/cron/output/*/*.md 2>/dev/null); do
    grep -qxF "$f" "$SEEN" && continue
    echo "$f" >> "$SEEN"
    if grep -q '^\*\*Status:\*\* silent' "$f"; then
      [[ -n "${SHOW_SILENT:-}" ]] && note "$(date +%H:%M) pulse ran: nothing sent (quiet hours, held)"
      continue
    fi
    printf '  ┌─ phone · %s %s\n' "$(sed -n 's/^\*\*Run Time:\*\* .* \([0-9:]*\):[0-9]*$/\1/p' "$f")" \
      "──────────────────────────────────────────────────"
    sed '1,/^---$/d' "$f" | sed '/./,$!d' | fold -s -w 64 | sed 's/^/  │ /'
    printf '  └%s\n' "────────────────────────────────────────────────────────────────"
    shown=0
  done
  return $shown
}
wait_phone() {  # tick cron until the phone shows something (or $1 seconds pass)
  local until=$(( $(date +%s) + ${1:-150} ))
  while (( $(date +%s) < until )); do
    hermes cron tick >/dev/null 2>&1 || true
    phone && return 0
    sleep 10
  done
  note "(nothing arrived in time)"; return 1
}

# --- fresh scratch state -----------------------------------------------------------------------
say "Setup: fresh scratch Hermes, stub model, 1-minute timings"
hermes hyperactive uninstall >/dev/null
rm -f "$DATA"/hyperactive.db*
set_cfg pulse_every "every 1m"; set_cfg deliver local; set_cfg card_timeout 1m
set_cfg stale_after 3d; set_cfg quiet_hours ""
hermes config set model.provider custom >/dev/null
hermes config set model.base_url http://127.0.0.1:18766/v1 >/dev/null
hermes config set model.default stub >/dev/null
python dev/stub_llm.py & STUB=$!
sleep 1
# What the user told Hermes earlier in the week.
hermes hyperactive goal year "Win a hackathon prize" >/dev/null
hermes hyperactive goal quarter "Stay fit" >/dev/null
hermes hyperactive goal quarter "Keep the site online" >/dev/null
hermes hyperactive followup 2 "$(date -d 'tomorrow 06:00' +%Y-%m-%dT%H:%M)" "Gym at 6am." >/dev/null
hermes hyperactive setup
ls "$HERMES_HOME"/cron/output/*/*.md 2>/dev/null > "$SEEN" || true

# --- 1. It listens and commits -----------------------------------------------------------------
say "1. It listens and commits"
chat "I want the hackathon PR open by Tuesday."
note "the agent filed it under the year goal and scheduled its own check-in:"
hermes hyperactive status | sed -n '/^Goals/,/^$/p' | sed 's/^/    /'
hermes cron list 2>&1 | grep -E "hyperactive-pulse|Schedule:|Mode:" | sed 's/^ */    /'

# --- 2. It speaks first ------------------------------------------------------------------------
say "2. It speaks first (nobody types; the pulse cron job fires on its own)"
wait_phone 180 || true

start=$(date +%H:%M); end=$(date -d '+3 min' +%H:%M)
set_cfg quiet_hours "$start-$end"; hermes hyperactive setup >/dev/null
hermes hyperactive followup 4 "in 1m" "Morning. The PR still needs screenshots; 20 minutes would do it." >/dev/null
note "quiet hours $start-$end (standing in for 22:00-07:00); a check-in comes due inside them:"
SHOW_SILENT=1 wait_phone 300 || true
set_cfg quiet_hours ""; hermes hyperactive setup >/dev/null

# --- 3. It knows its limits --------------------------------------------------------------------
say "3. It knows its limits (two tap cards, nobody answers)"
chat 'tool: ha_card {"question": "Move gym to 7am tomorrow?", "recommended": "yes", "stakes": "low", "goal_id": 2, "action": {"type": "reschedule", "followup_id": 1, "when": "'"$(date -d 'tomorrow 07:00' +%Y-%m-%dT%H:%M)"'"}}
tool: ha_card {"question": "Pay the $40 domain renewal?", "options": ["pay", "skip"], "recommended": "pay", "stakes": "money", "goal_id": 3, "action": {"type": "schedule", "text": "Renew the domain: it lapses tonight.", "when": "in 1m"}}' quiet
note "(the agent created two cards with ha_card: one low stakes, one money)"
wait_phone 180 || true   # asked
wait_phone 180 || true   # re-asked
wait_phone 180 || true   # gym applied, domain held

say "The user answers the money card with a plain reply"
chat "pay"
wait_phone 180 || true   # the reminder the 'pay' action scheduled

say "/ha why: every message traces to a goal"
hermes hyperactive why | sed 's/^/    /'
say "Done. Stopping the stub model and any scratch gateway."
