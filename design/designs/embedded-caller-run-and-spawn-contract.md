# 설계: 임베디드 호출자 실행 레코드 및 직접 생성 작업 할당 규약 (Embedded Caller Run Records and Direct-Spawn Assignments)

**상태:** 구현 완료  
**작성일:** 2026-07-15  
**결정:** `design/decisions.md` TH-D7  
**주요 소비자:** `loopy-loop` (더 큰 세션이나 워크플로 상태 머신을 소유하는 모든 임베딩 호출자를 위한 범용 API).

## 문제 정의 (The problem)

과거 SDK는 단일 실행의 산출물을 두 개의 비공개 위치로 나누어 저장했습니다. 전체 조율자 레코드는 `~/.team-harness/runs/<run-id>/run.json`에 저장된 반면, 작업자 로그와 `worker_sessions.json`은 `<output_dir>/<run-id>/`에 위치했습니다. 따라서 임베딩 호출자는 team-harness 내부 구조를 알지 못하면 독립적이고 정본인 단일 실행 레코드를 식별할 수 없었습니다. SDK 결과는 실행 ID만 노출했기 때문에, 성공 및 실패 경로 모두에서 호출자가 파일 경로를 직접 재구성해야 했습니다.

또한 하네스 조율자는 사용자의 작업을 수신했지만 외부 세션 트리로부터 타입화된 식별자를 전달받지 못했습니다. 조율자의 직접적인 `spawn_agent` 호출은 자유 형식의 프롬프트만 전달했습니다. 조율자가 작업자에게 외부 워크플로를 말로 설명할 수는 있었지만, 모든 작업자 생성이 부모 시도, 세션 깊이, 절대 작업 경로, 출력 위치, 감사 가능한 작업/역할 라벨을 전달받는다는 보장이 없었습니다.

호출자는 메모리 내 조율자 트랜스크립트가 사라진 후에도 생성된 조율자 입력, 각 직접 할당 작업, 작업자 stdout/stderr, 공급자 세션 식별자가 한곳에 모여 있고 탐색 가능하게 유지될 필요가 있었습니다.

## 공개 기능 협상 (Public capability negotiation)

`team_harness.caller_contract`는 `get_capabilities()`와 `TEAM_HARNESS_CAPABILITIES`를 export합니다. 호출자는 설치된 패키지 버전을 추측하거나 생성자 매개변수를 검사하는 대신, 명명된 시맨틱을 협상합니다. 호출자 계약 버전 1이 제공하는 기능들:

- `caller_run_record_v1` — 호출자가 절대 추적 루트를 제공할 수 있으며, 성공 및 구조화된 실패 모두에서 정본 `run.json` 경로를 반환받음.
- `coordinator_input_v1` — 생성된 시스템/사용자 입력이 클라이언트 생성이나 모델 검색 전에 원자적으로 영속화됨.
- `spawn_assignment_v1` — 모든 직접 생성 작업자가 자동 할당 엔벨로프와 유효 프롬프트 바닥글을 전달받음.
- `nested_caller_context_v1` — 내장 `type=harness` 자손이 검증된 외부 호출자 컨텍스트 엔벨로프와 부모 하네스 실행 계통을 전달받음.
- `capability_roster_context_v1` — 호출자가 동결된 기능 명부 경로, 다이제스트, 컴팩트 JSON 요약을 루트, 중첩 및 직접 에이전트 할당 컨텍스트에 첨부할 수 있음.

정수 버전이 아닌 **기능 명칭**이 호환성 게이트 역할을 합니다.

## 호출자 컨텍스트 및 정본 경로 (Caller context and canonical paths)

임베딩 호출자는 `TeamHarness(caller_context=CallerContext(...))` 인자를 전달합니다. `CallerContext`의 필수 항목:

- 호출자 소유의 절대 `trace_root`
- 절대 `parent_assignment_path`
- 부모 시도, 루트 세션 및 현재 세션 식별자
- 현재 세션 깊이 및 워크플로 역할
- 선택적인 절대 `relevant_state_paths`

호출자는 `capability_roster_path`, `capability_roster_sha256`, `capability_roster_summary`를 추가로 제공할 수 있습니다.

각 호출에 대해 `TeamHarness.run()`은 `<trace_root>/<run-id>/`를 생성합니다. 이 자식 디렉터리가 정본 실행 및 아티팩트 디렉터리가 됩니다. 여기에는 `run.json`, `coordinator_input.json`, `worker_sessions.json`, `workers/` 로그, `agents/` 할당 엔벨로프가 포함됩니다. 호출자가 논리적 시도를 재시도할 때 파괴적 충돌이 방지됩니다. `TeamHarnessResult`는 `run_json_path`, `session_output_dir`, `coordinator_input_path`를 반환하며, `TeamHarnessError.detail`도 동일한 필드를 반환합니다.

## 조율자 입력 및 식별자 (Coordinator input and identity)

`TeamHarness.run()`은 시스템 및 사용자 메시지를 생성하고, 자동 호출자 컨텍스트 시스템 바닥글을 적용한 뒤, 공급자에 연결하기 전에 `coordinator_input.json`을 원자적으로 기록합니다. 이 파일은 조율자 호출에 사용된 정확한 논리적 메시지를 담고 있습니다.

설정이나 프롬프트 생성이 실패하더라도, 컨텍스트 인식 실행은 정본 경로를 반환하고 `status: "incomplete"` 및 사전 점검 실패 사유가 기록된 `coordinator_input.json`을 남깁니다.

자동 시스템 바닥글이 조율자에게 알려주는 내용:
- 자신이 소유한 루트/세션/깊이/워크플로 할당 정보
- 절대 부모 할당 및 관련 상태 경로의 위치
- 명시적 하네스 실행 ID 및 기록 위치
- 생성된 에이전트들은 위임자일 뿐이며, 조율자 본인이 통합 및 루프 수준 결정에 대한 책임을 진다는 점

## 동적 직접 생성 작업 할당 (Dynamic direct-spawn assignment)

`spawn_agent`는 네 가지 선택적 메타데이터 필드를 받습니다:
- `delegated_role`
- `delegated_task_id`
- `expected_outputs`
- `state_responsibility`

조율자가 이 값들을 동적으로 지정합니다. 하네스는 이를 기록하지만 생성을 승인하거나 모델을 선택하는 등의 용도로는 사용하지 않습니다.

서브프로세스를 실행하기 전에 `tools/agent_tools.py`는 `agents/<agent-id>/agent_assignment.json`을 작성합니다. 여기에는 부모 하네스 실행, 외부 시도/세션 정보, 절대 할당 경로, 에이전트 출력 디렉터리, 메타데이터 필드 및 두 가지 프롬프트 형태가 포함됩니다:
1. `authored_prompt` — 조율자가 직접 작성하여 위임한 원본 프롬프트
2. `effective_prompt` — 작성된 프롬프트에 설정된 접미사와 자동 바닥글이 결합된 최종 프롬프트

## 프롬프트, 출력 및 세션 캡처 (Prompt, output, and session capture)

`tracking/persistence.py`는 크래시가 발생해도 파일이 잘리지 않도록 구조화된 JSON을 원자적으로 기록합니다. `agents/spawner.py`는 작업자의 stdout과 stderr를 호출자 소유의 정본 로그 경로에 직접 기록합니다.

### 공급자 세션 ID 마감 처리
세션 ID가 공급자의 최종 이벤트에서만 방출되는 경우가 있습니다. 따라서 생성된 작업자 감시자(watcher)와 세션 캡처 코루틴은 즉시 버려지는 태스크가 아니라 실행별 `AgentManager`에 보존됩니다. `harness._finalize_run()`은 먼저 모든 작업자를 종료 상태로 만든 뒤 해당 태스크들을 대기합니다. 감시자 중지 이벤트를 확인하고 최종 스캔을 마친 후에만 `run.json`을 마감하고 `worker_sessions.json`을 작성합니다.

### 실행 중 세션 재개 (Resume)
같은 실행 내에서 이전 세션을 이어갈 때, 조율자는 이미 알고 있는 안정적인 하네스 에이전트 ID를 전달합니다:
```text
spawn_agent(
    type="codex",
    prompt="리뷰 수정 사항 3개를 적용하고 유효성 검사를 다시 실행하십시오.",
    cwd="/absolute/repository/path",
    mode="resume",
    resume_from_agent_id="agent_aed3b8a457d8",
)
```
`tools/agent_tools.py`는 현재 `AgentManager`를 통해 해당 소스를 리졸브한 뒤 캡처된 벤더 세션 ID만 `agents/spawner.py`에 전달합니다. 소스 프로세스가 종료되었고 작업자 타입이 일치할 때만 리졸브가 성공합니다.

## 중첩 하네스 컨텍스트 전파 (Nested harness context propagation)

호출자 컨텍스트를 가진 조율자가 `spawn_agent(type="harness", ...)`를 선택하면, `tools/agent_tools.py`는 새로운 `CallerContext`를 도출하여 자식 환경에 `TEAM_HARNESS_CALLER_CONTEXT`로 주입합니다. 자식 `TeamHarness` 생성자는 명시적 SDK 컨텍스트가 없을 때 이를 로드하고 검증합니다.

중첩 컨텍스트는 부모 시도, 루트/현재 세션, 세션 깊이, 워크플로 역할 등을 유지합니다. 부모 할당을 직접 에이전트 할당으로 변경하고, 중첩 실행 아티팩트를 `<agent-output>/harness_runs/<nested-run-id>/` 아래에 배치하며, 현재 실행을 `parent_harness_run_id`로 기록합니다. 하네스 조율자를 중첩 추가하는 것은 하나의 루프 할당 내부에서의 동적 위임이지, 새로운 loopy-loop 계층을 생성하는 것이 아닙니다.

## 코드 맵 및 검증

- `caller_contract.py`: 공개 컨텍스트, 기능(capabilities) 및 조율자 바닥글.
- `harness.py`: 호출자 소유 경로 선택, 공급자 호출 전 입력 영속화, 구조화된 결과/에러 경로, 컨텍스트 전파.
- `tools/agent_tools.py`: 동적 스키마 필드, 할당 엔벨로프, 직접 에이전트 바닥글.
- `agents/spawner.py`: 작업자 프로세스 그룹 식별자 및 호출자 소유 실행 디렉터리 아래 stdout/stderr 직접 캡처.
- `tracking/run_log.py` 및 `tracking/persistence.py`: 원자적 구조화 영속성.
- `tracking/worker_sessions.py`: 영속화된 요약, 세션 메타데이터 및 호출 아티팩트.
- `tests/test_caller_contract.py` 및 `tests/test_process_lifecycle.py`: 기능, 경로, 순서, 실패, 할당, 프롬프트, 출력, 세션 캡처 및 프로세스 그룹 테스트.
