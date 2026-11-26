# 공통 실행 파이프라인: PATH shim 연결

직접 실행과 다른 진입점이 각자 정책을 구현하면 적용 범위가 달라진다. `src/team_harness/exec_pipeline.py`는 실행 사건 `ExecEvent`, 단계 결과 `StageResult`, 순서가 있는 `register_pre`/`register_post`와 `run_pre`/`run_post`를 제공한다. 사건에는 실행자와 작업 위치, argv, 시각·PID·종료 코드와 결과 경로가 들어가며 JSON으로 직렬화한다. 단계 예외는 결과 1로 기록하고 다음 단계를 실행한다. 단계 결과와 사건은 기존 watch 디렉터리의 `exec_pipeline.jsonl`에 추가된다. 단계는 메모리에서 원래 인자와 메시지를 사용한다. 장부에는 프롬프트 노출을 막기 위해 인자·임의 문맥·경고·사건 메시지를 저장하지 않고 단계 메시지는 존재 여부만 기록한다. 프롬프트 해시와 단계 이름·코드·차단 여부 및 사건 식별자는 유지한다.

`launch_shim.py`의 관찰 진입점은 사건을 만들고 pre를 실행한 뒤 기존처럼 실제 도구를 exec한다. 기존 정책과 슬롯 예약, 쿼터 잠금, 지시서 검사, 반복 경고는 `exec_pipeline.py`의 독립 pre 단계로 등록된다. 경고와 `shim_decisions` 기록은 기존 관찰 모드다. `observation_events.py`는 관찰 작업의 종료를 감지해 PID와 프로세스 시작 토큰으로 원래 사건을 찾아 post를 부른다. `observed_verify.py`가 이전처럼 비동기 검수를 담당하며 K6의 PASS/FAIL/보류 판정을 유지한다. shim의 미연결 자원 예약만 있는 작업은 독립적으로 등록된 th 작업과 구분한다.

`exec`로 프로세스를 치환하는 기존 쉘 shim은 종료 코드를 관찰자에게 돌려주지 않는다. 따라서 현재 `exit_code`는 알 수 없으면 null이며, post의 단계 성공은 검수 요청을 처리했다는 뜻이지 검수 PASS가 아니다. 관찰 사이에 종료된 짧은 프로세스나 관찰 대상이 아닌 작업에는 post가 보장되지 않는다. 단계 등록은 해당 파이프라인 프로세스에 적용된다. 쉘 shim의 관찰은 별도 Python 프로세스이므로 호출자 프로세스에서 한 등록이 그 프로세스에 자동 전파되지는 않는다.

| th-launch | watchd | run/repl | protocol | SDK | PATH shim | Claude hook | Codex hook | git hook |
|---|---|---|---|---|---|---|---|---|
| th 하위 건너뜀 | 기존 작업 감시 | 없음: 내장 코디 별도 | th 하위 건너뜀 | 없음 | P2 예정 | `th-exec-pipeline.py` pre/post 및 job-start | 없음: 후속 단위 | 없음 |

새 기능은 사건을 받아 `StageResult(name, code, message, block)`를 반환하는 함수를 만들고, 파이프라인을 실행할 프로세스에서 `register_pre(name, fn)` 또는 `register_post(name, fn)`을 호출한다. 단계는 등록 순서로 실행된다. `block`은 호출자가 해석하는 데이터이며 이 관찰 전용 shim은 기존처럼 차단하지 않는다. 다른 진입점의 연결과 강제 실행은 후속 단위다.
