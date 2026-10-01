## Before you touch anything in this task, verify — don't assume

These four checks are cheap and each one has directly prevented a real,
already-happened mistake in this project. Do them before writing or running
anything, not after something goes wrong.

1. **Confirm what a directory actually is before trusting its name.** Run
   `git remote -v` (or equivalent) rather than trusting a path name or a
   workspace/tab label to tell you what's checked out there — a worktree or
   IDE workspace folder can be named after one project while containing a
   completely different one.
2. **List broadly before concluding something doesn't exist.** Use `ls` /
   `find -type f` over the whole directory before concluding "there's no
   script for X" from a narrow keyword search. An empty result from a
   guessed filename pattern means the guess was wrong, not that the file is
   absent.
3. **Read the project's own existing docs before acting.** `CLAUDE.md`,
   `AGENTS.md`, `reports/`, design docs — read these before writing or
   running code in a project you have not already read them in this task.
   A previous session's work does not carry over as memory for you; these
   files are the only thing that does.
4. **Back up shared, non-reproducible state before overwriting it.** Model
   checkpoints, split files, and similar generated-but-irreplaceable
   artifacts are frequently gitignored and unrecoverable once overwritten.
   Before writing to such a path in place, check whether something valuable
   is already there and copy it aside first (a timestamped backup), the way
   this kind of project's own training scripts often already do
   (`best_before_<reason>_<date>.pt`).

## Token minimization is the first priority (user directive 2026-09-30)

- Read each doc/config once; do not re-read unchanged files.
- For anything that runs longer than a few minutes (training, image
  generation, large batch inference): write the script, run a 1–3 item smoke
  test, then launch the full run detached (`setsid nohup ... &`), write the
  exact command and log path into your README, and END your turn. Do not
  wait, sleep, or poll for it to finish.
- Do not print large diffs, whole files, or long logs; show only what is
  needed. Keep the final report short (what changed, where, blockers).
- If you hit a quota / rate-limit / capacity error, stop immediately.
- Never detect job completion with `pgrep -f`/`ps | grep` name matching (the watcher matches its own command line and hangs). Use DONE/FAILED marker files with a max wait, and kill by recorded PID, not `pkill -f`.
