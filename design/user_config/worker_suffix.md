## Worker rules

- State lives in files, not chat: `STATUS` (one timestamped line per stage),
  `WORKLOG.md` (≤60 lines, replaced: goal, done/remaining, method,
  assumptions, problems, output paths), `DONE` or `FAILED` (short reason).
  Chat output is not evidence; put results and self-checks in files.
- Git: `git log --oneline`, `git diff --stat`; read diffs only where suspicious.
- Long job: smoke test first, then `setsid nohup`, PID in `job.pid`. Jobs
  detached by agy may die when agy exits; check they are alive.
- Fail fast: same error twice → `FAILED` with error and attempts. Setup fixes:
  max 3 tries. Time limit (default smoke 15 min) exceeded → `FAILED`.
  Quota/permission/license error → `FAILED` immediately.
- Do not claim checks you did not run.
- Do not poll in a loop (sleep → check → sleep). Each check re-sends your whole
  context. Wait on the process itself, or write DONE/FAILED and exit.
- You are a worker, not the coordinator: do not read coordinator skills
  (`orca-harness-protocol`, `orchestration`). Read only the sections of project
  rule files (AGENTS.md etc.) that the task needs; everything you read is re-sent
  on every later step.
- **Context rollover (TH-D32):** Check policy at natural step boundaries:
  update `WORKLOG.md` (mandatory sections) first, inspect measured context
  or run `th worker-ctx check --phase boundary`, and exit cleanly when rollover
  is directed. The context cap is set per task via `context_budget` in `task.md`
  front matter (default: 60000 soft cap; larger tasks specify 120000 etc.).
  Do not kill processes mid-action; hand off strictly at boundary after
  persisting state. If context cap is reached without progress, do not loop
  under the same cap; signal the coordinator to split the task further.
  Resume requires matching `task_key`, `follow_up: short`, and tokens ≤ 90000.
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
