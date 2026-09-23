# team-harness 빠른 사용법

## 1. 최초 설치

```bash
uv tool install -e /home/hs/rang/team-harness
codex login
claude                 # 로그인 확인
agy                    # 로그인 확인
```

`-e` 설치는 `/home/hs/rang/team-harness`의 현재 코드를 사용하므로 이 디렉터리는
사용할 branch에 두세요.

프로젝트별 설정이 필요하면 대상 프로젝트에서 한 번 실행합니다.

```bash
th init                # .team-harness/config.toml
th init --global       # ~/.team-harness/config.toml
```

현재 기본 구성은 **코디네이터 Codex**, **worker Codex·Claude·Antigravity(`agy`)**입니다.

## 2. 사용할 수 있는 스킬

| 스킬 | 현재 상태 | 용도 |
|---|---|---|
| `team-harness` | 저장소 제공 | `th run`의 작업 분할, worker 위임·검증 지침 |
| `orca-harness-protocol` | Codex·Claude·Agy에 설치됨 | Orca에서 `th cord`로 코디네이터 시작 |
| `orchestration` | 전역 설치됨 | Orca worker 생성, 메시지, 완료·정리 관리 |

저장소가 제공하는 스킬 확인:

```bash
cd /home/hs/rang/team-harness
npx -y skills add . --list --full-depth
```

두 스킬을 모든 프로젝트에서 사용:

```bash
npx -y skills add . --skill team-harness orca-harness-protocol \
  -a codex -a claude-code -a antigravity-cli --global -y
```

현재 `orca-harness-protocol`은 `~/.agents/skills/`에 설치되어 있습니다. 설치 후 연
**새 에이전트 세션**부터 스킬을 인식합니다.

## 3. 기본 명령

| 명령 | 기능 |
|---|---|
| `th run "작업"` | 코디네이터가 계획하고 worker에게 위임하는 1회 실행 |
| `th run -f task.md` | 파일의 작업 지시 실행 |
| `th repl` | 대화형 실행 |
| `th logs` | 최근 실행 로그 |
| `th logs <RUN_ID>` | 특정 실행 로그 |
| `th reap <RUN_ID>` | 비정상 종료 뒤 남은 worker 확인·정리 |
| `th --help` | 전체 명령 확인 |

`th repl` 안에서는 `/agents`, `/log`, `/compact`, `/clear`, `/kill <id>`, `/quit`를
사용할 수 있습니다.

## 4. Harness Protocol A/B/C

| MODE | 동작 |
|---|---|
| **A 경쟁** | 두 worker가 독립 구현 → 교차 검토 → 사용자가 결과 선택 |
| **B 인계** | 기존 branch·worktree·상태를 유지하고 다른 agent가 이어받음 |
| **C 파이프라인** | 설계 → 구현·테스트 → 검수 순서로 진행 |

```bash
# A 시작
th protocol run --mode a "작업" --repo .

# C 시작
th protocol run --mode c "작업" --repo .

# A 결과 선택
th protocol resume --task-id <ID> --run-dir <DIR> --repo . \
  --selection SELECT_WORKER_1

# 중단된 C 이어서 실행
th protocol resume --task-id <ID> --run-dir <DIR> --repo .

# B로 Codex에게 인계
th protocol relay --task-id <ID> --run-dir <DIR> --repo . \
  --next-agent codex
```

MODE 실행은 task worktree에 checkpoint를 만들지만 base branch를 자동으로 merge하거나
push하지 않습니다.

## 5. Orca IDE에서 프롬프트로 실행

Orca에서 대상 프로젝트의 작업 트리를 만들고 Codex·Claude·Agy 중 하나를 연 뒤 입력합니다.

```text
th cord
> team-harness 코디네이터 모드가 준비됐습니다.

A 로그인 오류를 고치고 테스트해줘
C graft 켜고 설정 파서를 정리해줘
B codex
상태
토큰
종료
```

`th cord`는 raw shell 명령이 아니라 **에이전트 프롬프트 명령**입니다. 현재 스킬은 이후
문장을 `th protocol ... --no-visible`로 변환합니다. worker가 Orca의 개별 에이전트 창에
나타나는 실행 adapter는 아직 추가되지 않았습니다.

## 6. 상태와 토큰 사용량

```bash
th protocol status                    # 최근 Protocol 실행
th protocol status --run-dir <DIR>    # stage, 실패 원인, 시간, 토큰·비용
```

시간은 하네스가 직접 측정합니다. 토큰·비용은 worker CLI가 보고한 stage만 합산하므로
`N개 중 M개 반영` 경고가 있으면 부분 합계입니다.

## 7. graft 코드 그래프

이번 실행만 켜기:

```bash
TEAM_HARNESS_CONTEXT_GRAPH=1 th protocol run --mode c "작업" --repo .
```

프로젝트에서 항상 켜기:

```toml
# .team-harness/config.toml
[context_graph]
enabled = true
```

graft는 worker별 코드 탐색 토큰을 줄이기 위한 선택 기능입니다. 그래프는
`~/.team-harness/graft/`에 저장되며 빌드 실패 시에도 작업은 계속됩니다.

## 8. 모델 바꾸기

```bash
th protocol run --mode c "복잡한 작업" --repo . \
  --set-role mode_c_implement.model=gpt-5.6-sol \
  --set-role mode_c_implement.effort=medium
```

고정 설정은 `.team-harness/config.toml`의 `[protocol.roles.<역할>]`에 저장합니다.

문제가 생기면 먼저 `th protocol status --run-dir <DIR>`의 blocker와 실패 원인을
확인하세요.
