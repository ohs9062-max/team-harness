# Harness Lab - Common Agent Rules

이 저장소는 여러 AI가 MODE A(독립 경쟁), B(인계), C(역할 분업)에 따라
작업을 수행하고 결과를 파일로 전달·비교하는 하네스 실험 환경이다.

모든 AI는 작업 전에 이 문서를 공통 규칙의 정본으로 확인한다.
MODE별 실행 계약은 `MODES.md`, 실행 흐름은 `WORKFLOW.md`, 코드 원칙은 `ENGINEERING_POLICY.md`를 따른다.

## 1. 기본 원칙

- 작업 전에 현재 목표와 관련 파일을 먼저 확인한다.
- 존재하지 않는 코드, 파일, 실행 결과를 추측하지 않는다.
- 확인되지 않은 내용은 사실처럼 단정하지 않는다.
- 기존 구조를 이해하기 전에 구조를 변경하지 않는다.
- 필요한 범위만 수정한다.
- 불필요한 파일이나 폴더를 생성하지 않는다.
- 기존 파일을 이유 없이 삭제하거나 대체하지 않는다.
- 작업 결과보다 작업 근거와 검증 가능성을 우선한다.

## 2. MODE 우선

사용자가 MODE를 지정하면 해당 실행 계약을 따른다.

- "A로 해" → `MODES.md` MODE A
- "B로 이어서 해" → `MODES.md` MODE B
- "C로 해" → `MODES.md` MODE C

어떤 AI에게 처음 명령하든 동일한 실행 계약이 적용된다.
AI가 MODE를 임의로 변경하거나 생략하지 않는다.

사용자가 MODE를 지정하지 않고 한 번의 목표로 자동 완성을 요청하면 `demo/orchestrator`는 MODE C를 기본값으로 사용한다. 이 기본값은 Runner에만 적용되며, MODE A의 사용자 선택 Gate나 MODE B 인계 계약을 우회하지 않는다.

### 자동 Runner 공통 규칙

- AI 역할은 read-only(분석/설계/검수), write(구현/FIX), system(CHECK/FINAL)으로 분리한다.
- 구현에 참여한 실제 Agent는 같은 실행의 REVIEW 후보가 될 수 없다.
- 결정론적 CHECK를 LLM REVIEW보다 먼저 수행한다.
- 명시적인 REVIEW 판정이 없거나 서로 모순되면 `BLOCKED`다.
- CLI 장애 fallback과 병렬 quorum은 계획에 명시된 범위에서만 허용한다.
- 런타임 정본은 `.harness/runs/<TASK-ID>/state.json`, 인계는 `handoff.json`, 감사 기록은 `events.jsonl`이다.
- Runner는 task worktree 안의 local checkpoint commit만 자동 생성할 수 있다.
- MODE A의 base 통합은 `WAITING_USER` 이후 사용자 선택을 받은 Codex만 수행하며 push는 하지 않는다.

## 3. 작업 시작 절차

작업을 시작하기 전에 다음 순서로 확인한다.

1. 루트 `AGENTS.md`
2. `MODES.md` — 사용자가 지정한 MODE
3. 코드 작업이면 `ENGINEERING_POLICY.md`
4. 현재 에이전트의 역할 문서 (`claude/`, `codex/`, `gemini/`)
5. `shared/TASK.md`의 현재 목표와 MODE
6. `shared/context.md`의 현재 상태
7. 작업 대상의 실제 파일
8. **Git Preflight** (`MODES.md` 공통 Gate)

이전 에이전트의 설명만 믿지 말고 실제 파일을 직접 확인한다.

## 4. 작업 범위

실제 코드 작성 및 수정 실험은 기본적으로 `demo/` 디렉토리에서 수행한다.

- `claude/`: Claude 역할 및 세션 설정
- `codex/`: Codex 역할 및 세션 설정
- `gemini/`: Gemini 역할 및 세션 설정
- `shared/`: 현재 TASK의 계약, 과정, 근거, 비교, 검수와 인계 상태
- `artifacts/`: 후속 TASK에서 재사용할 채택된 최종 결과물
- `demo/`: 실제 코드 작성·수정 영역

명시적인 작업 지시가 없다면 다른 에이전트의 설정 파일을 수정하지 않는다.

## 5. Git 안전 규칙

다음 작업을 임의로 수행하지 않는다.

- `git push`
- force push
- `reset --hard`
- 원격 브랜치 삭제
- 기존 브랜치 삭제
- 사용자 작업을 덮어쓰는 명령

5개 이상의 파일을 수정해야 하는 작업이라면 현재 Git 상태를 먼저 확인한다.

일반 작업에서는 checkpoint 생성을 제안하되 사용자의 지시 없이 임의로 commit하지 않는다.
단, MODE A/B/C Runner 실행 승인은 task worktree 내부 local checkpoint commit 권한을 포함한다.
이 예외는 base merge나 push 권한을 포함하지 않는다.

커밋 메시지 형식:

```
<유형>(<담당>): <변경 요약>
```

유형 예시: `feat`, `fix`, `docs`, `refactor`, `test`
담당 예시: `claude`, `codex`, `gemini`

### Git Preflight (필수)

모든 MODE에서 작업 시작 전 반드시 Git Preflight를 수행한다.
상세 절차는 `MODES.md` 공통 Gate를 따른다.

Git repository가 아닌 대상에 대해 사용자가 "직접 수정 허용"을 명시하지 않았다면
직접 수정하지 않고 `BLOCKED` 상태로 사용자 판단을 요청한다.

### base 보호 (MODE A/B/C)

MODE A Worker는 Codex/Gemini 독립 worktree, MODE B는 기존 task worktree,
MODE C는 pipeline worktree에서 작업한다. base/master는 Worker write 장소가 아니다.

MODE A 사용자 선택 전 통합 금지. 선택 후 `CODEX_MERGE`만 base에 쓸 수 있다.
push, force push, reset, branch 삭제는 계속 금지한다.

## 6. 구현 규칙

- 최소 변경을 우선한다.
- 기존 로직을 불필요하게 다시 작성하지 않는다.
- 관련 없는 리팩토링을 함께 수행하지 않는다.
- 단순히 더 좋아 보인다는 이유로 구조를 변경하지 않는다.
- 기존 명명 규칙과 코드 스타일을 우선한다.
- 새 의존성은 명시적인 필요가 있을 때만 제안한다.
- 실제 요구사항에 없는 기능을 임의로 추가하지 않는다.

코드 구현 원칙(기존 기능 보존, 하드코딩 최소화, 파일 분리 기준, 단순성 우선 등)은
`ENGINEERING_POLICY.md`를 따른다.

빌드 실패, 테스트 실패, 환경 문제 등 예상치 못한 오류가 발생하면:

- 해결 시도 과정과 결과를 기록한다.
- 스스로 해결할 수 없으면 `shared/context.md`에 상황을 기록하고 사용자 판단을 요청한다.
- 원인이 불확실한 오류를 추측으로 우회하지 않는다.

## 7. 검증 규칙

구현한 에이전트의 자체 검증은 작업 확인 과정일 뿐 최종 검증으로 간주하지 않는다.
구현에 참여한 AI는 같은 결과의 독립 REVIEW를 겸하지 않는다.

문제가 발견되면 파일, 위치, 원인을 근거로 제시한다.
REVIEW 판정은 `PASS`, `FIX_REQUIRED`, `BLOCKED`만 사용하며
모호한 표현으로 다음 Stage를 자동 진행하지 않는다.

## 8. 작업 권한

프로젝트 내부의 파일 조회, 코드 및 문서 생성·수정, 빌드, 테스트, 실행,
로그 및 상태 조회, 오류 조사는 기본적으로 허용한다.

다음은 사용자의 명시적 승인 없이 수행하지 않는다.

- `git push` / force push
- `reset --hard`
- 브랜치 삭제
- 프로젝트 외부 파일 삭제 또는 변경
- `sudo`를 이용한 시스템 변경
- 시스템 패키지 제거
- 인증정보 / 토큰 / 비밀번호 변경
- 운영 데이터 삭제 또는 변경
- 실제 장비에 위험할 수 있는 명령

### 보안 규칙

API Key, Token, Password, Secret, 인증서, 실제 `.env` 값 등
민감정보를 `shared/` 문서, 로그 요약, 인계 문서에 원문 그대로 기록하지 않는다.
로그를 문서에 기록할 때 민감값은 마스킹한다. (예: `abcd****`)

## 9. 사용자와의 관계

AI는 최종 의사결정자가 아니다.
최종 방향 결정은 사용자가 한다.
중요한 변경이나 되돌리기 어려운 작업은 사용자의 판단 없이 진행하지 않는다.

### 사용자 의사결정 Gate

일반적인 기술 판단은 AI가 요구사항과 실제 코드 기준으로 판단한다.

다음 경우에는 사용자 판단을 기다린다.

- `ENGINEERING_POLICY.md` 정책 간 충돌
- 기존 기능의 삭제 또는 동작 변경
- 외부 인터페이스 변경
- 파일/폴더 구조의 큰 변경
- 새 의존성 또는 프레임워크 도입
- 보안 정책 변화
- 요구사항 범위 확대
- AI 간 설계/구현/검수 의견 충돌
- 여러 대안이 있고 결과 동작 자체가 달라지는 경우

중요한 사용자 의사결정은 `shared/DECISIONS.md`에 기록한다.

## 10. 완료 기준

다음 조건을 만족해야 작업 완료 후보로 판단한다.

- 요구사항을 충족했다.
- 변경 범위를 설명할 수 있다.
- 실제 변경 파일을 확인했다.
- 가능한 테스트 또는 실행 확인을 했다.
- 실패한 검증을 숨기지 않았다.
- 남은 문제를 명확히 기록했다.

`코드를 작성했다`는 사실만으로 작업 완료로 판단하지 않는다.
AI가 자신의 결과를 스스로 최종 완료로 선언하지 않는다.

## 11. 문서 작성자 메타데이터

AI가 `shared/` 작업 문서를 새로 작성하거나 실질적으로 갱신할 때는
본문 상단에 다음 작성 정보를 기록한다.

```markdown
## 작성 정보

- 작성자: <실제 작성 AI>
- 모델: <현재 실제 사용 모델 또는 미확인>
- 역할: <실제 수행 역할>
- 작성일: YYYY-MM-DD
- TASK-ID: <현재 TASK-ID>
```

단순 열람이나 오탈자 수정 같은 경미한 변경에는 작성 정보를 바꾸지 않는다.

## 12. TASK-ID

작업을 식별하기 위해 각 작업에 TASK-ID를 부여한다.

형식: `TASK-YYYY-MM-DD-NNN` (예: `TASK-2026-08-07-001`)

작성되는 shared/ 문서에는 현재 TASK-ID를 기록한다.
