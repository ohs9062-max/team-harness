# 독립 반증 검증

중요한 가설, 원인 확정, 실험 성공 판정 또는 아키텍처 결정을 코디네이터가 채택하기 직전에 실행한다. 오탈자나 경미한 변경마다 실행하지 않는다. 두 모델은 같은 주장과 증거를 받고 각자 그 주장이 틀렸을 가능성을 찾는다. 코디네이터가 최종 채택 권한을 유지한다. 과거 시범에서 두 모델이 같은 오진을 발견했으나, 한 모델이 추론을 사실처럼 보고하기도 했으므로 코디네이터는 PASS를 받은 근거도 원본과 대조해야 한다.

`scripts/th-crosscheck.sh start 결과폴더 --claim 결론파일 --criteria 기준파일 --evidence 증거경로... --source 원본경로... [--repo 저장소]`로 시작한다. 증거는 각 검증자에게 복사하고 해시를 고정한다. `--source`는 코디네이터가 고른 묶음 외에도 검증자가 누락이나 선별 편향을 찾아볼 원본 전체의 위치다. 접근 제한된 원본은 각자 복사본으로 제공한다. 접근 가능한 원본은 경로만 전달한다. 같은 결론과 증거를 다시 검증하려면 `--force`가 필요하다. 복사량 제한은 기본 50 MiB이며 `TH_CROSSCHECK_MAX_MB`로 바꾼다.

`verdict 결과폴더`는 CROSSCHECK.md를 갱신한다. 판정은 VERDICT.md 마지막 비어 있지 않은 줄의 `VERDICT: PASS`, `VERDICT: FAIL`, `VERDICT: INSUFFICIENT_EVIDENCE` 중 하나여야 한다. FAILED, LAUNCH_FAILED, 종료된 프로세스의 DONE 누락, 시간 초과, 증거 또는 복사본 변경, 저장소 변경은 UNAVAILABLE이다. `TH_CROSSCHECK_TIMEOUT_MIN`의 기본값은 30분이다. 저장소 상태를 감시하려면 시작할 때 `--repo`를 지정한다.

| 결과 | 종합과 종료 코드 |
|---|---|
| PASS + PASS | 채택 가능, 0 |
| 한쪽이라도 FAIL, INSUFFICIENT_EVIDENCE, UNAVAILABLE | 채택 보류, 1 |
| 아직 실행 중인 검증자 | 실행 중, 3 |

`decide 결과폴더 --adopt --reason 이유` 또는 `--reject`로 결정을 기록한다. 채택 보류를 채택할 때는 `--override-reason`이 필요하다. 기록은 결과 보고 전체를 포함해 반례와 미해결 항목을 보존한다.

읽기 전용은 지시서와 사후 해시 비교에 의존하며 OS 샌드박스가 아니다. 접근 가능한 원본이나 저장소 전체가 아닌 부분의 변경, 작업 중 변경 후 원상 복구, 외부 정보 유입은 완전히 막지 못한다. 사전 선택된 증거가 결론을 유리하게 보이게 할 수 있으므로 원본을 별도로 지정하고 직접 살펴본다. 사용량이나 동시 실행 상한에 걸리면 기존 th-launch가 해당 실행을 거부하며 이 도구는 LAUNCH_FAILED를 기록한다. 다른 검증자는 별도로 시도하고 어느 실패도 PASS로 바꾸지 않는다.

| 실행 경로 | 적용 위치와 확인 |
|---|---|
| Claude 코디네이터 + th-launch | 채택 전 명시적 start/verdict/decide. 검증자 실행은 기존 th-launch 정책과 감시를 사용 |
| th-watchd | 일반 워커와 같이 감시하므로 별도 변경 없음 |
| th run/repl, SDK, protocol | 해당 없음. 이들은 일반 워커 실행과 프로토콜 결과이지 코디네이터의 별도 채택 직전을 정의하지 않는다 |

새 TH-D 제목을 포함하는 push에는 채택된 CROSSCHECK.md 폴더를 가리키는 `Crosscheck: <폴더>` 커밋 트레일러가 필요하다. 예외 사유가 있으면 `TH_CROSSCHECK_SKIP`에 공백이 아닌 사유를 지정하며 events.log에 남는다. 기존 canary 검사는 별개로 계속 실행된다.
