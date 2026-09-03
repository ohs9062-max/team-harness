# 작업자 속도 제한 서킷 브레이커 (Worker rate-limit circuit breaker)

## 상태 및 목적 (Status and purpose)

이 문서는 실행 범위(run-scoped) 작업자 속도 제한(Rate-limit) 서킷 브레이커에 대한 구속력 있는 공식 설계 문서입니다 (TH-D10). 이미 하드 쿼터(quota) 거부를 보고한 공급자 패밀리를 조율자가 반복해서 다시 실행하는 것을 방지합니다. 이 기능은 작업자를 자동 재시도하거나, 작업자의 모델을 임의로 변경하거나, 어떤 대체 패밀리가 작업을 대신해야 할지 결정하지 않습니다.

예를 들어, Claude Code는 다음과 같은 두 개의 JSONL 레코드로 종료될 수 있습니다:

```json
{"type":"rate_limit_event","rate_limit_info":{"status":"rejected","resetsAt":1784811600,"overageStatus":"rejected"}}
{"type":"result","is_error":true,"api_error_status":429}
```

이 설계가 적용되기 전에는 실패한 에이전트가 가시적으로 남아 있었지만, 다음 조율자 턴에서 또 다른 Claude 프로세스를 실행하는 것을 막지 못했습니다. 그 결과 해당 공급자는 동일한 계정 전역 이유로 해당 프로세스를 계속 거부했습니다.

## 감지 경계 (Detection boundary)

`agents/rate_limits.py`는 `agents/spawner.py`가 모든 작업자에 대해 이미 캡처하고 있는 표준 출력(stdout) 파일을 읽습니다. 감지는 작업자가 최종 종료(terminal) 상태가 된 후에만 실행됩니다. 다음 두 가지 명확한 종료 결과를 감지합니다:

- `type == "result"`이고, 실패 표시(`is_error == true` 또는 `status == "error"`)가 있으며, 명시적인 429 상태가 포함된 JSON 객체. 이 상태는 `api_error_status`/`error.code`와 같은 숫자 필드일 수 있으며, Gemini stream-json의 경우 해당 종료 결과의 `error.message`에만 `API Error: 429` 표기를 유지할 수도 있습니다.
- `type == "rate_limit_event"`이고 중첩된 `rate_limit_info.status` 또는 `overageStatus`가 `"rejected"`인 JSON 객체 (동일한 스트림에서 성공적인 종료 결과가 뒤따르지 않는 한).

스캐너는 JSONL을 점진적으로 처리하며 유효하지 않거나 잘린 줄은 무시합니다. 작업자 CLI가 거부 이벤트를 한 번 보고한 뒤 내부적으로 재시도하여 성공적으로 마칠 수 있으므로 거부 이벤트는 잠정적인 것으로 간주됩니다. 성공적인 최종 결과는 이전의 거부 증거를 지우며, 종료 코드 0으로 끝난 작업자는 절대 서킷을 작동(trip)시키지 않습니다. 마지막 실패 429 응답은 종종 리셋 시간을 생략하므로, 앞선 거부 이벤트의 최신 리셋 시간을 유지합니다. 일반 텍스트 출력, 일반 API 장애, 과부하, 인증 오류, 작업 실패 등은 이 서킷을 열지(차단하지) 않습니다.

감시자(watcher)와 다른 상태 도구가 동기화되는 동안 stdout을 열 때 일시적으로 실패할 수 있습니다. `tools/agent_tools.py::_sync_finished_rate_limits`는 파일 스캔이 성공적으로 반환된 후에만 `AgentState.rate_limit_checked`를 설정합니다. `OSError`가 발생하면 해당 호출은 차단하지 않고 넘어가지만(fail-open), 다음번 spawn, wait, status, list 또는 가용성 동기화 시점에 해당 작업자를 다시 검사할 수 있도록 남겨둡니다.

## 상태 및 키 (State and key)

`RateLimitCircuitBreaker`는 `tools/agent_tools.py`에 의해 빌드되는 실행별 에이전트 도구 바인딩 내부에 상주합니다. 활성 맵은 에이전트 템플릿 이름(레코드에서는 `family`로 부름)을 키로 사용합니다. 템플릿 패밀리가 차단 범위가 됩니다. `claude`가 계정 속도 제한에 걸렸을 때 다른 Claude 모델을 선택하더라도 일반적으로 다른 공급자 계정으로 전환되지 않기 때문입니다. 실패한 작업자에 실제로 전달된 모델은 `agents/spawner.py`의 유효 모델 값에서 감사 메타데이터로 보존됩니다.

공급자의 Unix `resetsAt`이 서킷 만료 시간이 됩니다. 누락되었거나 유효하지 않은 경우 후보 만료 시간은 `tripped_at + rate_limit_default_cooldown_s`가 됩니다. 공급자는 Unix 시간을 초 또는 밀리초 단위로 인코딩할 수 있으며, 둘 다 UTC datetime으로 정규화됩니다. 재차 서킷이 걸리면 `max(current_family_expiry, candidate_expiry)`를 사용하므로, 나중에 발생한 단순 429 에러가 며칠짜리 공급자 리셋 시간을 더 짧은 기본 쿨다운으로 덮어쓸 수 없습니다. 만료 시점 이후에는 가용성 검사 또는 spawn 시 활성 항목이 제거되고 프로브(probe)로서 생성이 허용됩니다. 상태는 의도적으로 단일 실행 범위(run-local)로 유지됩니다. 새로운 하네스 실행은 이전 프로세스의 인메모리 상태 가정을 상속받지 않습니다.

## 생성 및 조율자 규약 (Spawn and coordinator contract)

`spawn_agent`가 에이전트 ID를 발급하거나 작업을 할당하기 전에, 새로 종료된 작업자들을 동기화하고 요청된 패밀리를 검사합니다. 서킷이 열려 있는(차단된) 경우 다음과 같은 형태의 JSON 문자열을 반환합니다:

```json
{
  "spawned": false,
  "status": "rate_limited",
  "family": "claude",
  "model": "claude-fable-5",
  "tripped_at": "2026-07-23T12:58:20+00:00",
  "resets_at": "2026-07-23T13:00:00+00:00",
  "reason": "worker result reported api_error_status=429",
  "requested_model": "claude-fable-5",
  "message": "Agent family 'claude' is rate-limited until ...",
  "available_families": ["codex", "gemini"]
}
```

이렇게 거부된 요청에 대해서는 서브프로세스, 할당 파일, `AgentState` 또는 `run.json.agents` 항목이 전혀 생성되지 않습니다. 이를 통해 기존의 spawn 성공 규약을 보존합니다. 성공적인 호출은 여전히 순수한 `agent_<id>` 문자열만 반환합니다.

반환 결과는 호환성을 위해 의도적으로 다형적(polymorphic)입니다. 성공적인 생성은 단순 ID이며, 속도 제한 단락(short-circuit)일 때만 위의 JSON 객체가 반환됩니다. 비LLM 호출자는 반환된 문자열을 라이브러리가 export하는 `team_harness.parse_rate_limited_spawn_result`에 전달해야 합니다. 이 함수는 `spawned`가 정확히 `false`이고 `status`가 `rate_limited`이며 필수 패밀리/리셋 필드가 존재할 때만 검증된 객체를 반환합니다. 정상 에이전트 ID, 무관한 JSON, 기존의 `ERROR:` 문자열에 대해서는 `None`을 반환합니다.

새로 추가된 `agent_availability` 조율자 도구는 브레이커 활성화 플래그, 사용 가능한 패밀리 이름, 활성 차단 레코드, 허용된 패밀리별 상태 레코드를 반환합니다. `list_agents`는 실제로 생성된 프로세스들의 배열로 유지되어 기존 호출자 호환성을 유지합니다.

## 영속 감사 규약 (Durable audit contract)

`tracking/models.py`는 `RunRecord`에 다음 최상위 필드를 추가합니다. 따라서 속도 제한이 걸리지 않은 실행을 포함하여 모든 새 `run.json`에 이 필드가 포함됩니다:

```json
{
  "rate_limited_families": [
    {
      "family": "claude",
      "model": "claude-fable-5",
      "tripped_at": "2026-07-23T12:58:20Z",
      "resets_at": "2026-07-23T13:00:00Z",
      "reason": "worker result reported api_error_status=429"
    }
  ]
}
```

이 항목들은 현재 활성 맵뿐만 아니라 감사 이력(audit history) 역할을 합니다. 만료 후에도 유지되며, 이후 추가로 관찰된 차단은 유효하고 단축되지 않는 패밀리 구간을 덧붙입니다. 턴당 `usage.prompt_tokens` 및 `usage.completion_tokens`를 포함한 기존 필드는 전혀 변경되지 않습니다.

## 설정 및 호환성 (Configuration and compatibility)

`[coordinator]` 아래 두 개의 키가 이 기능을 제어합니다:

```toml
rate_limit_circuit_breaker = true
rate_limit_default_cooldown_s = 900
```

쿨다운은 양수여야 합니다. `rate_limit_circuit_breaker = false`로 설정하면 감지, 영속화, 생성 차단을 건너뛰며, `agent_availability`는 허용된 모든 패밀리를 사용 가능한 것으로 보고합니다. 이는 TH-D10 이전의 동작으로 되돌리는 역할을 합니다.

이 변경은 `run.json`과 조율자 도구 세트에 대한 추가적(additive) 변경입니다. 1회성 서브프로세스 동작(TH-D2), 하네스 정상 반환의 의미(TH-D3), 작업자 세션 매니페스트, 또는 loopy-loop가 소비하는 기존 사용량 필드를 변경하지 않습니다.
