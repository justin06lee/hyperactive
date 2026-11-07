# NEXT: ranked backlog to finish by Tue Nov 10 evening PT

Deadline: Wed Nov 11, 1:00 PM PT (plan for this; the tweet says 2 PM). We need a catalog PR, an X
video under 90 s, and the Typeform. Catalog CI only runs after a maintainer starts it, so **the PR
should be open by Mon night**.

**Done in phase 2 (Nov 6):** tap cards end to end, commitments both ways, decisions with review
dates, stale-goal nudges, Inbox goal, `/ha why`, `pre_llm_call` injection and plain-reply answers,
self-heal and uninstall, README + Disclosure, MIT, catalog draft, and `dev/demo.sh`. All three demo
moments run against the stub. 21 tests pass, `hermes plugins validate plugin --install-deps`
passes, and the catalog draft passes `validate_plugin_catalog.py`.

## Ranked
1. **Real model pass** (blocked on Q1). Run the demo with a real LLM instead of the stub. Check:
   - that the model finds `ha_*` through Tool Search and picks `stakes` honestly (money and public,
     never low);
   - that it answers cards from non-exact replies ("yeah move it") via `ha_card`;
   - that it doesn't over-schedule.

   Tune the tool descriptions and the skill from what we see. This is the biggest unknown left.
2. **Telegram on a test bot** (blocked on Q2). Set `deliver: telegram` on the scratch gateway.
   - Test the reaction path (`on_platform_event`, written but **untested**) and plain replies.
   - Decide `attach_to_session` for the pulse job. If cron deliveries land in the chat session,
     the model sees the card it is answering, and the reaction heuristic matters less.
   - The seam is the `deliver` setting; no code change is expected.
3. **Daily brief** (blocked on Q1). An agent-mode cron job via `register_automation_blueprint`
   plus a `hyperactive:brief` skill. The seam is `store.attention()`, which already returns due
   cards, commitments and reviews for `pre_llm_call`; the brief adds stale goals and today's
   follow-ups. The pulse stays LLM-free. Add `register_automation_blueprint` and its capability
   to plugin.yaml and the catalog entry together.
4. **Catalog PR** (blocked on Q3):
   - Push the repo and pin the 40-char sha in `catalog/hyperactive.yaml`. Fill in `repo`,
     `maintainer` and `docs_url`.
   - Add an `image` (2:1) and screenshots. Phone frames from `dev/demo.sh` work as screenshots.
   - Re-run both validators, then open the PR with the Disclosure text from the README.
5. **Verify the `requires_hermes` floor.** Declared `>=0.23.0` because we've only run on HEAD
   981791d (past v0.23.0). The vendor clone is shallow and has no tags. Install the v0.23.0 tag
   in a second scratch venv and run tests + validate. If 0.22.x works too, lower the floor to
   widen reach.
6. **Demo video** (Tue). Record `dev/demo.sh` timings on a real phone. Say "time sped up:
   1-minute timers" on screen. Lead with moment 3: the stakes rule is the edge over heartbeat2
   and hermes-autonomous.
7. **Small polish:**
   - `ensure_pulse` drift check compares `schedule_display` to the configured string, so
     "every 5 minutes" vs "every 5m" would re-update every session. Harmless, but normalize it.
   - Quiet hours use the gateway process's local timezone. Honour Hermes's timezone setting if
     one exists.
   - `/ha goal` could take `--why`.
8. **Nice to have:**
   - Protected blocks (`protected: ["Mon-Fri 09:00-12:00"]`, like quiet hours).
   - `post_llm_call` to spot unrecorded commitments.
   - `ha_task` and people notes.
   - A plugin-RPC read API for the midna app.

## Known limits (also in the README)
- A Telegram reaction can't be matched to the exact message: Hermes doesn't return the delivered
  message id to the pulse script. It answers the only card (re)asked within two timeouts, else
  nothing.
- A bare "yes" is auto-answered only when exactly one card is waiting. With several waiting,
  "2 yes" works, or the model decides.

## Questions only you can answer
1. **Which LLM** for the real-model pass, the demo and the daily brief? Nous Portal (on-brand),
   OpenRouter, or local? I need a key in the scratch `.env`, never in the repo.
2. **Telegram:** may I create or use a separate *test* bot for the scratch gateway, kept apart
   from midna's and the live Hermes's? I need its token.
3. **Public repo:** which GitHub owner/name, and the `maintainer` handle? MIT is in place.
   Is "Justin" right as the author and copyright name?
4. **Demo content:** your real goals (you'd review them) or the fictional set in `dev/demo.sh`
   (hackathon PR, gym, domain renewal)?
