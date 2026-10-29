---
kind: code
task_key: zone_pattern_20261029
follow_up: normal
---
# 영역 가드: rg/grep/sed 검색 패턴 문자열을 경로로 판정하는 오탐 수정, 병합 후 push

규칙 문서·.claude/는 읽지 않는다. 기록은 이 폴더 STATUS·WORKLOG.md에만. 작업 트리 전체와 이 작업 폴더는 쓰기 허용. 시간 제한 없음. 할 일 1~4를 전부 끝내야 DONE.

## 재현 (10-29 16:34 brief_check_fp 워커 거부)
`rg -n 'events.log|_outputs/|archive.last|\.claude/archive' <자기 작업 폴더>/samples/*/task.md` → 패턴 안의 `\.claude/archive`를 경로로 보고 deny. 대상 파일은 자기 작업 폴더라 허용돼야 한다.
관련 코드: `src/team_harness/coordinator_zone.py`(`coordinator_paths_in_command`, 5f735e0에서 heredoc·patch 본문 제외 추가).

## 작업 위치
`git -C /home/hs/rang/team-harness worktree add /home/hs/orca/workspaces/team-harness/zonepat -b task/zone-pattern origin/main` 후 `uv sync --extra dev` 1회 허용.

## 할 일
1. `rg`, `grep`(`egrep`/`fgrep`), `ag`, `sed`, `awk`, `perl -ne`류에서 **패턴 인자**(옵션이 아닌 첫 위치 인자, `-e PAT`/`--regexp PAT`/`-f`가 아닌 경우, sed/awk의 스크립트 인자)는 경로 판정에서 제외하고, 나머지 위치 인자(파일·디렉터리)만 판정. 파이프·`;`·`&&`로 나뉜 각 명령 단위로 적용. 애매하면 기존대로 판정(보수적).
2. 테스트 6개 이상(가짜 경로는 tmp_path, 테스트 문자열의 보호 경로는 조각으로 이어 붙여도 됨): 위 재현 허용, `rg PAT <남의 .claude 파일>` deny 유지, `grep -e PAT file`, `sed -n 's/x/y/p' file`, `awk '/pat/' file`, 파이프 결합.
3. TH-D43 개정 한 줄, CHANGELOG 한 줄(문장에 보호 경로 문자열 대신 "코디네이터 기록 폴더").
4. 검증 `uv run ruff check src/ && uv run ruff format --check src/ && uv run pyright src/ && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH= uv run pytest src/tests/ -q -p pytest_asyncio.plugin | tail -n 20` → 커밋(실제 내용대로) → fetch → main==origin/main → `merge --ff-only` → `push origin main`. ff 불가면 rebase·재검증(다른 워커 동시 진행). push가 pre-push canary 훅에 막히면 그 출력대로 고친다. force·태그 금지. 병합 후 worktree 제거·`branch -d`.

## 검수
```bash
cd /home/hs/rang/team-harness && git fetch -q origin && test "$(git rev-parse main)" = "$(git rev-parse origin/main)" && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH= uv run pytest src/tests/test_coordinator_zone_guard.py -q -p pytest_asyncio.plugin | tail -n 1 | grep -q passed && echo ok
```

## 규칙
설치 금지(uv sync 1회 제외). 출력은 tail/-q로 줄인다. 고칠 방법을 아는 오류는 고치고 계속. 가드 거부는 나눠 재시도. 같은 수정 방법 2회 실패 시 FAILED. 애매하면 보수적으로 고르고 WORKLOG `결정:`에 적고 계속. 할 일 1~4를 모두 끝냈을 때만 DONE.
