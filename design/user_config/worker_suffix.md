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
