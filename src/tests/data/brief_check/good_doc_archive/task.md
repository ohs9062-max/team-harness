---
kind: code
task_key: doc_archive_20261029
follow_up: normal
context_budget: 120000
---
# 워크스페이스 문서·작업 폴더 자동 이관(inbox 3일, 나머지 7일), 병합 후 push

규칙 문서·.claude/는 읽지 않는다(아래 명시한 코드 파일만). 기록은 이 폴더 STATUS·WORKLOG.md에만. 작업 트리 전체와 이 작업 폴더는 쓰기 허용. 다른 `_outputs/*`는 읽지 않는다.
설계는 사용자·코디네이터가 확정했다. 시간 제한 없음. 할 일 1~6을 전부 끝내야 DONE이다.

## 현재 (코디네이터 확인)
- `scripts/th-launch.sh:17` `rotate_journal_if_needed`: work_log.md를 날짜별 `work_log_YYYYMMDD.md`로 옮김(보관본은 쌓이기만 함).
- `src/team_harness/worker_digest.py:605` workers.md 200줄 초과분 → workers_archive.md(무한 증가).
- inbox.md, 작업 폴더, `~/.team-harness/watch/events.log`(12,858줄)는 정책 없음.

## 작업 위치
`git -C /home/hs/rang/team-harness worktree add /home/hs/orca/workspaces/team-harness/docarch -b task/doc-archive origin/main` 후 `uv sync --extra dev` 1회 허용.

## 확정 설계 — 지우지 않고 옮기기만
새 모듈 `src/team_harness/workspace_archive.py`, 함수 `archive_workspace(owner_root, now, dry_run=False) -> list[Move]`. 기준:
1. inbox: `<owner>/.claude/inbox.md` 사건 중 **3일** 지났고 모든 세션 cursor(`.inbox_cursor`, `.inbox_cursor.d/*`)가 이미 지난 것 → `<owner>/.claude/archive/inbox_YYYYMM.md`에 덧붙임. inbox 기존 잠금(`rg -n 'inbox_cursor|flock|lock' src/team_harness/ scripts/th-inbox*.sh scripts/th-watch-lib.sh`로 확인) 아래에서 하고, cursor가 바이트/줄 위치 기반이면 잘라낸 만큼 모든 cursor를 같은 잠금 안에서 보정한다. `inbox.routes`도 같은 방식으로 정리.
2. 작업 폴더: watch 레지스트리(`~/.team-harness/watch/tasks.txt` 등)에 등록된 이 owner의 작업 폴더 중 DONE 있음 + VERIFIED 있음 + FAILED/VERIFY_FAILED 없음 + 살아 있는 워커 프로세스 없음 + 폴더 안 최신 mtime이 **7일** 지남 + `.th_worktree`가 가리키는 worktree가 남아 있으면 미커밋 변경 없음 → 같은 부모의 `_archive/YYYYMM/<이름>`으로 이동하고 레지스트리 경로 갱신(또는 등록 해제). 등록 안 된 폴더, 프로젝트 정식 산출물 경로(`dataset/`, `outputs/checkpoints/` 등)는 절대 건드리지 않는다.
3. `.claude/work_log_*.md`, `workers_archive.md`: **7일** 지난 것 → `<owner>/.claude/archive/`.
4. `~/.team-harness/watch/events.log`: 5MB 초과면 `events_YYYYMM.log`로 분리(watch 전역, owner 무관, 하루 한 번).
5. 모든 이동은 `<owner>/.claude/archive/archive.log`에 `시각 | 원경로 → 새경로` 한 줄. 이름 충돌 시 접미사 `.HHMMSS`.
- 실행 지점(AGENTS 규칙 4): th-watchd가 하루 한 번(마지막 실행 시각 파일 `~/.team-harness/watch/archive.last`) 등록된 모든 owner에 대해 호출. 실패는 `2>/dev/null`로 숨기지 말고 events.log에 기록. 수동 실행용 CLI `th archive [--dry-run] [--owner PATH]`(cli.py)도 같은 함수 호출. watchd가 없는 run/repl·protocol은 해당 실행 시작 시 같은 함수를 하루 한 번 기준으로 호출(같은 archive.last 공유).
- 다른 워커(auto_continue)가 `scripts/th-watch-lib.sh`, `th-watchd.sh`를 동시에 수정 중이다. watchd 쪽 변경은 호출 한두 줄로 최소화하고, 병합 시 ff 불가면 rebase·재검증.

## 할 일
1. 위 모듈 구현.
2. watchd 하루 한 번 호출, `th archive` CLI, run/repl·protocol 시작 시 호출.
3. 테스트 `src/tests/test_workspace_archive.py` 12개 이상(tmp_path): inbox 3일·cursor 미통과 보존·cursor 보정, 작업 폴더 7일·VERIFIED 필수·FAILED 제외·살아 있는 pid 제외·미커밋 worktree 제외·미등록 폴더 제외, journal 7일, events.log 5MB, dry-run은 이동 없음, 하루 한 번(archive.last).
4. `design/decisions.md` 새 결정 TH-D45 "워크스페이스 문서 이관"(기준 표, 경로별 실행 지점 표, 지우지 않는 원칙), CHANGELOG 한 줄.
5. 검증: `uv run ruff check src/ && uv run ruff format --check src/ && uv run pyright src/ && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH= uv run pytest src/tests/ -q -p pytest_asyncio.plugin | tail -n 20`.
6. 커밋(실제 내용대로) → fetch → main==origin/main → `merge --ff-only` → `push origin main`. force·태그 금지. 병합 후 worktree 제거·`branch -d`. **실제 워크스페이스에 대해 이관을 실행하지 않는다**(dry-run만 허용, 결과를 WORKLOG에 붙임).

## 검수
```bash
cd /home/hs/rang/team-harness && git fetch -q origin && test "$(git rev-parse main)" = "$(git rev-parse origin/main)" && test "$(grep -c '^def test_\|^async def test_' src/tests/test_workspace_archive.py)" -ge 12 && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH= uv run pytest src/tests/test_workspace_archive.py -q -p pytest_asyncio.plugin | tail -n 2 | grep -q passed && grep -q 'workspace_archive\|th archive' scripts/th-watchd.sh scripts/th-watch-lib.sh && grep -q 'TH-D45' design/decisions.md && echo ok
```

## 규칙
설치 금지(uv sync 1회 제외). 출력은 tail/-q로 줄인다. 고칠 방법을 아는 오류는 고치고 계속. 가드 거부는 나눠 재시도. 같은 수정 방법 2회 실패 시 FAILED. 애매하면 보수적으로(옮기지 않는 쪽) 고르고 WORKLOG `결정:`에 적고 계속. 할 일 1~6을 모두 끝냈을 때만 DONE.

## 수정 지시 (2차)
- 1차 중단 원인(가드 오탐): heredoc·apply_patch 본문의 `.claude/archive` 문장을 경로로 판정. 5f735e0에서 수정·병합됨 — worktree를 `git rebase origin/main`(먼저 미커밋 변경을 `wip:` 커밋) 후 이어서 진행.
- 1차 결과(`/home/hs/orca/workspaces/team-harness/docarch`의 workspace_archive.py, watchd·cli.py 변경)는 재사용, 다시 만들지 않는다. WORKLOG 1차 `남음:` 항목부터 이어서 할 일 1~6 전부.
