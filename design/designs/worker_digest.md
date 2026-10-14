# Worker digest: 판정과 요약의 분리

상태 확인은 LLM이 로그를 계속 읽는 방식 대신 기존 셸 감시기의 사건 감지로 시작한다. `scripts/th-watch-lib.sh::inspect_task_event`가 DONE/FAILED(FAILED.* 포함), 마지막 STATUS의 완료/실패, 프로세스 소실, 쿼터·용량 오류, CPU 누적 ticks와 파일 mtime이 600초 동안 바뀌지 않은 멈춤을 판단한다. 날짜가 붙은 STATUS도 같은 방식으로 처리한다. 파일 목록과 각 mtime을 비교하므로 다른 파일의 갱신도 멈춤 타이머를 초기화한다. job.pid/worker.pid가 유효하지 않아도 작업 폴더를 정확한 명령 인자로 가진 agy를 /proc에서 찾는다. 이것은 생존 관찰이며 실행 중 세션의 입양이나 재연결은 아니다(TH-D2).

`th-watchd.sh`는 사건을 표시한 뒤 `th-digest.sh`를 백그라운드로 요청한다. 알림, inbox, events.log, 기존 선택형 Jev 관찰과 th-watch의 깨우기는 유지된다. 완료의 verify.sh가 오래 걸려도 데몬 루프는 막히지 않는다. digest 자식은 daemon lock과 표준 스트림을 물려받지 않는다. `TH_ONCE=1`은 한 번 순회한 후 요청한 digest를 기다려 시험 프로세스를 남기지 않는다. 정상 데몬 종료도 digest 요청과 외부 명령 그룹을 정리한다. 새 데몬이나 주기적인 감시 LLM은 없다.

`src/team_harness/worker_digest.py::verify`는 작업별 flock 아래 상태, 기대 산출물, 수치, verify.sh, 프로젝트 hook을 검사한다. 항목의 PASS/WARN/FAIL/SKIP와 사유를 JSON 및 한 줄 텍스트로 저장한다. 전체 판정은 FAIL > WARN > PASS이고 SKIP은 악화시키지 않는다. FAILED는 오래된 DONE보다 우선한다. 미완료 상태에서 살아 있는 프로세스는 WARN, 프로세스도 없으면 FAIL이다. 완료 후 남아 있는 PID는 진단 정보로 기록한다. FAIL이면 VERIFY_FAILED, 그 외에는 VERIFIED를 쓰고 반대 마커를 제거한다.

산출물 목록은 task.md의 기대 산출물 절에만 한정한다. 백틱 경로(공백 포함 가능) 또는 단순 bullet 목록을 해석하며 파일/디렉터리/glob의 존재와 파일 수를 확인한다. 경로가 모호하면 WARN으로 표시한다. JSON의 중첩 숫자 및 숫자 문자열과 보고서 소수를 Decimal ROUND_HALF_UP으로 3자리 비교한다. 설정 값은 증거가 될 수 없으므로 results.json만 사용한다. 이는 숫자의 존재를 확인할 뿐 그 수치가 의미상 올바른 지표인지 증명하지 않는다. 그런 규칙은 프로젝트 hook에 둔다.

verify.sh는 기존 작업 트리에서 timeout 아래 실행하며 출력 끝 40줄을 보관한다. 기존 공통 git 검사(루트 새 Markdown 금지, pyproject.toml 변경 FAIL, uv.lock 변경 WARN)는 이 verify 항목에 포함한다. 내용과 작업 트리가 같은 검수는 `.th_verify_script.json`으로 캐시하여 th-watch와 daemon digest가 같은 검수를 두 번 돌리지 않는다. hook은 작업 폴더부터 부모 방향으로 발견한 첫 verify_hooks.py 또는 TH_VERIFY_HOOK을 사용한다. JSON 항목을 병합하고 오류/타임아웃은 FAIL이다. hook은 신뢰된 프로젝트 코드이며 LLM이 아니다. verify.sh와 hook에는 파일 수정 권한이 있을 수 있으므로 신뢰할 수 있는 작업에만 설치한다.

`digest`는 watch 디렉터리의 전역 flock으로 한 개씩 실행한다. 사건 종류와 daemon 사건 마커의 생성 시각/파일 식별자를 `.th_digest.<사건>`에 저장한다. 동일 요청은 다시 요약하지 않지만 프로세스 복구 후 새로운 소실이나 진행 후 새로운 멈춤은 별도 사건으로 기록한다. 원래 daemon/notified 마커의 분리는 유지한다. 강제 종료로 사건 통지 마커만 남고 digest가 끝나지 않은 경우 자동 복구는 제공하지 않는다. 수동 th-digest로 재생성할 수 있다. th-launch는 새 실행에서 기존 .th_* 상태를 초기화한다.

LLM 호출은 기본 agy이며 TH_DIGEST_WORKER/MODEL 환경변수로 변경한다. 같은 워커의 quota_lock이 있으면 호출하지 않는다. 고정 프롬프트를 앞에 두고 작업별 검수/보고서/STATUS를 뒤에 붙인다. 보고서는 각 32,000자까지 전달하고 요약은 5줄까지만 저장한다. 프롬프트에 쓰기와 재실행 금지를 명시하고 agy plan/sandbox, Claude tools 없음/설정 상속 차단, Codex read-only/shell tool 없음/MCP 없음으로 실행한다. bwrap이 전체 기존 파일시스템을 읽기 전용으로 마운트하고 임시 작업 디렉터리만 쓰기 허용한다. CLI에 필요한 런타임 상태를 쓰지 못하는 경우도 실패로 취급한다. bwrap 부재/권한 제한, 호출 실패, 최대 600초 시간 초과는 모두 검수 결과만 출력하는 경로로 돌아온다. 검수 판정은 LLM의 출력과 독립적이다.

스크립트가 제목 `## 시각 작업 [판정]`과 모든 검수 원문을 만든 뒤 선택적인 LLM 요약을 덧붙인다. LLM이 FAIL/WARN을 생략하거나 다른 문장으로 출력해도 검수 원문과 판정 파일은 유지된다. LLM 문장은 검증된 판정이 아니며 코디네이터는 제목/검수 줄을 기준으로 판단한다. `.th_owner`를 우선하는 inbox 위치 규칙으로 workers.md를 정하고 작업별 `.th_workers_file` override도 제공한다. 파일별 flock과 atomic replace로 동시 갱신을 보호한다. 200줄을 넘으면 오래된 블록을 workers_archive.md로 옮긴다. 최신 블록 자체가 200줄을 넘는 경우 앞 200줄을 유지하고 나머지 줄을 archive에 보관한다. 기존 TeamHarnessResult, SDK, config.toml 스키마, Protocol 작업 트리/세션 계약은 변경하지 않는다.

작업 결과는 workers.md의 요약으로 판단한다. FAIL·WARN이거나 이상할 때만 원본을 연다. 감시용 LLM 워커는 띄우지 않는다.
