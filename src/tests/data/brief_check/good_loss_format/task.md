---
kind: code
task_key: loss_format_20261029
follow_up: normal
---
# `th report` 표 가독성·원인 분류 개선, 병합 후 push

규칙 문서·.claude/는 읽지 않는다. 기록은 이 폴더 STATUS·WORKLOG.md에만. 작업 트리 전체와 이 작업 폴더는 쓰기 허용. 시간 제한 없음. 할 일 1~4를 전부 끝내야 DONE.

## 현재 문제 (코디네이터 실측, 0a2ee8e)
`uv run th report --days 1 --owner /home/hs/rang/team-harness` 출력:
- 한 행에 `작업별 토큰`·`원인`·`완료 시간`이 파이썬 dict 그대로(`{'/home/.../_outputs/x': 149499, ...}`) 찍혀 읽을 수 없다.
- 원인 분류: `{'가드 거부': 7, '기타': 2, '스스로 멈춤': 1, '멈춤': 4, '인계 없음/중단': 1}` — 실제로는 INCOMPLETE 자기 중단이 여러 건인데, 사유에 가드 거부 문구가 함께 있으면 "가드 거부"로 분류됨. 멈춤 4건은 모두 DONE 뒤 오탐이었는데 "오탐"으로 따로 안 잡힘.
`src/team_harness/loss_report.py`.

## 작업 위치
`git -C /home/hs/rang/team-harness worktree add /home/hs/orca/workspaces/team-harness/lossfmt -b task/loss-format origin/main` 후 `uv sync --extra dev` 1회 허용.

## 할 일
1. 출력 형식: ① 요약 표(날짜·프로젝트·launch·재실행·롤오버·자동 계속·토큰·원인 상위 3을 `이름 n` 형태 문자열) ② 작업별 표(작업 이름은 폴더 basename, 시도 수, 토큰, 완료까지 분, 마지막 상태) — 토큰 내림차순 상위 15. `--json`은 기존 구조 유지.
2. 분류: 사유에 `INCOMPLETE`·`미완료`·`남은`이 있으면 가드 문구가 같이 있어도 "스스로 멈춤"(가드가 원인이면 하위 표시 "가드 거부 후"). DONE이 이미 있는 작업의 "멈춤" 사건은 "멈춤 오탐". "워커 중단(인계 없음)"은 "인계 없음".
3. 테스트 갱신·추가 4개 이상, 실제 데이터로 `uv run th report --days 1 --owner /home/hs/rang/team-harness` 출력 전체를 이 폴더 `report_real.txt`에 저장. CHANGELOG 한 줄.
4. 검증 `uv run ruff check src/ && uv run ruff format --check src/ && uv run pyright src/ && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH= uv run pytest src/tests/ -q -p pytest_asyncio.plugin | tail -n 20` → 커밋(실제 내용대로) → fetch → main==origin/main → `merge --ff-only` → `push origin main`. ff 불가면 rebase·재검증. push가 pre-push canary 훅에 막히면 그 출력대로 고친다. force·태그 금지. 병합 후 worktree 제거·`branch -d`.

## 검수
```bash
cd /home/hs/rang/team-harness && git fetch -q origin && test "$(git rev-parse main)" = "$(git rev-parse origin/main)" && test -s _outputs/loss_format_20261029/report_real.txt && ! grep -q "{'" _outputs/loss_format_20261029/report_real.txt && echo ok
```

## 규칙
설치 금지(uv sync 1회 제외). 출력은 tail/-q로 줄인다. 고칠 방법을 아는 오류는 고치고 계속. 가드 거부는 나눠 재시도. 같은 수정 방법 2회 실패 시 FAILED. 애매하면 보수적으로 고르고 WORKLOG `결정:`에 적고 계속. 할 일 1~4를 모두 끝냈을 때만 DONE.
