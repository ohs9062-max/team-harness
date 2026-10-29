---
kind: code
task_key: archive_paths_20261029
follow_up: normal
---
# 문서 이관(TH-D45)을 run/repl·protocol 시작 시에도 호출, 병합 후 push

규칙 문서·.claude/는 읽지 않는다. 기록은 이 폴더 STATUS·WORKLOG.md에만. 작업 트리 전체와 이 작업 폴더는 쓰기 허용. 시간 제한 없음. 할 일 1~4를 전부 끝내야 DONE.
**실제 워크스페이스에 대해 이관을 실행하지 않는다.** 테스트는 TH_WATCH_DIR·owner를 tmp_path로.

## 배경
82f954a: `src/team_harness/workspace_archive.py`가 watchd(하루 한 번, `~/.team-harness/watch/archive.last` 공유)와 `th archive`에서만 호출된다. run/repl(`harness.py` TeamHarness.run 또는 cli.py run/repl 시작부)·protocol(mode_a/b/c 시작부) 호출 누락(AGENTS 규칙 4).

## 작업 위치
`git -C /home/hs/rang/team-harness worktree add /home/hs/orca/workspaces/team-harness/archpaths -b task/archive-paths origin/main` 후 `uv sync --extra dev` 1회 허용.

## 할 일
1. `rg -n 'def archive|archive.last|def main' src/team_harness/workspace_archive.py`로 하루 한 번 판정 진입점을 찾아(없으면 `archive_if_due()` 하나로 뺀다 — watchd가 쓰는 것과 같은 판정) TeamHarness.run 시작(SDK·th run·repl 공통 지점)과 protocol 공통 시작 지점에서 호출. 예외는 삼키지 말고 events.log에 기록 후 실행은 계속(이관 실패가 작업을 막지 않음). 백그라운드 스레드 불필요 — 빠른 판정 후 due일 때만 실행.
2. 테스트 `src/tests/test_archive_paths.py` 4개 이상: run 시작이 호출, protocol 시작이 호출, 이미 오늘 실행됐으면 아무것도 안 함, 이관 예외가 실행을 막지 않음.
3. TH-D45 경로 표 갱신, CHANGELOG 한 줄.
4. 검증 `uv run ruff check src/ && uv run ruff format --check src/ && uv run pyright src/ && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH= uv run pytest src/tests/ -q -p pytest_asyncio.plugin | tail -n 20` → 커밋(실제 내용대로) → fetch → main==origin/main → `merge --ff-only` → `push origin main`. force·태그 금지. 병합 후 worktree 제거·`branch -d`.

## 검수
```bash
cd /home/hs/rang/team-harness && git fetch -q origin && test "$(git rev-parse main)" = "$(git rev-parse origin/main)" && test "$(grep -c '^def test_\|^async def test_' src/tests/test_archive_paths.py)" -ge 4 && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH= uv run pytest src/tests/test_archive_paths.py -q -p pytest_asyncio.plugin | tail -n 2 | grep -q passed && grep -rq workspace_archive src/team_harness/harness.py src/team_harness/protocol/ && echo ok
```

## 규칙
설치 금지(uv sync 1회 제외). 출력은 tail/-q로 줄인다. 고칠 방법을 아는 오류는 고치고 계속. 가드 거부는 나눠 재시도. 같은 수정 방법 2회 실패 시 FAILED. 애매하면 보수적으로 고르고 WORKLOG `결정:`에 적고 계속. 할 일 1~4를 모두 끝냈을 때만 DONE.
