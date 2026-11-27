# 가로채기 경로 매핑과 실측 (2026-11-27)

이 표의 작업 생성은 해당 명령이 관리 작업 폴더와 종료 기록을 만드는 경우다. `미실측`은 작업 생성 여부를 판정하지 않았다는 뜻이다. 기본 검사는 현재 체크아웃 `scripts/hooks/th-exec-pipeline.py`와 설치된 PATH shim을 호출한다. 실제 watch나 사용자 설정은 쓰지 않는다. 아래 로그는 [matrix_run.log](/home/hs/rang/team-harness/_outputs/p6b_intercept_matrix_20261127/matrix_run.log)이며, 줄 번호는 기본 모드 재현 출력이다. hook 행은 실제 모델 세션이 아닌 입력 재생과 셸 실행이며 agy 대화형 입력도 동일한 hook 형식의 재생이다. 모델별 실제 연결과 승인 UI까지 증명하지 않는다.

| 실행 주체·경로 | 판정과 근거 |
|---|---|
| ① th-launch 워커 codex/agy/claude | codex 가짜 CLI 호출과 DONE 확인, 공통 사건 없음 — 그대로(빈칸), SKIP; agy/claude 미실측. 직접 실행은 `scripts/th-launch.sh:380` |
| ② th-job-start | 직접·--wait 각각 job.pid와 exit_code=7 확인, 공통 사건 없음 — 그대로(빈칸), SKIP (`scripts/th-job-start.sh:19`) |
| ③ watchd 재시도·자동 계속 | 테스트로 확인: `test_rollover_is_excluded_from_retry_count`, `test_continue_relaunch_uses_rollover_accounting` 통과. 실제 재시도 미실측, SKIP. 재시도는 `scripts/th-watch-lib.sh:464`, 자동 계속은 `src/team_harness/worker_relaunch.py:55` |
| ④ th run/repl | 미실측, SKIP — run은 코디네이터 API, repl은 대화형 진입점 (`src/team_harness/cli.py:332`, `:393`) |
| ⑤ protocol A/B/C | 미실측, SKIP — MODE C 역할 실행기 (`src/team_harness/protocol/mode_c.py:165`) |
| ⑥ SDK | 미실측, SKIP — `TeamHarness.run`은 코디네이터 실행을 준비 (`src/team_harness/harness.py:131`) |
| ⑦ 직접 codex exec / agy -p / claude -p | th 작업 생성(PATH shim) — 각 로그 49·50·51행, 가짜 실제 CLI와 설치 shim 사용 |
| ⑧ 직접 연 대화형 codex/agy/claude 프로세스 자체 | 그대로(의도) — `src/team_harness/launch_shim.py:172`의 비대화 분기와 `:183`의 직접 exec; 내부 명령은 각 hook 적용 대상 (`design/decisions.md:929`, `:942`) |
| ⑭ git merge/push | 격리 저장소의 merge와 로컬 bare 원격 push 각각 성공, 공통 사건 없음 — 그대로(빈칸), SKIP. merge 전용 hook 없음, push hook은 `.githooks/pre-push:1` |

## hook 명령 형태별 결과

각 칸의 숫자는 위 실측 로그의 행이다. `생성`은 **th 작업 생성(hook)**, `의도`는 **그대로(의도)**. 생성 칸은 실제 셸 실행 후 기록된 exit_code를 확인했다(단순 명령은 7, 나머지는 0). 비대상·이미 th는 분류기 `src/team_harness/exec_map.py:44`, `:165`에 따른다. Codex 승인 요청 payload는 `permission_mode=default`이며 `scripts/hooks/th-exec-pipeline.py:115`에서 재발행한다. 워커는 `:70`에 따라 유지한다. `&`는 종료를 기다려 기록을 확인한다.

| 경로 | python3 train.py | cd x && python3 train.py | pipe tail | 끝 & | $(…) | "$(…)" | ls | 이미 th |
|---|---|---|---|---|---|---|---|---|
| ⑨ Claude Bash | 생성 (1) | 생성 (2) | 생성 (3) | 생성 (4) | 생성 (5) | 생성 (6) | 의도 (7) | 의도 (8) |
| ⑩ Codex bypassPermissions | 생성 (9) | 생성 (10) | 생성 (11) | 생성 (12) | 생성 (13) | 생성 (14) | 의도 (15) | 의도 (16) |
| ⑪ Codex on-request | 거부·재발행 (17) | 거부·재발행 (18) | 거부·재발행 (19) | 거부·재발행 (20) | 거부·재발행 (21) | 거부·재발행 (22) | 의도 (23) | 의도 (24) |
| ⑫ agy skip-permissions | 생성 (25) | 생성 (26) | 생성 (27) | 생성 (28) | 생성 (29) | 생성 (30) | 의도 (31) | 의도 (32) |
| ⑬ agy 대화형 | 생성 (33) | 생성 (34) | 생성 (35) | 생성 (36) | 생성 (37) | 생성 (38) | 의도 (39) | 의도 (40) |
| ⑮ 워커 내부 셸 | 의도 (41) | 의도 (42) | 의도 (43) | 의도 (44) | 의도 (45) | 의도 (46) | 의도 (47) | 의도 (48) |

⑮는 이미 상위 th 작업이 있는 워커 내부의 중첩을 막는 의도다 (`design/decisions.md:884`, `scripts/hooks/th-exec-pipeline.py:70`). ⑪은 원 명령을 거부하고 이유의 감싼 명령은 hook 무출력 및 실행 후 exit_code 일치를 확인했다 (`design/decisions.md:929`). ⑬은 입력 재생이며, 별도의 실제 대화형 세션에서 승인 후 종료 코드 7을 기록한 근거는 `/home/hs/rang/team-harness/_outputs/p5d_agy_interactive_20261127/REPORT.md:4`이다. 이 한 실측은 모든 명령 형태를 증명하지 않는다.

## 한계와 재현

`bash scripts/th-intercept-check.sh`는 격리된 임시 HOME, TH_WATCH_DIR, 가짜 CLI를 사용하고 데몬을 끈다. `--live`는 기본적으로 꺼져 있으며 별도 인증이 필요한 격리 HOME에서 실제 CLI 네 번을 호출한다. 빈칸과 미실측 경로, 실제 hook 등록 및 승인 UI, 모든 셸 구문은 확인하지 않았다. 분류기의 기존 K22 한계는 `design/requirements.md:95` 참조.
