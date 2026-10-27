# Jev advice on every execution path (TH-D40)

## Purpose

Jev recommends a Codex tier and classifies worker failures.
It never changes a model, effort, retry, role, gate, or notification decision.
The existing coordinator tool-selection router remains unchanged.
Its limited scope (run/repl/SDK coordinator turns) is intentional and documented.
The old watch observation path previously used system Python and hid import errors.
This design repairs that interpreter path as well as adding advice.

## Shared decision boundary

`decisions/advisor.py` owns synchronous `advise_model_tier` and `advise_failure`.
Async callers run these functions through `asyncio.to_thread`.
All entry points use the same criteria, confidence threshold, and audit format.
Tier options are TERRA_MEDIUM, TERRA_HIGH, SOL_MEDIUM, SOL_HIGH.
Only Codex launches request tier advice.
Failure options are RETRY_SELF_ERROR, ENV_FIX, BRIEF_ERROR, ESCALATE_TIER, ASK_USER.
Failure inputs contain FAILED, the last five STATUS lines, and a task.md summary.
Built-in workers also supply exit code, prompt summary and bounded log tails.
Input text is bounded to 4000 characters before external transmission.
Requested model and effort are context, including explicit user selections.
They remain unchanged after advice.

## Configuration and bounds

Reuse decision_router settings and the existing Jev backend.
Shell entry points load merged global/local configuration once per process.
Python launch paths pass their existing decision_router settings.
Protocol without an explicit Config loads settings once for its target repository.
TEAM_HARNESS_JEV overrides enabled, including an unconditional 0 kill switch.
Secrets are loaded once from ~/.team-harness/secrets.env without replacing env keys.
Missing keys produce no recommendation and an audit reason.
TEAM_HARNESS_JEV_TIMEOUT defaults to 1.5 seconds for advisory calls.
The async backend has a deadline and the synchronous wrapper has a bounded wait.
Errors, timeouts, malformed responses and low confidence return None.
Audit failures also never interrupt worker execution.
Disabled advice performs no backend call and writes no disabled record.
Tests may explicitly select TH_TEST_MOCK_JEV=1; fake keys alone do not enable mocks.

## Applying paths

| Path | Attachment | Test |
| --- | --- | --- |
| th run | both agent_tools spawn bindings; wait_for_agents | fake subprocess, run.json decisions, failure text |
| th repl | same bound agent_tools | shared binding test |
| SDK TeamHarness.run | same bound agent_tools | shared binding test |
| th-launch / Orca | immediately after launch_policy | shell fake backend, output and JSON log |
| protocol MODE A/B/C | TeamHarnessAgentRunner role spawn including rollover | fake runner spawn, run.json decisions |
| th-watchd | failure event before inbox append | isolated registry and owner inbox |
| th-watch | observes workers registered by launch paths; daemon handles failure advice | shared event/parser regression |
| ths | displays existing inbox/task state; consumes additive advice | parser regression |

## Audit and presentation

Append each enabled attempt as one JSON object to TH_WATCH_DIR/jev_decisions.log.
Default directory is ~/.team-harness/watch.
Fields include path, kind, choice, confidence, latency_ms, reason, task and timestamp.
Successful advice and fallback attempts are also recorded in run.json decisions.
Protocol initializes an advisory run log in its role log directory when absent.
On resume it appends decisions while preserving existing run metadata.
Every decision has applied=false because advice does not control execution.
th-launch prints one Korean recommendation line with current model and effort.
Failure inbox advice is appended after the existing final column.
wait_for_agents preserves its JSON object and adds a jev field of recommendation lines.
An unavailable recommendation leaves the original result unchanged.

## Shell interpreter and failures

th_python in th-watch-lib prefers repository .venv/bin/python.
Otherwise use uv run --project repository python; if unavailable, log and skip.
Shell module calls use this helper, including the existing watch_cli observer.
Observer stderr failures become a one-line events.log diagnostic.
TEAM_HARNESS_JEV=0 suppresses both advisory and watch observation calls.

## Validation and rollout

Tests use temporary watch directories, fake processes and fake Jev responses only.
Cover timeout, no key, exception, confidence, disabled mode and unchanged launch arguments.
Run repository lint, formatting, types, tests and shell syntax checks before merging.
After merge run exactly one real tier request and retain its output and JSON audit.
Collect advice for one to two weeks, then report agreement with human decisions.
Automatic application requires a separate decision after that report.
