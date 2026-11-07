<!-- Draft PR text for NousResearch/hermes-agent: add plugin-catalog/hyperactive.yaml.
     Not opened. Before opening: push the repo, run catalog/pin.sh <sha>, re-run both validators.
     Title: feat(plugin-catalog): add hyperactive -->

## What does this PR do?

Adds a community catalog entry for **hyperactive** (`plugin-catalog/hyperactive.yaml`), a plugin
that makes Hermes speak first. It keeps the user's goals (life > year > quarter > week), promises
both ways and decisions with review dates. On Hermes's own cron, it sends a daily brief,
check-ins, decision reviews, nudges on stalled goals and "tap cards" (a quick question with a
recommended answer).

The rule that makes it safe to leave running: an unanswered card is asked again after an hour.
If it is low stakes, the recommendation is applied an hour later, as a typed action the plugin
performs on its own data (move or schedule its own reminder, set a goal's status). Money, public
and irreversible cards are never decided for the person. Every message names the goal it serves,
and `/ha why` explains any message.

Repo: https://github.com/justin06lee/hyperactive (plugin in `plugin/`, MIT).

## Type of Change

- [x] ✨ New feature (non-breaking change that adds functionality): a new catalog entry only.

## Disclosure

- **Files:** its data is in `HERMES_HOME/plugin-data/hyperactive/` (SQLite and `pulse.json`).
  It writes two 4-line shims, `HERMES_HOME/scripts/hyperactive_pulse.py` and
  `hyperactive_brief.py`, because cron only runs scripts from there.
- **Background work:** two recurring cron jobs.
  - `hyperactive-pulse` is no-agent: it prints due items, and Hermes delivers them.
  - `hyperactive-brief` runs in agent mode once a day at `brief_at`. Setting `brief_at` empty
    turns it off.

  Once set up, a missing job or shim is re-created at session start.
- **Model calls:** only the daily brief, at most one call a day through the user's configured
  model. It is skipped when the data script prints nothing. The pulse never calls a model.
- **Subprocesses:** `hermes cron list/create/edit/remove`. Only the `hermes hyperactive` CLI runs
  these, because it works outside a session where the cron tool isn't loaded.
- **Hooks:**
  - `pre_llm_call` adds a few lines about due items. When the whole message is an option of the
    only waiting card, it records that answer.
  - `on_session_start` runs the self-heal.
  - `gateway_platform_event` reads Telegram reactions to answer a card.
- **Autonomy:** it applies a low-stakes card without the user only after two unanswered asks. The
  action can only touch hyperactive's own reminders and goal status.
- **None:** no network calls of its own, no telemetry, no secrets, no dependencies beyond Hermes.
- `hermes hyperactive uninstall` removes both jobs and shims and keeps the data.

## How to Test

1. `hermes plugins install hyperactive && hermes plugins enable hyperactive`
2. `hermes config set plugins.entries.hyperactive.settings.deliver local`, then `hermes hyperactive setup`.
   `hermes cron list` should show `hyperactive-pulse` (no-agent) and `hyperactive-brief`.
3. Say "I want the hackathon PR open by Tuesday" in a chat. The model files it with `ha_goal_add`
   and schedules one check-in. `hermes hyperactive status` shows the goal and the check-in.
4. `hermes hyperactive brief` prints the data the morning brief is written from.

## Checks run

- `python scripts/validate_plugin_catalog.py plugin-catalog/hyperactive.yaml`: OK.
- `hermes plugins validate plugin --install-deps`: passed on v0.21.7, v0.22.0, v0.22.7, v0.23.0
  and a recent main. The floor `>=0.21.7` is declared in both the entry and `plugin.yaml`.
- The plugin's own tests (26, stdlib + a real `PluginManager`) pass on the same versions.
  Pulse delivery through cron and a forced brief run also work on each of them.
- I ran a live pass with Claude Sonnet as the model. The model found the `ha_*` tools through
  Tool Search, rated a $40 renewal as `money` and posting a video as `public`, and answered a
  loose reply ("yeah move it") with `ha_card`.

## Known issues

A Telegram reaction answers the most recently asked card only, because Hermes doesn't pass the
delivered message id to the cron script. The reaction path hasn't been tested against a live
bot yet. This is listed in `known_issues`.

## Screenshots

See `screenshots:` in the entry. They show real messages from a demo run with made-up goals.
