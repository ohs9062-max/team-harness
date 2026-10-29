---
kind: code
task_key: auto_continue_20261029
follow_up: normal
context_budget: 120000
---
# 워커 자기 중단 자동 계속(모든 실행 경로), 병합 후 push

규칙 문서·.claude/는 읽지 않는다. 기록은 이 폴더 STATUS·WORKLOG.md에만. 작업 트리 전체와 이 작업 폴더는 쓰기 허용. 다른 `_outputs/*`는 읽지 않는다.
설계는 코디네이터가 확정했다. 할 일 1~6을 전부 끝내야 DONE이다. 일부만 하고 DONE/FAILED를 쓰지 않는다.

## 배경
10-29 codex 워커(medium·high 모두)가 큰 작업에서 약 4분·3~5만 토큰에 한 단계만 하고 `FAILED: INCOMPLETE …` 또는 남은 항목을 적은 `DONE`으로 끝냈다(rollover_fix 3회, rollover_enforce, robot3 det_ditchw 등). worker_suffix 문구(4afb4cd)로는 막지 못했다. 코디네이터가 매번 알아채고 다시 띄우느라 시간이 샌다.

## 이미 된 것
- 6ce9e33 `worker_context.rollover_decision`, 롤오버 watchd 경로(rollover_watchd 작업, 이 작업 직전 병합)에서 만든 **공통 재실행 헬퍼** — `rg -n 'def .*relaunch|def .*rollover' src/team_harness/`로 찾아 재사용한다. 새 재실행 함수를 만들지 않는다.

## 작업 위치
`git -C /home/hs/rang/team-harness worktree add /home/hs/orca/workspaces/team-harness/autocont -b task/auto-continue origin/main` 후 `uv sync --extra dev` 1회 허용.

## 확정 설계
1. 공통 판정 `classify_stop(task_dir) -> "ok" | "self_stop" | "allowed_fail"` (새 모듈 `src/team_harness/stop_policy.py`):
   - self_stop: (a) DONE이 있는데 검수(verify.sh/VERIFY_FAILED) 실패, (b) FAILED 사유가 허용 사유(쿼터·rate limit·용량, 실제 권한/인증·라이선스, 같은 수정 2회 실패, 설정 수정 3회, 시간 제한, 다른 방식 2회 가드 거부)에 해당하지 않음 — 특히 `INCOMPLETE`, (c) DONE 본문에 남은 작업 표지(`남음|남은|remaining|INCOMPLETE|Partial|미구현|not implemented|일부만`)가 있음.
   - allowed_fail: 위 허용 사유. 쿼터 계열은 기존 잠금 동작 그대로.
   - 애매하면 self_stop(계속 쪽).
2. self_stop이면 자동 계속: 공통 재실행 헬퍼로 같은 작업 폴더 재실행, task.md 끝에 `## 자동 계속 (n차)` 절을 덧붙인다(직전 FAILED/DONE 사유 원문 + "남은 항목을 이어서 끝까지 하라, 이미 된 것은 다시 하지 마라" + 검수 실패면 검수 출력 tail 20줄). TH-D32대로 입력 맥락 ≤90000이면 세션 resume, 아니면 요약 재실행. 자동 계속은 작업당 최대 2회, th-launch 재시도 상한에 세지 않는다. 2회 넘으면 inbox에 "자동 계속 소진 — 작업 분할 필요"로 보고. 매 자동 계속도 inbox 사건 "자동 계속(n/2): 사유".
   - 쿼터 잠금·사용자 kill(`/kill`, KILLED 표지)·검수 없는 research 작업의 FAILED는 자동 계속하지 않는다.
3. 적용 경로(AGENTS 규칙 4): th-launch+th-watchd(완료/실패 사건 처리 지점, `scripts/th-watch-lib.sh` 라우팅), run/repl(`tools/agent_tools.py` 워커 종료 처리), protocol(mode_c 러너, mode_a/b 공유). 세 곳 모두 `classify_stop` 하나와 공통 재실행 헬퍼를 호출.
4. 테스트 `src/tests/test_auto_continue.py` 10개 이상: 분류 표(INCOMPLETE FAILED, 허용 사유 각 계열, 남은 항목 DONE, 검수 실패 DONE, 정상 DONE), 2회 상한, 재시도 상한 미계산, kill 제외, 세 경로가 같은 함수를 호출(monkeypatch).
5. `design/decisions.md`에 새 결정 TH-D44 "자기 중단 자동 계속"(배경 수치, 판정 표, 경로별 붙는 지점 표, 상한), CHANGELOG 한 줄. worker_suffix는 건드리지 않는다.
6. 검증: `uv run ruff check src/ && uv run ruff format --check src/ && uv run pyright src/ && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH= uv run pytest src/tests/ -q -p pytest_asyncio.plugin | tail -n 20` → 커밋(실제 내용대로) → fetch → main==origin/main → `merge --ff-only` → `push origin main`. ff 불가면 rebase·재검증. force·태그 금지. 병합 후 worktree 제거·`branch -d`.

## 검수
```bash
cd /home/hs/rang/team-harness && git fetch -q origin && test "$(git rev-parse main)" = "$(git rev-parse origin/main)" && test "$(grep -c '^def test_\|^async def test_' src/tests/test_auto_continue.py)" -ge 10 && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH= uv run pytest src/tests/test_auto_continue.py -q -p pytest_asyncio.plugin | tail -n 2 | grep -q passed && grep -q 'classify_stop' src/team_harness/tools/agent_tools.py && grep -rq 'classify_stop' src/team_harness/protocol/ && grep -rq 'classify_stop\|stop_policy' scripts/ src/team_harness/watch_inspect.py && grep -q 'TH-D44' design/decisions.md && echo ok
```

## 규칙
설치 금지(uv sync 1회 제외). 출력은 tail/-q로 줄인다. 고칠 방법을 아는 오류는 고치고 계속. 가드 거부는 나눠 재시도. 같은 수정 방법 2회 실패 시 FAILED. 애매하면 보수적으로 고르고 WORKLOG `결정:`에 적고 계속. 할 일 1~6을 모두 끝냈을 때만 DONE.

## 수정 지시 (2차)
- 1차 중단 원인: worker_suffix의 "time limit (default smoke 15 min)"을 이 작업의 시간 제한으로 적용했다. **이 작업에는 시간 제한이 없다.** 15분 기본값은 스모크 실험 실행에만 해당한다. 시간을 이유로 멈추지 않는다.
- 1차 결과는 `/home/hs/orca/workspaces/team-harness/autocont`에 미커밋으로 있다(stop_policy.py, test_auto_continue.py, watch 경로 연결). 재사용하고 다시 만들지 않는다. 먼저 이것을 `wip:` 커밋으로 브랜치에 남긴 뒤 진행(병합 전 하나로 합쳐도 됨).
- 설계 확정(1차 `문제:`에 대한 답): run/repl·protocol은 th-launch 작업 폴더가 없으므로 그 경로 **자신의 재실행**을 쓴다. 워커가 끝나면 `classify_stop`에 해당 워커 출력 폴더(없으면 결과 텍스트를 받는 변형 `classify_stop_text`)를 넘기고, self_stop이면 같은 에이전트 설정으로 그 경로의 spawn 함수를 다시 호출한다. 프롬프트 끝에 "## 자동 계속 (n차)" 절(직전 사유 원문 + 남은 항목 이어서 끝까지)을 붙이고, 그 워커의 완료 공개는 최종 시도 뒤에만 한다. 작업당 2회 상한은 같다. 이 경로들은 worker_relaunch.py를 쓰지 않아도 된다(판정 함수만 공유).
- 할 일 0(추가): `design/user_config/worker_suffix.md`의 해당 문구를 "the time limit given in the instruction (smoke experiment runs default to 15 min; no limit otherwise)"로 고친다.
- 검수에 추가된 것 없음. 할 일 0~6 전부 끝냈을 때만 DONE.
