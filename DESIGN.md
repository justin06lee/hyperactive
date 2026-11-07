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

**Hermes mechanisms used (all public, all verified in the skeleton unless marked *planned*)**
- `ctx.register_tool` ×3 (`ha_goal_add`, `ha_followup`, `ha_status`). Plugin tools are deferred
  behind Tool Search by default, so the model reaches them via `tool_search`/`tool_call`.
- `ctx.register_system_prompt_section("hyperactive.goals")`: the live goal tree, frozen per session.
- `ctx.register_command("ha")` + `ctx.register_cli_command("hyperactive")`, `ctx.register_skill`.
- `ctx.dispatch_tool("cronjob_manage", …)` to create **one** recurring cron job: `no_agent=True`,
  `script=hyperactive_pulse.py` (a shim the plugin writes to `HERMES_HOME/scripts/`; cron only runs
  scripts from there). Outside a session (CLI) it falls back to `hermes cron create`.
- `plugins.plugin_storage.plugin_data_dir` for SQLite, `ctx.get_config` + `config_schema`.
- *Planned:* `register_automation_blueprint` (daily brief, weekly review as agent-mode cron
  jobs using the plugin skill), `pre_llm_call` (inject due cards/commitments into the turn),
  `gateway_platform_event` (Telegram reaction = tap on a card), `post_llm_call` (spot new
  commitments, "I'll send it Friday").

**Decision: no MCP server for v1.** Native plugin tools get Hermes approvals, hooks and catalog
capability checks for free, with no extra process. An MCP or plugin-RPC read API for the midna app
can come later on top of the same `store.py`.

**Data model** (`plugin/store.py`, one SQLite file). `goals(parent_id, horizon, title, why,
status, last_moved_at)` · `tasks(goal_id, due_at, status)` · `commitments(goal_id,
direction i_owe|they_owe, person_id, what, due_at)` · `decisions(goal_id, choice, rationale,
review_at)` · `people(name, notes, last_contact_at)` · `memories(text, tags)` ·
`followups(goal_id, kind followup|reminder|nudge|brief, text, due_at, status)` ·
`cards(goal_id, question, options, recommended, stakes low|money|public|irreversible, asked_at,
reasked_at, answer)` · `events` (an append-only audit of every autonomous act, with its goal).

**Proactive loop.** (1) Capture: the model records goals and commitments through tools and
schedules its own check-ins. (2) Pulse: a cron job runs every N minutes with **no LLM**. It
delivers due follow-ups, re-asks unanswered cards, and defers (never drops) anything inside quiet
hours. Empty output means silence. (3) Think: agent-mode cron jobs (daily brief, stale-goal
nudges, decision reviews) read the store and write prose. (4) Card rule (`store.card_action`):
ask, re-ask after 1h, and after another hour apply the recommendation **only if stakes=low**.
Money, public and irreversible cards stay held until a human answers. The rule is unit-tested.

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

**Dev setup.** `vendor/hermes-agent` is a read-only clone at 981791d. `.scratch/` holds a Python 3.14
venv with an editable install of a local clone (Hermes refuses wheel builds), `HERMES_HOME`
(`.scratch/env.sh`), and a stub model (`dev/stub_llm.py`). `dev/smoke.sh` runs the end-to-end test.
