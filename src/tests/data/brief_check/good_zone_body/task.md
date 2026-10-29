---
kind: code
task_key: zone_body_20261029
follow_up: normal
---
# 영역 가드: heredoc·apply_patch 본문 텍스트를 경로로 판정하는 오탐 수정, 병합 후 push

규칙 문서·.claude/는 읽지 않는다. 기록은 이 폴더 STATUS·WORKLOG.md에만. 작업 트리 전체와 이 작업 폴더는 쓰기 허용. 시간 제한 없음. 할 일 1~4를 전부 끝내야 DONE.

## 재현 (10-29 10:47 doc_archive 워커 중단)
워커가 자기 worktree의 `design/decisions.md`에 설계 문서 문장을 덧붙였는데, 문장 속 `.claude/archive로` 같은 **문서 텍스트**를 가드가 코디네이터 영역 경로로 판정해 거부:
- `cat >> design/decisions.md <<'EOF' … <owner>/.claude/archive로 옮긴다 … EOF` → deny
- apply_patch `*** Begin Patch / *** Update File: design/decisions.md / +… .claude/archive …` → deny
`src/team_harness/coordinator_zone.py`에는 heredoc·patch 본문 처리 없음(`rg -n '<<|Begin Patch' src/team_harness/coordinator_zone.py` 결과 없음).

## 작업 위치
`git -C /home/hs/rang/team-harness worktree add /home/hs/orca/workspaces/team-harness/zonebody -b task/zone-body origin/main` 후 `uv sync --extra dev` 1회 허용.

## 할 일
1. `coordinator_zone.py` 경로 추출(`coordinator_paths_in_command` 및 apply_patch 판정 지점, hook `scripts/hooks/coordinator_zone_guard.py`·`~/.claude/hooks/coordinator_zone_guard.py`가 부르는 함수)에서:
   - apply_patch: `*** Add File:` / `*** Update File:` / `*** Delete File:` / `*** Move to:` 헤더의 경로만 판정, 본문(`+`/`-`/` ` 줄)은 무시.
   - heredoc(`<<EOF`, `<<'EOF'`, `<<-EOF`, 임의 구분자): 본문 줄은 경로 판정에서 제외, 리다이렉트 대상(`>`/`>>`/`tee`)과 heredoc 앞 명령 부분만 판정. 단 heredoc을 **실행**하는 경우(`bash <<`, `sh <<`, `python <<`, `python3 -`, `node <<`)는 본문도 기존대로 판정(보수적).
2. 테스트(기존 `src/tests/test_coordinator_zone_guard.py`에 추가, 8개 이상, 가짜 경로는 tmp_path): 위 두 재현은 허용, patch 헤더가 남의 `.claude/` 파일이면 deny, `cat > <남의 .claude 파일> <<EOF` deny, `python3 - <<PY` 본문의 남의 경로 deny 유지, `<<-EOF` 구분자 처리.
   테스트 파일 수정은 apply_patch로 하되, 이 수정 전 가드에 본문이 막히면 테스트 문자열의 `.claude`를 `"." + "claude"`처럼 쪼개 쓴다.
3. `design/decisions.md` TH-D43 개정 단락 끝에 한 줄, CHANGELOG 한 줄. 문장에 `.claude/` 경로 문자열을 쓰지 말고 "코디네이터 기록 폴더"로 쓴다.
4. 검증 `uv run ruff check src/ && uv run ruff format --check src/ && uv run pyright src/ && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH= uv run pytest src/tests/ -q -p pytest_asyncio.plugin | tail -n 20` → 커밋 `fix(zone): ignore heredoc and patch body text` → fetch → main==origin/main → `merge --ff-only` → `push origin main`. ff 불가면 rebase·재검증. force·태그 금지. 병합 후 worktree 제거·`branch -d`.

## 검수
```bash
cd /home/hs/rang/team-harness && git fetch -q origin && test "$(git rev-parse main)" = "$(git rev-parse origin/main)" && git log -6 --format=%s origin/main | grep -q 'heredoc and patch body' && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH= uv run pytest src/tests/test_coordinator_zone_guard.py -q -p pytest_asyncio.plugin | tail -n 2 | grep -q passed && echo ok
```

## 규칙
설치 금지(uv sync 1회 제외). 출력은 tail/-q로 줄인다. 고칠 방법을 아는 오류는 고치고 계속. 가드 거부는 나눠 재시도. 같은 수정 방법 2회 실패 시 FAILED. 애매하면 보수적으로 고르고 WORKLOG `결정:`에 적고 계속. 할 일 1~4를 모두 끝냈을 때만 DONE.
