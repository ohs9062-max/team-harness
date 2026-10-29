# 변경 로그 (Changelog)

- TH-D47: 설명 절의 외부 경로 읽기 표현을 오류 대신 경고로 처리합니다.
- 검색 패턴에 있는 코디네이터 기록 폴더 표현의 영역 가드 오탐을 수정합니다.

- 완료 처리된 watch 작업을 빠르게 건너뛰고 24시간 뒤 보관 등록부로 옮겨 감시 지연을 줄입니다.
- 손상된 UTF-8과 JSON 줄이 있어도 손실 리포트를 생성합니다.
- TH-D47: 외부 경로 설명과 셸 문법의 지시서 점검 오탐을 줄입니다.
- TH-D47: 워커 지시서의 경로와 검수를 실행 전에 점검합니다.
- TH-D46: 하루 손실 리포트 CLI와 watchd 일일 요약을 추가합니다.

- 워커의 TH_JOB_TASK 누출과 다른 작업의 태그로 인한 생존 오판을 수정합니다.

- DONE 후 잔류 워커의 멈춤 오탐과 완료 알림 지연을 수정합니다.

- TH-D45: SDK 실행과 protocol 모드 시작 시 공유 일일 문서 이관 판정을 실행합니다.

- heredoc과 패치 문서 본문의 코디네이터 기록 폴더 표현에 대한 영역 가드 오탐을 수정합니다.

- TH-D44: 워커 자기 중단을 공통 판정하고 최대 2회 자동으로 이어서 실행합니다.

- watchd가 작업 경계에서 컨텍스트 롤오버를 강제하고 복구 요약으로 재실행합니다.
- 영역 가드 거부 문구가 다른 방법을 시도하기 전에 FAILED를 권하지 않도록 수정합니다.

- TH-D43: timeout 오탐 제거, 사용자 메시지별 가드 초기화 및 Codex 창 미확인 표시.

- 명령 경로 끝 문장부호와 한글 조사로 인한 워커 영역 가드 오탐을 수정합니다.

- TH-D43: Codex 코디네이터 폴링·권한 재시도 가드 및 워커 경로 오탐 수정.

- TH-D42: Add destructive-command guard and revise worker failure rules.

- TH-D40: Jev tier/failure advice across launch, run/repl/SDK, protocol and watch paths; repair shell Python selection (advisory only).

이 프로젝트의 모든 주목할 만한 변경 사항은 이 파일에 기록됩니다.

형식은 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)를 기반으로 하며,
이 프로젝트는 [유의적 버전(Semantic Versioning)](https://semver.org/spec/v2.0.0.html)을 준수합니다.

## [Unreleased]

- run/repl과 protocol 실행 중 작업 경계에서 강제 롤오버를 연결했습니다.

- 분리 작업의 실제 PID 기록과 환경 태그·세션 기반 생존 판정을 추가합니다.

### 변경됨 (Changed)

- 워커 읽기·출력 크기를 제한하고 자기 중단 출구를 제거했다.

- Codex 기본 워커 모델을 gpt-6.1-terra로 변경 (TH-D29 개정)

- **실행 중 롤오버 수정 (TH-D32).** Codex 텍스트 세션 ID와 작업별 예산을 추적하고 측정 불가 이유를 기록합니다.
- **워커 문서·인계 경계 강화 (TH-D32 보완).** 워커는 지시서의 `## 읽을 문서`만 읽고, 완료된 작업의 오래된 WORKLOG는 인계하지 않습니다. 감시기가 Codex의 현재 입력 맥락(누적 사용량 아님)을 측정해 상한 초과를 요청하며, coordinator 영역 훅·명시적 `allow_paths` 예외와 검수 호환성을 제공합니다.

- **워커 역할 격리 (TH-D34).** Shell·Protocol worker는 `TH_ROLE=worker`를 받고 coordinator journal 규칙·hook을 무시하며, Codex worker는 프로젝트 `AGENTS.md` 자동 로드를 끄고 front-matter prompt를 `--` 뒤로 전달합니다.

- **Claude 코디네이터 전용 및 명시 요청 워커 (TH-D30).** Claude는 기본 코디네이터 전용이며 Protocol 기본 역할·자동 대체·조사 병렬 참가자로 선택되지 않습니다. 사용자 명시 요청 때만 `claude-sonnet-5`(명시 모델 우선) Claude 워커를 실행하고, launcher는 `TH_USER_REQUESTED_CLAUDE=1` 없이는 exit 4로 거부합니다. MODE C 설계 기본 역할은 Codex `gpt-5.6-terra` high이고, Codex 쿼터 잠금 시 대체는 agy만 사용하며 코드 결과를 검증합니다.

- **`ths` 기본 출력 축약 및 쿼터 오탐 방지 (TH-D31).** 기본 조회는 실행 중과 최근 24시간 실패·검수 실패만 최대 10줄로 보이고, 전체 표는 `-a`로 유지합니다. 상태 표에는 대화 재출력 금지 안내를 붙였으며, `th-watch`는 프롬프트/worker suffix에 에코된 쿼터 문구를 잠금으로 오인하지 않고 쿼터 사건 뒤에도 이후 DONE/FAILED를 계속 알립니다.

- **Codex 워커 등급제 (TH-D29).** 워커와 Protocol Codex 역할의 기본값은 `gpt-5.6-terra medium`입니다. 복잡한 구현·디버깅은 Terra high, 핵심 로직·실험 분석은 `gpt-6.1-sol medium`, 어려운 오류·중요 설계는 Sol high로 하위 등급 실패 또는 명확한 용도일 때만 올립니다. agy는 단순 반복·대량·비코드 작업에만 쓰며 effort 인자가 없습니다. 명시적 오버라이드·설정 키·쿼터 잠금 대체 규칙은 유지하고, coordinator 기본 모델은 바꾸지 않습니다.

### 추가됨 (Added)

- 워크스페이스 기록과 완료 작업의 보존형 자동 이관 및 `th archive` 명령을 추가했습니다.

- **워커 컨텍스트 롤오버 세 경로 연결 및 기본 활성화 (TH-D32 3단계).**
  - 셸(`th-launch.sh`, `th-watch-lib.sh`, `th-status.sh`), run/repl(`agent_tools.py`, `manager.py`), 프로토콜(`mode_a.py`, `mode_b.py`, `mode_c.py`, `usage.py`) 세 실행 경로에 `worker_context` 서비스를 통합하고 `[worker_context] enabled = true` 기본 활성화.
  - 작업서 `task.md`의 `context_budget` 필드(기본 60000, 큰 단위 120000 등) 지원으로 최소 작업 단위 크기에 따른 유연한 소프트 캡 적용.
  - 진행 단계 경계(WORKLOG 갱신 직후)에서 롤오버를 집행하며, 직전 워커가 진척 없이 상한에 걸린 경우 작업을 더 쪼개도록 `WORKER_CONTEXT_PROGRESS_STALLED` 사건 발행.
  - `ProtocolState`에 `worker_context` 필드 및 `protocol_events.jsonl` 이벤트 기록 연동, stage 내부 롤오버 프로세스들의 사용량을 중복 없이 안전하게 합산하는 `combine_usages` 제공.
  - `design/user_config/worker_suffix.md`에 단계 경계 롤오버 및 실험별 `EXPERIMENT.md` 작성 규칙 반영.

- **명시적 worker context 등록부와 판정 CLI (TH-D32 2단계).**
  `worker_context` 서비스가 Codex rollout의 현재 입력 토큰(`last_token_usage.input_tokens`)만 측정하고,
  부작용 없는 60k/90k 판정과 프로젝트별 `.team-harness/worker_context/registry.json`의
  active → parked → retired 상태를 제공합니다. `th worker-ctx check|register|park|retire`는
  JSON 결과를 내며, `[worker_context]`(환경 변수 오버라이드 포함)는 `agy`를 기본 제외합니다.
  `worker_sessions.json`에는 선택적 context registry 참조만 추가했습니다. 이 단계는
  th-launch, th-watch, manager, run/repl 및 protocol에 연결하지 않으므로 자동 재개·재실행·종료를
  전혀 수행하지 않습니다. 다음 3단계에서 세 경로를 함께 연결하기 전까지 수동 CLI 호출만 가능합니다.

- **Worker digest (TH-D28).** 기존 watchd 사건 감지에 비동기 검수·읽기 전용 요약을 연결했습니다. 스크립트가 상태/산출물/수치/verify.sh/hook을 판정하고 workers.md에 원문을 보존합니다. 기본 agy 요약은 쿼터 잠금·실패·시간 초과 시 생략합니다. SDK·Protocol·config.toml 계약은 유지합니다. 셸 소비자는 데몬 최초 완료 알림의 검수가 `대기`일 수 있고 새 검수 결과가 있는 ths 열이 VERIFIED/VERIFY_FAILED로 표시됨에 유의해야 합니다. 기존 마커만 있는 작업의 한국어 표시는 유지합니다.

- **선택형 Jev decision router (TH-D24).** `[decision_router] enabled = true` 또는 `TEAM_HARNESS_JEV=1`이면 `th run`/SDK와 `th repl`이 매 coordinator 턴 전에 TypeSafe System One의 choice 질문으로 `FULL`/`DELEGATE`/`EDIT`/`MONITOR` 도구 프로필을 고릅니다. 충분한 confidence가 있는 응답만 적용하고 API key 부재, 오류·타임아웃, 잘못된 응답, 낮은 confidence는 전체 도구로 복귀합니다. 최근 상태는 `max_state_chars`로 제한되며 각 판단의 choice/probability/usage/latency/fallback과 도구 schema 전후 크기 및 추정 절감 토큰을 `run.json.decisions`에 기록합니다. `DecisionRouter`는 worker failure/review escalation/agent selection 종류도 표현하지만, 이번 릴리스는 기존 Protocol 계약을 지키기 위해 tool selection만 자동 적용합니다. 기본값은 꺼짐입니다.
- **Orca IDE용 대화형 Protocol 진입 스킬 (TH-D23).** `skills/orca-harness-protocol`을 설치하면 Orca의 Codex·Claude·Agy 프롬프트에서 `th cord`로 현재 세션을 코디네이터로 전환한 뒤 `A`, `B`, `C`, `상태`, `토큰`, `종료`를 입력할 수 있습니다. 1단계 구현은 자연어 명령을 기존 `th protocol` Runner로 변환하므로 MODE 상태 머신, Git Gate, 재개, 사용량 집계의 정본을 복제하지 않습니다. Orca의 tmux 중복 화면을 피하기 위해 `--no-visible`을 사용하며, worker 실행은 후속 Orca adapter 전까지 기존 배치 subprocess 방식입니다.
- **작업자용 코드 그래프 (선택, TH-D22).** `[context_graph] enabled = true` 또는 `TEAM_HARNESS_CONTEXT_GRAPH=1`이면, 하네스가 워커를 띄우기 전에 그 워커의 작업 디렉터리에 대해 [graft](https://github.com/trailhq/Graft)(`npx -y @nanonets/graft`, Node 필요)로 구조적 코드 그래프를 **실행당 디렉터리당 한 번** 빌드하고, 새 워커 프롬프트 앞에 그래프 조회 명령(`ask`/`callers`/`skeleton`/`grep`)을 붙입니다. 워커가 저장소 전체를 grep하고 파일을 통째로 읽는 대신 필요한 `파일:줄`로 바로 가게 해 탐색 토큰을 줄이는 것이 목적입니다. 그래프는 작업 트리 밖(`~/.team-harness/graft/`)에 저장되고 `.gitignore`도 건드리지 않으므로 protocol task 브랜치나 사용자 작업 트리에 파일이 생기지 않습니다(TH-D16). graft가 없거나 빌드가 실패하면 경고만 남기고 이전과 똑같이 실행합니다. `th run`/`th repl`/SDK와 `th protocol run|resume|relay` 모두에 적용됩니다. `Config.context_graph`, `TeamHarnessAgentRunner(context_graph=...)`, `load_context_graph_settings()`가 추가되었으며 모두 기본값이 있어 기존 소비자와 호환됩니다. **기본값은 꺼짐**입니다.
- **Protocol worker 실패 분류 및 패밀리 서킷 차단 (TH-D18).** MODE A/B/C가 공유하는 `TeamHarnessAgentRunner`가 이제 실패한 worker의 원인을 분류해 `AgentResult.failure_classification`에 담고 `protocol_state.json`의 `handoffs`와 `protocol_events.jsonl`에 기록합니다. TH-D10의 엄격한 stdout JSONL 스캔이 명시적인 하드 429를 찾은 경우에만 해당 에이전트 패밀리의 실행 범위 서킷이 열리고, 서킷이 열린 동안 같은 패밀리의 stage는 프로세스를 띄우지 않고 즉시 거부됩니다(`spawned=False`). 그 결과 stage 실패 메시지가 `"exited with code 1"`에서 `"... [rate_limit: ...]"`처럼 원인과 리셋 시각을 담게 됩니다. 하네스는 role에 지정된 백엔드를 자동으로 다른 패밀리로 교체하지 않습니다(TH-D6).
- **실행별 시간/토큰 집계 (TH-D21).** 새 `protocol/usage.py`가 stage별 벽시계 시간(하네스가 직접 측정 — 이전에는 계산 후 버려졌습니다)과 워커가 스스로 보고한 토큰 수·비용(claude의 `usage`+`total_cost_usd`, gemini의 `stats`, codex의 `token_count` 형태를 모두 인식)을 집계합니다. 모든 mode의 `handoffs`에 `duration_sec`와 `usage`가 기록되고, `th protocol status`가 총 시간·agent별/model별 시간 분해·토큰·비용을 출력합니다. **보고하지 않은 워커는 0이 아니라 미지(None)로 처리되며**, 합계가 실행된 stage 전부를 반영하지 않으면 "N개 중 M개만 반영" 경고를 명시합니다. 하네스는 모델 가격표를 갖지 않으므로 토큰을 돈으로 환산하지 않습니다. `extract_usage`/`summarize_usage`/`WorkerUsage`/`ProtocolUsageTotals`가 `team_harness.protocol`에서 export되고, `AgentResult`에 기본값 있는 `usage` 필드가 추가되었습니다(기존 `AgentRunner` 구현과 호환).
- **MODE C 실행 재개 (TH-D20).** `th protocol resume`이 저장된 상태의 mode를 읽어 MODE C 파이프라인을 `DESIGN`/`IMPLEMENT`/`REVIEW` 중 한 stage부터 다시 진행합니다(`--from-stage`, 생략 시 DONE이 아닌 가장 이른 stage). 기존 task worktree와 동결된 base commit을 재사용하므로, 5번째 stage에서 막힌 파이프라인을 이어갈 때 이미 끝난 DESIGN/IMPLEMENT의 워커 비용을 다시 지불하지 않습니다. `run_mode_c`와 새 `resume_mode_c`는 하나의 파이프라인 구현(`_run_pipeline`)을 공유합니다. worktree가 사라졌으면 조용히 다시 만들지 않고 그 이유로 BLOCKED 처리합니다(TH-D11).
- **`th protocol status` 추가 (TH-D20).** `--run-dir`로 한 실행의 stage별 기록(실행한 agent/model, 성공·실패, TH-D18의 실패 분류와 리셋 시각, CHECK 결과, blocker)을 보고, 생략하면 최근 protocol 실행 목록을 봅니다. 워커를 띄우거나 상태를 바꾸지 않는 조회 전용 명령입니다.
- `th protocol resume`의 `--selection`이 MODE A에서만 필수가 되었고, MODE C에는 `--from-stage`가 추가되었습니다. 서로 맞지 않는 조합은 오류로 거부됩니다. `resume_mode_c`와 `RESUMABLE_MODE_C_STAGES`가 `team_harness.protocol`에서 export됩니다.
- **Protocol role 설정이 `config.toml`을 따르고, 실행별 CLI 오버라이드가 추가됨 (TH-D19).** 프로젝트 `.team-harness/config.toml`과 전역 `~/.team-harness/config.toml`의 `[protocol.roles.<역할>]` 테이블로 7개 role의 `agent`/`model`/`effort`를 설정할 수 있습니다 — 지금까지 protocol role은 `HARNESS_MODE_*` 환경변수로만 설정 가능해서, CLAUDE.md가 문서화한 프로젝트 전체 설정 우선순위에서 유일하게 빠져 있었습니다. 우선순위는 높은 쪽부터 `--set-role` → `HARNESS_MODE_*` → 프로젝트 `config.toml` → 전역 `config.toml` → 내장 기본값입니다.
- `th protocol run|resume|relay`에 `--set-role <역할>.<필드>=<값>`(반복 가능), `--agent-timeout`, `--check-timeout` 플래그 추가. 타임아웃은 지금까지 함수 기본값(600초/300초)으로만 존재해 CLI에서 도달할 수 없었습니다. 잘못된 role/필드 이름은 조용히 무시되지 않고 오류로 거부됩니다.
- `load_protocol_config`에 `runtime_roles`/`role_tables`/`config_start_dir` 인자와 `load_protocol_role_tables`, `validate_role_tables`, `PROTOCOL_ROLE_NAMES`, `PROTOCOL_ROLE_FIELDS` 추가. 기존 `HARNESS_MODE_*` 사용법과 기존 인자는 그대로 동작합니다.
- `AgentResult`에 `spawned: bool = True`와 `failure_classification: dict | None = None` 필드 추가(둘 다 기본값이 있어 기존 `AgentRunner` 구현과 호환됩니다), 그리고 `AgentResult.resolve_effective_model()` 헬퍼 추가 — 실행되지 않은 stage는 설정된 모델을 `effective_model`로 보고하지 않고 `None`을 보고합니다(TH-D6 감사 정합성). MODE A에 4번, MODE C에 1번 중복돼 있던 모델 리졸브 로직을 이 헬퍼로 통합했습니다.

- **MODE A/MODE B 테스트 추가.** `mode_a.py`(859줄, block 종료점 약 10개, 병렬 fan-out 3단계, 사용자 선택 resume)와 `mode_b.py`(244줄)에는 직접 테스트가 아예 없었습니다(`mode_c.py`는 575줄의 테스트 보유). 41개 테스트를 추가해 protocol 패키지 전체 커버리지가 94%(mode_a 94%, mode_b 100%)가 되었습니다.

### 수정됨 (Fixed)

- 실행 중 워커 감시가 캡처 로그의 실제 Codex 세션 ID로 맥락을 측정하도록 수정.

- **protocol 상태 파일에 저장되는 모든 토큰 수가 `[MASKED]`가 되던 문제.** `protocol/state.py`의 비밀정보 마스킹 키 패턴이 `input_tokens`/`output_tokens`/`total_tokens`의 "token" 부분에 매칭되어, 사용량 수치가 로그에는 있는데도 저장 시점에 전부 가려졌습니다. 이제 sensitive 키에 걸려도 값이 실제 숫자면 마스킹하지 않습니다(자격증명은 맨 숫자가 아닙니다). 문자열·dict·리스트는 이전과 동일하게 마스킹되어 비밀정보 보호는 약화되지 않았습니다.
- MODE A에서 worker가 실패했을 때 그 워커의 실제 오류 메시지가 유실되던 문제. `state.blocker = result.error_message`로 담아둔 값이 바로 다음 줄의 `_block()`에 의해 `"Required MODE A worker failed: worker_1"`이라는 일반 메시지로 덮어써져, 그 대입은 사실상 죽은 코드였고 무엇이 잘못됐는지에 대한 유일한 설명(TH-D18의 실패 분류와 리셋 시각 포함)이 사라졌습니다. 이제 blocker가 레인 이름과 원인을 함께 담습니다.
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

- inbox 사건을 코디네이터 세션별로 라우팅하고 Codex waiter를 차단한다 (TH-D41).
