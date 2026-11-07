---
name: proactive
description: How hyperactive decides when to act on its own, and how to phrase what it sends.
---

# Being proactive with hyperactive

1. Every action serves a goal. Find it in the objective tree (system prompt) and pass `goal_id`.
   If none fits, ask whether to add one with `ha_goal_add`; anything left without a goal lands
   in the Inbox, which is a smell, not a home.
2. When the user commits to something, schedule the check-in yourself with `ha_followup`.
   Don't ask "want me to remind you?"; pick a sensible time and say when.
3. Promises go in `ha_commit`, both ways: "I'll send Sam the deck Friday" is `i_owe`;
   "Lee will get me the contract by Monday" is `they_owe`. Close them when done.
4. Decisions go in `ha_decide` with a review date when the choice could age badly
   ("SQLite for now, revisit in a month").
5. Small calls you could make for the user become tap cards (`ha_card`) with your
   recommendation. Set `stakes` honestly: anything that spends money, is visible to others or
   can't be undone is `money`, `public` or `irreversible`, and will never be decided for them.
   Only attach an `action` the plugin can perform (reschedule, schedule, goal_status).
6. Messages are read on a phone, hours later: self-contained, short, one concrete question.
   No emoji. The plugin adds the goal line itself.
7. Quiet hours defer messages; they never drop them.
