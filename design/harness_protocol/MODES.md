# 실행 모드 계약

이 문서는 MODE A / B / C의 정의, 상태 머신, 필수 Gate를 정의하는 정본이다.
모든 AI는 사용자가 MODE를 지정하면 이 문서의 해당 계약을 따른다.

공통 행동 규칙은 `AGENTS.md`, 실행 흐름은 `WORKFLOW.md`, 코드 원칙은 `ENGINEERING_POLICY.md`를
따른다.

---

## MODE 정의

| MODE | 이름 | 한 줄 설명 |
|------|------|-----------|
| A | PARALLEL COMPETITION | 같은 작업을 Codex/Gemini가 독립 수행 → Cross Review → Compare → 사용자 선택 |
| B | RELAY | 같은 작업을 다른 AI가 이어받음 |
| C | ROLE PIPELINE | 역할별 전문 분업 (Claude 설계 → Codex 구현 → Gemini 검수) |

사용자는 "A로 해", "B로 이어서 해", "C로 해"로 지시한다.
어떤 AI에게 처음 명령하든 동일한 실행 계약이 적용된다.

---

## 공통 Gate

### Git Preflight (필수)

모든 MODE에서 작업 시작 전 반드시 수행한다.

```
1. 작업 대상이 Git repository인지 확인
2. Git이면:
   - repository root
   - current branch
   - base branch / base commit
   - dirty state (uncommitted changes)
   - existing worktrees
3. Git이 아니면:
   - STATUS: BLOCKED
   - 사용자에게 선택 요청:
     a) Git 초기화
     b) 올바른 Git repository 경로 지정
     c) Git 없이 직접 수정 허용
   - 사용자가 c를 명시하지 않았다면 직접 수정 금지
```

### Merge Gate (필수)

사용자 승인 없이 다음을 수행하지 않는다.

- merge
- cherry-pick
- 수동 파일 복사 통합
- push

### Entry AI와 Worker AI

- Entry AI: 최초 사용자 요청을 받은 AI. Coordinator 역할만 수행한다.
- Worker AI: 실제 작업을 수행하는 AI.
- Entry AI가 자동으로 Worker를 겸하지 않는다.
- 대화형 MODE A에서 Entry AI는 TASK 정의, Git Preflight, worktree 준비 후 사용자 선택 Gate를 관리한다.
- 자동 Runner의 기본 경로는 MODE C다. Runner가 Coordinator와 Worker CLI를 직접 호출하며 Entry/Worker 실행 세션을 분리한다.
- Runner의 병렬 Stage는 결과 충돌을 막기 위해 READ_ONLY만 허용한다. 병렬 구현 경쟁인 MODE A는 독립 worktree와 사용자 선택 Gate를 유지한다.
- CLI 인증·quota·timeout 실패는 숨기지 않고 기록하며, 계획에 정의된 fallback 또는 quorum으로만 계속한다.

---

## MODE A — PARALLEL COMPETITION

### 기본 Worker

- 기본: Codex + Gemini (2인)
- 한쪽 Worker 결과만으로는 A가 완료되지 않는다

### 상태 머신

```
DEFINE
  Entry AI가 TASK 정의, MODE A 기록
  ↓
GIT_PREFLIGHT
  대상 repository, base branch, base commit 확인
  ↓
WORKTREE_SETUP
  base commit freeze
  독립 worktree 생성:
    task/<TASK-ID>/codex
    task/<TASK-ID>/gemini
  ↓
INDEPENDENT_WORK
  각 Worker가 독립 수행 (구현/조사)
  독립 작업 완료 전 상대 결과에 접근 금지
  각자 checkpoint 확보
  ↓
WORKER_GATE
  두 Worker 모두 성공해야 다음 진행
  한쪽이라도 실패하면:
    STATUS: BLOCKED
    사용자에게 상태 보고
    사용자 판단 대기
  ↓
CROSS_REVIEW
  Gemini → Codex 결과 검토
  Codex → Gemini 결과 검토
  각 Worker가 finding 기록
  ↓
RESPONSE (1회)
  각 Worker가 상대 finding에 답변:
    ACCEPT / REJECT / PARTIAL / NEEDS_TEST
  무한 토론 금지
  추가 Round는 사용자 승인 + 이유 기록
  ↓
COMPARE
  비교 보고서 작성 (아래 항목 포함)
  ↓
WAITING_USER
  사용자 선택 대기
  AI가 자동으로 하나를 선택하거나 merge하면 안 됨
  ↓
USER_SELECT
  사용자가 다음 중 선택:
    SELECT_CODEX
    SELECT_GEMINI
    SELECT_HYBRID
    REWORK
    CANCEL
  ↓
CODEX_MERGE (사용자 선택에 따라)
  Codex가 선택된 결과만 base working tree에 통합
  CHECK 실행, push/commit 없음
  ↓
FINAL
```

### 비교 보고서 필수 항목

Development A:

- Codex 구현 요약
- Gemini 구현 요약
- 변경 파일 차이
- 설계 차이
- 테스트 결과
- 각 구현의 장점 / 단점
- 각 AI가 상대 구현에서 발견한 문제
- 상대 지적에 대한 답변 (ACCEPT / REJECT / PARTIAL)
- 동의한 부분 / 끝까지 의견이 다른 부분
- 추천안
- 각 안 선택 시 영향
- Hybrid가 가능하면 그 방법

Research A:

- 각 AI의 조사 요약
- Claim/Source ID (각 AI namespace)
- Cross Verification 결과
- Normalized Claim과 Verified Set
- 동의/불일치 항목
- 추천 종합안

### Cross Review vs REVIEW

| 구분 | Cross Review (A) | REVIEW (C) |
|------|-----------------|------------|
| 목적 | 서로 다른 두 독립 구현을 각 Worker가 상대 관점에서 검토 | 구현 결과가 요구사항에 맞는지 독립 검증 |
| 수행자 | 각 Worker (Codex ↔ Gemini) | 구현에 참여하지 않은 AI (기본: Gemini) |
| 결과 | finding + response | PASS / FIX_REQUIRED / BLOCKED |
| 관계 | A 전용 | C 전용 |

### 원본 보호

- base/master working tree는 AI 작업 장소가 아니다.
- Worker는 반드시 독립 worktree에서 작업한다.
- 사용자 merge 승인 전: merge, cherry-pick, 수동 파일 복사 통합, push 금지
- local worker checkpoint는 MODE A 실행 승인 범위이며 사용자 선택 승인이 아니다.
- 사용자 선택 후 최종 통합 담당은 항상 Codex다.

---

## MODE B — RELAY

### 발생 조건

- 토큰 한도 / 세션 종료 / 장애 / AI 일시 사용 불가
- 사용자가 역할 교체를 지시

### 상태 머신

```
(기존 작업 중)
  ↓
RELAY_TRIGGER
  현재 AI가 진행 불가
  ↓
CONTEXT_SAVE
  넘겨주는 AI가 shared/context.md에 기록:
    - TASK-ID, MODE
    - CURRENT-STAGE, STATUS
    - 이전/현재 작업자, 모델, 역할
    - 완료한 작업 / 진행 중 작업 / 남은 작업
    - 중요한 판단, 주의사항, 제약
    - 작업 branch, 기준 branch, checkpoint commit
    - 변경 파일, 테스트 상태, 산출물
    - 다음 Stage, 다음 담당, 이어받을 역할
    - RELAY 사유
  checkpoint가 없으면 "없음"과 미커밋 상태 명시
  ↓
NEXT_AI_START
  이어받는 AI가 확인:
    1. AGENTS.md, MODES.md, 자신의 역할 문서
    2. shared/TASK.md, shared/context.md
    3. 관련 작업 문서
    4. Git 상태 (branch, diff, checkpoint)
  ↓
GIT_VERIFY
  checkpoint와 실제 diff 대조
  이전 AI의 설명과 실제 상태가 다르면:
    실제 Git 상태를 정본으로 사용
    차이를 기록
  ↓
CONTINUE
  인계된 CURRENT-STAGE와 역할을 유지
  미완료 지점부터 같은 목표를 계속
  완료된 범위를 불필요하게 다시 작성하지 않음
```

### 핵심 규칙

- B는 새 worktree를 만들지 않는다 (기존 작업선에서 계속).
- 이어받는 AI는 자기 기본 강점을 이유로 다른 Stage에서 시작하지 않는다.
- 완료됐다고 기록된 작업을 처음부터 다시 하지 않는다.
- 자신이 구현에 참여했다면 같은 결과의 독립 검수자로 기록하지 않는다.
- Runner는 `--mode B --resume <TASK-ID> --relay-agent <AI> --execute`로 state를 복구한다.
- 기록된 branch/worktree/checkpoint와 실제 `git status`, `git log`, `git diff`가 다르면 Git을 정본으로 사용하고 차이를 runtime event에 남긴다.

---

## MODE C — ROLE PIPELINE

MODE C Runner는 `task/<TASK-ID>/pipeline` branch와 하나의 pipeline worktree를 만든다.
Claude, Codex, Gemini는 그 worktree를 순서대로 사용하며 base/master에 Worker write를 하지 않는다.

### 기본 역할 배치

| Stage | 기본 담당 | 설명 |
|-------|----------|------|
| ANALYZE / DESIGN | Claude | 요구사항 분석, 구조 설계 |
| IMPLEMENT / TEST | Codex | 구현, 수정, 실행, 테스트 |
| REVIEW | Gemini | 독립 검수, 반례 탐색, 요구 누락 확인 |

역할은 기본값이다. 사용자가 특정 AI 교체를 지시하면 변경한다.

### 상태 머신

```
DEFINE
  Entry AI가 TASK 정의, MODE C 기록
  ↓
GIT_PREFLIGHT
  ↓
DESIGN (Claude)
  요구사항 분석, 설계, 제약, 검증 기준
  shared/DESIGN.md에 기록
  ↓
IMPLEMENT (Codex)
  설계 기반 구현
  shared/IMPLEMENTATION.md에 기록
  ↓
TEST (Codex)
  실행, 테스트
  결과를 IMPLEMENTATION.md에 추가
  ↓
CHECK (System)
  발견/지정된 결정론적 검사를 실행
  ↓
REVIEW (Gemini)
  독립 검수
  shared/REVIEW.md에 판정:
    ├─ PASS → FINAL
    ├─ FIX_REQUIRED → FIX (Codex) → TEST → CHECK → REVIEW
    └─ BLOCKED → 사용자 판단 요청
  ↓
FINAL
```

### FIX Loop

- REVIEW에서 FIX_REQUIRED 판정 시:
  - Codex가 FIX → TEST → CHECK
  - Gemini가 다시 REVIEW
  - 설정된 `max_review_cycles` 안에서 반복하고 초과하면 BLOCKED
- 구현에 참여한 AI는 같은 결과의 독립 REVIEW를 겸하지 않는다
- FIX 기본 담당은 Codex다. IMPLEMENT가 fallback Agent로 실제 완료된 경우에는 그 실제 writer가 FIX를 이어받고, reviewer는 계속 분리한다.
- 성공한 pipeline 결과는 task branch에 local checkpoint commit으로 보존한다. base 통합과 push는 별도다.

---

## TASK.md 포맷

MODE에 따라 TASK.md에 기록하는 필드:

### 공통

```
- TASK-ID: TASK-YYYY-MM-DD-NNN
- USER-REQUEST: (원문 보존)
- MODE: A / B / C
- TARGET-REPOSITORY: (경로)
- BASE-BRANCH: (branch 이름)
- BASE-COMMIT: (commit hash)
- STATUS: (DEFINE / IN_PROGRESS / WAITING_USER / DONE / BLOCKED)
```

### MODE A 추가

```
## Worker 상태

### codex
- branch: task/<TASK-ID>/codex
- worktree: (경로)
- status: PENDING / IN_PROGRESS / DONE / FAILED
- checkpoint: (commit hash)
- test-status: PASS / FAIL / PENDING

### gemini
- branch: task/<TASK-ID>/gemini
- worktree: (경로)
- status: PENDING / IN_PROGRESS / DONE / FAILED
- checkpoint: (commit hash)
- test-status: PASS / FAIL / PENDING

## Cross Review
- codex-reviews-gemini: PENDING / DONE
- gemini-reviews-codex: PENDING / DONE
- response-round: 0 / 1

## Compare
- status: PENDING / DONE
- user-selection: WAITING_USER / SELECT_CODEX / SELECT_GEMINI / SELECT_HYBRID / REWORK / CANCEL
- merge-status: PENDING / DONE
```

### MODE B 추가

```
## RELAY
- previous-agent: (AI 이름)
- next-agent: (AI 이름)
- relay-reason: (사유)
- branch: (작업 branch)
- checkpoint: (commit hash)
- remaining-work: (요약)
```

### MODE C 추가

```
## Stage Plan

### DESIGN
- agent: Claude
- status: PENDING / IN_PROGRESS / DONE

### IMPLEMENT
- agent: Codex
- status: PENDING / IN_PROGRESS / DONE

### TEST
- agent: Codex
- status: PENDING / IN_PROGRESS / DONE

### REVIEW
- agent: Gemini
- status: PENDING / IN_PROGRESS / DONE
- verdict: PENDING / PASS / FIX_REQUIRED / BLOCKED
```

---

## Research에서의 MODE A

Research A도 동일한 독립 → Cross Verification → Compare 흐름을 따른다.

- 각 Worker는 자기 AI namespace로 원본 Claim/Source ID를 생성한다.
  예: `CODEX-C001`, `GEMINI-C001`
- 독립 작업 완료 전 상대 결과를 읽지 않는다.
- Cross Verification 후 Compare에서 Normalized Claim으로 묶는다.
- 의미가 다른 유사 주장을 억지로 합치지 않는다.

### 최신 정보 조사

사용자가 "현재", "최신", "최근", "2026", "현재 알고리즘" 등을 요청하면,
출처에 다음을 기록한다.

```
- SOURCE-DATE: (출처 발행일)
- CURRENT-APPLICABILITY: CURRENT / PARTIALLY_CURRENT / HISTORICAL / UNKNOWN
- CURRENTNESS-EVIDENCE: (현재 유효성 판단 근거)
```

---

## Protocol과 Runner의 경계

이 문서(MODES.md)와 관련 문서(AGENTS.md, WORKFLOW.md)는 **Protocol**이다.
어떤 작업을 어떻게 해야 하는지 정의한다.

**Runner**는 실제 Claude/Codex/Gemini CLI를 호출하여 Protocol을 실행하는 프로그램이다.

현재 상태:
- Protocol: 이 문서와 `HARNESS_AGENTS.md`, `WORKFLOW.md`가 정본이다.
- Runner: `src/team_harness/protocol/`이 Team Harness의 기존 Agent 실행 엔진 위에서 MODE A/B/C를 실행한다.
- MODE A: 두 독립 worktree, required Worker Gate, checkpoint, Cross Review, Response, Compare, WAITING_USER와 Codex 통합을 지원한다.
- MODE B: 기존 TASK-ID state와 worktree를 로드해 실제 Git을 대조하고 남은 Stage부터 relay한다.
- MODE C: 하나의 pipeline worktree에서 DESIGN→IMPLEMENT/CHECK→REVIEW/FIX→FINAL과 checkpoint를 실행한다.

Runner의 런타임 기록은 Team Harness run_dir 아래 `protocol_state.json`에 저장한다. 문서 상태와 실제 `protocol_state.json`, Git diff가 다르면 실제 상태를 정본으로 삼는다.

Agent 매핑:
- 논리 역할 `gemini` → Team Harness agent type `antigravity` → CLI `agy`
- 구 `gemini` CLI는 Harness Protocol의 Gemini 역할에 사용하지 않는다.
