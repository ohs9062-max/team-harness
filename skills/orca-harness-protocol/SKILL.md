---
name: orca-harness-protocol
description: Orca IDE의 Codex, Claude, Agy 에이전트 창에서 사용자가 `th cord`로 team-harness 코디네이터 역할을 시작하거나, "A로 해", "B로 이어서 해", "C로 해", MODE A/B/C, graft, protocol 상태 또는 토큰 사용량을 요청할 때 사용합니다. 현재 에이전트 세션을 대화형 Harness Protocol 코디네이터로 운영하고 사용자 선택 및 재개 Gate를 관리합니다. 일반적인 A/B 테스트의 알파벳은 MODE 명령으로 해석하지 않습니다.
---

# Orca용 Harness Protocol 코디네이터

사용자는 Orca IDE에서 작업 트리를 만든 뒤 이 스킬이 설치된 에이전트 창에 MODE를
자연어로 지시한다. 현재 이 스킬은 대화형 진입점이며, 실행과 상태 저장은 검증된
`th protocol` Runner에 맡긴다. 따라서 실행 중 생긴 worker는 아직 Orca Dispatch가
아니며 Orca 작업자 창으로 표시되지 않는다.

## `th cord`로 코디네이터 시작

`th cord`는 **에이전트 대화 입력 명령**이다. raw shell에서 실행하는 `th` CLI
subcommand가 아니다.

사용자가 에이전트 프롬프트에 `th cord`라고 입력하면 이 세션을 Harness Protocol의
Entry AI로 전환한다. 다음을 한 번 확인한다.

1. 현재 작업 디렉터리와 대상 Git 저장소
2. `th protocol --help`가 실행되는지
3. 최근 protocol 실행이 있는지(`th protocol status`, 읽기 전용)

준비가 끝나면 아래처럼 짧게 답하고 다음 명령을 기다린다.

```text
team-harness 코디네이터 모드가 준비됐습니다.
대상: <현재 저장소>
명령: A <작업> | B [agent] | C <작업> | 상태 | 토큰 | 종료
```

이후 같은 대화에서 `A`, `B`, `C`를 명시한 요청은 아래 계약으로 처리한다. 사용자가
`종료`, `th cord off`, `코디네이터 종료`라고 하면 역할을 끝내고 일반 에이전트로
돌아간다고 확인한다. 단순 질문에 코디네이터가 맞느냐고 물으면, 활성화된 세션에서는
“이 Orca 창의 team-harness 진입 코디네이터”라고 답한다. 내부 `th run`의 모델
코디네이터와 동일한 프로세스라고 주장하지 않는다.

## 한 번만 준비하기

한 요청을 처리하는 동안 아래 자료와 상태는 처음 한 번만 읽고 재사용한다.

1. 현재 저장소의 `AGENTS.md`
2. 필요한 경우 `design/harness_protocol/MODES.md`, `WORKFLOW.md`,
   `ENGINEERING_POLICY.md`, `HARNESS_AGENTS.md`
3. 현재 Git 상태와 선택한 protocol 실행의 `protocol_state.json`

파일이 실행 도중 바뀌었다는 증거가 없으면 같은 자료를 단계마다 다시 읽지 않는다.
Orca의 구조화된 조율 기능을 직접 사용해야 하는 후속 구현에서는 설치된
`orchestration` 스킬도 한 번 읽는다.

## 명령 해석

MODE로 명시한 표현만 MODE 명령으로 해석한다. 예를 들어 “A/B 테스트를 고쳐줘”의
알파벳은 MODE A가 아니다.

| 사용자 표현 | 동작 |
|---|---|
| `A로 해: <작업>`, `MODE A <작업>` | 새 MODE A 실행 |
| `C로 해: <작업>`, `MODE C <작업>` | 새 MODE C 실행 |
| `B로 이어서 해`, `MODE B로 <agent>에게 넘겨` | 기존 실행을 MODE B로 인계 |
| `A 결과에서 Codex 선택`, `2번 선택`, `Hybrid` | WAITING_USER인 MODE A 재개 |
| `C 이어서 해` | 저장된 MODE C를 완료되지 않은 최초 stage부터 재개 |
| `상태 보여줘`, `토큰 얼마나 썼어` | 저장 상태를 읽기 전용으로 조회 |

MODE 뒤에 작업 설명이 없고 대화에도 목표가 없을 때만 한 문장으로 목표를 묻는다.
기존 실행을 가리키는 TASK-ID/run-dir가 대화에 있으면 재사용한다. 없다면
`th protocol status`로 후보를 확인한다. 현재 저장소의 후보가 하나면 사용하고,
여러 개면 잘못된 작업을 인계하지 않도록 후보를 보여주고 하나를 묻는다.
사용자가 지정한 agent 이름은 설치된 protocol agent type에 맞는 소문자 값으로
정규화한다(예: `Codex` → `codex`).

## 실행 절차

1. 현재 Orca 작업 트리의 절대경로를 대상 저장소로 정한다.
2. `git status`, 현재 branch, 기준 commit, worktree 목록을 확인한다. Runner도 같은
   검사를 수행하지만, 코디네이터는 실행 전에 대상이 맞는지 사용자에게 보여준다.
3. 사용자 문장을 다음 Runner 동작으로 변환한다.

```text
A 시작  -> th protocol run --mode a "<작업>" --repo <현재 작업 트리> --no-visible
C 시작  -> th protocol run --mode c "<작업>" --repo <현재 작업 트리> --no-visible
B 인계  -> th protocol relay --task-id <ID> --run-dir <DIR> --repo <repo> --next-agent <agent> --no-visible
A 선택  -> th protocol resume --task-id <ID> --run-dir <DIR> --repo <repo> --selection <선택> --no-visible
C 재개  -> th protocol resume --task-id <ID> --run-dir <DIR> --repo <repo> --no-visible
조회    -> th protocol status --run-dir <DIR>
```

작업 문자열과 경로를 셸 코드로 이어 붙이지 말고 인자로 안전하게 전달한다. 사용자의
작업 설명은 의미를 요약해 바꾸지 않고 보존한다. Orca 안에서는 tmux 관찰 창이 중복되므로
`--no-visible`을 사용한다.

4. 출력된 TASK-ID와 run-dir를 대화 상태에 보존한다. 정상 반환만으로 성공이라 하지
   않고 `status`, stage, blocker와 worker 결과를 확인한다.
5. MODE A가 `WAITING_USER`이면 비교 보고서와 다음 선택지를 사용자에게 보여준 뒤
   기다린다: `SELECT_WORKER_1`, `SELECT_WORKER_2`, `SELECT_HYBRID`, `REWORK`, `CANCEL`.
   실제 state의 worker 번호와 agent 이름을 함께 표시해 이름을 추측하지 않는다.
6. base branch merge, cherry-pick, 수동 통합, push는 별도 사용자 선택 전에는 실행하지
   않는다. task worktree의 protocol checkpoint는 MODE 실행 지시에 포함된 권한이다.

## graft

사용자가 `graft 켜고 A로 해`처럼 이번 실행만 요청하면 해당 Runner 프로세스에
`TEAM_HARNESS_CONTEXT_GRAPH=1`을 전달한다. 항상 켜 달라고 요청하면 대상 저장소의
`.team-harness/config.toml`에 아래 설정을 추가하고 변경 사실을 알린다.

```toml
[context_graph]
enabled = true
```

graft는 worker 작업 디렉터리마다 실행 중 한 번만 준비한다. 빌드 실패는 경고로
보고되며 MODE 실행 자체를 실패로 바꾸지 않는다. 같은 실행의 worker마다 코디네이터가
직접 `graft build`를 반복하지 않는다.

## 토큰과 사용량

`th protocol status --run-dir <DIR>` 결과에서 다음을 구분해 보고한다.

- 실행 시간은 Runner가 직접 잰 값이다.
- 토큰과 비용은 worker CLI가 보고한 stage만 합산한 값이다.
- `N개 stage 중 M개 반영`이면 부분 합계라고 그대로 표시한다.
- Orca 상태 표시줄의 계정 한도 사용률은 provider 전체 계정 상태이며 이 실행의 토큰
  합계가 아니다.

토큰을 보고하지 않은 worker를 0으로 계산하거나, 가격표로 비용을 추정하지 않는다.

## 사용자에게 보여줄 결과

실행 시작 시 MODE, 대상 저장소, TASK-ID와 run-dir를 짧게 알린다. 완료 또는 중단 시에는
현재 stage와 상태, 결과 branch/checkpoint, 검사 결과, blocker, 사용량의 완전성을
보고한다. MODE A의 선택 Gate나 BLOCKED 상태에서는 다음에 사용자가 답해야 할 내용을
한 문장으로 명확히 제시한다.
