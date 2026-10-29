---
kind: code
task_key: job_tag_leak_20261029
follow_up: normal
---
# 테스트가 띄운 가짜 codex가 실제 작업의 TH_JOB_TASK를 물려받아 "살아 있음"으로 오판 — 수정, 병합 후 push

규칙 문서·.claude/는 읽지 않는다. 기록은 이 폴더 STATUS·WORKLOG.md에만. 작업 트리 전체와 이 작업 폴더는 쓰기 허용. 시간 제한 없음. 할 일 1~5를 전부 끝내야 DONE.

## 원인 (코디네이터 확인 완료 — 다시 조사하지 않는다)
- 10-29 DONE 후 "멈춤" 오탐 4건(auto_continue, rollover_paths, archive_paths, done_linger)의 원인.
- 워커가 검증을 `scripts/th-job-start.sh`로 돌리면 `export TH_JOB_TASK="$task"`(th-job-start.sh:12)가 pytest에 상속된다.
- `src/tests/test_scripts_shell.py:164` 부근 테스트(`test_th_launch_appends_determi…`, task `task_no_step_recovery`)가 실제 `th-launch.sh`로 가짜 codex(`<tmp>/bin/codex exec …`)를 setsid로 띄우고, 이 프로세스가 **종료하지 않고 남는다**. 실행 시 `ps`로 24개 잔류를 확인(최장 3시간), 각 environ에 `TH_JOB_TASK=/home/hs/rang/team-harness/_outputs/<실제 작업>`과 `TH_WORKER_OUTPUT_DIR=/tmp/pytest-of-hs/…/task_no_step_recovery`.
- `scripts/th-job-liveness.py:26`은 environ의 `TH_JOB_TASK=<task>` 태그로 생존을 판정 → 실제 작업이 계속 살아 있는 것으로 보여 DONE 완료 판정이 막히고 멈춤 사건이 났다. 코디네이터가 잔류 프로세스를 kill하자 14:45에 세 작업의 "완료" 사건이 즉시 나왔다.

## 작업 위치
`git -C /home/hs/rang/team-harness worktree add /home/hs/orca/workspaces/team-harness/jobtag -b task/job-tag-leak origin/main` 후 `uv sync --extra dev` 1회 허용.

## 할 일
1. th-launch: 워커 실행 환경에서 상속된 `TH_JOB_TASK`를 제거한다(워커는 job이 아니다). 다른 상속 job 변수가 있으면 같이.
2. th-job-liveness: 태그가 맞아도 그 프로세스의 `TH_WORKER_OUTPUT_DIR`가 있고 대상 작업 폴더와 다르면(다른 작업/테스트 tmp) 생존으로 세지 않는다.
3. 테스트 격리: `src/tests/conftest.py`에 autouse fixture로 `TH_JOB_TASK`, `TH_WORKER_OUTPUT_DIR`, `TH_WORKER_ALLOW_PATHS`, `TH_ROLE`, `TH_SESSION_KEY`, `ORCA_PANE_KEY`를 delenv(테스트가 필요하면 각자 setenv). 해당 th-launch 테스트의 가짜 codex는 즉시 종료하게 하고, 테스트 종료 시 그 테스트가 띄운 프로세스 그룹을 정리(teardown). 다른 shell 테스트 중 setsid로 남는 가짜 워커가 있으면 같이 정리.
4. 테스트 추가 4개 이상: th-launch가 TH_JOB_TASK를 넘기지 않음, liveness가 다른 TH_WORKER_OUTPUT_DIR 프로세스를 무시, 같은 폴더면 여전히 생존, **전체 pytest 실행 뒤 `/tmp/pytest-of-*/…/bin/codex` 잔류 프로세스 0개**(검증 단계에서 `ps -eo cmd | grep -c 'pytest-of-.*/bin/codex exec'`로 확인해 WORKLOG에 기록).
5. TH-D38(th-job-start) 또는 생존 판정 관련 결정에 개정 한 줄, CHANGELOG 한 줄. 검증 `uv run ruff check src/ && uv run ruff format --check src/ && uv run pyright src/ && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH= uv run pytest src/tests/ -q -p pytest_asyncio.plugin | tail -n 20` → 커밋(실제 내용대로) → fetch → main==origin/main → `merge --ff-only` → `push origin main`. force·태그 금지. 병합 후 worktree 제거·`branch -d`.

## 검수
```bash
cd /home/hs/rang/team-harness && git fetch -q origin && test "$(git rev-parse main)" = "$(git rev-parse origin/main)" && git log -3 --format=%s origin/main | grep -qi 'TH_JOB_TASK\|job tag\|liveness' && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH= uv run pytest src/tests/test_scripts_shell.py -q -p pytest_asyncio.plugin 2>&1 | tail -n 1 | grep -q passed && sleep 3 && test "$(ps -eo cmd | grep -v grep | grep -c 'pytest-of-.*/bin/codex exec')" = 0 && echo ok
```

## 규칙
설치 금지(uv sync 1회 제외). 출력은 tail/-q로 줄인다. 고칠 방법을 아는 오류는 고치고 계속. 가드 거부는 나눠 재시도. 같은 수정 방법 2회 실패 시 FAILED. 애매하면 보수적으로 고르고 WORKLOG `결정:`에 적고 계속. 할 일 1~5를 모두 끝냈을 때만 DONE.
