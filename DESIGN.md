# hyperactive: design (draft, Nov 6)

**What it is.** Most agents wait to be asked; hyperactive is a Hermes plugin that keeps a person's
objective tree (life > year > quarter > week), commitments and decisions, and then acts on its own
schedule: follow-ups, reminders, a daily brief, nudges on stalled goals and "tap cards". Every
proactive message names the goal it serves, and nothing irreversible is ever decided for the person.

```
 user (Telegram / CLI / Desktop)                       Hermes core (unmodified)
   │  "I want to ship X by Fri"                       ┌──────────────────────────────┐
   ▼                                                  │ agent loop ── system prompt  │
 ┌────────────── hyperactive plugin ──────────────┐   │   ▲  "objective tree" section│
 │ tools: ha_goal_add  ha_followup  ha_status ... │◄──┤   │                          │
 │ /ha slash cmd · `hermes hyperactive` CLI       │   │ cron scheduler (gateway     │
 │ skill hyperactive:proactive                    │   │  ticker or `hermes cron     │
 │ store.py ──► plugin-data/hyperactive/*.db      │   │  tick`)                      │
 │   goals tasks commitments decisions people     │   │   │ every N min             │
 │   memories followups cards events(audit)       │   │   ▼                          │
 │ pulse.py (no LLM) ◄── scripts/hyperactive_pulse│◄──┤ job "hyperactive-pulse"      │
 │   due items -> stdout ───────────────────────────►│   no_agent, deliver=telegram │
 └────────────────────────────────────────────────┘   └──────────────────────────────┘
```

**Hermes mechanisms used (all public; verified on the scratch install unless marked)**
- `ctx.register_tool` ×6: `ha_goal_add`, `ha_followup`, `ha_card`, `ha_commit`, `ha_decide`,
  `ha_status`. By default, plugin tools sit behind Tool Search (`tool_search`/`tool_call`).
- `ctx.register_system_prompt_section("hyperactive.goals")`: the live goal tree, frozen per session.
- `ctx.register_command("ha")` + `ctx.register_cli_command("hyperactive")`, `ctx.register_skill`.
- `ctx.dispatch_tool("cronjob_manage", …)` keeps two recurring cron jobs in place, each fed by a
  shim the plugin writes to `HERMES_HOME/scripts/`. It also syncs schedule and deliver drift, and
  outside a session (CLI) it falls back to `hermes cron list/create/edit/remove`.
  - `hyperactive-pulse`: `no_agent=True`, `script=hyperactive_pulse.py`.
  - `hyperactive-brief`: agent mode at `brief_at`, `script=hyperactive_brief.py`,
    `skills=[hyperactive:brief]`. The script prints the day's facts. Empty output makes Hermes skip
    the model call.
- Hooks:
  - `pre_llm_call`: answers a card on a bare-option reply and injects due items.
  - `on_session_start`: self-heals the job and shim once set up.
  - `gateway_platform_event`: a Telegram reaction answers a card. *Written, untested: no bot yet.*
- `plugins.plugin_storage.plugin_data_dir` for SQLite, `ctx.get_config` + `config_schema`.
- *Not used:* `register_automation_blueprint`. A blueprint can't carry a pre-run script, so a
  blueprint brief would call the model every day even with nothing to say, and it would have to
  fetch its data through a tool. *Planned:* `post_llm_call` to spot new commitments such as "I'll
  send it Friday".
- Hermes's timezone (`hermes_time.get_timezone_name`) is written to `pulse.json`, so the cron
  scripts use it for local times and quiet hours.

**Decision: no MCP server for v1.** Native plugin tools get Hermes approvals, hooks and catalog
capability checks for free, with no extra process. An MCP or plugin-RPC read API for the midna app
can come later on top of the same `store.py`.

**Data model** (`plugin/store.py`, one SQLite file). `goals(parent_id, horizon, title, why,
status, last_moved_at, nudged_at)` · `tasks(goal_id, due_at, status)` · `commitments(goal_id,
direction i_owe|they_owe, person_id, what, due_at)` · `decisions(goal_id, choice, rationale,
review_at)` · `people(name, notes, last_contact_at)` · `memories(text, tags)` ·
`followups(goal_id, kind followup|reminder|nudge|brief, text, due_at, status)` ·
`cards(goal_id, question, options, recommended, stakes low|money|public|irreversible, action,
asked_at, reasked_at, held_at, answer, answered_by)` · `events(kind, goal_id, ref)`: an append-only
audit of every autonomous act, with its goal and cause, which `/ha why` reads. Rows without a
goal are filed under an "Inbox" goal.

**Proactive loop.** (1) Capture: the model records goals and commitments through tools and
schedules its own check-ins. (2) Pulse: a cron job runs every N minutes with **no LLM**. It
delivers due follow-ups, re-asks unanswered cards, and defers (never drops) anything inside quiet
hours. Empty output means silence. (3) Think: the daily brief is an agent-mode cron job. Its
script reads the store (`pulse.brief`, read-only) and the model writes the prose. Stale-goal
nudges and decision reviews stay in the LLM-free pulse. (4) Card rule (`store.card_action`):
ask, re-ask after 1h, and after another hour apply the recommendation **only if stakes=low**.
Money, public and irreversible cards get one hold notice, then wait for a human. "Apply" runs a
typed action that the plugin performs itself (`reschedule`, `schedule`, `goal_status`), never a tool
call. The rule is unit-tested and has run live through cron.

**In scope for Wednesday:** goal tree, follow-ups and reminders, commitments both ways, decisions
with review dates, daily brief, stale-goal nudge, tap cards with the timeout rule, quiet hours,
Telegram delivery, README with disclosures, a clean `validate`, and the catalog PR.
**Out of scope:** calendar sync and protected blocks from a real calendar (config-only windows),
people/CRM depth, embeddings, a Desktop UI, MCP, multi-user, and anything midna-specific.

**Demo, 3 moments (<90 s).**
1. *It listens and commits.* In Telegram: "I want the hackathon PR open by Tuesday." Hermes files
   it under "Win a hackathon prize (year)" and replies "I'll check in at 6 pm." Cut to
   `hermes cron list` showing the pulse.
2. *It speaks first.* The phone buzzes with nobody typing: "[goal: Ship catalog PR] Is the PR
   open? Yesterday you were blocked on screenshots." Then a quiet-hours message held overnight
   lands at 07:00.
3. *It knows its limits.* Two tap cards go unanswered. "Move gym to 7am? (low)" re-asks, then
   applies itself. "Pay the $40 domain renewal? (money)" stays held: "I won't decide money for you."

**Compatibility.** `requires_hermes >=0.21.7`. Tests, validate, pulse delivery and a brief run
pass on v0.21.7, v0.22.0, v0.22.7, v0.23.0 and 981791d (`.scratch/floor/`, one venv each).
`hermes cron create --catch-up` only exists from 0.22.7, so the plugin doesn't pass it.

**Dev setup.** `vendor/hermes-agent` is a read-only clone at 981791d. `.scratch/` holds a Python 3.14
venv with an editable install of a local clone (Hermes refuses wheel builds), `HERMES_HOME`
(`.scratch/env.sh`), and a stub model (`dev/stub_llm.py`). `dev/smoke.sh` runs the end-to-end test;
`dev/demo.sh` runs the 3 demo moments with 1-minute timings and prints what the phone shows. With
`DEMO_MODEL_URL/DEMO_MODEL/DEMO_MODEL_KEY_ENV` it uses a real OpenAI-compatible model instead. Its
key lives in the scratch `.env`. Gotcha: `hermes chat` may hand turns to an idle scratch gateway,
which keeps the plugin code it loaded, so stop scratch gateways after editing the plugin.
