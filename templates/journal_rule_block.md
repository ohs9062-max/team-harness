<!-- TEAM-HARNESS-COORDINATOR-JOURNAL:BEGIN -->
## Coordinator journal (automatic)

Regardless of mode or loaded skills, coordinators must maintain the current
workspace's `.claude/current_status.md` and `.claude/work_log.md`.
Read `current_status.md` at startup, after compaction, and before long work.
Update only changed status lines; append one timestamped work-log line for
every instruction, start, completion, failure, decision, or mistake.  Scripts
record repeating instruction/completion/failure events automatically; the
coordinator records judgments, causes, decisions, and lessons.  Read detailed
history only from the work-log tail.  Full format: team-harness
`design/coordinator_journal.md`.
<!-- TEAM-HARNESS-COORDINATOR-JOURNAL:END -->
