# 전역 검증 정책 — 사용자 요구·구현 상태·실패 추적표

갱신: 2026-11-25 15:20 (코디네이터 작성). 워커는 이 문서를 **읽기 전용 기준**으로 쓴다. 상태가 바뀌면 코디네이터가 갱신한다.
요구 ID(R-번호)를 지시서·REPORT·커밋 메시지에 인용한다.

## 0. 위치

| 이름 | 위치 |
|---|---|
| main | `376283a` (origin 동일). 배포(th-sync)는 `d3cf149`, 배포 승인 없음 |
| 통합 커밋 | 브랜치 `task/final_main_20261125`, worktree `/home/hs/rang/team-harness/_outputs/final_main_20261125/wt`, 커밋 `193e53c` (main 위 단일 커밋, 42파일 +1864/-133) + **미커밋** 쿼터 재거부 pending 수정(`src/team_harness/result_guard.py`, `src/tests/test_result_guard.py`) |
| 정리 브랜치 | `task/test_hygiene_20261125` (30220b6, a6b4887, 0ecc0b3), worktree `/home/hs/rang/team-harness/_outputs/test_hygiene_20261125/wt` |
| 설계 v2 | `/home/hs/rang/team-harness/_outputs/verify_policy_design3_20261125/design.md` |
| 구현 검수(진행 중) | `/home/hs/rang/team-harness/_outputs/impl_audit_20261125/audit.md` (결과 1: 함수 단위 mock 판정, 종단·main 대비 회귀 대부분 미판정) |
| 작업 단위 분할(진행 중) | `/home/hs/rang/team-harness/_outputs/work_units_20261125/units.md` |

## 1. 사용자 요구 (원문 취지)

| ID | 요구 | 출처(날짜) |
|---|---|---|
| R1 | 중요한 결정·확인·채택·결과 순간에 검증이 **자동 발동**. 사용자가 "이거 중요하니 검증해"라고 하거나 코디네이터 재량으로 정하지 않고, **정책·로직이 정한다**. 학습·로직에 국한하지 않고 어떤 프로젝트·작업공간에서도 | 11-24 |
| R2 | team-harness에서 만들어 **전역 배포**(다른 프로젝트에 따로 만들지 않음) | 11-24 |
| R3 | 결과 코드: **0 = 실패류**(실패·미달·구현 안 됨·효과 없음·기각), **1 = 애매·대기·멈춤**, **2 = 정상**. 문장 해석 없음 | 11-24 |
| R4 | **사실로 확인된 심각한 결함만 사용자에게**. 나머지는 워커끼리 조율. 사용자 개입 최소 | 11-25 |
| R5 | **같은 문제는 최대 2회 검증**. 하루 상한 없음(문제당 2회만) | 11-25 |
| R6 | 코디네이터가 검증 없이 넘어가는 것 차단 + 워커와 싸우며 하루 허비하지 않기 | 11-24 |
| R7 | 코디네이터 자신의 잘못(지시 오인, 워커 결과 그대로 믿기, 직접 한 실험 미검증)도 잡기 | 11-24, 11-25 robot3 사례 |
| R8 | 사용자가 멈춘 작업은 자동 재시도 금지 | 11-25 |
| R9 | 사용량 한도 잠금은 만료되어야 함(수동 rm 불필요) | 11-25 |
| R10 | 반복 알림은 한 번만 | 11-25 |
| R11 | 손실 리포트 반복 버그 수정 | 11-25 |
| R12 | 테스트가 감시 프로세스를 남기지 않기, 가짜 알림 금지, 판정 뒤 반례가 버려지지 않기, 감시 프로세스 자기 종료 | 11-25 |
| R13 | 반영은 **기능 단위로 작게**, 단계별로. 몰아서 하지 않기. 검증은 main 대비 회귀·심각 결함만 차단, 새 기능 경계 사례는 '알려진 한계' | 11-25 |
| R14 | 외부 분석 반영: 실행≠검증≠결론 타당(**VERIFIED/VALIDATED 분리**), 장시간 학습 전 메커니즘 측정 게이트, 3계층 검증(결정론 우선), 요구사항 ID 추적 | 11-25 |
| R15 | 모든 실행 경로 적용(th-launch·job-start·watchd·protocol A/B/C·run/repl·SDK + **th 밖 직접 실행**: Claude hook) — AGENTS.md 규칙 4 | 상시 |
| C1 | 우회 환경변수(TH_CROSSCHECK_SKIP, TH_CANARY_SKIP, TH_PATHS_SKIP) 금지. main merge·push는 승인 범위 안에서만. kill·삭제는 승인 필요 | 상시 |

## 2. 구현 상태

| ID | 내용 | 상태 | 위치 |
|---|---|---|---|
| R11 | 손실 리포트: 디렉터리 아닌 owner 정규화, 루트별 실패 격리, report.last 기록 | **main 반영** | 376283a |
| R8 | `th-stop.sh` + `STOPPED_BY_USER` 표식, watchd 자동 재시도·계속·롤오버·자동 검증 차단, 수동 th-launch 시 해제, 부모 정지 시 하위 검증 워커 정지 | 통합 커밋(미반영) | 193e53c |
| R9 | 쿼터 잠금 `expires=`(메시지 시각+5분, 기본 12h, 용량 15분), 자정 넘김 다음 날 보정, 기존 잠금 작성+12h | 통합 커밋(미반영) | `src/team_harness/quota_lock.py` |
| R10 | 자동 재시도 실패 알림 작업·종류·사유 기준 1회 | 통합 커밋(미반영) | `scripts/th-watch-lib.sh` |
| R3 | REPORT 첫 줄·저널 결정 줄 정확 형식 판정, 누락·오형식=1 | 통합 커밋(미반영) | `src/team_harness/result_guard.py` |
| R1 일부 | 결과 0·실험 완료 시 자동 독립 검증(일반 작업의 단순 FAILED는 제외), 쿼터 거부 시 pending 후 재시도 | 통합 커밋(미반영) + 미커밋 수정 | result_guard.py, th-watchd.sh |
| R1 일부 | 실험 지시서 `## 입력 반영`·`## 대조군` 필수(TH_BRIEF_OK로 우회 불가) | 통합 커밋(미반영) | `src/team_harness/brief_check.py` |
| R5 | 결정 ID별 crosscheck 2회 상한, 3번째 rc=2 + `채택 판단 요청 — 사용자` 사건. **같은 decision ID일 때만 공유** | 통합 커밋(미반영) | `src/team_harness/crosscheck.py` |
| R6 | main push 범위에 `Crosscheck:` 또는 `Verify-Exempt:` trailer 필수(team-harness 저장소만) | 통합 커밋(미반영) | `.githooks/pre-push` |
| R12 | DONE 뒤 증거 변경에도 FAIL+반례 보존(PASS는 엄격), FAIL 문구는 실제 FAIL 항목, 테스트 TH_NO_DAEMON 기본, TH_ONCE crosscheck 제외, adopt_brief 경로 오인 수정 | 통합 커밋에 cherry-pick(미반영) | 30220b6, a6b4887 |
| R12 | watch 디렉터리·잠금 파일이 사라지면 watchd 자기 종료 | 정리 브랜치, 통합 커밋 포함 여부 **impl_audit 확인 필요** | 0ecc0b3 |
| R7 일부 | 저널(코디네이터 work_log) 중요 결정·결과 0 → 자동 검증. 줄 내용 해시 집합 커서 | **구현됐으나 기본 꺼짐**(`.team-harness/journal-auto-verify`=true일 때만) | result_guard.py, `scripts/journal-hook.sh` |
| R1·R4·R7·R14·R15 나머지 | 설계 v2 1~3단계: `verify_policy.toml`, 장부 `verify/events.jsonl`, 심각도 수치 기준, case.json 재실행 판정, 제3 검토자 1명, 자동 수정 1회, 장시간 작업 전 검증, 직접 수정 코드 첫 결과 검증, merge·push·배포·삭제 전 관문(전역), Stop hook(결과 없이 턴 종료 차단), run/repl 최종 게이트, 21시 일일 목록, 지시서에 결과 코드 뜻 자동 삽입, VERIFIED/VALIDATED, 메커니즘 측정 게이트, 요구사항 ID | **미구현** | 설계 v2 |
| R2 | 전역 배포 | **미실행**(승인 필요) | th-sync |

## 3. 실패·반례·알려진 한계 (해결 전 반영 금지 여부 표시)

| # | 내용 | 근거 | 상태 |
|---|---|---|---|
| K1 | 저널: inode 교체 + 앞부분 편집 시 새 결정 누락(오프셋 방식) | `_outputs/final_fix_20261125/.th_adopt/terra/VERDICT.md` | 해시 집합 방식으로 해결 |
| K2 | 저널: 첫 줄 삭제 + 새 결정 추가 시 누락 | `_outputs/final_fix_20261125/.th_adopt_retry/terra/VERDICT.md` | 해결 |
| K3 | 저널: 이전 offset 커서 이행 시 대기 결정 누락(2건) | `_outputs/journal_cursor_20261125/.th_adopt*/terra/VERDICT.md` | 이행 코드 제거로 해결(이전 형식은 배포된 적 없음) |
| K4 | 저널: 같은 작업 폴더를 가리키는 서로 다른 중요 결정 2줄 중 두 번째 검증 생략(task_key 마커 공유) | `_outputs/journal_final_20261125/.th_adopt/terra/VERDICT.md`, `result_guard.py:83` | **미해결** — 저널 자동 검증 기본 꺼짐의 이유 |
| K5 | 재시도 중 쿼터 재거부를 failed로 기록 → 이후 자동 검증 영구 0회 | `_outputs/ship_core_20261125/.th_adopt/terra/VERDICT.md`, `result_guard.py:127` | 미커밋 수정으로 해결(impl_audit mock: pending→pending→started), 커밋 전 |
| K6 | `observed_verify.py:109` FAIL이 아닌 WARN·검증 없음도 PASS로 전달 | 외부 분석, 코디네이터·impl_audit 확인(main·통합 둘 다) | **미해결, 심각(잘못된 성공 보고)** — main에 이미 있는 결함 |
| K7 | `brief_check.py:212` 원인 가설 있는데 증상 재현 절 누락이 warn | 외부 분석, 코디네이터 확인 | **미해결** |
| K8 | 테스트 리퍼가 다른 실행의 테스트 watchd까지 TERM | `_outputs/ship_core_20261125/REPORT.md` | 범위 축소 지시만, **미구현** |
| K9 | 2회 상한은 같은 decision ID만 공유(자동·수동·저널 키가 다르면 각각) | `_outputs/final_review_codex_20261125/findings.md` #5 | 문서화만 |
| K10 | run/repl 내장 코디네이터는 실험 절 누락을 경고만, 자동 crosscheck 없음 | agy findings F10 | 문서화만 |
| K11 | 검증 중 추적되지 않은 운영 파일 변화로 저장소 상태 변경 UNAVAILABLE | CROSSCHECK.md 다수 | 저장소 상태 비교에서 추적되지 않은 파일 제외; HEAD와 추적 파일 변경은 감지 |
| K12 | 전체 테스트 중 `test_th_launch_rejects_claude_without_user_request`는 워커 환경의 `TH_USER_REQUESTED_CLAUDE=1` 상속 시 실패 | test_hygiene2 REPORT | 관문은 `env -u TH_USER_REQUESTED_CLAUDE`로 실행 |
| K13 | 실제 inbox(`/home/hs/rang/team-harness/.claude/inbox.md`) 884~894줄 가짜 사건 남음(저장소 안 fixture 원인) | 코디네이터 확인 | 삭제는 승인 필요, 재발 방지: fixture는 저장소 밖 `mktemp -d` |
| K14 | `/tmp/th-test-home-*` 약 60개 잔존 | test_hygiene3 REPORT | 이번 실행 증가분만 관리 |
| K23 | design 디렉터리 source의 채택 영수증에 decisions.md 대상 누락 | `src/team_harness/adoption_gate.py` | 해결 (커밋: fix(R6,K23): bind design directory receipt subject) |

## 4. 오늘의 진행 원칙 (R13)

1. 기능 추가 동결 → 통합 커밋을 기능 단위로 쪼개 하나씩 검수·검증·main 반영.
2. 검증 주장은 "main 376283a 대비 회귀 없음 + 심각 결함(보안·삭제·손실·잘못된 성공 보고) 없음". 새 기능 경계 사례는 위 3장에 추가.
3. 검증 중에는 작업 폴더·worktree를 수정하지 않는다(증거 변경 → UNAVAILABLE).
4. 재현·테스트 fixture는 저장소 밖. 관문: `uv run ruff check src/`, `uv run ruff format --check src/`, `uv run pyright src/`, `env -u PYTHONPATH -u TH_USER_REQUESTED_CLAUDE bash scripts/th-test-watchd.sh`.
5. 결과 코드 뜻: 2 = 요구 전부 완료·관문 통과, 1 = 일부 미완·판정 보류, 0 = 실패(종료 코드 관례와 반대).

## 5. 지시서 결함 (15:30 추가 — 사용자 지적)
| # | 내용 | 근거 | 상태 |
|---|---|---|---|
| K15 | **오늘까지 지시서가 th 경로 구현만 요구했다.** `paths:` 줄에 "가드 hook(th 밖 직접 실행)" 같은 단어만 넣었고, th 밖에서 같은 판정이 **어디에 붙어 동작하는지·재현 명령**은 요구하지 않았다. 설계 v2도 "th 밖"이 1회만 나온다. `brief_check.py:221`·`paths_check.py:62`는 "th 밖" 단어 존재만 확인해 통과시켰다 | `_outputs/stop_retry_fix_20261125/task.md:7`, `verify_policy_design3_20261125/design.md` | **미해결** — work_units 단위 0: 지시서·설계에 기능별 `th 밖 적용 지점`(전역 hook·git hook·전역 명령)과 `## th 밖 재현`(th-launch 없이 실행한 명령·출력) 필수, 없으면 brief_check error·verify FAIL |

## 6. 구조 원칙 (15:35 — 사용자 지시, 모든 단위의 기준)

- 상태: Claude Bash hook에 공통 파이프라인과 동기 job-start 연결을 추가한다. PATH shim 실행기 연결은 P2 범위다.
- 원인: 판정 로직의 **입력이 th 작업 폴더(STATUS·DONE·REPORT)** 였다. th로 띄우지 않으면 폴더가 없어 아무것도 발동하지 않는다. 그 위에 "th 밖" 행을 덧붙이는 식이라 반복 실패.
- 원칙: 판정 로직은 **실행 사건 하나**(누가·어디서·어떤 명령·출력·종료 코드·결과 파일)를 입력으로 받는 공통 함수다. th는 여러 호출자 중 하나일 뿐이다.
- 진입점 매핑(어댑터): th-launch/watchd(작업 폴더 → 사건), Claude hook(PreToolUse/PostToolUse의 Bash 명령·출력 → 사건, Stop → 최종 보고), Codex/agy hook 또는 전역 래퍼, git hook(merge·push), th run/repl·protocol·SDK. 코디네이터가 무엇으로 실행하든 hook이 사건을 넘긴다 — 코디네이터 선택에 의존하지 않는다.
- 기존 th 기능은 그대로 두고 어댑터로 감싼다.
- (15:40 사용자 정정) hook이 사건만 남기면 **사후 기록**일 뿐, 작업 폴더·STATUS·정지·쿼터·재시도·자동 검증 같은 th 실행 기능은 th로 안 켜면 여전히 없다. 따라서 진입점 매핑은 **실행 자체를 th 실행기로 감싸는 것**이어야 한다:
  - Claude PreToolUse hook이 명령을 **바꿔서**(updatedInput) th 실행기로 감싼다 — 매핑 표에 걸리는 명령(에이전트 CLI codex/agy/claude -p, 학습·평가·실험, 장시간 작업)은 작업 폴더 자동 생성 + th 실행 기능 적용. 짧은 조회 명령은 기록만.
  - 셸에서 직접 치는 경우: PATH 앞 전역 shim(codex/agy/claude 등)이 같은 실행기로 감싼다.
  - 매핑 표(명령 패턴 → 실행 종류·인자)는 전역 설정 파일 하나, th-sync로 배포. 코디네이터 선택과 무관.
- (15:45 코디 확인) **가로채기는 이미 있다**: TH-D52/53(2026-11-16) 전역 PATH shim `~/.team-harness/shim/bin/{codex,agy}` → `shim/codex`·`launch_shim.py`. 단 동작은 "실행 전 관찰 기록만, 판정 강제 없음"(`launch_shim.py:1`)이고, 관찰 작업 검증은 `observed_verify.py`(K6 WARN→PASS 결함). 빠진 것: ① shim이 th 실행 기능(작업 폴더·정지·쿼터·결과 코드·자동 검증)을 적용하지 않음, ② claude·학습/평가 명령(python 등)은 shim 대상 아님, ③ Claude 코디의 Bash 직접 실행은 PreToolUse 가드가 차단·관찰만. → **새로 만들지 말고 기존 shim·관찰 경로를 확장**한다.
- (15:40 사용자 지시, 최종 원칙) **기능별로 가로채기를 붙이지 않는다.** 가로채기 지점(PATH shim·Claude hook·git hook)은 하나의 공통 파이프라인 `실행 전 단계 → 실행 → 실행 후 단계`를 부르고, th 기능은 이 파이프라인에 **등록**된다(정지 확인·쿼터 잠금·지시서 점검·실행 정책·결과 코드·자동 검증·2회 상한·관찰 기록 …). th-launch/watchd도 같은 파이프라인을 부른다. 새 th 기능은 등록만 하면 누가·무엇으로·어떻게 실행하든 적용된다.
- (P0) 공통 사건·단계 API와 PATH shim pre 및 관찰 종료 post를 연결했다. 직접 exec의 종료 코드는 미관찰이며, 다른 가로채기 지점은 후속 단위다.
