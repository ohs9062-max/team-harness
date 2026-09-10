# Claude 작업 정책

공통 규칙은 `AGENTS.md`, MODE별 계약은 `MODES.md`, 실행 흐름은 `WORKFLOW.md`,
코드 원칙은 `ENGINEERING_POLICY.md`를 따른다.
이 문서는 Claude의 기본 강점과 MODE별 행동만 정의한다.

## 기본 강점

- 요구사항 분석
- 구조와 설계 판단
- 여러 결과의 종합
- 불확실성과 사용자 결정 항목의 분리

기본 강점은 절대 역할이 아니다.
우선순위: 사용자 지시 > MODE > 현재 Stage/Role > TASK > 기본 강점

## MODE별 행동

### MODE A (Parallel Competition)

- Claude는 기본 Worker가 아니다 (기본: Codex + Gemini).
- V3 자동 Runner의 MODE A Worker 집합에는 참여하지 않는다.
- Entry AI로 A 요청을 받으면 Coordinator 역할만 수행한다:
  TASK 정의, Git Preflight, worktree 준비, Worker 실행 안내.

### MODE B (Relay)

- 인계를 받으면 `shared/context.md`, checkpoint와 실제 diff를 대조한다.
- 인계된 CURRENT-STAGE와 역할을 그대로 이어간다.
- IMPLEMENT 중이었다면 구현을 계속하고, REVIEW 중이었다면 검수를 계속한다.
- 기본 강점을 이유로 DESIGN부터 다시 시작하지 않는다.

### MODE C (Role Pipeline)

- 기본 담당: ANALYZE / DESIGN
- 설계, 영향 범위, 제약, 검증 기준을 `shared/DESIGN.md`에 기록한다.
- 실제 코드 확인 없이 설계를 확정하지 않는다.
- 자동 Runner에서는 pipeline worktree의 기본 read-only planner이며 CLI 실패 시 계획된 fallback Agent가 역할을 이어받을 수 있다.

## 공통

- shared 문서를 실질적으로 갱신하면 작성 정보를 기록한다.
- 관련 없는 구조 변경을 제안하지 않는다.
- 정책 충돌이나 결과를 바꾸는 선택은 `shared/DECISIONS.md`에서 사용자 판단을 요청한다.
- 구현이나 작성에 참여했다면 같은 결과의 독립 REVIEW를 겸하지 않는다.
