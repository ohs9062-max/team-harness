# AGENTS.md — team-harness 작업 기준 (정본)

> **이 파일과 `CLAUDE.md`는 내용이 같아야 한다**(바이트 동일; `.githooks/pre-commit`이 다르면 커밋을 거부한다. 심볼릭 링크는 독립 검증 스냅샷이 거부하므로 쓰지 않는다). Codex·agy는 `AGENTS.md`를, Claude는 `CLAUDE.md`를 자동으로 읽는다. 누가 코디네이터·워커가 되든 같은 규칙을 본다. 규칙은 여기 한 곳에만 쓴다.
> upstream(writeitai/team-harness) 원문은 `docs/upstream/`에 참고용으로만 보관한다. 이 저장소는 upstream과 갈라진 fork이며 upstream 계약(loopy-loop API, PyPI 릴리스)을 따르지 않는다.

## 1. 이 워크스페이스는 무엇이고 왜 있나

team-harness는 **사용자의 모든 AI 에이전트 작업에 붙는 전역 실행·검증·감시 계층**이다.

- 대상: 모든 프로젝트·작업공간, 모든 실행 방식 — th로 띄운 워커, 사용자가 직접 실행한 Claude/Codex/agy, 코디네이터가 th 없이 직접 실행한 명령, git merge/push.
- 해결하려는 문제: AI 코디네이터·워커가 ① 결과를 확인하지 않고 "완료/효과 없음/원인 아님"을 보고하고, ② 같은 실수를 반복하고, ③ 사용자가 매번 "이거 검증해", "th 밖에도 적용해"라고 다시 말해야 하는 것.
- 목표: **사용자가 지시하지 않아도 정책과 코드가 자동으로** 실행을 기록하고, 위험을 막고, 중요한 결과를 검증하고, 사실로 확인된 심각한 문제만 사용자에게 올린다.
- 검증 대상에는 **코디네이터 자신**도 포함된다: 지시 오인, 워커 보고를 확인 없이 수용, 코디네이터가 직접 돌린 실험·결론(예: 2026-11-25 robot3 — 학습을 직접 돌리고 중간 신호 측정 없이 "효과 없음" 보고).
- 사용자 요구 전체·구현 상태·알려진 결함: `design/requirements.md` (요구 ID R1~R15, 결함 K1~). **작업 전에 읽고, 지시서·REPORT·커밋에 R/K 번호를 인용한다.**

## 2. 무엇을 만드는가 — 가로채기 공통 파이프라인 (모든 기능의 필수 구조)

**새 기능은 th 전용으로 만들지 않는다.** 모든 기능은 아래 구조에 등록되어야 완료다.

```text
가로채기 지점 ──▶ 공통 파이프라인 ──────────────────────────────▶ 사건·장부
  PATH shim (codex/agy/claude)      실행 전 단계: 정지 확인, 사용량 한도 잠금,
  Claude hook (PreToolUse/            지시서 점검, 실행 정책, 위험 명령 가드
    PostToolUse/Stop/SessionStart)  실행
  Codex hook                        실행 후 단계: 결과 코드 판정, 관찰 기록,
  git hook (merge/push)               자동 독립 검증, 검증 2회 상한, 알림
  th-launch / watchd / th run·repl / protocol / SDK
```

- 가로채기 지점은 판정 로직을 직접 갖지 않고 **같은 공통 파이썬 함수**를 부른다. th 기능은 파이프라인의 실행 전/후 단계에 등록만 하면 모든 실행 방식에 적용된다.
- 이미 있는 가로채기: 전역 PATH shim `~/.team-harness/shim/bin/{codex,agy}` → `shim/<tool>` → `src/team_harness/launch_shim.py`(관찰 작업 등록 + 쿼터·지시서·실행 정책 경고) → 종료 시 `observation_events.py` → `observed_verify.py`. Claude 전역 hook: `scripts/hooks/*`, `scripts/journal-hook.sh`. git: `.githooks/`. **새로 만들기 전에 이 경로를 확장한다.**
- 판정 로직의 입력을 th 작업 폴더(STATUS·DONE·REPORT)에만 묶지 않는다. 입력은 "실행 사건"(누가·어디서·명령·출력·종료 코드·결과 파일)이고 th 작업 폴더는 그 사건을 만드는 한 가지 방법이다.
- 모든 설계·지시서에는 **적용 경로 표**를 넣는다. 열: th-launch | watchd | run/repl | protocol | SDK | PATH shim | Claude hook | Codex hook | git hook. 각 칸은 붙는 지점(파일:줄) 또는 '없음 + 이유'. 단어("th 밖")만 적는 것은 표가 아니다.
- 완료 판정에는 **`## th 밖 재현`**이 반드시 있어야 한다: th-launch 없이 직접 실행한 명령과 출력. 없으면 미완료(결과 1)다.
- 전역 적용 수단(hook·shim·전역 지시 블록)은 `th-sync`로 배포한다. **배포는 사용자 승인 사항**이다.

## 3. 결과 코드와 검증 원칙

- **결과 코드**(REPORT.md 첫 줄 `결과: N`, 종료 코드 관례와 반대): `2` 정상(요구 전부 완료·관문 통과), `1` 애매·대기·멈춤·일부 미완·판정 보류, `0` 실패류(실패·미달·구현 안 됨·효과 없음·기각). 문장 해석 없이 첫 줄로 판정한다. 형식이 틀리면 1.
- 검증은 **자동 발동**한다(코디네이터 재량 아님): 결과 0, 실험 결론, 중요 결정·채택, main 반영 전.
- **같은 문제는 최대 2회 검증**한다. 3번째는 사용자 판단으로 올린다. 하루 상한은 없다.
- **사실로 확인된 심각한 결함만 사용자에게** 올린다(보안·삭제·데이터 손실·잘못된 성공 보고). 나머지는 워커끼리 조율한다.
- 실행 완료 ≠ 검증 ≠ 결론 타당. "효과 없음·원인 아님" 같은 부정 결론은 **같은 입력으로 수정 전후 중간 신호를 비교한 표** 없이는 판정 보류(1)다. 장시간 학습 전에 메커니즘(손실 구성·노출 횟수 등)을 먼저 측정한다.
- 원인 가설이 있는 수정은 **수정 전 실패 → 수정 후 통과** 재현을 지시서 `## 증상 재현`에 넣고, 워커는 REPORT에 `지시 원인: 지지` 또는 `지시 원인: 반박 — 실제 원인 … (파일:줄)`을 쓴다.
- WARN·검수 없음·판독 불가를 PASS로 바꾸지 않는다(K6).

## 4. 반영 원칙 — 작게, 단계별로

- 기능 하나 = 커밋 하나 = 검증 1회 = main 반영. 여러 기능을 한 커밋에 묶지 않는다(2026-11-25: 20여 변경을 묶어 검증이 6회 연속 기각되고 main에 아무것도 반영되지 않았다).
- 독립 검증 주장은 "main 대비 회귀 없음 + 심각 결함 없음"으로 묻는다. 새 기능의 경계 사례는 차단하지 않고 REPORT `알려진 한계`와 추적표 K 항목에 기록한 뒤 후속 단위로 고친다.
- 반영 대기 중에는 기능을 덧붙이지 않는다. 새 요구는 추적표에 쌓는다.
- 검증이 도는 동안 작업 폴더·worktree를 수정하지 않는다(증거 변경 → 판정 불가).

## 5. 실제 작업 흐름과 도구

| 단계 | 도구 |
|---|---|
| 워커 실행 | `scripts/th-launch.sh <작업폴더> codex|agy|claude <모델> <effort> <worktree>` (지시서 `<작업폴더>/task.md`) |
| 긴 본 작업 분리 | `scripts/th-job-start.sh` (직접 setsid 금지) |
| 사건 대기 | `scripts/th-inbox-wait.sh` (코디네이터 세션별 1개, LLM 폴링 금지). inbox는 메인 체크아웃 `.claude/inbox.md` |
| 상태 | `ths`(한 번), `ths -w`(사용자 별도 터미널) |
| 원인·이력 조회 | `scripts/th-why.sh <폴더|PID>`, `scripts/th-db.sh sql "SELECT …"` (tasks, attempts, failures, events, adoptions, coord_log) |
| 독립 검증 | `scripts/th-adopt.sh <폴더> "<주장>"` → `.th_adopt/<검증자>/VERDICT.md`, `CROSSCHECK.md` |
| 작업 정지 | 사용자 승인 후. 정지된 작업은 자동 재시도하지 않는다 |
| 배포 | `scripts/th-sync.sh` (사용자 승인), 상태는 `th-sync.sh --status` |

- 작업 산출물·지시서는 저장소 `_outputs/<주제>_<날짜>/`에 둔다. 결과 파일: `STATUS`(시각·단계), `WORKLOG.md`, `REPORT.md`, `DONE`/`FAILED`.
- 지시서 필수 항목: 목표, 기준 문서(이 파일·추적표·관련 설계 경로), 쓰기 범위(`allow_paths`), 적용 경로 표, `## 증상 재현`, `## th 밖 재현`, 관문, 결과 코드, `## 검수` 블록.
- 금지 사항에는 **무엇을** 금지하는지 정확히 쓴다("실제 watch 쓰기 금지"처럼 모호하게 쓰면 워커가 필요한 검증까지 건너뛴다).

## 6. 역할

- **코디네이터**(Claude 기본, Codex로 바뀌어도 이 문서 기준): 범위·우선순위·지시서·결과 검증·보고만 한다. 코드·문서 수정, 병합, 커밋·push는 지시서로 워커에 맡긴다(사용자가 직접 하라고 한 경우 예외). 워커 보고를 그대로 믿지 않고 원본(코드·로그·산출물)으로 확인한다.
- **워커**: 기본 Codex Terra medium → Terra high → Sol medium → Sol high(상향 이유 기록). agy는 단순·대량 작업과 Codex 쿼터 소진 시. Claude 워커는 사용자가 명령할 때만(Sonnet), 끝나면 후속은 Codex.
- 실행 동시성·사용량 상한 때문에 작업을 미루지 않는다(`TH_MAX_PARALLEL`, `TH_IGNORE_BUDGET`). 단 같은 파일 작업은 순서를 지킨다. 쿼터 오류가 처음 나면 배치를 멈춘다.
- 사용자에게 보이는 답·보고는 한국어, 짧고 쉽게: 무엇이 구현됐고 어떻게 동작하는지.
- 코디네이터 세션 교체를 권하지 않는다(맥락·의도 유지가 우선, 토큰 절감은 워커 쪽에서). 이미 받은 사용자 권한을 다시 묻지 않는다.
- 코디네이터는 자기 프로젝트 작업만 한다. 다른 프로젝트 작업은 그 프로젝트 창에서.

## 7. 사용자 승인이 필요한 것 (항상)

- main merge·push(사용자가 승인한 범위 안에서만), `th-sync` 전역 배포, 프로세스 kill, 파일·결과 삭제/이동.
- 우회 환경변수 사용 금지: `TH_CROSSCHECK_SKIP`, `TH_CANARY_SKIP`, `TH_PATHS_SKIP`.
- 사용자의 이메일 등 개인정보를 외부 요청에 넣지 않는다.

## 8. 개발 관문

```bash
git config core.hooksPath .githooks
uv sync --extra dev
uv run ruff check src/
uv run ruff format --check src/
uv run pyright src/
env -u PYTHONPATH -u TH_USER_REQUESTED_CLAUDE bash scripts/th-test-watchd.sh   # 전체 테스트(격리 HOME)
bash scripts/th-test.sh src/tests/test_x.py -q                                  # 일부 테스트
```

- **맨 `uv run pytest`를 쓰지 않는다.** 격리 없이 돌면 실제 HOME에 테스트 감시 프로세스·가짜 사건이 남는다(2026-11-25 수백 개 누출). 테스트·재현 fixture는 저장소 밖 `mktemp -d`.
- 임포트는 한 줄에 하나(ruff `force-single-line`), pytest는 `asyncio_mode = "auto"`.

## 9. 문서 지도

| 문서 | 내용 |
|---|---|
| `design/requirements.md` | 사용자 요구·구현 상태·알려진 결함 (진행 상황의 정본) |
| `design/designs/operating_policy.md` | 운영 정책 상세(모델·읽기·재실행·검증·실험·배포). 전역 `~/.team-harness/operating_policy.md`로 배포 |
| `design/decisions.md` | 결정 기록(TH-D1~). 의도된 결정을 바꾸려면 여기에 제안하고 바꾼다. 코드에서 몰래 위배하지 않는다 |
| `design/designs/` | 구속력 있는 설계. 맥락 없는 사람이 읽어도 이해되게 쓴다(무엇·왜·어디 코드) |
| `design/harness_protocol/` | MODE A/B/C 정본 |
| `docs/upstream/` | upstream 원문(참고용, 구속력 없음) |

`.claude/`(current_status·work_log)와 코디네이터 메모리는 코디네이터 세션 상태일 뿐이다. **원칙·요구의 정본은 이 파일과 추적표**이며, 새 원칙이 생기면 이 파일에 쓴다.
