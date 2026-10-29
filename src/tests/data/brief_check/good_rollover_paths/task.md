---
kind: code
task_key: rollover_paths_20261029
follow_up: normal
context_budget: 120000
---
# 롤오버 강제 동작 — run/repl(agent_tools)·protocol(mode_c) 경로 연결, 병합 후 push

규칙 문서·.claude/는 읽지 않는다. 기록은 이 폴더 STATUS·WORKLOG.md에만. 작업 트리 전체와 이 작업 폴더는 쓰기 허용. 다른 `_outputs/*`는 읽지 않는다.
설계 확정. 시간 제한 없음. 할 일 1~5를 전부 끝내야 DONE이다.

## 이미 된 것 (origin/main)
- 6ce9e33 `worker_context.rollover_decision` (continue/rollover/split_needed).
- 0bda510 watchd 경로 롤오버(`worker_relaunch.py`).
- 9f5a0fd 자동 계속: agent_tools·mode_c에 "같은 에이전트 설정으로 그 경로의 spawn을 다시 호출 + 프롬프트 끝 절 추가, 완료 공개는 최종 시도 뒤" 재실행 구조가 이미 있다(`rg -n 'classify_stop|자동 계속|auto_continue' src/team_harness/tools/agent_tools.py src/team_harness/protocol/mode_c.py src/team_harness/agents/manager.py`). **이 재실행 구조를 재사용**한다. 새 재실행 함수를 만들지 않는다.

## 작업 위치
`git -C /home/hs/rang/team-harness worktree add /home/hs/orca/workspaces/team-harness/rollpaths -b task/rollover-paths origin/main` 후 `uv sync --extra dev` 1회 허용.

## 할 일
1. agent_tools(run/repl): 워커 대기 중 기존 대기 주기마다 그 워커의 맥락 측정(worker_context의 측정 함수, 세션 ID는 3f85a19 방식) → `rollover_decision`. rollover면 그 워커 프로세스 종료(manager의 기존 kill 경로) → 9f5a0fd 재실행 구조로 다시 호출, 덧붙이는 절은 `## 롤오버 (n/3)`: 직전 WORKLOG.md/STATUS 끝 30줄 요약 + "이미 된 것은 다시 하지 말고 이어서". 자동 계속 2회 상한과 별개로 롤오버 3회 상한. split_needed면 콘솔/결과에 "작업 분할 필요" 표시만.
2. protocol mode_c(mode_a/b 공유 러너): 같은 방식.
3. 테스트 `src/tests/test_rollover_paths.py` 6개 이상: 두 경로 각각 초과+경계 → 종료+재호출(monkeypatch), 경계 아님 → 유지, split_needed → 표시만, 3회 상한, 자동 계속 상한과 독립.
4. TH-D32 개정 단락에 "run/repl·protocol 경로 구현됨" 한 줄 + 경로별 붙는 지점 표 갱신, CHANGELOG 한 줄.
5. 검증 `uv run ruff check src/ && uv run ruff format --check src/ && uv run pyright src/ && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH= uv run pytest src/tests/ -q -p pytest_asyncio.plugin | tail -n 20` → 커밋(실제 내용대로) → fetch → main==origin/main → `merge --ff-only` → `push origin main`. 다른 워커(doc_archive)가 cli.py·실행 시작부를 동시 수정 중 — ff 불가면 rebase·재검증. force·태그 금지. 병합 후 worktree 제거·`branch -d`.

## 검수
```bash
cd /home/hs/rang/team-harness && git fetch -q origin && test "$(git rev-parse main)" = "$(git rev-parse origin/main)" && test "$(grep -c '^def test_\|^async def test_' src/tests/test_rollover_paths.py)" -ge 6 && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH= uv run pytest src/tests/test_rollover_paths.py -q -p pytest_asyncio.plugin | tail -n 2 | grep -q passed && grep -q rollover_decision src/team_harness/tools/agent_tools.py && grep -rq rollover_decision src/team_harness/protocol/ && echo ok
```

## 규칙
설치 금지(uv sync 1회 제외). 출력은 tail/-q로 줄인다. 고칠 방법을 아는 오류는 고치고 계속. 가드 거부는 나눠 재시도. 같은 수정 방법 2회 실패 시 FAILED. 애매하면 보수적으로 고르고 WORKLOG `결정:`에 적고 계속. 할 일 1~5를 모두 끝냈을 때만 DONE.
