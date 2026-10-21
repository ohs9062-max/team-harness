## Worker rules

Do not read or write the coordinator journal (`.claude/current_status.md`, `.claude/work_log.md`, `.claude/inbox.md`, `.claude/workers.md`); global journal rules apply to coordinators only.

- State lives in files, not chat: `STATUS` (one timestamped line per stage),
  `WORKLOG.md` (≤60 lines, replaced: goal, done/remaining, method,
  assumptions, problems, output paths), `DONE` or `FAILED` (short reason).
  Chat output is not evidence; put results and self-checks in files.
- Long loops/training: append progress lines to `STATUS` as
  `YYYY-MM-DD HH:MM:SS 처리 N/M장 X s/장` or `YYYY-MM-DD HH:MM:SS epoch=k/E`
  so `ths` can estimate the finish time. Do not compute an ETA yourself.
- Git: `git log --oneline`, `git diff --stat`; read diffs only where suspicious.
- Long job: smoke test first, then `setsid nohup`, PID in `job.pid`. Jobs
  detached by agy may die when agy exits; check they are alive.
- Fail fast: same error twice → `FAILED` with error and attempts. Setup fixes:
  max 3 tries. Time limit (default smoke 15 min) exceeded → `FAILED`.
  Quota/permission/license error → `FAILED` immediately.
- Do not claim checks you did not run.
- Do not poll in a loop (sleep → check → sleep). Each check re-sends your whole
  context. Wait on the process itself, or write DONE/FAILED and exit.
- You are a worker, not the coordinator. Read only paths and sections named in
  the task instruction's `## 읽을 문서`; when it says none or has no such list,
  read no rule/status documents. If another document is necessary, do not
  explore for it: write the reason in `FAILED`. Code and data needed for the
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
