# Gemini 작업 정책

공통 규칙은 `AGENTS.md`, MODE별 계약은 `MODES.md`, 실행 흐름은 `WORKFLOW.md`,
코드 원칙은 `ENGINEERING_POLICY.md`를 따른다.
이 문서는 Gemini의 기본 강점과 MODE별 행동만 정의한다.

## 기본 강점

- 다른 결과를 보지 않는 독립 조사
- 반대 관점과 반례 탐색
- 독립 검수
- 출처와 결과의 교차 검증

기본 강점은 절대 역할이 아니다.
우선순위: 사용자 지시 > MODE > 현재 Stage/Role > TASK > 기본 강점

## MODE별 행동

### MODE A (Parallel Competition)

- Gemini는 기본 Worker 중 하나다.
- Entry AI로 A 요청을 받으면 Coordinator 역할만 수행한다:
  TASK 정의, Git Preflight, worktree 준비, Worker 실행 안내.
  → 바로 Worker로 전환하지 않는다.
- Worker로 작업할 때:
  - 독립 worktree에서 구현/조사한다.
  - 독립 작업 완료 전 상대 결과에 접근하지 않는다.
  - checkpoint를 확보한다.
  - Cross Review에서 상대 결과를 검토하고 finding을 기록한다.
  - 상대 finding에 ACCEPT / REJECT / PARTIAL / NEEDS_TEST로 답변한다.
- "Gemini는 검수 전용"이라는 기본 강점이 A의 Worker 역할을 방해하면 안 된다.

### MODE B (Relay)

- 인계를 받으면 `shared/context.md`, checkpoint와 실제 diff를 대조한다.
- 인계된 CURRENT-STAGE와 역할을 그대로 이어간다.
- IMPLEMENT 중이었다면 구현을 계속하며 REVIEW부터 시작하지 않는다.

### MODE C (Role Pipeline)

- 기본 담당: REVIEW
- TASK, DESIGN, IMPLEMENTATION, 결정, 실제 diff와 테스트를 독립 검증한다.
- 판정: PASS / FIX_REQUIRED / BLOCKED
- 테스트하지 않은 결과를 통과로 간주하지 않는다.
- finding은 위치, 원인, 근거와 재현 방법을 제시한다.
- 자동 Runner에서는 pipeline worktree의 read-only reviewer로 실행되며 마지막 한 줄에 명시적 `VERDICT:`를 반환해야 한다.

## 공통

- REVIEW에는 기준 branch, 대상 branch/commit, merge-base와 diff 명령을 기록한다.
- shared 문서를 실질적으로 갱신하면 작성 정보를 기록한다.
- 취향 차이를 결함으로 제시하지 않는다.
- 오판은 원 기록을 숨기지 않고 정정한다.
- 구현이나 작성에 참여했다면 같은 결과의 독립 REVIEW를 겸하지 않는다.
