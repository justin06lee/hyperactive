# NEXT: ranked backlog to finish by Tue Nov 10 evening PT

Deadline: Wed Nov 11, 1:00 PM PT (plan for this time; the tweet says 2 PM). That means a catalog PR,
an X video under 90 s, and the Typeform. Catalog CI only runs after a maintainer starts it, so the PR
should be open by **Mon night**.

**Competition (checked in vendor/plugin-catalog, Nov 6):** `heartbeat2` ("your agent follows up
on its own", it even uses `/pulse`), `hermes-autonomous` (proactive follow-ups from recent
conversation), `alans-way` (idle check-ins), `waypoint` (waiting-on reminders) and
`morning-briefing`. **Follow-ups alone are not new.** Our edge has to be visible in the demo: the
**objective tree** (every message traces to a goal), **tap cards with the stakes rule**,
**commitments in both directions**, and **decision reviews**. Items 1–6 below are that edge.

## Ranked
1. **Tap cards end to end.** Add an `ha_card` tool and pulse handling (ask → re-ask at 1h → apply if
   low-stakes / hold if money, public or irreversible; the rule already exists and is tested in
   `store.card_action`). Answer paths: `/ha answer <id> <choice>`, a plain reply, and a Telegram
   reaction via the `gateway_platform_event` hook. "Apply" needs a typed action the plugin
   performs itself (reschedule a task, mark a decision), never an arbitrary tool call (rule 12).
2. **Real delivery on Telegram.** Set `deliver: telegram` on a scratch gateway with a *test* bot.
   Decide whether the pulse should use `attach_to_session: true` so replies land in context.
3. **Commitments both ways + decisions.** Tools `ha_commit` (i_owe / they_owe, person, due) and
   `ha_decide` (review_at). The pulse emits "you owe Sam the deck today" / "Sam owes you the
   contract, 3 days late, nudge?" and "review the decision 'use SQLite' today".
4. **Stale-goal nudge.** A week goal with no `last_moved_at` change in N days becomes a nudge.
   Bump `last_moved_at` whenever a task, commitment or follow-up on it changes.
5. **Daily brief** as an agent-mode cron job via `register_automation_blueprint("daily-brief")`,
   using skill `hyperactive:brief`. It needs a real LLM (see Q2). The pulse stays LLM-free.
6. **Traceability surface.** `/ha why <event>` and a `events` view: "I messaged you because goal #2
   hasn't moved in 4 days." Refuse to schedule a follow-up with no goal (or file it under an
   "inbox" goal).
7. **`pre_llm_call` injection.** Put due cards and today's commitments into the turn so the model
   brings them up naturally. Keep the system-prompt tree small (it is frozen per session).
8. **Tool discoverability.** Plugin tools are deferred behind Tool Search. Check that a real model
   finds `ha_*` reliably, or ask in the PR/Discord whether `tools.tool_search.defer` may exclude
   them. Fill out the remaining tools (`ha_task`, `ha_goal_update`, `ha_memory`, `ha_person`).
9. **Packaging for the catalog.** README with quickstart and a **Disclosure** section: it writes
   `HERMES_HOME/scripts/hyperactive_pulse.py`, creates a cron job, may run `hermes cron create` as
   a subprocess from the CLI, makes no network calls, sends no telemetry. Also `license: MIT`,
   `version`, `catalog/hyperactive.yaml` with `subdir: plugin` and a pinned 40-char SHA. Run
   `hermes plugins validate plugin --install-deps` (it passes today). Add an image and screenshots.
10. **Robustness.** Re-run the pulse self-heal on session start (shim deleted, job removed).
    `hermes hyperactive uninstall` should remove the job and shim. Watch for clock and timezone edge
    cases (store in UTC, render in local time). Settings changes should rewrite `pulse.json` and
    update the job's schedule and deliver target. Add a pytest for `register()` using
    `tests/hermes_cli/test_plugin_api_compat.py`.
11. **Demo video** (Tue). Script the 3 moments in DESIGN.md. Speed up time by setting
    `pulse_every: every 1m` and "in 1m" follow-ups, and say so on screen.
12. Nice-to-have: a protected-blocks config (`protected: ["Mon-Fri 09:00-12:00"]`) that works like
    quiet hours, plus a read API (plugin RPC) for the future midna app.

## Questions only you can answer
1. **Is hyperactive the entry?** The brief recommended EdgeRoute, which has empty catalog ground.
   hyperactive is up against heartbeat2 and hermes-autonomous. I think the goal tree and stakes
   rule set it apart, but it's your call. If yes, go all in. Don't split the week.
2. **Which LLM for the demo and the daily brief?** Nous Portal (on-brand), OpenRouter, or local?
   Everything so far ran on a stub model (`dev/stub_llm.py`), and nothing needed a key.
3. **Telegram:** may I create or use a separate *test* bot for the scratch gateway, kept apart
   from midna's and the live Hermes's bot? I'll need its token (to go in the scratch `.env`, never
   in the repo).
4. **Public repo:** which GitHub owner and name (e.g. `<you>/hyperactive`), and is the `maintainer`
   handle for the catalog yours? Is MIT OK? I haven't pushed anything.
5. **Demo content:** use your real goals (you'd review them) or a believable fictional person?
6. **Is the name "hyperactive" final?** No catalog collision. Possible pitch line: "Hermes, but it
   texts you first."
