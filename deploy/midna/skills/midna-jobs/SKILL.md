---
name: midna-jobs
description: How to run long background jobs so their completion always wakes you, and how to schedule things that must arrive on time.
---

# Long jobs and on-time messages

## Long background jobs

When work will take more than a few minutes (a build, a scrape, a render, a long agent run):

1. Start it so it survives a gateway restart and writes its outcome to one result file when it
   ends, e.g. `systemd-run --user --unit job-<name> bash -lc '<command> > <dir>/output.log 2>&1;
   echo "exit $?" >> <dir>/output.log; mv <dir>/output.log <dir>/result.md'`. Write the result file
   last, in one step, so it only exists once the job is done.
2. Register it with the job watcher, saying what the job is and what to do with the result:

   `python3 -I {{CODE}}/deploy/midna/jobwatch.py --hermes-home {{HERMES_HOME}} add <dir>/result.md --note "<what it is; what to tell the user or do next>"`

3. Tell the user it's running. Don't poll it yourself.

The `midna-jobwatch` cron job checks every 2 minutes and starts a turn for you when the result
file appears, with the note and the start of the result. This works for jobs started from the
app as well as from Telegram, and across restarts. (`terminal(background=true, notify=true)` alone
only wakes you on Telegram, and not after a restart.)

## Things that must arrive on time

Prayer times, meeting reminders, "leave now" and anything else whose value is its timing go
through `ha_followup` with kind `reminder`. It needs no model call, and if Hermes is down at that
moment it is sent late rather than lost. Use a one-shot cron job only when the moment needs
thinking (checking something, drafting something). Those are re-created by the nightly one-shot
sweep if they come due while Hermes is down, so they may run late.
