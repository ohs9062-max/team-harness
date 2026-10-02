## Worker operating policy

Apply this policy when starting a new worker. A policy change affects only a
new launch: let an already-running worker finish, then launch it again with
the current suffix.

- Pass context through state files, not conversation: project status docs
  (overall flow/decisions), per-task WORKLOG.md (methods/assumptions),
  STATUS/DONE/FAILED (state), and result tables.
- Git is for history and rollbacks. Routine checks stay at `git log --oneline`
  and `git diff --stat`; read specific portions only when suspicious (never read
  full diffs).
- Worker chat output is not evidence. Record completion, metrics, and
  conclusions only in files (DONE/FAILED, result tables, self-checks).
- For a long task, run a small smoke test, then detach the main job using
  setsid nohup and record its PID in `job.pid`; when it ends, write `DONE`, or
  `FAILED` with a short reason. Note that background jobs detached by agy may
  die when agy exits; verify liveness after finish.
- Append one timestamped line to `STATUS` when a stage changes. Keep a
  `WORKLOG.md` of at most 60 lines by replacing it with the current state:
  goal/criteria, completed and remaining work, method, assumptions, changes,
  problems, and output paths.
- The coordinator does not receive normal progress updates. `th-watch` wakes
  it only for completion, failure, quota/capacity worker errors, a missing
  process, or a stall. `th-status` is run once only when status is requested.
  A missing process must be detected within one minute; a live process with no
  CPU or output-file change is stalled after ten minutes.
- A worker conclusion is not automatically a project conclusion. Before
  accepting a learned or compared result, the coordinator checks the stated
  baseline (for example, the pre-training score) and whether the result is
  plausible. Keep a failed method separate from the model/result it measured.
- Use as many workers as are affordable and efficient for the work. Preserve
  token budget and task state in the registry; do not impose a fixed worker
  count.
- Model choice: `gpt-5.6-sol` is strictly for tasks where terra failed or
  architectural judgment is needed—never for scripted edits, setup, or trial-and-error.
  Medium importance uses `gpt-5.6-terra` high; default is `gpt-5.6-terra` medium.
- Worker splitting: Models cannot change mid-run; do not specify per-step models.
  Only split when upfront cost is heavy (>50k tokens) into "cheap prep → WORKLOG.md
  handoff → target model main run". Select models via th-launch args. Verify with scripts.
- `agy` has ample token headroom but greater shortcut/false-completion risk;
  use it for simple/bulk work and verify doubtful completion.
- Put code and logic work in a concise worker brief. The coordinator verifies
  results and writes code directly only when that is necessary.
