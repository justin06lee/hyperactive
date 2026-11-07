# NEXT: ranked backlog to finish by Tue Nov 10 evening PT

Deadline: Wed Nov 11, 1:00 PM PT (plan for this; the tweet says 2 PM). We need a catalog PR, an X
video under 90 s, and the Typeform. Catalog CI only runs after a maintainer starts it, so **the PR
should be open by Mon night**.

**Done in phase 2 (Nov 6):** tap cards, commitments, decisions, stale nudges, Inbox, `/ha why`,
`pre_llm_call`, self-heal, README + Disclosure, MIT, catalog draft, `dev/demo.sh` on the stub.

**Done in phase 3 (Nov 7):**
- **Real-model pass (partial).** Claude Sonnet via an OpenAI-compatible endpoint on the scratch
  Hermes; ~8 turns before the endpoint went away at ~11:35 PT. What it showed, and what changed:
  - Tool Search works: it reaches `ha_*` with `tool_describe` + `tool_call`. It sometimes calls
    `ha_status` directly first, gets "does not exist", and recovers on its own.
  - "PR open by Tuesday" was first filed as `ha_commit` to a person called "Hackathon", with 2-3
    check-ins. After the description changes it is a week goal under the year goal, with one
    check-in (3 runs out of 3).
  - `stakes` was honest every time: gym move `low`, $40 renewal `money`, posting a video `public`.
  - The gym card had no `action` until scheduled follow-up ids were in the system prompt and a
    low-stakes card without an action got a note in the tool result. Then it attached
    `reschedule`, using "Sun 07:00", which the parser rejected. `parse_when` now reads `7am`,
    `tomorrow 07:00`, `Sun 07:00` and `Nov 14 07:00`.
  - "yeah move it", with two cards waiting, became `ha_card` on the gym card. It left the money
    card alone and said why.
  - Run from the repo directory, it read NEXT.md with the terminal tool. Demo chats now run in an
    empty temp dir.
- **Daily brief.** `hyperactive-brief` is an agent-mode cron job at `brief_at` (default 08:00,
  empty = off). Its script `pulse.brief()` prints the day's facts, read-only. Empty output skips
  the model call. Skill: `hyperactive:brief`. `/ha brief` shows the data. It runs end to end with
  the stub (skill and data reach the prompt); **prose from a real model not seen yet.**
- **Floor:** `requires_hermes >=0.21.7`, verified on v0.21.7 / v0.22.0 / v0.22.7 / v0.23.0 /
  981791d. Along the way, `--catch-up` turned out not to exist before 0.22.7, so it's dropped.
- **Polish:** schedule drift compares Hermes's parsed display; quiet hours and local times follow
  Hermes's timezone; `/ha goal --why`; `reschedule` can reword the reminder.
- **Catalog prep:** repo/maintainer/docs_url filled in, 2:1 image + 5 screenshots
  (`dev/media.py`), `catalog/pin.sh`, `catalog/PR.md`, version 0.3.0. Both validators pass (the
  catalog one with a stand-in sha).

## Ranked
1. **Finish the real-model pass** when the endpoint answers again (`/v1/models`). Keep it small:
   - one full `DEMO_MODEL_URL=… DEMO_MODEL=sonnet DEMO_MODEL_KEY_ENV=HA_REAL_MODEL_KEY dev/demo.sh`
     (about 10 min). Watch moment 3 to the end, and the "yes" reply on the money card;
   - one forced brief run (`hermes cron run <brief id>` + `hermes cron tick`) to judge the prose
     and the skill. Tune `plugin/skills/brief/SKILL.md` from it;
   - then refresh the screenshots if the real wording differs (`dev/media.json`).
2. **Push + pin + PR** (midna, after Justin OKs): push to `justin06lee/hyperactive`, then
   `catalog/pin.sh <sha>`, re-run `validate_plugin_catalog.py`, and open the PR with
   `catalog/PR.md`. If the history is rewritten before the push, pin the sha that was pushed.
3. **Telegram on a test bot** (needs a test bot token; never the live `hermes` user's bot):
   - put the token in the scratch `.env` and set `deliver: telegram`;
   - run the scratch gateway and test plain replies plus the 👍/👎 reaction path, which is still
     untested;
   - decide `attach_to_session` for the pulse job: if deliveries land in the chat session, the
     model sees the card it's answering;
   - if reactions work, drop the catalog `known_issues` line.
4. **Demo video** (Tue). Record `dev/demo.sh` timings on a real phone and say "time sped up:
   1-minute timers" on screen. Lead with moment 3: the stakes rule is the edge over heartbeat2 and
   hermes-autonomous.
5. **Nice to have:**
   - Protected blocks (`protected: ["Mon-Fri 09:00-12:00"]`, like quiet hours).
   - `post_llm_call` to spot unrecorded commitments.
   - `ha_task` and people notes.
   - A plugin-RPC read API for the midna app.
   - The brief currently skips days when only year/quarter goals exist. A "pick a week goal?"
     line could help there.

## Known limits (also in the README)
- A Telegram reaction can't be matched to the exact message: Hermes doesn't return the delivered
  message id to the pulse script. It answers the only card (re)asked within two timeouts, else
  nothing.
- A bare "yes" is auto-answered only when exactly one card is waiting. With several waiting,
  "2 yes" works, or the model decides (seen working with Sonnet).
- A running gateway keeps the plugin code it loaded; restart it after updating the plugin.

## Questions only Justin can answer
1. OK to push and open the catalog PR with the text in `catalog/PR.md` (midna does it)?
2. A test Telegram bot token for item 3?
3. The demo's moment 1 in real-model mode adds "Check in with me in a minute." OK for the video,
   with the "time sped up" note?
