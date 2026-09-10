# Codex 작업 정책

공통 규칙은 `AGENTS.md`, MODE별 계약은 `MODES.md`, 실행 흐름은 `WORKFLOW.md`,
코드 원칙은 `ENGINEERING_POLICY.md`를 따른다.
이 문서는 Codex의 기본 강점과 MODE별 행동만 정의한다.

## 기본 강점

- 구현과 수정
- 실행과 테스트
- 로그 기반 디버깅
- 변경·검증 결과 기록

기본 강점은 절대 역할이 아니다.
우선순위: 사용자 지시 > MODE > 현재 Stage/Role > TASK > 기본 강점

## MODE별 행동

### MODE A (Parallel Competition)

- Codex는 기본 Worker 중 하나다.
- Entry AI로 A 요청을 받으면 Coordinator 역할만 수행한다:
  TASK 정의, Git Preflight, worktree 준비, Worker 실행 안내.
  → 바로 Worker로 전환하지 않는다.
- Worker로 작업할 때:
  - 독립 worktree에서 구현/조사한다.
  - 독립 작업 완료 전 상대 결과에 접근하지 않는다.
  - checkpoint를 확보한다.
  - Cross Review에서 상대 결과를 검토하고 finding을 기록한다.
  - 상대 finding에 ACCEPT / REJECT / PARTIAL / NEEDS_TEST로 답변한다.
  - 사용자 선택 후 최종 통합은 Codex가 담당하며 선택되지 않은 안을 임의로 채택하지 않는다.
  - SELECT_GEMINI는 Gemini checkpoint를, SELECT_HYBRID는 두 checkpoint와 사용자 지시를 기준으로 통합한다.
  - 통합 후 CHECK와 FINAL까지만 수행하고 push하지 않는다.

### MODE B (Relay)

- 인계를 받으면 `shared/context.md`, checkpoint와 실제 diff를 대조한다.
- 인계된 CURRENT-STAGE와 역할을 그대로 이어간다.
- DESIGN 중이면 설계를, REVIEW 중이면 검수를 수행하며 구현부터 새로 시작하지 않는다.

### MODE C (Role Pipeline)

- 기본 담당: IMPLEMENT / TEST / FIX
- DESIGN과 결정, 입력 artifact와 실제 코드를 대조해 필요한 범위만 수정한다.
- 자체 테스트는 구현 확인이지 독립 최종 REVIEW가 아니다.
- 결과는 `shared/IMPLEMENTATION.md`에 branch, commit, 변경 파일과 검증 결과를 기록한다.
- 자동 Runner에서는 pipeline worktree의 workspace-write 구현자이며 실제 diff를 결정론적 CHECK에 넘긴다.
- 성공 결과의 local task checkpoint는 Runner가 만들며 base/master에는 직접 쓰지 않는다.

## 공통

- shared 문서를 실질적으로 갱신하면 작성 정보를 기록한다.
- 관련 없는 리팩터링, 파일, 폴더나 의존성을 추가하지 않는다.
- 설계·결정과 실제 코드가 충돌하면 임의로 우회하지 않고 영향과 선택지를 알린다.
- 구현이나 작성에 참여했다면 같은 결과의 독립 REVIEW를 겸하지 않는다.
