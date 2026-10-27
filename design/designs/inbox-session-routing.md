# Inbox session routing (TH-D41)

여러 코디네이터가 한 owner inbox를 공유할 때 사건을 띄운 세션이 읽도록 한다.
세션 키 우선순위는 `ORCA_PANE_KEY`, `CLAUDE_CODE_SESSION_ID`, `TH_SESSION_KEY`이며,
키가 없으면 기존 공유 커서를 쓴다. watchd는 inbox 줄을 바꾸지 않고 줄 시작 바이트 오프셋과
대상 키를 `inbox.routes`에 기록한다. 키가 없는 작업과 기존 줄은 `*` 전체 대상이다.
사이드카와 inbox 기록은 커서 잠금 안에서 수행한다. 각 세션은 자기 커서로 모든 완성된 줄을
지나가되 자기 대상과 전체 대상만 출력한다. 첫 사용은 EOF에서 시작한다. Codex는 waiter 대신
사용자 요청 시 peek를 쓰며 `--ack`만 커서를 바꾼다. peek는 커서가 없으면 보존된 기록부터 표시한다. 대상 waiter가 없으면 즉시 미수신을 알린다.

| 경로 | 적용 지점 | 테스트 |
|---|---|---|
| th-launch | `.th_owner`와 함께 `.th_session` 기록 | launch 경로 확인 |
| th run / repl / SDK spawn_agent | 해당 없음 — watch 미등록, 진행·완료는 run 상태로 확인 | spawn 코드 경로 확인 |
| protocol MODE A/B/C | 해당 없음 — watch 미등록, 진행·완료는 `th protocol status`/run 상태로 확인 | protocol spawn은 `.th_session`을 만들지 않음 |
| watchd / th-watch | watchd는 사건 오프셋·대상 및 미수신 알림 기록; th-watch는 별도 관찰하며 짧은 제한에도 첫 검사 보장 | inbox 테스트 |
| ths | 대상 커서와 살아 있는 waiter 확인 | 미수신 즉시 표시 테스트 |
| th-inbox-wait / peek | 세션 커서, 라우팅, Codex 차단 및 명시적 ack | 세션 A/B, broadcast, 기존 커서, Codex 테스트 |
