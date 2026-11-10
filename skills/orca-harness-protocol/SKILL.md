---
name: orca-harness-protocol
description: Claude 코디네이터에서 th cord로 작업 분담·워커 실행·완료 사건·A/B/C 흐름을 운영한다. 워커로 실행 중이거나 team-harness 자체 코드를 개발하는 경우에는 적용하지 않는다.
---

# Orca 코디네이터

`TH_ROLE=worker`이면 이 스킬을 적용하지 않는다. `th cord`는 셸 subcommand가 아닌 에이전트 프롬프트 명령이다. 일상 코디네이터는 Claude이며 Codex 워커는 그대로 지원한다.

## 시작과 공통 정책

- 전역 `~/.team-harness/operating_policy.md`를 한 번 읽는다. 개발 체크아웃에서는 `design/designs/operating_policy.md`가 정본이다. 모델·위임·읽기·재실행·검증 기준을 여기서 중복 정의하지 않는다.
- 현재 저장소와 `th protocol --help`, 최근 protocol 상태를 한 번 확인한다. workspace current_status로 사용자 의도와 진행 작업을 복원한다.
- 준비 후 대상 저장소와 `A <작업> | B [agent] | C <작업> | 상태 | 토큰 | 종료`를 짧게 안내한다. 종료 요청이면 일반 에이전트 역할로 돌아간다.

## 작업 위임

- 작은 명확한 작업은 `th-launch.sh <작업폴더> codex <작업트리>`로 맡긴다. 실행 모델·effort가 필요하면 명시 인자를 사용한다. 프로젝트의 읽을 규칙·산출물·검수 기준을 task.md에 적는다.
- 코드 설계와 독립 검수가 필요할 때 MODE C, 독립 대안 비교에는 A, 기존 작업 인계에는 B를 사용한다. 정확한 계약은 정본 `design/harness_protocol/MODES.md`, 인자는 CLI 도움말을 따른다.
- protocol 결과는 task branch/worktree에 남는다. 사용자가 승인한 범위에서만 base 통합·push한다. 이전에 받은 승인을 반복해서 묻지 않는다.
- 워커가 재부모화되는 경우 최초 PID만 보고 다시 띄우지 않는다. 긴 본 작업은 th-job-start.sh로 분리한다. 쿼터 잠금과 실행 거부의 원인을 먼저 확인한다.

## 사건과 완료

- 첫 워커 시작 전에 세션별 `th-inbox-wait.sh` 하나를 백그라운드로 준비한다. 깨어나면 사건을 처리하고 다시 대기한다. LLM 감시 워커와 반복 상태 조회는 만들지 않는다.
- inbox는 Git 공통 디렉터리의 메인 체크아웃에 있다. 다른 세션의 사건을 대신 소비하지 않는다.
- 상태 요청은 짧은 `ths` 한 번, 지속 표시는 사용자의 별도 터미널 `ths -w`를 사용한다. 정상 표를 대화에 다시 복사하지 않는다.
- 완료는 스크립트 판정과 산출물로 확인한다. FAIL/WARN이면 원본 검수 결과·관련 로그를 필요한 범위만 확인한다. important 결론은 `th-adopt.sh`로 독립 검증하고 채택 이유를 기록한다.
- 정책·기능 배포 상태는 `th-sync.sh --status`로 확인한다. 배포 변경 알림을 받은 세션은 변경된 정책만 한 번 반영하고 진행 작업은 유지한다.
