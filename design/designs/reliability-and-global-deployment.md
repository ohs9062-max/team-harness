# 검수·문서 편집 보존과 전역 배포

## 문제와 목표

문서 이관이 실패하면 저장해 둔 원문을 덮어써서 그 사이 사용자가 추가한 규칙까지 잃을 수 있었다. 검수는 DONE 시각만 같으면 실제 검사 대상이 바뀌어도 이전 PASS를 반환했다. push 관문은 과거의 채택 문구 하나로 다른 새 결정을 통과시켰다. 이 설계는 파일 내용·검토 대상·프로세스 시작 신원을 근거로 작업을 적용하며, 검증한 코드를 다른 코디네이터가 전역 명령으로 사용할 수 있게 한다.

## 문서 이관

`docs_migrate.launch`는 메인 체크아웃의 깨끗한 규칙 문서를 저장하고 작업 폴더 아래 detached worktree를 만든다. 워커는 그 worktree의 AGENTS.md·CLAUDE.md와 지정 보관 문서만 편집한다. 원본이 수정되는 동안 이관 워커는 원본에 쓰지 않는다.

`finish`는 시작 당시 대상 문서 중 하나 이상이 줄었는지 확인한다. CLAUDE.md만 길어서 시작한 작업도 허용한다. 문서 삭제·심볼릭 링크·원문 줄 누락·본문 증가를 거부한다. 줄었거나 원래 내용이 제거된 문서 각각에 생성한 보관 문서 경로가 남아 있지 않으면 적용하지 않는다. 예를 들어 두 번째 문서에서 경위 세 줄을 옮기고 제목 세 줄을 추가해 줄 수가 같아도 보관 경로가 필요하다. 변경하지 않은 문서는 참조를 추가하지 않아도 된다. 원문 줄의 보존은 기계적으로 확인할 수 있지만 어떤 문장이 현재 유효한 규칙인지의 의미 판단까지 증명하지는 않는다. 워커 지침은 현재 규칙을 본문에 보존하고 경위만 옮기도록 한다.

메인 문서가 원문과 같고 적용 경로에 미커밋 변경이 없을 때만 Git의 `apply --index`로 문맥과 인덱스를 확인하며 적용한다. 적용·커밋 실패 시 현재 파일을 복원하거나 삭제하지 않고 실패와 제안 위치를 남긴다. 이전 형식의 in-place 작업도 실패 시 원문 덮어쓰기를 하지 않는다. 독립 worktree는 성공·실패 모두 점검용으로 보존하며, 자동 강제 삭제하지 않는다.

## 검수 입력과 재검수

`worker_digest.check_script`는 임의 셸 스크립트의 입력을 추측하지 않는다. 기본은 실제 재실행이다. 작업자가 모든 파일 입력을 worktree 기준 경로 또는 절대 경로의 JSON 배열 `verify_inputs.json`에 선언하면 스크립트·경로·DONE 세대·입력 파일 내용으로 캐시 키를 만든다. 누락·잘못된 선언·디렉터리·링크 입력은 캐시를 끈다. 검사가 입력을 바꾸면 결과를 저장하지 않는다.

시간·환경·네트워크·선언하지 않은 파일에 의존하는 검사는 이 캐시를 사용하면 안 된다. 대용량 디렉터리 전체 해시를 매 사건마다 만들지 않는다. `th-verify.sh <task> --force`는 선언된 캐시도 무시한다. 외부 파일 변경을 항상 실시간 감시한다는 계약은 아니다. 새 산출물은 재검수해야 한다.

## 프로세스 종료와 자원 예약

`worker_relaunch`는 worker.pid.start의 PID와 시작 신원을 읽는다. 기존 셸 토큰 boot:ticks는 공통 linux:boot:ticks로 변환한다. 시작 신원이 없거나 바뀌면 종료를 보류한다. 실제 TERM/KILL은 `agents/process_identity.kill_group`가 신호마다 신원을 확인한다. 긴 job을 보존하는 경로도 job 그룹 분리를 재확인하고 같은 함수를 사용한다.

`resource_slots`는 전역 watch 디렉터리의 admission.lock 아래 실행 제한 확인과 슬롯 예약을 함께 수행한다. 예약 중에는 launcher/coordinator 시작 신원을, 실행 후에는 worker 그룹 신원을 저장한다. 프로세스가 사라지거나 신원이 바뀐 슬롯은 다음 예약에서 회수한다. 기존 tasks.txt 작업도 중복 없이 포함한다. 같은 이름의 다른 작업과 동일 폴더의 여러 예약은 각각 센다.

공유하는 강제 정책은 동시 실행 수·최근 Codex 예산·쿼터 잠금이다. 모델 상향 사유·task kind·셸 재시도 상한은 기존 th-launch 운영 정책이며 SDK 전체에 새로 강제하지 않는다. 새 SDK 생성도 자원 부족이면 실행 전에 실패할 수 있다. SDK 반환 구조와 TH-D3 의미는 유지하며 사용자 지정 template·provider는 제거하지 않는다.

셸 사전 확인도 공유 예약의 SDK·protocol 워커를 포함한다. 사전 확인은 이후의 슬롯 확보를 보장하지 않으므로 `th-launch`는 실제 예약에 성공한 뒤 컨텍스트를 등록하고 이전 완료·실패·검수 표식과 실행 시도 기록을 변경한다. 프로세스 생성 전에 실패하면 준비한 컨텍스트와 예약을 해제해 같은 작업 키를 다시 사용할 수 있게 한다. 예약만 한 런처도 좀비 상태이면 슬롯을 회수한다. DONE 잔류 프로세스를 정리하는 셸 호출도 작업 경로를 Python에 넘겨 같은 시작 신원 확인을 거친다.

## 채택 검증

`crosscheck.decide`는 사람 읽기용 CROSSCHECK.md와 함께 adoption.json을 기록한다. 최신 채택/기각, 검증 manifest·증거 목록·판정 원문·보고서 해시, 실제 검토한 design/decisions.md의 내용 해시를 보존한다.

`verdict`·`notify`는 현재 증거의 유효성을 계속 검사하지만 채택 기록이 생긴 뒤에는 결정 당시 CROSSCHECK.md를 다시 쓰지 않는다. 조회 시각만 달라져도 보고서 해시가 깨지던 문제를 막기 위한 구분이다. 증거 변경이 발견되면 현재 판정은 보류가 될 수 있으며, 저장된 결정을 갱신하는 동작은 명시적 `decide`로 제한한다. 조회와 결정의 파일 작업은 같은 폴더 잠금으로 직렬화한다.

재결정 중 파일 저장 오류가 발생하면 이전 보고서와 채택 기록을 복원한다. 실제 receipt 임시 파일 쓰기·교체 실패를 주입해 이전 승인이 계속 검증되는지 확인한다. 두 파일을 한 번에 교체하는 OS 트랜잭션은 아니므로 프로세스 강제 종료나 복원 자체의 디스크 오류까지 원자성을 보장하지는 않는다. 이때 해시가 어긋나면 push 관문은 승인을 거부하며, 자동으로 채택을 복구하거나 불완전한 증거를 승인하지 않는다.

기본 Codex 검증자가 예산·쿼터로 거부되면 제한을 우회하지 않는다. `th-adopt.sh <task> <claim> --worker agy`로 별도의 단일 검증자를 명시 선택할 수 있다.

pre-push는 새 TH-D를 추가하는 각 커밋의 Crosscheck 트레일러를 따로 검사한다. 검사 대상 커밋의 결정 문서가 검토한 내용과 같고 기록이 바뀌지 않았어야 한다. 다른 커밋의 승인이나 Markdown 문구만으로 통과하지 않는다. 기존 Markdown-only 기록은 새 결정 push를 승인하지 않으며 재판정 또는 기록되는 TH_CROSSCHECK_SKIP 사유가 필요하다. 이는 운영 관문이며 악의적인 로컬 파일 위조를 막는 보안 서명은 아니다.

## 유지보수와 배포

watchd는 사건·검수 결과 전달을 담당한다. 별도 maintenance 프로세스가 일일 보고·보관·문서 이관과 배포 확인을 수행한다. 개별 유지보수 실패는 로그에 남기고 다른 업무를 계속한다. run/repl·SDK·protocol 시작은 소유 workspace를 등록하고 singleton maintainer를 요청한다. 테스트의 TH_ONCE 또는 TH_NO_MAINTENANCE=1은 상주 프로세스를 만들지 않는다.

일일 보고의 대상은 maintenance 소유자 등록부와 tasks.txt·tasks_done.txt의 합집합이다. SDK로만 실행하거나 완료 작업이 보관 등록부로 옮겨져도 보고 대상에서 빠지지 않는다. 소유자 등록은 중복을 피하고 조회는 빈 줄·손상된 행을 건너뛴다.

`th-sync.sh --apply`는 정본 체크아웃의 tracked 변경이 커밋되고 origin/main에 push된 경우만 배포한다. 전역 명령을 정본 소스에 연결하고 공통 정책·워커 규칙·스킬·hook을 설치한다. 기존 provider·키·모델·사용자 prompt는 보존한다. 식별 가능한 예전 관리 prompt만 공통 정책 참조로 이관하며 그 안의 실험 기준은 사용자 확장으로 남긴다. 파일을 바꾸기 전 홈의 deployment-backups에 보관한다. 설정 내용과 인증 정보는 저장소나 상태 출력에 노출하지 않는다.

워크트리는 ancestor 관계일 때만 ff-only로 갱신한다. 이미 배포 revision을 포함하고 추가 local commit이 있는 작업선은 contains_revision으로 표시하고 보존한다. 사용자 편집과 충돌하거나 이력이 분기되면 blocked로 기록하고 stash/reset/강제 checkout을 하지 않는다. 해당 워크트리의 전역 명령은 정본 설치를 계속 사용한다. `--status`는 파일·링크 해시, 소스 미커밋 변경, 정본 HEAD와 배포 revision의 차이 및 각 워크트리의 실제 HEAD를 보여준다. 배포 manifest는 마지막 성공한 설치를 가리킨다. 여러 전역 파일의 교체는 하나의 OS 트랜잭션이 아니므로 중간 실패는 오류로 보고하고 다시 동기화한다.

`.th_docs/docs_migrate_*/worktree`는 검토용 이관 산출물이므로 배포 갱신에서 제외하고 그 이유를 표시한다. 설치 별칭·설정·hook·AGY 지시문도 변경 전 백업과 배포 해시 추적에 포함하며, 바이트·링크가 같으면 불필요한 백업을 만들지 않는다. 설치 스크립트는 임시 HOME에 복사한 기존 설정을 먼저 병합하고 선언한 자산의 변경분만 실제 홈에 적용한다. 관리 블록 밖 사용자 재읽기 규칙을 설치기가 바꾸지 않으며 같은 hook을 반복 설치하지 않는다. 새 세션의 SessionStart(startup·clear)는 이미 읽는 정책 버전을 기록하고, 기존 세션의 다음 입력에서만 변경된 정책을 전달한다. resume·compact를 새 세션 시작처럼 초기화해 변경 알림을 잃지 않는다. 버전 커서는 워크스페이스별 잠긴 JSON 파일에 최근 128개 세션까지만 보관한다. 오래된 커서가 정리된 세션은 다음 입력에서 정책을 다시 확인할 수 있다.

관리 hook의 판정은 journal·image·zone 설치기가 공통 `hook_identity` 함수를 사용한다. 명령을 실행하지 않고 셸 인자로 파싱해, 알려진 관리 스크립트를 표준 형식으로 호출하는 경우만 교체한다. `echo journal-hook.sh`처럼 이름을 출력하거나 인자로 쓰는 사용자 명령, 따옴표 안 명령 치환과 모호한 복합 명령은 보존한다. 관리 스크립트 뒤의 사용자 인자·환경 설정도 보존 대상으로 구분한다. 부분문자열만으로 hook을 삭제하지 않으며, 임시 설치 HOME과 실제 배포 HOME의 경로를 구분해 같은 관리 hook의 중복을 정리한다. 이 함수는 일반 셸의 의미를 완전히 해석하는 도구가 아니며, 확실하게 식별하지 못한 명령은 삭제하지 않는다.

새 프로세스는 즉시 새 코드를 사용한다. Claude의 기존 UserPromptSubmit hook은 다음 사용자 입력에서 정책 버전이 달라진 경우 한 번만 새 정책을 제공한다. 읽은 prompt·장기 실행 subprocess·이미 import한 라이브러리 코드를 소급 교체하지 않는다. maintenance는 Python 소스 변경 시 다음 주기에 재실행한다. 실행 중 작업을 업데이트 목적으로 죽이지 않는다.

## 적용 경로 표

| 경로 | 붙는 지점 | 검증 |
|---|---|---|
| th-launch | 상태 변경 전 슬롯 예약, 준비 실패 롤백, PID 기록 후 bind | test_resource_slots, test_scripts_shell |
| th run / repl / SDK | 공통 agents.spawner, startup maintenance 등록 | test_spawner, test_archive_paths, test_harness |
| protocol A/B/C | 기존 공통 spawner와 startup 등록 | test_archive_paths, protocol 회귀 테스트 |
| watchd / th-watch | 작업 경로 전달과 실제 신원 확인 종료, 독립 maintenance 기동 | test_done_linger, test_rollover_watchd, test_scripts_shell |
| ths | 기존 관찰 등록부 유지, 실행 슬롯을 만들지 않음 | test_status_coordinator_groups |
| 검수 | worker_digest 공통 함수와 --force | test_worker_digest |
| 문서 이관 | 독립 worktree와 조건부 적용 | test_docs_migrate |
| 일일 보고 / SDK·protocol 소유자 | maintenance 등록부와 현재·완료 작업을 공통 조회 | test_maintenance, test_loss_report |
| push 관문 / 결과 조회·알림 | 각 커밋에 adoption_gate 적용, 결정 보고서 보존 | test_crosscheck, test_adopt_redesign |
| 전역 배포 / 다른 worktree | deployment와 공통 명령·정책, 이관 산출물 제외 | test_deployment, test_policy_notice |
| journal·image·zone 설치기 | 공통 hook_identity로 실제 관리 호출만 교체 | test_hook_identity, test_deployment, test_policy_notice |
| 코디네이터 정책 | 전역 지시문·스킬, config의 operating_policy_file | test_config, test_policy_notice |

## 호환성

TeamHarnessResult·세션 manifest·provider·작업 template·protocol 원본 보호 계약을 유지한다. operating_policy_file·verify_inputs.json·--force·th-sync는 추가 기능이다. 검수 횟수와 자원 부족 시 생성 실패, 시작 신원 없는 워커의 종료 보류, 새 TH-D 승인 파일 형식은 의도적인 동작 변경이다. 전역 자동 배포는 초기 명시적 설치 이후 활성화되며 홈 디렉터리가 다른 사용자를 임의로 설정하지 않는다.
