---
name: brief
description: How to write hyperactive's daily brief from the data its brief job collects.
---

# The hyperactive daily brief

The cron job's script output holds today's data: cards waiting on the user, what is scheduled
today, promises due or overdue, decisions up for review, stalled goals and the active week goals.
Write the brief from that data only. Never invent an item, a time or a goal.

1. Open with the single most important thing today, in one sentence. A card or promise that is
   due beats a scheduled check-in, and an overdue promise beats both.
2. Then up to 5 short lines, most urgent first. Each line ends with the goal it serves, as
   `(goal: <title>)`.
3. Cards: repeat the question and the recommendation, and say how to answer ("reply 2 yes").
   Never answer a card yourself, and never call ha_card to answer one in the brief.
4. Stalled goals get one line asking for the next step or whether it's done. Do not nag.
5. End with one concrete suggestion for the day's first step, tied to a week goal.
6. Phone-sized: under 120 words, no headings, no emoji, no pleasantries.
7. Don't schedule anything new from the brief. The user can ask for that in their reply.
