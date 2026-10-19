# 워커 컨텍스트 롤오버 (TH-D32)

상태: 공식 설계, 구현 전. 이 문서는 현재 구현을 설명하는 문서가 아니다.
롤오버는 대화가 커진 워커를 종료하고 작업현황 문서로 새 워커에게 일을 넘기는 절차다.
목적은 누적 입력 비용을 줄이면서 완료한 작업과 실험 판단을 잃지 않는 것이다.
## 1. 정책과 경계

- 현재 맥락(active input context)은 마지막 모델 호출의 입력 토큰 수다. 누적 사용량이 아니다.
- 일반 작업의 맥락 상한은 고정 60k가 아니라 작업서마다 `context_budget`으로 정한다.
  코디네이터가 최소 작업 단위 크기로 정한다(기본 60000, 큰 단위는 120000 등).
  작업서에 없으면 설정의 `[worker_context] soft_cap_tokens`(60000)를 따른다.
  상한 초과를 관측하면 프로세스를 즉시 죽이지 않고 진행 중인 자연스러운 단위를 마친다.
  감시기는 Codex rollout의 `last_token_usage.input_tokens`라는 현재 입력 맥락만 측정해
  `context_exit_requested.json`을 한 번 쓰며, 누적 "tokens used"는 상한 판정에 쓰지 않는다.
  단위는 작업서의 단계(파일 수정, 테스트 실행, 결과 정리 등)이며 단계 경계(WORKLOG 갱신 직후)에서
  인계·교체 판정을 한다. 정확히 상한(60000/context_budget)이면 계속한다.
  직전 워커가 진척(git diff/체크포인트/문서 진척) 없이 상한에 걸렸으면 같은 상한으로 재시작하지 않고
  작업을 더 쪼개라는 사건을 코디네이터에 낸다.
- 다음 작업도 60k(또는 context_budget) 미만이면 같은 논리 세션을 활용하되, 모든 후속 resume에는 아래 세 조건이 필수다.
  (a) 같은 `task_key`, (b) 작업서의 `follow_up: short`, (c) 최신 맥락 ≤ 90000.
  하나라도 아니면 새 세션과 작업현황 문서로 시작한다. 60k 미만도 예외가 아니다.
- 세 조건을 만족하는 짧은 후속 작업은 60k 초과 세션도 재개할 수 있다.
  이 예외는 해당 후속 작업에만 유효하다. 90k 초과 관측 시 자연스러운 단위 뒤 롤오버한다.
- 길이와 작업 동일성을 LLM이 추측하지 않는다. 코디네이터가 작업서에 명시한다.
- TH-D2/4에 따라 resume도 새 1회성 프로세스다. 실행 중 stdin에는 어떤 지시도 보내지 않는다.
- 작업 하나의 동시 워커 프로세스는 1개다. 인계 종료 후 즉시 프로세스를 정리한다.
  대기 세션은 프로세스 없이 공급자 세션 파일과 메타데이터만 보관하므로 즉시 폐기하지 않는다.
- 작업 키별 최근 2개를 보관하고 초과분은 오래된 순서로, parked 후 24시간 경과 시 폐기한다.
- 2026-10-15 실측 기반 추정: 후속 20회면 resume 80k×20≈1.6M, 새 워커 55k×20≈1.1M
  (약 30% 절감). 2~3회면 약 0.2M 대 0.3M으로 재독 비용 때문에 resume이 유리할 수 있다.
  이는 보장값이 아니며 오래 쉰 세션의 첫 호출은 캐시 만료로 비캐시일 수 있다.
## 2. 작업서와 설정

작업서 `task.md` 선두에 다음 YAML 블록을 둔다. API 호출도 같은 필드를 구조화해 전달한다.
```yaml
---
task_key: experiment/resnet-baseline
follow_up: short
context_budget: 120000
---
```
`task_key`는 코디네이터가 유지하는 불변 문자열이다. 다른 일을 같은 키로 합치지 않는다.
`follow_up`은 `short` 또는 `normal`이며 생략하면 `normal`이다. 잘못된 값은 실행 전 거부한다.
`context_budget`은 코디네이터가 작업 단위에 맞춰 지정하는 맥락 상한(기본 60000, 큰 단위 120000 등)이며
생략 시 설정의 `soft_cap_tokens`를 따른다.
`short`는 짧은 수정·테스트 재실행·직전 디버깅 마무리만 표시한다. 키 누락은 신규 실행 전
코디네이터가 채우도록 오류로 반환하며, 기존 비활성화 경로는 그대로 허용한다.
Protocol은 루트 키 아래 lane/stage 식별자를 붙여 코디네이터가 키를 확정한다.
MODE A의 병렬 lane은 각각 별도 작업으로 취급하며 다른 lane의 세션을 재개하지 않는다.

설정은 TH-D19의 CLI/런타임 → 환경 → 프로젝트 → 전역 → 기본값 계층을 공유한다.
별도 설정 파일 없이 `[worker_context]` 하나만 추가한다.
```toml
[worker_context]
enabled = true
soft_cap_tokens = 60000
resume_max_tokens = 90000
max_parked_sessions = 2
parked_ttl_hours = 24
disabled_agents = ["agy"]
```
종류별 비활성화도 위 목록 하나로 처리한다. 설정은 시작 때 한 번 읽으며 세 경로가 동일하게 해석한다.
## 3. 측정과 상태 등록부

공유 Python 모듈 `src/team_harness/worker_context.py`(신설)가 측정·판정·등록부 변경을 전담한다.
Codex는 캡처한 세션 ID와 일치하는 `~/.codex/sessions/**/rollout-*.jsonl`의
`event_msg/token_count`에서 `payload.info.last_token_usage.input_tokens`를 읽는다.
Claude는 stream-json의 마지막 완료된 모델 호출 usage를 읽고, 캐시 읽기/쓰기 입력이
분리된 이벤트에서는 비캐시 입력과 합쳐 현재 입력을 만든다. 누적 result usage는 쓰지 않는다.
중복 이벤트는 호출 ID로 제거한다. 공급자 형식을 fixture로 검증하기 전에는 unknown 처리한다.
Agy는 토큰 정보가 없어 초기 자동 롤오버 대상에서 제외한다. 측정 상태 `estimated`와 null을
기록하며 로그 크기나 근거 없는 배수로 수치를 만들지 않는다(TH-D21).
소스 경로·이벤트 위치·호출 ID·측정 시각을 보관하고 JSONL은 마지막 읽은 위치부터 읽는다.
파일 축소/교체 때만 해당 세션 소스를 재탐색하며, 설정·규칙·프롬프트를 재독하지 않는다.

저장소별 `.team-harness/worker_context/registry.json`에 실행 간 등록부를 둔다.
Git 없는 작업은 작업 루트 아래 같은 상대 경로를 쓴다. 각 실행은 이 등록부의 절대 경로를 받는다.
기존 `worker_sessions.json`의 필드·의미는 보존하고 context 참조만 선택적으로 추가한다.
필드: `schema_version`, `workspace_id`, `task_key`, `session_id`, `agent_type`, `model`,
`state`, `pid`, `pgid`, `process_starttime`, `run_id`, `lane`, `stage`, `worktree`, `branch`,
`input_tokens`, `measurement_status`(measured/estimated/unknown), `source`, `source_offset`,
`call_id`, `measured_at`, `created_at`, `parked_at`, `last_used_at`, `retired_at`,
`worklog_path`, `experiment_path`, `checkpoint`, `reason`, `generation`, `pending_launch`.
시각은 UTC로 저장한다. 활성 PID가 없어도 세션 ID만 있으면 parked일 수 있다.

| 상태 전이 | 조건과 효과 |
| --- | --- |
| 신규 → active | 키별 실행 잠금 확보, 프로세스 생성과 ID 캡처 후 등록 |
| active → parked | 단계 문서 확정, 종료 확인, PID/PGID 해제; 완료·롤오버 모두 세션 보존 |
| parked → active | 세 조건 + TTL/보관 한도/공급자 resume 지원/같은 종류 확인 후 새 프로세스 |
| parked → retired | TTL 또는 개수 초과; 자동 resume 대상 제외, 해당 공급자 세션만 안전하게 폐기 |
| active → retired | 직접 전이 금지; 먼저 프로세스 종료를 확인하고 parked를 거친다 |

retired는 재개할 수 없다. 공유 로그·다른 세션·살아 있는 세션 파일은 삭제하지 않는다.
폐기 실패는 retired 상태와 오류를 남겨 후속 정리하며 작업 실패와 구분한다.
후보는 최근 사용 순으로 고르며 resume도 최신 WORKLOG와 Git 상태를 확인한다.
키별 실행 잠금과 등록부 파일 잠금, 임시 파일+원자 교체, generation 비교로 이중 실행을 막는다.
런처 사망 후에는 PID 재사용을 starttime으로 검증하고 기존 수거 계약을 따른다. 프로세스를 입양하지 않는다.
## 4. 판정 함수와 집행 순서

`decide(request, snapshot, settings, now) -> decision`은 부작용 없는 단일 판정 함수다.
입력: 키·follow_up·context_budget·종류·lane/stage·평가 시점(`dispatch`/`boundary`), 후보 세션과 측정값,
문서 체크포인트, 종료 여부, 긴 실행 대기 요청, 설정. 입력 객체는 호출자가 임의 추론하지 않는다.
출력: `action`(`continue`/`rollover`/`resume`/`fresh`/`blocked`), `reason`,
`session_id`, `input_tokens`, `measurement_status`, `worklog_path`, `generation`.
CLI `th worker-ctx check --task-file … --registry … --session-id … --phase boundary`
는 JSON으로 같은 결과를 반환한다. 정상 판정 exit 0, 잘못된 입력/읽기 실패는 비영(continue로 위장 금지).
등록·종료확정·정리도 `th worker-ctx register|park|retire`로 같은 모듈을 호출한다.

1. dispatch는 키 잠금, parked 정리 후보 계산, 최신 측정 확인 후 세 조건을 판정한다.
   종류·작업 범위가 다르거나 측정값을 확인할 수 없으면 resume하지 않고 fresh를 반환한다.
2. boundary는 워커가 단계를 마친 뒤 WORKLOG를 원자 갱신하고 직접 check를 호출한다.
   일반 작업 > context_budget(생략 시 soft_cap_tokens 60k), 허용된 short 후속 > 90k, 또는 긴 실행 대기 요청이면 rollover다.
   상한 초과 시 임의로 즉시 끊지 않고 진행 단계 경계(WORKLOG 갱신 직후)에서 인계·교체한다.
   직전 워커가 진척 없이 상한에 걸렸으면 같은 상한으로 재시작하지 않고 작업을 더 쪼개라는 사건을 코디네이터에 낸다.
   unknown은 경고와 마지막 유효 측정 시각을 남긴다. 유효 측정이 없으면 자동 토큰 롤오버를 보류한다.
3. rollover면 문서 경로와 이유를 기록하고 종료한다. 하네스는 출력 판정과 실제 종료만 집행한다.
   기존 프로세스가 살아 있는 동안 후임을 띄우지 않는다. 경계 확인 누락은 경고하며 자동 성공으로 보지 않는다.
4. 후임은 fresh 프로세스에 작업서·WORKLOG(실험이면 최대 80줄 EXPERIMENT 포함)만 인계받는다.
   이전 대화/전체 로그는 넣지 않는다. 필요 파일은 문서의 경로로 찾아 읽는다.
5. 후임 생성 실패는 명시적 실패 사건이다. 기존 parked 세션과 인계 문서를 유지하고
   자동 resume 폴백을 하지 않는다(TH-D4). 보관 초과 정리는 후임 생성 성공 뒤 확정한다.
   TTL이 지난 세션도 실패 복구 자료로 유지할 수 있으나 resume 자격은 복원하지 않는다.
## 5. 작업현황과 실험 문서

`WORKLOG.md`를 확장 재사용한다. 이미 worker_suffix가 단계 상태를 파일에 남기도록 하므로
새 문서와 내용이 어긋나는 일을 피할 수 있다. 위치는 작업 결과 폴더이며 60줄 이내로 교체한다.
필수 항목: **목표 / 현재 상태 / 변경 파일 / 체크포인트 / 테스트 / 미해결 / 다음 할 일 /
반복 금지 / 롤오버 이유**. 상단 한 줄에 키·단계·요약을 둔다. 체크포인트는 브랜치·SHA·worktree와
미커밋 diff 경로, Git 없는 일은 산출물 경로를 기록한다. 테스트는 실제 명령·결과·미실행을 구분한다.
롤오버가 아니면 이유는 `없음`이다. Git 상태와 문서가 다르면 Git을 신뢰하고 차이를 먼저 확인한다.
TH-D16에 따라 사용자/base 브랜치에 WIP 커밋을 만들지 않는다. 체크포인트 커밋은
워커 전용 worktree/task 브랜치에서만 허용한다. 그 밖은 파일·diff를 보존한다.

여러 모델을 학습·비교하는 논리 실험 하나마다 `<결과루트>/experiments/<experiment_key>/EXPERIMENT.md`
하나를 둔다. **목적 / 기준선 / 실행 목록 / 결과표 / 결정**을 필수로 하며 실행별 ID·모델·설정·명령·
시드·데이터 버전·job PID/로그/산출물 경로·상태, 결과표의 지표·출처·실패/미완료를 기록한다.
워커가 실행 시작·종료·분석·결정 단계마다 갱신하고 WORKLOG는 이 문서를 링크한다.
새 실험 워커는 이 문서로 실험 맥락을 복원하며 이전 세션 로그를 재독하지 않는다.
실행 제어에 필요한 작업서와 WORKLOG의 키·다음 할 일은 함께 받는다.
3단계 worker_suffix 규칙안: “매 단계 WORKLOG 필수 칸 갱신 → worker-ctx check → 지시대로 종료;
학습·비교는 실험별 EXPERIMENT 갱신; 긴 실행을 분리한 뒤 기다리지 말고 인계 후 종료.”
## 6. 긴 실행 대기와 실패

학습 등 장시간 job은 worker와 별도 프로세스 그룹으로 분리하고 `job.pid`와 프로세스 시작 시각,
완료 marker·exit code·산출물 경로를 기록한다. worker.pid와 동일한 PID는 긴 job으로 오인하지 않는다.
워커는 분리 실행을 확인한 뒤 문서를 갱신하고 즉시 parked로 끝낸다. sleep/폴링 대기를 하지 않는다.
`th-watch-lib.sh::inspect_task_event`는 job 생존 중 실제 도구 실행 이벤트의 sleep 또는
sleep+상태 조회 대기가 연속 2회이고 그 사이 작업 산출물/단계 변화가 없으면 경고한다.
프롬프트 에코·설명문·단순 무출력만으로 판정하지 않으며 이벤트 미지원은 감지 불가로 표시한다.
기존 감시 틱에서 요청 파일 `context_exit_requested.json`을 기록하고 기존 callback/digest로 알린다.
워커는 다음 경계에서 이를 읽어 문서를 쓰고 종료한다. stdin 지시나 새 감시 LLM은 만들지 않는다.
요청 후 60초 내 응답이 없으면 하네스가 최소 문서를 쓴 뒤 worker 그룹만 TERM,
10초 뒤 잔존 worker만 KILL한다. job 그룹 분리가 검증되지 않으면 강제 종료는 보류하고 경고한다.
job은 계속 감시하며 완료를 기존 th-watch 사건으로 코디네이터에 알린다. 결과 분석은 강제 fresh로
배정한다. 동일 키·short라도 이 사건에서는 기다리던 세션을 재개하지 않는다.
job의 완료와 worker의 인계 완료를 구분하며 인계만으로 최종 작업 DONE을 만들지 않는다.

문서 작성 실패 시 시스템은 마지막 정상 문서를 보존하고 별도 최소 WORKLOG 스냅샷을 만든다:
키·목표·현재 단계·브랜치/SHA 또는 산출물·실측 테스트 상태·오류·다음 검증·반복 금지·이유.
알 수 없는 항목은 unknown으로 쓴다. 이 경로를 등록부의 worklog_path로 삼고 검증 후 인계한다.
최소 문서도 쓸 수 없으면 blocked로 알리고 후임을 실행하지 않는다. 기존 파일과 세션은 유지한다.
쿼터 첫 오류는 해당 배치 전체의 추가 생성을 멈춘다. 자동 재시도로 남은 작업을 띄우지 않는다.
## 7. 세 경로의 연결과 호환성

- 셸: `scripts/th-launch.sh`의 프롬프트 조립/생성 전 dispatch, 종료 수거 시 park를 연결한다.
  현재 초기화의 job.pid/문서 삭제를 인계 시 금지한다. `th-watch.sh`/`th-watchd.sh`는
  공통 `th-watch-lib.sh::inspect_task_event`에서 판정 사건·긴 job 완료를 처리한다.
- run/repl: `tools/agent_tools.py::_resolve_resume_session_id`의 기존 검증과 생성 핸들러에
  `build_agent_tool_bindings::_spawn_agent`에 dispatch를 추가한다. `agents/spawner.py::spawn`은 결정된 fresh/resume만 실행한다.
  `AgentManager`의 wait/finalization과 `agents/session_capture.py::capture_session_id_from_path`로
  종료·세션 ID를 확정하고 `tracking/worker_sessions.py::write_worker_sessions_manifest`에 참조를 남긴다.
- protocol: `mode_a.py::run_mode_a`의 lane별 호출, `mode_b.py::run_mode_b`의 relay,
  `mode_c.py::_run_stage`와 `TeamHarnessAgentRunner.run_agent`에 같은 서비스와 경계 프롬프트를 연결한다.
  MODE C stage 간 fresh는 TH-D20대로 유지하고 stage 내부만 롤오버한다. 기존 stage 결과가
  완료되기 전의 롤오버는 stage 성공이 아니다. 완료한 작업부터 이어서 동일 stage를 마친다.
  `protocol/models.py::ProtocolState`의 별도 `worker_context` 참조와 `protocol/state.py`의
  `ProtocolStateManager.append_event`로 `protocol_events.jsonl`에 기록한다.
  `state.handoffs`는 stage 기록으로 보존한다. `protocol/usage.py::summarize_usage`에는
  stage 내부 모든 프로세스의 실제 사용량을 한 번만 합쳐 전달하며 현재 맥락을 비용 합계에 더하지 않는다.
- 요약: `worker_digest.py::digest`와 `scripts/th-status.sh::render`의 기존 출력에
  `키 | 단계 | 맥락 58k(measured) | active/parked | 다음 동작 | WORKLOG 경로` 한 줄을 붙인다.
  `.claude/workers.md`를 평소 읽고 이상 때만 전체 문서를 연다. 새 요약 장치를 만들지 않는다.
- 기존 manifest 소비자·TH-D3 반환 의미·stage 재개 계약은 보존한다. 활성화 시 새 실행 제한과
  설정 스키마가 추가되므로 구현 릴리스 CHANGELOG에 소비자 영향·비활성화 방법을 명시한다.
## 8. 테스트 및 구현 분할

검증 목록: 59999/60000/60001, 89999/90000/90001 경계; 같은/다른 키와 short/normal 조합;
unknown/오래된 측정·누적/현재 사용량 혼동·Claude 캐시·Codex 세션 오매칭·Agy 제외;
active 중 이중 실행 거부·재시작/PID 재사용·TTL 24h/최근 2개·동시 등록·후임 실패 보존;
문서 실패/최소 문서 실패·Git 불일치/base 무커밋·실험 인계·설정 우선순위/종류별 끄기;
세 경로의 동일 fixture 판정·MODE A 격리/B 릴레이/C stage 내 재개·비용 중복 없는 집계;
긴 job 생존/worker 종료/job 완료 fresh·프롬프트 sleep 오탐·강제 종료의 job 보존·쿼터 배치 정지.

**2단계 — 측정·판정·등록부·CLI만 구현, 자동 연결 전.** 변경 파일:
`src/team_harness/worker_context.py`(신설), `config.py`, `cli.py`,
`agents/session_capture.py`, `tracking/worker_sessions.py`, `tracking/models.py`,
`tests/test_worker_context.py` 및 CLI/설정/측정 fixture(신설), `CHANGELOG.md`.
긴 실행 요청/프로세스 식별·실험 경로 필드와 최소 문서 생성도 이 서비스에 넣는다.

**3단계 — 세 경로 연결·문서.** 변경 파일: `scripts/th-launch.sh`, `th-watch-lib.sh`,
`th-watch.sh`, `th-watchd.sh`, `th-status.sh`; `src/team_harness/tools/agent_tools.py`,
`agents/manager.py`, `agents/spawner.py`, `protocol/models.py`, `protocol/state.py`,
`protocol/mode_a.py`, `protocol/mode_b.py`, `protocol/mode_c.py`, `protocol/usage.py`,
`worker_digest.py`; `design/user_config/worker_suffix.md`, `CLAUDE.md`, `CHANGELOG.md`,
본 설계,경로별 통합 테스트 및 긴 job/실험 인계 fixture.
이 단계에서 긴 대기 감지·종료·완료 알림과 실험 문서 규칙을 실제 연결한다.
2단계만 끝난 상태에서 일부 경로만 자동 재실행하지 않는다. 세 경로 연결을 모두 검증한 뒤 활성화한다.
