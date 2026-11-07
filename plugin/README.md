# hyperactive

**Hermes, but it texts you first.**

Most agents wait to be asked. hyperactive keeps your goals (life > year > quarter > week), the
promises you've made and the ones owed to you, and the decisions you want to revisit. Then it
speaks up on its own schedule: a morning brief, check-ins, reminders, decision reviews, a nudge
when a goal stalls, and small yes/no "tap cards" it can settle for you.

Two rules make it safe to leave running:

- **Every message names the goal it serves.** `/ha why` explains any message: what caused it and
  which goals it traces up to.
- **It never decides money, public or irreversible things for you.** An unanswered card is asked
  again after an hour. If it's low stakes, the recommendation is applied an hour after that. Anything
  else waits for you, however long that takes.

```
Move gym to 7am tomorrow?  yes / no
I'd go with yes. If I don't hear back, I'll do that in 2h.
Reply yes or no.
goal: Stay fit (quarter) · card 3
```

## Quickstart

```bash
hermes plugins install hyperactive        # from the catalog
hermes plugins enable hyperactive
hermes config set plugins.entries.hyperactive.settings.deliver telegram   # or origin, local, ...
hermes config set plugins.entries.hyperactive.settings.quiet_hours 22:00-07:00
hermes config set plugins.entries.hyperactive.settings.brief_at 08:00     # empty turns the brief off
hermes hyperactive setup                  # optional: the first scheduled item does this too
```

Then talk to Hermes as usual: "I want the hackathon PR open by Tuesday", "I'll send Sam the deck
Friday", "We're going with SQLite, let's revisit in a month". Hermes files these in the tree and
schedules its own check-ins. A gateway (`hermes gateway`) has to be running for cron to fire.

## What it adds to Hermes

| Tool | Does |
|------|------|
| `ha_goal_add` | Adds a goal to the tree, optionally with a check-in. |
| `ha_followup` | Schedules something to say later: a follow-up, reminder or nudge. |
| `ha_card` | Asks a quick question with a recommended answer, or records the answer. |
| `ha_commit` | Tracks a promise either way: `i_owe` or `they_owe`, with a due date. |
| `ha_decide` | Records a decision, its rationale and a review date. |
| `ha_status` | Shows the tree and everything tracked or scheduled. |

`/ha` (also available as `hermes hyperactive …`):

```
/ha                          goals, cards, commitments, schedule
/ha answer <card> <option>   answer a tap card (a plain reply like "yes" or "2 skip" works too)
/ha why [event]              why hyperactive sent its last message
/ha brief                    the data today's brief is written from
/ha decision <id> keep|revise [new choice]
/ha done <commitment>        close a commitment
/ha goal <horizon> <title> [--parent N] [--why '...'] [--follow-up 'in 2h']
/ha followup <goal> <when> <text>
/ha setup | /ha uninstall
```

## How it works

- **Pulse.** One Hermes cron job, `hyperactive-pulse`, runs a small script every few minutes
  (`no_agent`, so no model call). It prints what is due, and Hermes delivers that to your
  `deliver` target. An empty run sends nothing. Each message holds at most `max_items` items;
  anything left over waits for the next pulse.
- **Daily brief.** A second cron job, `hyperactive-brief`, runs at `brief_at` in agent mode. A
  small script first prints today's facts (waiting cards, today's schedule, due promises,
  reviews, stalled and week goals), and Hermes's model writes a short brief from them using the
  `hyperactive:brief` skill. When there is nothing to brief, the script prints nothing and Hermes
  skips the model call.
- **Tap cards.** `ask → re-ask after card_timeout → apply` only when `stakes: low`. Money,
  public and irreversible cards get one "still yours to decide" notice, then silence. "Apply"
  means the plugin runs a typed action itself (move a reminder, schedule a follow-up, set a goal's
  status). It never makes an arbitrary tool call.
- **Answers.** `/ha answer`, or a plain reply. If your whole message is one option of the only open
  card ("yes", "skip"), or names a card ("2 skip"), it's recorded before the model sees the turn.
  Otherwise the model sees the open cards and calls `ha_card`. Telegram reactions (👍 / 👎) answer
  the card asked most recently.
- **Context.** Your goal tree goes in the system prompt. A few lines about due cards, commitments
  and reviews are added to each turn, so Hermes can bring them up naturally.
- **Quiet hours** hold messages back; they are never dropped. Nothing is asked, re-asked or
  applied during them.
- **Stale goals.** A week goal with no activity for `stale_after` (a quarter goal: 4× that) gets
  one nudge, and only one, until it moves again.

## Settings

| Key | Default | |
|-----|---------|--|
| `pulse_every` | `every 5m` | Pulse schedule. |
| `deliver` | `origin` | Where proactive messages go (Hermes cron `deliver` grammar). |
| `quiet_hours` | empty | Local window, e.g. `22:00-07:00`. |
| `card_timeout` | `1h` | Re-ask after this; apply low-stakes cards after twice this. |
| `stale_after` | `3d` | Stillness before a week goal gets a nudge. |
| `max_items` | `5` | Most items in one message. |
| `brief_at` | `08:00` | Local time of the daily brief. Empty turns it off. |

Local times follow Hermes's `timezone` setting (or `HERMES_TIMEZONE`), else the machine's.

Settings changes reach the cron job at the next session start or `hermes hyperactive setup`.

## Disclosure

- **Files.** It stores its data in `HERMES_HOME/plugin-data/hyperactive/` (one SQLite file and
  `pulse.json`). The first time something is scheduled, or on `setup`, it writes two 4-line shims,
  `HERMES_HOME/scripts/hyperactive_pulse.py` and `hyperactive_brief.py`, because Hermes cron only
  runs scripts from there.
- **Background work.** It creates two recurring Hermes cron jobs: `hyperactive-pulse` (no-agent,
  script output delivered by Hermes) and, unless `brief_at` is empty, `hyperactive-brief` (agent
  mode, once a day). Once set up, it re-creates a missing job or shim and syncs settings at
  session start. `hermes hyperactive uninstall` removes both jobs and shims and leaves your data
  in place.
- **Model calls.** The daily brief is the only model call hyperactive causes. It is one call a
  day, made by Hermes's cron with your configured model, and skipped when there is nothing to
  brief. The pulse never calls a model.
- **Subprocesses.** From the `hermes hyperactive` CLI only, which runs outside a session where
  the cron tool isn't loaded, it runs `hermes cron list/create/edit/remove`.
- **Hooks.**
  - `pre_llm_call` adds a few lines of context to your turns. When your whole message is a card
    option, it records that answer.
  - `on_session_start` runs the self-heal.
  - `gateway_platform_event` reads Telegram reactions to answer cards.
- **Autonomy.** A low-stakes tap card can be applied without you after two unanswered asks. It
  can only move or schedule hyperactive's own reminders or change a goal's status.
- **None of these.** It makes no network calls of its own. It sends no telemetry, needs no
  secrets, and has no dependencies beyond Hermes.

## Known limits

- Telegram reactions can't be matched to the exact message, because Hermes doesn't tell the
  pulse script the id of the message it delivered. A reaction answers the only card asked in the
  last two timeouts. If more than one card was asked in that window, the reaction does nothing.
  This path hasn't been tested against a live bot yet.
- By default, Hermes puts plugin tools behind Tool Search, so the model reaches `ha_*` through
  `tool_search`. In testing with Claude Sonnet this worked without extra prompting.
- After the plugin changes, restart the gateway: a running gateway keeps the code it loaded.

MIT licensed.
