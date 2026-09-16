# 변경 로그 (Changelog)

이 프로젝트의 모든 주목할 만한 변경 사항은 이 파일에 기록됩니다.

형식은 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)를 기반으로 하며,
이 프로젝트는 [유의적 버전(Semantic Versioning)](https://semver.org/spec/v2.0.0.html)을 준수합니다.

## [Unreleased]

### 추가됨 (Added)

- **Protocol worker 실패 분류 및 패밀리 서킷 차단 (TH-D18).** MODE A/B/C가 공유하는 `TeamHarnessAgentRunner`가 이제 실패한 worker의 원인을 분류해 `AgentResult.failure_classification`에 담고 `protocol_state.json`의 `handoffs`와 `protocol_events.jsonl`에 기록합니다. TH-D10의 엄격한 stdout JSONL 스캔이 명시적인 하드 429를 찾은 경우에만 해당 에이전트 패밀리의 실행 범위 서킷이 열리고, 서킷이 열린 동안 같은 패밀리의 stage는 프로세스를 띄우지 않고 즉시 거부됩니다(`spawned=False`). 그 결과 stage 실패 메시지가 `"exited with code 1"`에서 `"... [rate_limit: ...]"`처럼 원인과 리셋 시각을 담게 됩니다. 하네스는 role에 지정된 백엔드를 자동으로 다른 패밀리로 교체하지 않습니다(TH-D6).
- **MODE C 실행 재개 (TH-D20).** `th protocol resume`이 저장된 상태의 mode를 읽어 MODE C 파이프라인을 `DESIGN`/`IMPLEMENT`/`REVIEW` 중 한 stage부터 다시 진행합니다(`--from-stage`, 생략 시 DONE이 아닌 가장 이른 stage). 기존 task worktree와 동결된 base commit을 재사용하므로, 5번째 stage에서 막힌 파이프라인을 이어갈 때 이미 끝난 DESIGN/IMPLEMENT의 워커 비용을 다시 지불하지 않습니다. `run_mode_c`와 새 `resume_mode_c`는 하나의 파이프라인 구현(`_run_pipeline`)을 공유합니다. worktree가 사라졌으면 조용히 다시 만들지 않고 그 이유로 BLOCKED 처리합니다(TH-D11).
- **`th protocol status` 추가 (TH-D20).** `--run-dir`로 한 실행의 stage별 기록(실행한 agent/model, 성공·실패, TH-D18의 실패 분류와 리셋 시각, CHECK 결과, blocker)을 보고, 생략하면 최근 protocol 실행 목록을 봅니다. 워커를 띄우거나 상태를 바꾸지 않는 조회 전용 명령입니다.
- `th protocol resume`의 `--selection`이 MODE A에서만 필수가 되었고, MODE C에는 `--from-stage`가 추가되었습니다. 서로 맞지 않는 조합은 오류로 거부됩니다. `resume_mode_c`와 `RESUMABLE_MODE_C_STAGES`가 `team_harness.protocol`에서 export됩니다.
- **Protocol role 설정이 `config.toml`을 따르고, 실행별 CLI 오버라이드가 추가됨 (TH-D19).** 프로젝트 `.team-harness/config.toml`과 전역 `~/.team-harness/config.toml`의 `[protocol.roles.<역할>]` 테이블로 7개 role의 `agent`/`model`/`effort`를 설정할 수 있습니다 — 지금까지 protocol role은 `HARNESS_MODE_*` 환경변수로만 설정 가능해서, CLAUDE.md가 문서화한 프로젝트 전체 설정 우선순위에서 유일하게 빠져 있었습니다. 우선순위는 높은 쪽부터 `--set-role` → `HARNESS_MODE_*` → 프로젝트 `config.toml` → 전역 `config.toml` → 내장 기본값입니다.
- `th protocol run|resume|relay`에 `--set-role <역할>.<필드>=<값>`(반복 가능), `--agent-timeout`, `--check-timeout` 플래그 추가. 타임아웃은 지금까지 함수 기본값(600초/300초)으로만 존재해 CLI에서 도달할 수 없었습니다. 잘못된 role/필드 이름은 조용히 무시되지 않고 오류로 거부됩니다.
- `load_protocol_config`에 `runtime_roles`/`role_tables`/`config_start_dir` 인자와 `load_protocol_role_tables`, `validate_role_tables`, `PROTOCOL_ROLE_NAMES`, `PROTOCOL_ROLE_FIELDS` 추가. 기존 `HARNESS_MODE_*` 사용법과 기존 인자는 그대로 동작합니다.
- `AgentResult`에 `spawned: bool = True`와 `failure_classification: dict | None = None` 필드 추가(둘 다 기본값이 있어 기존 `AgentRunner` 구현과 호환됩니다), 그리고 `AgentResult.resolve_effective_model()` 헬퍼 추가 — 실행되지 않은 stage는 설정된 모델을 `effective_model`로 보고하지 않고 `None`을 보고합니다(TH-D6 감사 정합성). MODE A에 4번, MODE C에 1번 중복돼 있던 모델 리졸브 로직을 이 헬퍼로 통합했습니다.

### 수정됨 (Fixed)

- `test_bash_cancellation_cleans_up_process_group` 및 `test_bash_cancellation_escalates_for_sigterm_ignoring_group`의 심각한 간헐적 실패(단독 실행 시 12회 중 10회 실패). `bash`는 프로세스 그룹을 죽이고 자신이 띄운 셸을 정상적으로 reap하지만, 셸의 자식은 테스트 프로세스의 손자입니다. 셸이 먼저 죽으면 이미 죽은 손자는 init으로 재양육되어 init이 reap할 때까지 좀비로 남고, 그 동안 `os.kill(pid, 0)`은 계속 성공합니다. 이 창은 약 20ms에 불과하지만 두 테스트가 `task.cancel()` 직후 즉시 단정하고 있어 부하가 걸린 머신에서 대부분 실패했습니다. 이제 유한한 데드라인(5초) 안에서 폴링하며, 프로세스가 실제로 종료된다는 사실은 그대로 검증합니다. `shell_tools`의 정리 로직 자체는 정상이었으며 변경하지 않았습니다.
- `test_stdout_read_error_leaves_scan_retryable`가 벽시계 시간에 따라 실패하던 문제. 이 테스트는 `claude_rate_limit.jsonl` 픽스처의 절대 `resetsAt`(1784811600 = 2026-07-23T13:00Z)에 의존하는데 `now`를 고정하지 않아, 실제 시간이 그 시각을 지난 뒤로는 서킷이 생성 즉시 만료되어 항상 실패했습니다. 형제 테스트와 동일하게 `rate_limits._utc_now`를 픽스처 리셋 이전으로 고정합니다.

## [0.7.0] - 2026-07-20

### 추가됨 (Added)

- **실행 범위 작업자 속도 제한 서킷 브레이커 (TH-D10).** 종료된 작업자의 stdout JSONL에서 실패한 429 결과 및 성공으로 이어지지 않은 거부된 `rate_limit_event` 레코드를 검사합니다. 성공한 작업자는 절대 서킷을 작동시키지 않으며, 일시적인 stdout 읽기 오류는 재시도 가능합니다. 서킷이 걸린 에이전트 템플릿 패밀리는 공급자 리셋 시간(또는 900초 기본 쿨다운)까지 다른 프로세스를 띄우지 않고 차단됩니다. 조율자는 `agent_availability`를 검사할 수 있으며, `run.json`에 `rate_limited_families` 감사 목록이 추가됩니다.
- 새로운 `[coordinator]` 설정: `rate_limit_circuit_breaker = true` 및 `rate_limit_default_cooldown_s = 900`. SDK 생성자에서도 동일한 오버라이드를 지원합니다.
- 비LLM 호출자를 위해 다형적 반환 결과를 처리할 수 있는 `parse_rate_limited_spawn_result` 헬퍼 함수 추가.

## [0.6.1] - 2026-07-20

### 추가됨 (Added)

- **Grok Build CLI 작업자 지원.** 비대화형 무인 실행을 위한 내장 `grok` 에이전트 템플릿: `--always-approve`, `--output-format streaming-json`, `--no-auto-update`, 기본 모델 `grok-4.5`, 선택적 `--reasoning-effort`, 최종 NDJSON `end` 이벤트(`sessionId`) 캡처 후 `--resume` 지원. 외부 인증 (`XAI_API_KEY` 또는 `grok login`). 샘플 설정, TUI 색상/이모지, 문서 및 단위 테스트 포함.

### 마이그레이션 (Migration)

- 이전에 무관한 바이너리를 위해 커스텀 `[agents.grok]`을 정의했던 사용자는 이름을 바꾸거나 상속 필드를 명시적으로 초기화해야 합니다 (`openhands`와 동일한 방식).

## [0.6.0] - 2026-07-18

장기 조율자 실행을 위한 컨텍스트 절약: 재전송되는 접두사 비용 절감, 실제 압축 트리거 지원, 컨텍스트 및 run.json 기록 한도 제한.

### 추가됨 (Added)

- **프롬프트 캐싱 (Prompt caching).** 새로운 `coordinator.prompt_cache` 설정 (`"auto"` | `"off"`, 기본값 `"auto"`). Anthropic 계열 모델의 경우 시스템 메시지와 각 턴의 마지막 메시지에 임시 캐시 중단점(`cache_control: {"type": "ephemeral"}`)을 설정하여 후속 턴에서 접두사를 캐시 판독합니다. 캐시 판독된 프롬프트 토큰은 `TurnRecord.usage`의 `cached_prompt_tokens`로 기록됩니다.
- **안전망 압축 (Safety-net compaction).** 새로운 `coordinator.compact_above_tokens` 설정 (`int | None`, 기본값 `None`). 설정된 토큰 수에 도달하면 사용자 또는 도구 결과 경계에서 즉시 압축이 실행됩니다. 반복 압축 시에도 초기 작업 프롬프트는 원본 그대로 보존됩니다.
- **작업자 출력 한도 제한.** `read_agent_output(tail_bytes=...)`에 상한을 두는 `coordinator.read_output_max_tail_bytes` (기본값 16384). `read_new_agent_output`의 상한을 설정하는 `coordinator.read_new_output_max_bytes` (기본값 65536).
- **run.json 영속화 한도 제한.** run.json에 저장되는 도구 결과 및 메시지를 잘라내는 `coordinator.run_log_tool_result_max_bytes` (기본값 8192). 인메모리 컨텍스트와 사용량 집계는 영향받지 않습니다.
- SDK `TeamHarness(...)` 생성자에 `compact_above_tokens` 및 `prompt_cache` 매개변수 추가.

### 변경됨 (Changed)

- 직접 생성 작업자 바닥글이 작업자에게 stdout을 결과 카드(15줄 이내: 결과, 핵심 결정, 변경 파일, 상세 파일 경로)로 끝마치고 긴 보고서는 파일로 작성하도록 안내하여 매 턴 전체 스트림이 재전송되는 것을 방지합니다.

## [0.5.4] - 2026-07-17

### 추가됨 (Added)

- 임베디드 호출자가 `capability_roster_context_v1`을 협상하고 `CallerContext`에서 동결된 하네스 기능 명부 경로, 다이제스트, 컴팩트 JSON 요약을 전달할 수 있게 됨.

## [0.5.3] - 2026-07-17

### 변경됨 (Changed)

- 조율자의 `read_file` 및 `read_new_file_content` 호출이 도구 결과당 파일 내용 기준 32,768자 및 UTF-8 인코딩 후 32KiB로 제한됨. `read_file`은 `offset_chars` / `limit_chars` 페이지네이션을 지원하며 증분 판독기는 FIFO 백로그를 보존함 (TH-D9).

### 수정됨 (Fixed)

- 대용량 원시 아티팩트를 읽을 때 단일 턴에 전체 파일이 주입되어 컨텍스트 압축을 우회하던 결함 수정.

## [0.5.2] - 2026-07-17

### 추가됨 (Added)

- 조율자 `bash` 도구가 장기 실행 포그라운드 명령을 위한 양의 정수 `timeout_seconds`를 지원함 (기본값 120초 유지) (TH-D8).

### 수정됨 (Fixed)

- 타임아웃, 취소 또는 실패한 셸 도구 호출이 짧은 SIGTERM 유예 후 전체 프로세스 그룹을 수거하도록 수정.

## [0.5.1] - 2026-07-17

### 추가됨 (Added)

- 동일 실행 내에서 안전하게 세션을 이어갈 수 있는 `spawn_agent(mode="resume", resume_from_agent_id="<agent-id>")` 추가 (TH-D4).

### 수정됨 (Fixed)

- 실시간 조율자가 아직 발행되지 않은 벤더 세션 ID를 추측할 필요가 없어짐. `mode="resume"` 없이 재개 선택자를 전달하면 새로운 작업자를 조용히 띄우는 대신 실패하도록 수정.

## [0.5.0] - 2026-07-16

### 추가됨 (Added)

- **명시적 임베디드 호출자 계약 (TH-D7).** `CallerContext`, `get_capabilities()`, `TEAM_HARNESS_CAPABILITIES`를 통해 명명된 기능 계약을 협상 지원.
- `TeamHarnessResult`가 `run_json_path`, `session_output_dir`, `coordinator_input_path`를 반환함.
- 클라이언트 생성 전에 생성된 시스템/사용자 입력을 원자적으로 기록.
- 모든 직접 생성 작업자에게 `agent_assignment.json` 및 유효 프롬프트 바닥글 제공.
- 작업자 stdout/stderr를 정본 실행 디렉터리 아래에 직접 캡처하고 세션 ID 최종 스캔을 대기함.
- 내장 `type=harness` 자손에게 `TEAM_HARNESS_CALLER_CONTEXT`를 전파.

### 수정됨 (Fixed)

- 작업자 감시자 및 세션 캡처 실패가 정상 마감 전에 유출되지 않도록 수정. 프로세스 테이블 프로브 실패 시에도 생성된 PGID를 사용하여 안전하게 정리.

## [0.4.0] - 2026-07-14

### 추가됨 (Added)

- **생성별 추론 노력 오버라이드 (TH-D6).** `spawn_agent(effort=…)` 지원.
- **run.json 내 모델/추론 노력 감사 추적 기록.** `requested_model` / `requested_effort` 및 `effective_model` / `effective_effort` 기록.

## [0.3.1] - 2026-07-14

### 추가됨 (Added)

- `antigravity` 에이전트 템플릿에 `--model` 플래그 주입 지원.
- 공식 문서 사이트 구축 (team-harness.writeit.ai).

## [0.3.0] - 2026-07-13

### 추가됨 (Added)

- Google Antigravity CLI(`agy`) 인쇄 모드 서브프로세스를 위한 내장 `antigravity` 작업자 지원.
- **영속적 작업자 프로세스 식별자 및 고아 프로세스 수거 (TH-D5).** 작업자를 자체 프로세스 그룹 리더(`start_new_session=True`)로 실행하고 `pid`/`pgid`/`starttime`을 영속화. `th reap` 명령 및 `reap_run()` API 추가.
- Linux 커널 부팅 ID + 시작 틱을 활용한 안전한 식별자 검증 및 재할당 방지.
- GPT-5.6 모델 패밀리(`gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`) 컨텍스트 추적 레지스트리 지원.

### 변경됨 (Changed)

- 기본 조율자 및 Codex 작업자 모델을 `gpt-5.6-sol`로 업데이트.
- `worker_sessions.json` 스키마 버전 2 → 3 상향 (pid, pgid, starttime 필드 추가).
- `run.json` 원자적 쓰기(임시 파일 + rename) 적용.
- 프로세스 그룹 단위 종료 및 수거 적용.

## [0.2.10] - 2026-05-26

### 변경됨 (Changed)

- `spawn_agent(worker_label=...)`가 `output_path`를 대체.

## [0.2.8] - 2026-05-14

### 수정됨 (Fixed)

- 작업자 출력이 `<stem>.stdout.jsonl` 및 `<stem>.stderr.log`로 분리되어 아티팩트 디렉터리 충돌 방지.

## [0.2.7] - 2026-05-12

### 추가됨 (Added)

- 조율자 `spawn_agent` 스키마에 기본 템플릿 플래그 명시.
- 완료된 작업자의 메타데이터를 포함한 장애 진단 정보 추가.

### 변경됨 (Changed)

- 기본 GPT 모델 참조를 `gpt-5.5`로 업데이트.

### 수정됨 (Fixed)

- `read_new_agent_output`이 유한한 창을 읽도록 개선하여 대량 백로그 유입 방지.
- 중복 단독 플래그 디듀플리케이션 지원.

## [0.2.6] - 2026-05-06

### 추가됨 (Added)

- `spawn_agent`에 `mode = "resume"` 및 `resume_from_session_id` 제어 지원.

### 수정됨 (Fixed)

- Claude Code 작업자의 최종 이벤트 세션 ID 캡처 지원.

## [0.2.5] - 2026-05-05

### 추가됨 (Added)

- `worker_sessions.json` 스키마 v2에 작업자 장애 진단 정보 추가.
- 구조화된 진단 정보를 포함하는 `TeamHarnessError.detail` 추가.

## [0.2.4] - 2026-05-05

### 추가됨 (Added)

- Python SDK `TeamHarness(output_dir=...)` 옵션 추가.

## [0.2.3] - 2026-05-05

### 추가됨 (Added)

- Python SDK `agent_models` 및 `agent_reasoning_efforts` 옵션 추가.

## [0.2.2] - 2026-04-27

### 추가됨 (Added)

- 개발 가이드 `CLAUDE.md` 추가.
- `.claude/skills/team-harness/SKILL.md` 에이전트 스킬 추가.

## [0.2.1] - 2026-04-27

### 추가됨 (Added)

- Agent Skills 표준 지원 (`.agents/skills/` 디렉터리의 `SKILL.md` 탐색).
- 조율자 시스템 프롬프트에 스킬 목록 렌더링.

### 제거됨 (Removed)

- **주요 변경**: 기존 파이썬 기반 스킬 시스템 제거. 마크다운 지침 파일 형식으로 전환.

## [0.2.0] - 2026-04-27

### 변경됨 (Changed)

- **주요 변경**: 공개 SDK 클래스명 변경 (`Harness` → `TeamHarness`, `HarnessResult` → `TeamHarnessResult`, `HarnessError` → `TeamHarnessError`).
- **주요 변경**: 환경 변수명을 `TEAM_HARNESS_*`로 변경 (`HARNESS_*` 대체).
- 조율자 시스템 프롬프트 간소화.

### 수정됨 (Fixed)

- REPL에서 Shift+Enter가 제출 대신 줄바꿈을 입력하도록 수정.

## [0.1.6] - 2026-04-22

### 추가됨 (Added)

- API 오류 발생 시 대체 하네스로의 자동 장애 조치(Auto-failover) 프로토콜 추가.
- 에이전트 로그에서 API 오류를 탐지하는 `api_error_classifier` 모듈 추가.

## [0.1.5] - 2026-04-22

### 추가됨 (Added)

- 스트리밍 출력 중 인라인 마크다운 렌더링(굵은 글씨, 제목, 인용문) 지원.

## [0.1.4] - 2026-04-21

### 추가됨 (Added)

- URL 하이라이트(시안색 밑줄) 및 2칸 들여쓰기 시각 효과 추가.

## [0.1.3] - 2026-04-20

### 추가됨 (Added)

- 스피너 애니메이션, iTerm2 탭 진행률 표시, 프롬프트 스타일링, 에이전트별 이모지, 경로 색상 표시 등 터미널 시각 기능 추가.

## [0.1.0] - 2026-04-17

### 추가됨 (Added)

- REPL 붙여넣기 미리보기(4줄 이상 붙여넣기 시 접기 표시) 추가.

## [0.0.1] - 2026-04-16

### 추가됨 (Added)

- `team-harness` 최초 공개 릴리스.
- 외부 작업자 CLI(Codex, Gemini, Claude Code, opencode, pi, OpenHands) 오케스트레이션.
- OpenAI 호환 API 및 Codex 구독 백엔드 지원.
- 대화형 REPL (`/compact`, `/clear`, `/agents`, `/log`, `/quit`).
- 자동 압축, 세션 추적, 실행 로그, `th init` 지원.
