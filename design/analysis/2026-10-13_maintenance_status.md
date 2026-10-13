# team-harness 유지보수 현황과 예정 작업 (2026-10-13)

하루 동안 진행한 정제 작업의 결과, 확인 수준, 남은 문제, 다음 작업을 정리한다. 다음 코디네이터가
이 문서만 읽고 이어갈 수 있게 쓴다. main 커밋 범위: `481d3aa..921c3fc` (65개, 모두 origin push).

## 1. 사용자 작업 방식과 결정 (이후 작업의 전제)

- Orca 에이전트 창에서 `th cord` 코디네이터에게 대화로 맡긴다. 옵션(모델, graft, Jev)은 코디네이터가 판단한다.
- 코디네이터 세션을 길게 유지한다. 토큰보다 맥락·의도 파악이 우선이고, 토큰 절감은 워커 쪽에서 찾는다.
- **로직 구현·코드 변경은 codex**(terra 기본, sol은 terra 실패·설계 판단 시). **agy는 단순·대량·비코드 작업**과
  codex 쿼터 잠금 시 대체에만 쓴다. agy 결과가 숏컷이면 terra로 다시 돌린다.
- 작은 수정도 코디네이터가 직접 하지 않고 워커에 맡긴다. 코디네이터는 지시·검증·병합만 한다.
- team-harness 작업 산출물은 저장소 `_outputs/`(git 무시)에 둔다.
- 검증된 변경은 main 병합과 push까지 해도 된다. **검사가 통과해야만 병합하도록 `&&`로 연결한다.**
- 코디네이터를 깨우는 동작(완료·실패·쿼터·소실·멈춤)은 유지한다. 깨우는 횟수 줄이기는 하지 않는다.
- 숏컷 추가 검사(사용처 없는 함수, 범위 밖 파일)는 하지 않는다. 워커 선택으로 대응한다.
- 옛 protocol 실행 기록은 정리하지 않고 그대로 둔다.

## 2. 오늘 해결한 문제

### 2.1 알림·감시 (`scripts/`)
| 문제 | 해결 | 확인 수준 |
|---|---|---|
| 감시가 사건 하나 뒤 종료, 6시간 뒤 조용히 종료 → 멈춤을 모름 | 상시 감시 데몬 `th-watchd.sh`(th-launch가 자동 시작), 데스크톱 알림, 작업 공간별 `.claude/inbox.md` | 데몬 실동작, 자동 시작 실사용 확인 |
| 다른 코디네이터의 사건을 가로챔 | `.th_owner` 소유자 기록, 없으면 `work_log`로 추론 | 실사용 확인 |
| 등록부 동시 쓰기 유실, `ths` 오표시, claude 워커 미지원, 손자 프로세스 CPU 누락 등 | flock, 워커 PID 확인, claude 분기, 자손 CPU 합산 등 (ops-02~05, 07~09, 14, 15, 25) | 테스트 |
| 쿼터 소진 뒤 같은 워커 반복 실행 | `quota_lock.<워커>` — th-launch가 거부(종료 코드 3) | 테스트 |
| 데몬 자동 시작 경로 버그, 테스트가 데몬 59개 누수 | script_dir을 cd 전에 계산, 테스트가 데몬 종료 | 전후 데몬 수 0→0 |
| LLM이 sleep→확인을 반복하며 토큰 소모 | 코디네이터·워커 규칙에 폴링 금지 | 규칙 |

### 2.2 결과 검증
- 지시서 `## 검수` 블록 → 작업 폴더 `verify.sh`. 완료 시 감시가 실행해 `VERIFIED`/`VERIFY_FAILED`, 알림에 "검수 통과/실패".
  공통 검사: `uv.lock`·`pyproject.toml` 변경 경고, 저장소 루트 보고서 파일. 실사용 4건 통과.
- 한계: 사용처 없는 함수, 하지 않은 확인을 했다는 보고, 범위 밖 수정은 잡지 못한다(의도적으로 추가하지 않음).

### 2.3 토큰
| 문제 | 해결 | 측정 |
|---|---|---|
| 조율자 고정 전송량 | 도구 설명 축약, 실행마다 바뀌는 값을 프롬프트 끝(Run context)으로 | 6,855→6,063 토큰(근사) |
| 워커가 사용자 MCP 상속 | claude `--strict-mcp-config`, codex `-c mcp_servers={}` (TH-D25) | — |
| 워커가 코디네이터 스킬·긴 규칙 문서를 읽음, codex 워커 기본이 sol | 워커 규칙·꼬리말·스킬 설명 수정, codex 워커 기본 terra | th run 워커 18.7만→9.7만 |
| 작은 작업에 MODE C(3단계) | 작은 작업은 codex 워커 하나 기준, th run 조율자·protocol에도 경중별 모델 선택 | 규칙 |
| 자동 압축이 매 턴 반복 | 압축 후 토큰 추정치 갱신 (CORE-2) | 재현 테스트 |
| Jev가 거의 미적용 | 기준 0.85→0.6(전역 설정), 연속 3회 오류 시 그 실행에서 끔 | 실 API 호출 확인, 7회 중 2회 적용 |

### 2.4 Harness Protocol (A/B/C)
- 추적 안 되는 파일(`data/`, 보고서)로 실행 전 검사에서 막히던 문제 → 무시, 수정된 추적 파일은 경고 후 진행.
- 대상 저장소 venv·pytest로 검사, 취소·예외 시 BLOCKED 기록, `th protocol clean`(기본 미리보기), 재개 경로, ignored 디렉터리 링크 옵션.
- **실사용 확인**: robot_vision3_autolabel에서 MODE C 완주(설계 claude → 구현 codex terra → 검수 agy, 393초).

### 2.5 기타 버그
- `th run` 답이 두 번 출력·저장(codex 스트림 delta와 완료 항목 중복) 수정.
- `uv.lock`에 `python-dotenv` 누락(9월 `15eaeea`부터) — 고정 모드 테스트 실패로 워커들이 우회 코드를 쓰게 만든 근본 원인. 잠금 갱신.
- core 11건(CORE-1~10, 12): 워커 출력 누락, 상대경로 cwd, grep 타임아웃, ls/glob 상한, 종료 데드라인 등.
- `th run` 내장 조율자가 바깥 코디네이터의 `.claude/current_status.md`를 덮어씀 → run_dir 아래 작업일지, 작업 폴더 `.claude` 파일 쓰기 거부.

### 2.6 문서·규칙
- `start.md`: 기능별 사용법, 붙어 있는 도구 목록(0절), 옵션은 코디네이터 관리.
- 코디네이터 스킬: 옵션 판단, 사용자 의도 기록, 폴링 금지, 작업 크기별 실행 방식, agy 사용 범위, inbox 먼저 읽기.
- Jev 키: `~/.team-harness/secrets.env`(저장소 밖, 600), `.gitignore`에 `.env`·`secrets.env`.

## 3. 리뷰 결과 (2026-10-13 전체 리뷰)

원문: `_outputs/review_20261013/{core,protocol,ops}/FINDINGS.md` (core 15, protocol 25, ops 25).
반영: core 11, protocol 14, ops 14. 나머지는 4.2 참고.

## 4. 남은 문제와 예정 작업

### 4.1 다음에 할 것 (권장 순서, 코드 변경은 codex)
| # | 문제 | 근거 |
|---|---|---|
| A1 | MODE C가 대상 저장소에 `DESIGN.md`를 커밋 | docstring 1줄 작업에 146줄 DESIGN.md가 task 브랜치에 들어감. `protocol_outputs/`로 옮겨야 함 |
| A2 | agy 실제 PID 탐지 실패 | th-launch가 agy마다 "reparented PID not found within 20s" 경고 |
| A3 | `FAILED.*` 변형 이름을 실패로 인식 못 함 | 라벨링 세션 `FAILED.round1_metadata_indexing`이 "중단?"으로 표시 |
| A4 | `ths`에 오래전 완료 작업이 40줄 가까이 남음 | 예전 감시가 완료 뒤 등록부에서 빼지 않음. 기본은 최근만, `--all`로 전체 |
| A5 | `.claude/inbox.md` 무한 증가 | 데몬이 추가만 함. 읽은 줄 표시나 회전 필요 |
| A6 | codex 조율자의 캐시 토큰이 실행 기록에 비어 있음 | 캐시 개선 효과를 측정 못 함. 원인 조사부터 |
| B1 | `th init` 설정 템플릿에 `worker_isolate_mcp` 없음, 같은 설정 이름 2개(`worker_strict_preamble`) | ops-10, ops-22 |

A1~A5와 B1은 codex 워커 하나로 묶고, A6은 조사를 따로 한다.

### 4.2 보류
- ops-16(agy 설정 경로), ops-13·P14(장시간 작업 기본 타임아웃), P04·P12·P19(MODE A·B 재개·확장), P22·P24(task-id 지정, tmux 창 정리),
  P25(상태 파일 키 마스킹), CORE-13~15, 테스트 assert 보강(ops-11, 17, 18, 23, 24), ops-19, ops-20.

### 4.3 관찰 뒤 판단
- Jev 감시 판단: 데몬이 `~/.team-harness/watch/jev_decisions.log`에 기록만 한다. 1~2주 쌓이면 판단 품질을 보고 적용 여부를 정한다.
- graft를 th-launch 워커에 연결할지: 워커 입력 토큰 중 저장소 탐색 비중을 측정한 뒤 판단.
- 고친 뒤 MODE C 토큰 재측정(워커가 규칙 문서를 덜 읽는지).

### 4.4 정리 거리
- 병합 완료된 작업트리 8개(`/home/hs/orca/workspaces/team-harness/fix-*`)와 `fix/*` 브랜치 삭제.
- 실사용 테스트 브랜치 `task/20261013_044753_005c735e/pipeline`(robot_vision3_autolabel) — 사용자 결정 대기.
- `~/다운로드/생성물/token_saving_20261013` 이동 여부 — 사용자 결정 대기.
- 재부팅 뒤 데몬 자동 시작(systemd 사용자 유닛, `scripts/th-watchd.service`) — 원할 때만.

## 5. 다음 코디네이터를 위한 주의

- pytest는 `env -u PYTHONPATH`(전역 ROS 경로가 pytest 플러그인을 깨뜨림), uv는 `--frozen`. 새 작업트리는 `uv sync --frozen --extra dev`부터.
- 검사와 병합은 반드시 `&&`로 연결한다(2026-10-13 `;` 연결로 포맷 위반이 main에 push됨, `921c3fc`로 수정).
- 감시는 백그라운드 명령으로만 띄운다. 포그라운드 명령 안에서 `&`로 띄우면 알림을 못 받는다.
- 프로세스 확인은 `pgrep -f '^bash .*/th-watchd[.]sh$'`처럼 시작 패턴으로. 단어만 쓰면 워커 프롬프트에 걸린다.
- `th run` 실험을 코디네이터 작업 폴더에서 돌리지 않는다(수정 전 버전이 상태 파일을 덮어썼음).
- 라벨링 세션의 `p3_pseudo_st`, `p4_strong`은 실제로 죽어 있었다(그쪽 코디네이터 몫).
