## Worker operating policy

Apply this policy when starting a new worker. A policy change affects only a
new launch: let an already-running worker finish, then launch it again with
the current suffix.

- For a long task, run a small smoke test, then detach the main job. Record
  that main job's PID in `job.pid`; when it ends, write `DONE`, or `FAILED`
  with a short reason. Do not treat chat output as completion.
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
- Agent choice is a judgment call, not an automatic router: `agy` has ample
  token headroom but greater shortcut/false-completion risk, so use it for
  simple or bulk work and verify doubtful completion. For Codex use
  `gpt-5.6-sol` low for truly important work, `gpt-5.6-terra` high for
  medium-importance work, and `gpt-5.6-terra` medium by default.
- Put code and logic work in a concise worker brief. The coordinator verifies
  results and writes code directly only when that is necessary.
