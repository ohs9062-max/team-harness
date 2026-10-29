---
kind: code
task_key: rollover_fix_20261029
follow_up: normal
context_budget: 120000
allow_paths:
  - /home/hs/orca/workspaces/team-harness/rollfix/outputs
  - /home/hs/orca/workspaces/team-harness/rollfix/_outputs
  - /tmp
---
# 워커 맥락 롤오버(TH-D32)가 한 번도 작동하지 않은 원인 수정, 병합 후 push

규칙 문서·.claude/는 읽지 않는다. 기록은 이 폴더 STATUS·WORKLOG.md에만.

## 배경 (코디네이터 실측)
`_outputs/ctx_growth_measure_20261029/report.md`·`timeline.csv`: context_budget 기본 60000인데 codex 워커 세션 입력이 96,783 / 113,090 / 64,761 토큰까지 커졌고 롤오버·압축 0회. robot3 10-26 이후 146회 launch 중 롤오버 0회.

## 작업 위치
`git -C /home/hs/rang/team-harness worktree add /home/hs/orca/workspaces/team-harness/rollfix -b task/rollover-fix origin/main` 후 `uv sync --extra dev` 1회 허용.

## 읽을 문서 (필요 구간만 rg로 찾아 읽기, 한 번에 200줄 이하)
- `design/decisions.md`의 TH-D32 항목
- `src/team_harness/worker_context.py`
- `rg -n 'context_budget|rollover|worker_context' scripts/ src/team_harness/` 결과 구간

## 할 일
1. 원인 규명: 누가 언제 워커 입력 토큰을 측정하고 롤오버를 거는지(th-launch / th-watchd / agent_tools / protocol) 추적하고, 위 세 세션에서 왜 안 걸렸는지 WORKLOG `원인:`에 근거(파일:줄)와 함께 적는다. codex 세션 로그(`~/.codex/sessions/…jsonl`)의 token_count 이벤트를 실제로 읽는지 확인.
2. 수정: 예산 초과 시 롤오버가 실제로 일어나게 한다. 판정은 공통 함수(worker_context.py) 하나, 세 실행 경로(th-launch+watchd, run/repl agent_tools, protocol mode_a/b/c)가 모두 사용(AGENTS 규칙 4). 워커가 스스로 멈추길 기다리는 방식이면 안 된다 — 감시 쪽(watchd 등)이 측정·판정해서 단계 경계(WORKLOG 갱신 직후)에 종료·요약 재실행을 건다. 설계가 애매하면 TH-D32 문구를 따르는 쪽을 고르고 `결정:`에 적고 계속.
3. 테스트: 가짜 codex 세션 jsonl(tmp_path)로 예산 초과→롤오버 판정, 미만→유지, 각 경로가 같은 함수를 호출하는지 검사. 5개 이상.
4. `design/decisions.md` TH-D32에 개정 단락(2026-10-29, 원인·수치), CHANGELOG 한 줄.
5. 검증: `uv run ruff check src/ && uv run ruff format --check src/ && uv run pyright src/ && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH= uv run pytest src/tests/ -q -p pytest_asyncio.plugin | tail -n 20`.
6. 병합: 커밋 `fix(context): enforce worker context rollover` → fetch → main==origin/main 확인 → `merge --ff-only task/rollover-fix` → `push origin main`. ff 불가면 rebase·재검증(다른 워커가 worker_suffix를 동시에 고친다 — 그 파일은 건드리지 않는다). force·태그 금지. 병합 후 worktree 제거·`branch -d`.

## 검수
```bash
cd /home/hs/rang/team-harness && git fetch -q origin && test "$(git rev-parse main)" = "$(git rev-parse origin/main)" && git log -6 --format=%s origin/main | grep -q 'enforce worker context rollover' && grep -q '2026-10-29' design/decisions.md && echo ok
```

## 규칙
설치 금지(uv sync 1회 제외). 출력은 tail/-q로 줄인다. 고칠 방법을 아는 오류는 고치고 계속한다. 가드 거부는 명령을 나눠 재시도. 같은 수정 방법이 두 번 실패하면 FAILED. 애매하면 보수적으로 고르고 WORKLOG `결정:`에 적고 계속. 끝나면 DONE.

## 수정 지시 (2차)
- 1차 중단 원인(코디네이터 지시서 오해): front matter `allow_paths`는 **추가** 허용 목록이다. 작업 트리 `/home/hs/orca/workspaces/team-harness/rollfix` 전체(코드 수정)와 이 작업 폴더(`_outputs/rollover_fix_20261029`, STATUS·WORKLOG·DONE 기록), worktree 생성은 원래 허용된다. 1차에서 `/tmp/rollover_fix_20261029`에 쓴 기록은 무시한다.
- 실제로 가드가 거부하면 명령을 나눠 다시 시도하고, 그래도 거부되면 거부 메시지 원문을 FAILED에 적는다. 거부되기 전에 미리 중단하지 않는다.

## 수정 지시 (3차)
- 2차 실패 원인(코디네이터 지시서 오류): 다른 작업 폴더 `_outputs/ctx_growth_measure_20261029/`는 영역 가드가 막는다. 필요한 근거는 이 작업 폴더에 복사해 두었다: `evidence_report.md`, `evidence_timeline.csv`. 다른 `_outputs/*` 폴더는 읽지 않는다.
- codex 세션 jsonl 형식 확인은 `~/.codex/sessions/`의 파일을 `rg`/`tail`로 필요한 줄만 본다. 그것도 거부되면 테스트용 가짜 jsonl(token_count 이벤트)을 직접 만들어 진행한다 — 중단하지 않는다.
- 작업 트리 전체와 이 작업 폴더는 쓰기 허용이다(2차 지시 유지).

## 수정 지시 (4차, terra high) — 설계는 코디네이터가 확정. 이 결정대로 구현하고 설계 이유로 멈추지 않는다
3차 진단(WORKLOG `원인:`)을 그대로 쓴다. 다시 조사하지 않는다.
- 결정 1 (세션 ID): watch_inspect의 실행 중 측정 경로(:166-191)도 저장된 session_id가 자리표시자이거나 jsonl을 못 찾으면, 대기(parking) 경로(:120-135)와 같은 방법으로 캡처 로그에서 실제 세션을 찾아 레지스트리를 갱신한 뒤 측정한다. 공통 해석 함수 하나로 뺀다.
- 결정 2 (강제): 예산 초과 + 마지막 확인 이후 WORKLOG.md가 갱신됨(단계 경계)이면, 감시 쪽이 워커를 SIGTERM 하고 기존 요약 기반 재실행(3a32be6 recovery 경로)으로 새 세션을 띄운다. 요청 파일만 쓰는 현재 동작(:194-211)은 이 강제 동작으로 대체. 같은 task_key 롤오버는 최대 3회, 직전 롤오버 이후 STATUS 단계 진척이 없으면 롤오버 대신 "작업 분할 필요" 사건만 보고(TH-D32 기존 문구).
- 결정 3 (경로): 판정은 worker_context.py 함수 하나. th-launch+watchd는 결정 2. agent_tools(run/repl)와 protocol mode_c는 워커 대기 루프에서 같은 판정 함수를 주기적으로 호출하고, 초과+경계면 같은 방식(종료→요약 재실행)으로 처리. mode_a/b가 mode_c AgentRunner를 공유하면 그걸로 충족.
- 범위가 커도 멈추지 않는다: 단계별로 커밋하며 진행하고, 시간이 부족하면 남은 단계가 아니라 **완료한 단계까지 검증·병합**한 뒤 DONE에 남은 항목을 적는다(INCOMPLETE로 FAILED 금지).
