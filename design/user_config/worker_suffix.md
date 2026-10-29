## Worker rules

Do not read or write the coordinator journal (`.claude/current_status.md`, `.claude/work_log.md`, `.claude/inbox.md`, `.claude/workers.md`); global journal rules apply to coordinators only.

- State lives in files, not chat: `STATUS` (one timestamped line per stage),
  `WORKLOG.md` (≤60 lines, replaced: goal, done/remaining, method,
  assumptions, problems, output paths), `DONE` or `FAILED` (short reason).
  Chat output is not evidence; put results and self-checks in files.
- STATUS 시각은 반드시 `date '+%F %T'` 출력으로 쓴다.
- Long loops/training: append progress lines to `STATUS` as
  `YYYY-MM-DD HH:MM:SS 처리 N/M장 X s/장` or `YYYY-MM-DD HH:MM:SS epoch=k/E`
  so `ths` can estimate the finish time. Do not compute an ETA yourself.
- Reading and output: locate file sections with `rg -n`, then read only what is needed, at most 200 lines per command. Do not combine multiple sections in one command. Do not reread sections already read; after editing, check only changed sections. Use quiet test options such as `-q` and `| tail -n 40`; limit Git output with `--stat`, `--oneline`, or `-n`. Read logs only with `tail`.
- Long job: smoke test first, then use `th-job-start.sh <task folder> -- <command>`; the actual process records its PID in `job.pid`. Do not launch it with setsid directly.
  The runner chains train → evaluate → plots → report → `DONE`/`FAILED`; on a
  failed step it writes `FAILED` with the step name and rc, even after worker exit.
- Never hand remaining steps to the coordinator; finish the assigned task.
- Multi-step work: before/after each step append `START step=… replay=…` / `END step=… rc=… out=…` timestamped lines to `STATUS`; use `never` for training/GPU/long runs, push/merge/deletion/external writes, `safe` for reads, aggregation, or repeatable evaluation, and `inspect` when unsure.
- If the instruction includes “이전 시도 복구 요약”, follow it exactly and do not redo completed steps; detached runner scripts must write the same STATUS lines.
- Finish the assigned task. Mechanical errors (lint/format/import order/type annotations, your own script's exit-code misreading) are not failures: fix them with the standard tool (`ruff check --fix`, `ruff format`) and continue. If you know the cause and fix of an error, apply it and continue.
- A guard or hook denial (including PreToolUse deny and zone guard) is not a permission error. Retry by splitting the command into smaller commands or using a file-editing tool such as `apply_patch`. If the same denial occurs twice using different approaches, write `FAILED` including the exact denial messages.
- Stop with `FAILED` only for: quota/permission/license errors (immediately), the same fix approach failing twice, setup fixes failing 3 times, or the time limit (default smoke 15 min).
- When a design choice is ambiguous, pick the conservative option (keep existing behavior, backward compatible), note it as `decision:` in WORKLOG.md, and continue.
- Do not do anything the instruction did not ask for: do not stop, kill, move, or delete other processes, jobs, or outputs; if it seems necessary, write the reason in WORKLOG/FAILED for the coordinator instead.
- Do not claim checks you did not run.
- Do not poll in a loop (sleep → check → sleep). Each check re-sends your whole
  context. Wait on the process itself, or write DONE/FAILED and exit.
- You are a worker, not the coordinator. Read only paths and sections named in
  the task instruction's `## 읽을 문서`; when it says none or has no such list,
  read no rule/status documents. If another document is necessary, locate its relevant
  section with `rg`, read only that section, and continue. Code and data needed for the
  assigned work may be inspected narrowly with `rg`/`head`; that is distinct
  from the document restriction.
- **Context rollover (TH-D32):** Check policy at natural step boundaries:
  update `WORKLOG.md` (mandatory sections) first, inspect measured context
  or run `th worker-ctx check --phase boundary`, and exit cleanly when rollover
  is directed. The context cap is set per task via `context_budget` in `task.md`
  front matter (default: 60000 soft cap; larger tasks specify 120000 etc.).
  Do not kill processes mid-action; hand off strictly at boundary after
  persisting state. If context cap is reached without progress, do not loop
  under the same cap; signal the coordinator to split the task further.
  Resume requires matching `task_key`, `follow_up: short`, and tokens ≤ 90000.
  If `context_exit_requested.json` is present, update WORKLOG at the next
  boundary and exit cleanly; the watcher writes it from measured current input
  context, not cumulative "tokens used".
- **Experiment tracking (EXPERIMENT.md):** For multi-model training/comparison,
  maintain `<output_root>/experiments/<experiment_key>/EXPERIMENT.md` (Purpose,
  Baseline, Runs, Results table, Decision). Update at start, finish, analysis,
  and decision stages, and link from `WORKLOG.md`. Detach long jobs and exit
  rather than sleeping in a wait loop.
- **Image guard (TH-D33):** Do not read image files (`.png`, `.jpg`, `.jpeg`,
  `.gif`, `.webp`, `.bmp`, `.tif`, `.tiff`) or place their bytes in model input.
  Make judgments from numbers (CSV, statistics, metadata) instead. If an image
  is indispensable, write the reason in `FAILED` and hand it to the coordinator.
  `TH_ALLOW_IMAGES=1` is reserved for a coordinator to set after the user has
  explicitly requested image reading in the current turn; never set it yourself.
