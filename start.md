# team-harness 기능별 사용법

자세한 설정 스키마는 [README.md](./README.md), 설계 이유는
[design/decisions.md](./design/decisions.md)를 봅니다. 이 문서는 "무엇을 어떤 명령으로
쓰는가"만 기능별로 정리합니다.

| # | 기능 | 언제 쓰나 | 핵심 명령 |
|---|---|---|---|
| 0 | 붙어 있는 도구 목록 | 무엇이 붙어 있나 볼 때 | — |
| 1 | 설치·설정 | 처음 한 번 | `uv tool install -e`, `th init` |
| 2 | Orca 코디네이터 (`th cord`) | 평소 작업의 기본 진입점 | 에이전트 창에 `th cord` |
| 3 | 워커 실행·감시 스크립트 | 데이터·GPU·장시간 작업 위임 | `th-launch.sh`, `th-watch.sh`, `th-status.sh` |
| 4 | 쿼터 잠금 | 쿼터 오류 뒤 재시도 낭비 방지 | 자동 (`quota_lock.<워커>`) |
| 5 | 내장 코디네이터 실행 | 하네스가 직접 계획·위임 | `th run`, `th repl` |
| 6 | Harness Protocol A/B/C | 저장소 코드 변경 | `th protocol run/resume/relay/status` |
| 7 | 로그·정리 | 실행 기록 확인, 고아 워커 정리 | `th logs`, `th reap` |
| 8 | 토큰 절감 | 기본 적용, 측정 | `worker_isolate_mcp`, `measure_preamble.py` |
| 9 | 모델·추론 노력 | **코디네이터가 자동 선택** | (직접 쓸 일 없음) |
| 10 | graft 코드 그래프 | **코디네이터 판단**, 기본 꺼짐 | (직접 쓸 일 없음) |
| 11 | Jev 결정 라우터 | **자동 적용** (`th run`·`th repl`) | (직접 쓸 일 없음) |
| 12 | 코디네이터 작업일지 | 상태·이력 보존 | `.claude/current_status.md`, `work_log.md` |

---

## 0. team-harness가 붙이는 것 (도구 목록)

하네스(마구)는 모델에 도구와 실행 환경을 붙여 일을 시키는 틀입니다. team-harness는 세 층을 붙입니다.

### 0-1. 조율자 LLM의 도구 21개 (`th run`, `th repl`)

| 묶음 | 도구 | 하는 일 |
|---|---|---|
| 워커 부리기 | `spawn_agent` | codex·claude·agy 등 워커 CLI를 하위 프로세스로 띄워 일을 맡김 |
| | `wait_for_agents`, `wait_for_any` | 워커가 전부 / 하나라도 끝날 때까지 대기 |
| | `read_agent_output`, `read_new_agent_output` | 워커 출력 읽기(전체 / 지난번 이후 새 부분만) |
| | `agent_status`, `list_agents`, `agent_availability` | 워커 상태·목록·사용 가능한 워커 종류 |
| | `kill_agent` | 워커 강제 종료 |
| 파일 | `read_file`, `read_new_file_content` | 파일 읽기(전체 / 바뀐 부분만) |
| | `write_file`, `append_file`, `edit_file`, `multi_edit_file` | 파일 쓰기·추가·수정 |
| | `ls`, `glob`, `grep` | 목록·파일 찾기·내용 검색(결과 길이 상한, grep 타임아웃) |
| 셸 | `bash` | 명령 실행 |
| 계획 | `todo_write`, `todo_read` | 할 일 목록 관리 |

정의 위치: `src/team_harness/tools/` (`agent_tools.py`, `fs_tools.py`, `shell_tools.py`, `todo_tools.py`).

### 0-2. 도구 바깥에서 조율자를 받치는 장치

| 장치 | 역할 |
|---|---|
| 자동 압축 | 대화가 길어지면 이전 기록을 요약해 줄임 (`tracking/context.py`) |
| Jev 결정 라우터 | 매 턴 필요한 도구 묶음만 골라 보냄 (11절) |
| 쿼터 차단기 | 워커 rate limit 시 같은 종류 워커 실행을 멈춤 |
| 스킬 | `.agents/skills/`의 지침 문서를 조율자가 읽고 따름 |
| graft | 워커에게 코드 심볼·호출 관계 그래프 제공(기본 꺼짐, 10절) |
| 워커 템플릿 | 워커 CLI별 실행 플래그·모델·세션 재개, MCP 격리 (`agents/template.py`) |
| 실행 기록·reaper | 실행마다 `run.json`, 비정상 종료 뒤 남은 워커 정리 (`th reap`) |
| Harness Protocol | A 경쟁 / B 인계 / C 파이프라인 작업 절차 (6절) |

### 0-3. `th cord`로 일할 때 붙는 것

이때 조율자는 Claude Code·Codex 자체이고(자기 도구를 이미 가짐), team-harness는 그 위에
여러 워커를 묶는 고삐와 감시 장치를 덧붙입니다.

| 붙이는 것 | 역할 |
|---|---|
| `orca-harness-protocol` 스킬 | 코디네이터 운영 규칙(워커 선택, 검증, 작업일지, 폴링 금지) |
| `th-launch.sh` | 워커 분리 실행 + 공통 규칙(`worker_suffix.md`) 부착 (3절) |
| `th-watch.sh`, `th-status.sh` | 사건 감시와 상태 표 — 셸 스크립트, LLM 토큰 0 (3절) |
| 작업 폴더 규약 | `task.md`, `STATUS`, `WORKLOG.md`, `DONE`/`FAILED` (3절) |
| 작업일지 | `.claude/current_status.md`, `work_log.md` (12절) |
| `th protocol` | 코드 변경일 때 A/B/C 절차 (6절) |

## 1. 설치·설정

```bash
uv tool install -e /home/hs/rang/team-harness
codex login
claude                 # 로그인 확인
agy                    # 로그인 확인
```

`-e` 설치는 `/home/hs/rang/team-harness`의 현재 코드를 그대로 씁니다. 이 디렉터리를
main에 두면 main 변경이 바로 반영됩니다.

```bash
th init                # 프로젝트 로컬 .team-harness/config.toml
th init --global       # 전역 ~/.team-harness/config.toml
th init --force        # 주석 달린 기본 템플릿으로 덮어쓰기
```

설정 우선순위: CLI 플래그 → 환경 변수(`TEAM_HARNESS_*`) → 로컬 `.team-harness/config.toml`
→ 전역 `~/.team-harness/config.toml` → 내장 기본값.

스킬 설치(새 에이전트 세션부터 인식):

```bash
cd /home/hs/rang/team-harness
npx -y skills add . --list --full-depth
npx -y skills add . --skill team-harness orca-harness-protocol \
  -a codex -a claude-code -a antigravity-cli --global -y
```

| 스킬 | 용도 |
|---|---|
| `team-harness` | `th run`의 작업 분할, worker 위임·검증 지침 |
| `orca-harness-protocol` | Orca에서 `th cord`로 코디네이터 시작 |
| `orchestration` | Orca worker 생성, 메시지, 완료·정리 관리 |

## 2. Orca 코디네이터 (`th cord`)

Orca에서 대상 프로젝트 작업 트리를 만들고 Codex·Claude·Agy 중 하나를 연 뒤 입력합니다.
`th cord`는 shell 명령이 아니라 **에이전트 프롬프트 명령**입니다.

```text
th cord
> team-harness 코디네이터 모드가 준비됐습니다.

학습 결과 정리해서 agy로 돌려       # 일반 작업 → 3번 스크립트로 워커 위임
C 설정 파서를 정리해줘              # 코드 변경 → MODE C
A 로그인 오류를 고치고 테스트해줘   # 코드 변경, 두 구현 비교 → MODE A
B codex                             # 진행 중 작업을 codex에 인계 → MODE B
상태                                # th-status.sh 1회
토큰                                # 토큰 사용량
종료
```

- 코디네이터는 계획·짧은 지시·결과 검증·보고만 하고, 코드 작성과 실행은 워커에 맡깁니다.
- 워커의 채팅 출력은 근거가 아닙니다. 완료와 수치는 파일(`DONE`/`FAILED`, 결과표)로만 인정합니다.
- 운영 규칙 원문: `~/.claude/skills/orca-harness-protocol/SKILL.md`.

## 3. 워커 실행·감시 스크립트

데이터 처리, 학습, 라벨링, 리뷰처럼 오래 걸리거나 저장소 코드 변경 흐름이 필요 없는
작업에 씁니다. 상태는 대화가 아니라 **작업 폴더 안 파일**로 주고받습니다.

### 작업 폴더 규약

| 파일 | 쓰는 쪽 | 의미 |
|---|---|---|
| `task.md` | 코디네이터 | 워커 지시서 (필수, `## 검수` 블록 포함 가능) |
| `verify.sh` | th-launch | `task.md`에서 추출된 보조 검수 스크립트 |
| `VERIFIED` / `VERIFY_FAILED` | th-watch/th-watchd | 보조 검수 결과 (통과 / 실패 항목+출력 끝 40줄) |
| `worker.pid` / `job.pid` | th-launch / 워커 | 워커 PID / 분리 실행한 본 작업 PID |
| `STATUS` | 워커 | 단계 변화마다 한 줄 |
| `WORKLOG.md` | 워커 | 60줄 이내 현재 상태 (목표·진행·방법·문제·산출물 경로) |
| `DONE` / `FAILED` | 워커 | 완료 / 실패와 이유 |
| `codex.log` / `agy.log` | th-launch | 워커 출력 |
| `.th_owner` | th-launch | 이 작업을 띄운 작업 공간 (감시 소유자 판정) |
| `.th_worktree` | th-launch | 보조 검수를 실행할 작업 트리 경로 |

### task.md 검수 블록 예시

`task.md` 끝에 `## 검수`와 bash 코드 블록을 작성하면 `th-launch`가 자동으로 `verify.sh`로 추출해 워커 완료 시 보조 검수를 수행합니다:

```markdown
## 검수
```bash
uv run --frozen pytest src/tests/ -q
```
```

### th-launch — 워커 분리 실행

```bash
# 사용: th-launch.sh <작업폴더> [codex|agy] <모델> <effort> <작업트리> [추가 인자]
scripts/th-launch.sh _outputs/fix_20261013/core agy default /path/to/worktree
scripts/th-launch.sh ./job codex gpt-6.1-sol medium /path/to/repo
```

- `task.md` 뒤에 `~/.team-harness/worker_suffix.md`(워커 공통 규칙)를 항상 붙입니다.
  정책 변경은 다음 launch부터 적용됩니다.
- `task.md`의 `## 검수` 블록을 `verify.sh`로 추출해 저장합니다(없으면 경고만).
- 데몬(`th-watchd.sh`)이 아직 실행 중이지 않으면 `setsid nohup`으로 자동 띄웁니다.
- 워커 종류를 생략하면 codex. agy 모델 생략·`default`는 Gemini 3.8 Flash (High).
- codex는 `-c mcp_servers={}`로 MCP 서버 상속 없이 실행합니다(사용자 config는 유지).
- agy는 시작 직후 다른 PID로 재부모화되므로 최대 20초 동안 실제 PID를 다시 찾아 기록합니다.
- 실행 기록은 작업 공간 `.claude/work_log.md`에 "지시" 줄로 남습니다.
- 같은 종류 워커에 쿼터 잠금이 있으면 실행을 거부합니다(4번).

### th-watchd — 상시 감시 데몬 (권장)

```bash
scripts/th-watchd.sh
```

- 등록부(`tasks.txt`)를 상시 감시하며 사건 발생 시마다:
  1. 소유 작업 공간의 `.claude/inbox.md`에 한 줄 기록 (`시각 | 작업 | 사건 | 요약 | 검수결과`)
  2. `notify-send`가 설치되어 있으면 데스크톱 알림 (없으면 건너뜀)
  3. `~/.team-harness/watch/events.log`에 기록
  4. (옵션) Jev 관찰 CLI(`watch_cli.py`)를 호출하여 `jev_decisions.log`에 기록 (`TH_JEV_WATCH=off`로 비활성화 가능)
- 단일 인스턴스(`watchd.lock`), 스크립트 수정 시 자동 `exec` 재실행.
- 표시 파일로 `.th_daemon.*`을 사용하므로 코디네이터 깨우기용 `th-watch.sh`와 간섭하지 않습니다.

#### systemd 사용자 서비스 설치 방법 (선택 사항)

실제 백그라운드 상주를 systemd로 관리하려면 다음 단계를 따릅니다 (자동 등록되지 않으므로 필요 시 직접 등록):

```bash
mkdir -p ~/.config/systemd/user
cp scripts/th-watchd.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now th-watchd.service
```

### th-watch — 사건 하나만 알리고 종료 (코디네이터 깨우기용)

```bash
scripts/th-watch.sh [최대대기초]          # 기본 21600초
TH_WATCH_DIR=... TH_STALL_SEC=600 TH_POLL_SEC=30 scripts/th-watch.sh
```

- 알리는 사건: **완료, 실패, 쿼터·용량 오류, 프로세스 소실(1분 이내), 멈춤(CPU·산출물
  10분 무변화)**. 정상 진행은 알리지 않습니다. 사건을 하나 출력하면 종료하므로 다시 실행합니다.
- 완료 시 `verify.sh` 및 공통 검사를 실행해 "완료 (검수 통과/실패/없음)"을 출력합니다.
- **자기 작업 공간이 띄운 작업만 알립니다.** 표시 파일로 `.th_notified.*`를 사용합니다.
- 등록부: `~/.team-harness/watch/tasks.txt`, 사건 기록: `~/.team-harness/watch/events.log`.
- 시험할 때는 실제 등록부 대신 임시 `TH_WATCH_DIR`을 씁니다.

### th-status — 요청할 때만 표로 한 번

```bash
scripts/th-status.sh
```

작업·내용·상태·검수(통과/실패/대기/-)·마지막 변화·완료 예상·진행을 표로 출력합니다.

## 4. 쿼터 잠금

- `th-watch`가 워커 로그에서 쿼터·용량 오류(`quota`, `rate limit`, `usage limit`,
  `at capacity`)를 보면 `~/.team-harness/watch/quota_lock.<codex|agy>`를 만듭니다.
  다른 작업 공간의 작업에서 난 오류도 잠금 파일은 만듭니다(알림은 소유자에게만).
- `th-launch`는 잠금이 있으면 같은 종류 워커를 띄우지 않고 종료 코드 3으로 끝냅니다.

```bash
cat ~/.team-harness/watch/quota_lock.codex      # 원인 확인
rm  ~/.team-harness/watch/quota_lock.codex      # 쿼터가 풀린 뒤 해제
TH_IGNORE_QUOTA_LOCK=1 scripts/th-launch.sh ... # 1회 무시
```

## 5. 내장 코디네이터 실행 (`th run`, `th repl`)

하네스의 자체 조율자 LLM이 작업을 나눠 워커 CLI에 위임합니다.

| 명령 | 기능 |
|---|---|
| `th run "작업"` | 1회 실행 |
| `th run -f task.md` | 파일의 지시 실행 |
| `th run --agents codex,claude --model ... --cwd ...` | 워커 종류·모델·작업 경로 지정 |
| `th repl` | 대화형 실행 |

REPL 명령: `/agents`, `/log`, `/compact`, `/clear`, `/kill <agent_id>`, `/quit`.

- 세션 산출물은 실행한 저장소의 `_outputs/<run_id>/`에 생깁니다(git 무시 대상).
- SDK: `TeamHarness(...).run(task)` → `TeamHarnessResult` (README "Python SDK").

## 6. Harness Protocol A/B/C — 저장소 코드 변경

| MODE | 동작 |
|---|---|
| **A 경쟁** | 두 worker가 독립 구현 → 교차 검토 → 사용자가 결과 선택 |
| **B 인계** | 기존 branch·worktree·상태를 유지하고 다른 agent가 이어받음 |
| **C 파이프라인** (기본) | 설계 → 구현·테스트 → 검수 순서로 진행 |

```bash
th protocol run --mode c "작업" --repo .          # C 시작 (기본 모드)
th protocol run --mode a "작업" --repo .          # A 시작
th protocol resume --task-id <ID> --run-dir <DIR> --repo . \
  --selection SELECT_WORKER_1                      # A 결과 선택
th protocol resume --task-id <ID> --run-dir <DIR> --repo .   # 중단된 C 이어서
th protocol relay  --task-id <ID> --run-dir <DIR> --repo . \
  --next-agent codex                               # B: codex에 인계
th protocol status                                 # 최근 실행 목록
th protocol status --run-dir <DIR>                 # 단계·실패 원인·시간·토큰
```

주요 옵션: `--no-visible`(tmux 창 끔), `--agent-timeout 600`(단계별 워커 제한 초),
`--check-timeout 300`, `--set-role ROLE.FIELD=VALUE`.

- 결과는 `task/<TASK-ID>/...` 브랜치에 커밋되며 base 작업 트리를 자동 merge·push하지
  않습니다(TH-D16). 채택은 사람이 merge합니다.
- 토큰·비용은 worker CLI가 보고한 단계만 합산합니다. `N개 중 M개 반영` 경고는 부분 합계입니다.
- 막히면 먼저 `th protocol status --run-dir <DIR>`의 blocker를 확인합니다.

## 7. 로그·정리

```bash
th logs                     # 최근 실행 로그
th logs <RUN_ID>            # 특정 실행
th reap <RUN_ID> --dry-run  # 비정상 종료 뒤 남은 워커 확인만
th reap <RUN_ID>            # SIGTERM → (--grace-s 뒤) SIGKILL
```

실행 기록(`run.json`)에는 조율자 턴별·워커별 입력 토큰과 캐시 토큰
(`input_tokens`, `cached_prompt_tokens`)이 남습니다.

## 8. 토큰 절감

기본으로 켜져 있는 것:

- **프롬프트 캐시 친화 순서**: 조율자 시스템 프롬프트에서 실행마다 바뀌는 값(세션 경로,
  시각)을 맨 끝 `Run context`로 모아 앞부분이 실행 간 캐시됩니다.
- **도구 스키마 축약**: 도구 설명은 1~2문장, 상세 사용법은 시스템 프롬프트에 한 번만.
- **워커 MCP 격리(TH-D25)**: claude 워커 `--strict-mcp-config`, codex 워커
  `-c mcp_servers={}`. 사용자 전역 MCP 도구 정의가 워커마다 실리지 않습니다.
- **워커 공통 규칙 축소**: `worker_suffix.md`에는 워커가 지킬 규칙만 둡니다.

MCP 도구가 꼭 필요한 워커가 있으면 끕니다.

```bash
TEAM_HARNESS_WORKER_ISOLATE_MCP=false th run "작업"
```

```toml
# .team-harness/config.toml
[coordinator]
worker_isolate_mcp = false
```

고정 전송량 측정:

```bash
uv run python scripts/measure_preamble.py   # 시스템 프롬프트 + 도구 스키마 토큰(근사)
```

## 9~11. 코디네이터가 관리하는 옵션

평소에는 `th cord` 코디네이터에게 하고 싶은 일만 말하면 됩니다. 아래 옵션은 코디네이터가
작업에 맞춰 정하고 보고에 한 줄로 알립니다(규칙: `skills/orca-harness-protocol/SKILL.md`
"옵션은 코디네이터가 판단한다"). 아래 명령은 직접 `th`를 실행할 때의 참고용입니다.

| 옵션 | 현재 동작 |
|---|---|
| 모델·effort | 코디네이터가 워커 선택 기준으로 `th-launch` 인자에 넣음. `th run` 조율자도 같은 기준으로 워커 모델 선택 |
| graft | 기본 꺼짐. `th-launch` 워커에는 아직 연결되지 않음. `th run`·protocol로 큰 저장소 코드 작업을 할 때만 코디네이터가 켬 |
| Jev | 전역 설정으로 항상 켜짐. `th run`·`th repl` 내장 조율자에만 효과. 실패·쿼터 소진 시 전체 도구로 자동 대체, 연속 3회 오류면 그 실행에서 꺼짐 |

### 9. 모델·추론 노력 (참고)

```bash
th protocol run --mode c "복잡한 작업" --repo . \
  --set-role mode_c_implement.model=gpt-6.1-sol \
  --set-role mode_c_implement.effort=medium
```

고정 설정은 `.team-harness/config.toml`의 `[protocol.roles.<역할>]`에 저장합니다.
`th-launch` 워커는 실행 인자(모델, effort)로 고릅니다. 기본 선택 기준:
gpt-6.1-sol medium(일반 코드·중요 로직 설계/구현·A/B/C Codex 역할),
gpt-6.1-sol high(고난도 또는 medium 실패 시만),
agy Gemini 3.8 Flash (High)(단순·대량·비코드, 결과 검증 필요).
Claude `th cord` 코디네이터는 Opus 5.5를 유지하며, Codex 쿼터 잠금 시 Claude/agy로
대체하고 사용자가 명시한 모델은 그대로 따릅니다. `th run`/`th repl`도 기본 모델은
gpt-6.1-sol medium입니다. 모델 생략 실행: `scripts/th-launch.sh ./job codex /path/to/repo`.

### 10. graft 코드 그래프 (참고)

워커가 코드 심볼·호출 관계를 그래프로 찾게 해 탐색 토큰을 줄입니다(브랜치 병합 도구가
아닙니다).

```bash
TEAM_HARNESS_CONTEXT_GRAPH=1 th protocol run --mode c "작업" --repo .
```

```toml
[context_graph]
enabled = true
```

그래프는 `~/.team-harness/graft/`에 저장되며 빌드가 실패해도 작업은 계속됩니다.

### 11. Jev 결정 라우터 (참고)

조율자의 다음 턴에 필요한 도구 묶음만 골라 보냅니다. 실패하거나 confidence가 낮으면
전체 도구를 씁니다.

```bash
export TYPESAFE_API_KEY="..."
TEAM_HARNESS_JEV=1 th run "작업"
```

```toml
[decision_router]
enabled = true
confidence_threshold = 0.85
```

측정값은 해당 실행 `run.json`의 `decisions`에서 확인합니다.

## 12. 코디네이터 작업일지

모든 코디네이터는 작업 공간에 두 파일을 유지합니다(정본: `design/coordinator_journal.md`).
단, `th run`/`th repl` 내장 조율자는 `<run_dir>/journal/`에 기록하며 작업 공간의 `.claude/` 작업일지는 읽기 전용입니다.

| 파일 | 내용 |
|---|---|
| `.claude/current_status.md` | 40줄 이내 현재 상태. 시작·대화 요약 직후·긴 작업 전에 읽음 |
| `.claude/work_log.md` | 지시·시작·완료·실패·결정·실수마다 한 줄. 날짜가 바뀌면 `work_log_<날짜>.md`로 보관 |

`th-launch`와 `th-watch`는 지시·완료·실패 줄을 자동으로 남기고, 판단·원인·교훈은
코디네이터가 적습니다.

```bash
scripts/install-journal-rule.sh   # 전역 지시 파일에 작업일지 규칙 설치(멱등)
# scripts/journal-hook.sh         # Claude Code SessionStart 훅: current_status.md를 맥락으로 제공
```
