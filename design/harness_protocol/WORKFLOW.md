# Harness Lab 운영 가이드

이 문서는 TASK 시작부터 완료까지의 실제 운영 흐름을 설명한다.
MODE별 실행 계약은 `MODES.md`, 공통 규칙은 `AGENTS.md`, 코드 원칙은 `ENGINEERING_POLICY.md`를 따른다.

---

## 1. 공통: TASK 시작

### DEFINE — 사용자 요청을 TASK로 변환

최초 요청을 받은 AI가 Entry AI / Coordinator를 맡는다.

```
사용자 요청 원문 보존
→ MODE 확인 (명시가 없으면 자동 Runner는 C)
→ TASK-ID 생성
→ Git Preflight (MODES.md 공통 Gate)
→ shared/TASK.md에 계약 기록
→ 자동 Runner가 선택된 MODE A/B/C 계약 실행
```

Entry AI는 Worker를 자동으로 겸하지 않는다.
사용자가 MODE를 지정하지 않고 자동 완성을 요청하면 MODE C를 기본값으로 사용한다. 결과 동작이 달라지는 MODE A/B 선택이 꼭 필요한 경우에만 사용자에게 확인한다.

### 새 TASK 점검표

- [ ] TASK-ID와 사용자 원문 요청이 있다.
- [ ] MODE가 정해졌다. (A / B / C)
- [ ] Git Preflight를 통과했다.
- [ ] TARGET-REPOSITORY, BASE-BRANCH, BASE-COMMIT이 있다.
- [ ] MODE에 맞는 필드가 TASK.md에 있다. (Worker 상태 / RELAY / Stage Plan)
- [ ] shared 과정 문서와 artifacts 최종 결과를 혼동하지 않았다.

### 작업 주기

1. 이전 TASK가 종료되고 Git에 보존됐는지 확인한다.
2. 일반 수동 작업은 checkpoint를 제안하고, Runner task worktree는 실행 승인 범위에서 local checkpoint를 만든다.
3. 새 TASK-ID로 필요한 shared 문서를 초기화한다.
4. TASK에 MODE, TASK-ID, 입출력과 완료 조건을 기록한다.
5. RELAY 때 context를 최신 상태로 갱신한다.
6. 독립 검증과 사용자 판단 후 artifact와 RESULT를 확정한다.

---

## 2. shared 문서 책임

| 문서 | 책임 |
|------|------|
| TASK.md | 사용자 원문, MODE, Git 정보, Worker 상태 / Stage Plan을 담은 작업 계약 |
| context.md | 현재 상태, Stage 요약과 RELAY 인계 스냅샷 |
| DECISIONS.md | 사용자가 확정해야 할 중요한 방향 |
| RESEARCH.md | 독립 조사 근거 (Claim/Source ID) |
| COMPARE.md | 비교 기록 (A의 비교 보고 또는 Research의 Normalized Claim) |
| DESIGN.md | C의 설계 결과 |
| IMPLEMENTATION.md | 구현·테스트 결과 |
| REVIEW.md | C의 독립 검수 기록 |
| RESULT.md | 사용자 판단과 TASK 종료 상태 |

모든 파일을 항상 만들 필요는 없다. 현재 MODE와 작업에 필요한 문서만 사용한다.

### COMPARE vs REVIEW 구분

- COMPARE: 여러 후보 결과를 비교·선별하는 과정 (A의 비교 보고, Research의 Claim 비교)
- REVIEW: 선택·종합된 결과의 독립 최종 검증 (C의 검수)
- 두 책임을 한 문서에 섞지 않는다.

### Cross Review vs REVIEW 구분

- Cross Review: A에서 각 Worker가 상대 구현을 검토 → COMPARE.md 또는 별도 기록
- REVIEW: C에서 구현에 참여하지 않은 AI의 독립 검수 → REVIEW.md
- 서로 다른 책임이다.

---

## 3. MODE A 운영 흐름

### Development A

```
DEFINE (Entry AI)
→ Git Preflight
→ Worktree Setup (task/<TASK-ID>/codex, task/<TASK-ID>/gemini)
→ Codex 독립 구현 + Gemini 독립 구현
→ Worker Gate (둘 다 성공 필수)
→ Cross Review (1회)
→ Response (1회: ACCEPT / REJECT / PARTIAL / NEEDS_TEST)
→ Compare 보고서 작성
→ WAITING_USER
→ 사용자 선택 (SELECT_CODEX / SELECT_GEMINI / SELECT_HYBRID / REWORK / CANCEL)
→ Codex Merge (선택된 결과만 local base에 통합)
→ CHECK
→ FINAL
```

Worker 둘 다 작업을 완료하기 전까지 상대 결과를 읽거나 복사하지 않는다.
한쪽이 실패하면 BLOCKED → 사용자에게 상태 보고.
COMPARE가 끝나도 AI가 자동 merge하지 않는다 → WAITING_USER.

### Research A

```
DEFINE (Entry AI)
→ Git Preflight
→ Worktree Setup
→ Codex 독립 조사 + Gemini 독립 조사
→ Worker Gate
→ Cross Verification
→ Normalized Claim / Verified Set
→ Compare 보고서 작성
→ WAITING_USER
→ 사용자 선택
→ FINAL
```

각 AI는 자기 namespace로 Claim/Source ID를 생성한다.
예: `CODEX-C001`, `GEMINI-C001`. 같은 의미의 Claim만 Normalized Claim으로 묶는다.

---

## 4. MODE B 운영 흐름

```text
(기존 작업 중)
→ RELAY 발생 (토큰/세션/장애/사용자)
→ 넘겨주는 AI: shared/context.md 갱신
→ 이어받는 AI:
  1. AGENTS.md, MODES.md, 역할 문서 확인
  2. shared/TASK.md, shared/context.md 확인
  3. Git 상태 실제 대조 (branch, diff, checkpoint)
  4. 이전 AI 설명과 실제 상태가 다르면 → 실제 Git 상태가 정본
  5. 인계된 Stage와 역할을 유지하고 남은 작업부터 계속
```

자동 재개:

```bash
python3 -m demo.orchestrator --repo /path/to/repo --mode B \
  --resume TASK-ID --relay-agent gemini --execute
```

Runner는 새 worktree를 만들지 않고 state의 기존 worktree에서 `git status`, `git log`,
`git diff`를 다시 확인한다. 불일치는 event/state에 기록하고 실제 Git 기준으로 계속한다.

---

## 5. MODE C 운영 흐름

```
DEFINE (Entry AI)
→ Git Preflight
→ pipeline worktree 생성 (`task/<TASK-ID>/pipeline`)
→ Claude: ANALYZE / DESIGN → shared/DESIGN.md
→ Codex: IMPLEMENT / TEST → shared/IMPLEMENTATION.md
→ System: deterministic CHECK
→ Gemini: REVIEW → shared/REVIEW.md
  ├─ PASS → FINAL
  ├─ FIX_REQUIRED → Codex FIX → TEST → CHECK → Gemini REVIEW
  └─ BLOCKED → 사용자 판단
→ FINAL
```

### 자동 Runner

```bash
python3 -m demo.orchestrator --doctor
python3 -m demo.orchestrator "사용자 작업 목표" --execute
```

Runner는 다음 Gate를 추가로 강제한다.

1. 설계/검수는 read-only, 구현/FIX만 write 권한을 부여한다.
2. 각 Agent의 출력은 `.harness/runs/<TASK-ID>/outputs/`에 저장하고 `handoff.json`과 inline excerpt로 다음 Stage에 전달한다.
3. 테스트·lint·typecheck·build를 안전하게 발견해 REVIEW보다 먼저 실행한다. 검사 없음은 PASS가 아니라 `WAIVED`로 기록한다.
4. REVIEW는 명시적인 `VERDICT: PASS | FIX_REQUIRED | BLOCKED`만 인정한다.
5. FIX_REQUIRED면 구현 Agent가 수정하고 TEST, CHECK와 독립 REVIEW를 새로 실행한다.
6. Runner는 pipeline task branch의 local checkpoint만 만들며 base merge와 push는 수행하지 않는다.

역할은 기본 배치이며 사용자가 교체를 지시하면 변경한다.
구현에 참여한 AI는 같은 결과의 독립 REVIEW를 겸하지 않는다.

---

## 6. Stage Exit Gate

### DEFINE 완료

- TASK 계약이 존재하고 MODE가 정해졌다.
- Git Preflight를 통과했다.
- 사용자 원문, 입출력 artifact, 목표·범위·제약과 완료 조건이 정의됐다.

### DESIGN 완료 (C)

- 구현자가 판단할 수 있는 설계, 영향 범위, 제약과 검증 방법이 있다.

### IMPLEMENT 완료 (C)

- 요구 구현을 완료하고 실제 변경 범위를 기록했다.

### TEST 완료 (C)

- 필요한 테스트를 실제 수행하고 명령·결과를 기록했다.
- 실패를 숨기지 않고 미해결 상태를 구분했다.

### REVIEW 완료 (C)

- 독립 검수 조건을 충족한 reviewer가 실제 결과와 근거를 확인했다.
- 판정이 PASS, FIX_REQUIRED, BLOCKED 중 하나다.

### INDEPENDENT_WORK 완료 (A)

- Worker가 구현/조사를 완료하고 checkpoint를 확보했다.
- 독립 작업 중 상대 결과에 접근하지 않았다.

### CROSS_REVIEW 완료 (A)

- 각 Worker가 상대 결과를 검토하고 finding을 기록했다.
- Response를 1회 주고받았다.

### COMPARE 완료 (A)

- 비교 보고서에 필수 항목이 모두 포함됐다 (MODES.md 참조).
- 사용자 선택을 WAITING_USER로 기록했다.

### RESEARCH 완료

- 주요 주장에 Claim ID와 근거가 연결됐다.
- 출처가 Source ID로 기록됐다.
- 확인 불가 사항과 추가 검증 필요 항목이 구분됐다.

### FINAL 완료

- MODE에 따른 모든 필수 단계가 완료됐다.
- 사용자 최종 결정이 기록됐다.
- 최종 artifact 또는 구현이 식별됐다.

---

## 7. Worktree와 Checkpoint

### Worktree (MODE A/B/C)

MODE A에서 독립 작업을 위해 worktree를 생성한다.

```
base/
├── task/<TASK-ID>/codex    (codex worktree)
└── task/<TASK-ID>/gemini   (gemini worktree)
```

두 Worker는 같은 base commit에서 시작한다.
base/master에 직접 코드를 쓰지 않는다.

- MODE B: state에 기록된 기존 task worktree를 그대로 사용한다.
- MODE C: `task/<TASK-ID>/pipeline` worktree 하나를 모든 역할이 순차 사용한다.
- MODE A 사용자 선택 후 `CODEX_MERGE`만 base working tree 통합 권한을 가진다.

### Checkpoint

checkpoint commit은 AI 간 인계·비교·검수용 task branch 로컬 스냅샷이다.
MODE A/B/C Runner 실행 승인은 task worktree checkpoint를 포함하지만 push나 base 통합 승인은 아니다.
MODE A base 통합은 사용자 선택 후 Codex가 수행하고 push는 별도 승인이다.

다른 worktree는 merge-base와 triple-dot diff로 실제 작업 변경을 검수한다.

```bash
git merge-base <base-branch> <work-branch>
git diff <base-branch>...<work-branch>
```

---

## 8. artifact 관리

재사용할 결과는 `artifacts/<TASK-ID>/<의미 있는 이름>`에 저장한다.
artifact는 다음 TASK가 바로 읽고 작업할 수 있는 완성된 결과물이어야 한다.

RESULT에는 Stage 상태 요약, REVIEW 판정, artifact 경로, 사용자 최종 결정을 기록한다.
