# Coordinator journal rule

Every coordinator writes and maintains these workspace files regardless of
mode, agent, or whether a harness skill is loaded.  This is an automatic
standing rule, not a project-specific policy.

- `.claude/current_status.md` is overwritten and kept to 40 lines or fewer.
  It contains: fixed principles, adopted model, a table of active work
  (folder, worker, purpose, start, expected finish), decisions waiting for an
  owner, the five newest decisions (who, when, why), and recent mistakes and
  lessons.
- `.claude/work_log.md` is append-only, one event per line, in the form
  `YYYY-MM-DD HH:MM | kind(지시/시작/완료/실패/결정/실수) | work | one line | path`.
  At a date boundary, move the prior day's file to `work_log_YYYYMMDD.md` and
  begin a new `work_log.md`.
- Read `current_status.md` at session start, immediately after a conversation
  summary, and before launching long work.  Read only the tail of
  `work_log.md` when detailed history is needed.
- For cost control, update only changed lines in `current_status.md`; do not
  rewrite it wholesale.  Append `work_log.md` with one shell append operation.
  Launch/watch scripts record repeating instruction, completion, and failure
  events automatically; coordinators write only judgment events (decisions,
  mistakes, and causes).

Use `templates/current_status.md` and `templates/work_log.md` for new
workspaces.  Global agent instructions make this rule apply even without a
skill.
