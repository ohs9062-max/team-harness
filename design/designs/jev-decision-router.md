# Jev decision router

## Goal

The coordinator currently sends every registered tool schema to its generation model on every
turn. Those schemas are repeated input tokens even when the next step only needs a small subset.
The optional Jev decision router classifies the next turn into a fixed tool profile before the
expensive coordinator request. It does not write code or replace a worker.

## Request flow

`coordinator/loop.py` takes at most `max_state_chars` characters from the six most recent
messages and asks one `choice` question through TypeSafe's `POST /v1/systemone` endpoint. The
choices are:

- `DELEGATE`: worker management plus inspection, shell, and todo tools.
- `EDIT`: file inspection/editing, shell, and todo tools.
- `MONITOR`: observe and control workers that already exist.
- `FULL`: every registered tool for mixed or uncertain work.

Tools unknown to the built-in profiles are always retained so an additive extension cannot become
unreachable after upgrading the router.

Only a choice at or above `confidence_threshold` is applied. A disabled router, missing API key,
timeout, HTTP error, malformed response, unknown choice, omitted confidence, or low confidence
selects `FULL`. The coordinator run therefore retains its previous capability when Jev is not
available.

## Data and measurement

The compact state is sent to TypeSafe when the feature is enabled. It deliberately omits the
system prompt when newer messages consume the character budget, but it can contain recent user
text and tool output. Projects should enable the feature only when that data may be sent to the
configured endpoint.

Every attempt is appended to `run.json.decisions`. The record contains the choice, confidence,
probabilities, backend usage, latency, fallback reason, selected tools, serialized tool schema
sizes before and after selection, and an estimate of coordinator input tokens saved. The estimate
uses four characters per token and measures only removed tool schemas; provider usage remains the
source of truth for billed tokens.

## Scope and MODE contracts

`DecisionRequest` also defines `worker_failure`, `review_escalation`, and `agent_selection` kinds
so those bounded decisions can use the same backend later. This release only invokes
`tool_selection`. MODE A still waits for the user's final selection, MODE C still requires checks
and a reviewer, and protocol roles do not switch agent families automatically. Those behaviors are
defined by the Harness Protocol and TH-D18.

The router is used by `th run`, the Python SDK, and `th repl`. Harness Protocol workers continue
through their existing one-shot runner, so enabling Jev does not alter MODE A/B/C stage semantics.

## Configuration

```toml
[decision_router]
enabled = true
backend = "jev"
api_url = "https://api.typesafe.ai/v1/systemone"
model = "jev-latest"
confidence_threshold = 0.85
timeout_s = 3.0
max_state_chars = 4000
tool_routing = true
```

Set `TYPESAFE_API_KEY`; `JEV_API_KEY` is accepted as an alias. For a single run,
`TEAM_HARNESS_JEV=1 th run "..."` enables the configured router. The default is disabled.
