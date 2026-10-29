---
kind: code
task_key: done_linger_20261029
follow_up: normal
---
# DONE 후 워커 프로세스 잔류를 "멈춤"으로 오탐·완료 알림 지연 수정, 병합 후 push

규칙 문서·.claude/는 읽지 않는다. 기록은 이 폴더 STATUS·WORKLOG.md에만. 작업 트리 전체와 이 작업 폴더는 쓰기 허용. 시간 제한 없음. 할 일 1~5를 전부 끝내야 DONE.

## 재현 (10-29 3회)
| 작업 | DONE 기록 | 받은 사건 |
|---|---|---|
| auto_continue_20261029 | 10:33 | 10:46 "멈춤 — CPU·산출물 685s 변화 없음(프로세스는 살아 있음)", 완료 사건 없음 |
| rollover_paths_20261029 | 11:00 | 11:13 멈춤 669s |
| archive_paths_20261029 | 11:18 | 11:31 멈춤 698s |
반면 일부 작업(zone_body 10:53, doc_archive 10:57)은 DONE 직후 "완료" 사건이 정상으로 왔다. 코디네이터는 완료를 13분 늦게 알고, 멈춤 오알림을 받는다.

## 코드 위치 (코디네이터 확인)
`scripts/th-watch-lib.sh`: 455-490 완료 판정(`terminal`은 `worker_digest terminal` → `scripts/th-job-liveness.py terminal`로 보정; daemon에서 DONE+current_worker_alive면 `return 0`), 655-680 멈춤 판정(stall_sec). `rg -n 'def .*terminal|alive' scripts/th-job-liveness.py src/team_harness/worker_digest.py`.

## 작업 위치
`git -C /home/hs/rang/team-harness worktree add /home/hs/orca/workspaces/team-harness/donelinger -b task/done-linger origin/main` 후 `uv sync --extra dev` 1회 허용.

## 할 일
1. 원인 규명(WORKLOG `원인:`, 파일:줄): 왜 위 3건은 DONE이 있는데 완료 판정에 안 들어가고 멈춤 판정까지 갔는지(liveness 보정이 DONE을 덮는지, 어느 감시 경로(.th_daemon/th-watch)인지), 그리고 codex 워커 프로세스가 마지막 메시지·`tokens used` 출력 뒤에도 10분 이상 살아 있는 이유(`~/.team-harness/watch/events.log`에서 해당 작업 줄만 `rg`, 해당 작업 폴더의 codex.log `tail`, th-launch.sh의 codex 실행 방식 — stdin·파이프·자식 프로세스).
2. 수정(확정 설계):
   - DONE이 있고 워커 마지막 출력 이후 변화가 없으면 **멈춤 사건을 내지 않는다**. 대신 DONE을 본 뒤 60초 유예 후 "완료" 사건을 낸다(검수 흐름은 기존 그대로).
   - 유예 뒤에도 워커 프로세스 그룹이 남아 있으면 그 그룹만 종료(`worker_relaunch.terminate_worker_group` 재사용)하고 events.log에 "DONE 후 잔류 프로세스 종료" 기록. 분리된 긴 job(`distinct_job_alive`)은 건드리지 않는다.
   - 잔류 원인이 th-launch 실행 방식(예: stdin 열림)이면 그것도 고친다.
   - 감시 경로 둘(.th_daemon, th-watch) 모두 같은 규칙.
3. 테스트(`src/tests/test_scripts_shell.py` 또는 새 파일, 4개 이상): DONE+살아 있는 프로세스+변화 없음 → 멈춤 없음·유예 후 완료, 유예 전엔 완료 안 냄, 잔류 그룹 종료 호출, 분리 job 살아 있으면 기존 동작 유지.
4. `design/decisions.md`의 관련 결정(`rg -n 'stall|멈춤' design/decisions.md`로 찾은 항목) 개정 한 줄, CHANGELOG 한 줄.
5. 검증 `uv run ruff check src/ && uv run ruff format --check src/ && uv run pyright src/ && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH= uv run pytest src/tests/ -q -p pytest_asyncio.plugin | tail -n 20` → 커밋(실제 내용대로) → fetch → main==origin/main → `merge --ff-only` → `push origin main`. force·태그 금지. 병합 후 worktree 제거·`branch -d`.

## 검수
```bash
cd /home/hs/rang/team-harness && git fetch -q origin && test "$(git rev-parse main)" = "$(git rev-parse origin/main)" && git log -3 --format=%b%s origin/main | grep -qi 'linger\|잔류\|stall' && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH= uv run pytest src/tests/ -q -p pytest_asyncio.plugin 2>&1 | tail -n 1 | grep -q passed && echo ok
```

## 규칙
설치 금지(uv sync 1회 제외). 출력은 tail/-q로 줄인다. 고칠 방법을 아는 오류는 고치고 계속. 가드 거부는 나눠 재시도. 같은 수정 방법 2회 실패 시 FAILED. 애매하면 보수적으로 고르고 WORKLOG `결정:`에 적고 계속. 할 일 1~5를 모두 끝냈을 때만 DONE.

## 수정 지시 (2차)
- 1차 중단 원인(코디네이터 지시서 오류): `~/.team-harness/watch/events.log`와 다른 작업 폴더는 영역 가드가 막는다. 필요한 근거를 이 폴더에 복사했다: `evidence_events.log`(세 작업의 events.log 줄), `evidence_<작업>_codex_tail.log`(codex.log 끝 30줄), `evidence_<작업>_STATUS`. 원래 경로는 읽지 않는다.
- 1차 진행분이 worktree `donelinger`에 있으면 재사용. 할 일 1~5 전부.
