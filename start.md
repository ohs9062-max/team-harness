========================================
설치 (한 번만)
========================================
  uv tool install -e /home/hs/rang/team-harness   # ~/.local/bin/th 생김

  -e 설치라서 이 폴더가 체크아웃한 브랜치 코드가 그대로 실행됨. 항상 main으로 둘 것:
    cd /home/hs/rang/team-harness && git checkout main && git pull


========================================
프로젝트에서 쓰기
========================================
  cd ~/내-프로젝트
  th init                    # .team-harness/config.toml 생성 (전역 설정만 쓸 거면 생략 가능)
  th run "작업 내용"           # 코디네이터가 이 폴더 기준으로 워커 실행


========================================
현재 구성: 코디네이터 = codex / 워커 = claude, agy, codex
========================================
~/.team-harness/config.toml (전역, 이미 설정됨):

  [coordinator]
  provider = "codex"                                    # 코디네이터: codex 구독 로그인 (API 키 불필요)
  allowed_agents = ["codex", "claude", "antigravity"]   # 워커: 이 셋만 spawn 가능

사전 조건: 셸에서 `codex login`, `claude` 로그인, `agy` 로그인이 돼 있을 것.
  - 워커(claude/codex/agy)는 각자 CLI 로그인만 쓰면 됨 → API 키 불필요.
  - 코디네이터는 team-harness가 직접 API를 호출하는 별도 연결. codex는 로그인
    토큰(~/.codex/auth.json)을 재사용하므로 키 없이 동작. 기본 모델은 gpt-5.6-sol.
  - th protocol(MODE A/B/C)은 코디네이터를 안 써서 이 설정이 필요 없음.

클로드를 코디네이터로 쓰려면 (현재 미지원):
  구독 로그인 재사용 기능이 없음 → Anthropic API 키가 필요하고 호출량만큼 종량 과금
  (Claude Code 구독과 별도 결제). 구독으로 하려면 claude CLI를 서브프로세스+MCP로
  붙이는 새 기능을 만들어야 함(미구현).

대안 (codex 대신 API 키를 쓸 경우) — 환경변수 권장, ~/.bashrc에 넣으면 매번 안 쳐도 됨:
  export OPENROUTER_API_KEY="sk-or-..."      # OpenRouter
  export OPENAI_API_KEY="sk-..."             # OpenAI 직접
  export TEAM_HARNESS_API_BASE="https://api.openai.com/v1"
  (이 경우 config.toml의 provider = "codex" 줄을 지우거나 provider = "openai_compat"로)
config.toml에 api_key="..."로 직접 넣는 것도 되지만 git 커밋 위험 있음.


========================================
기본 실행 명령
========================================
  th run "작업"     1회 실행. 코디네이터가 계획 세워 워커에게 위임
  th repl           대화형 (/clear /compact /agents /log /kill <id> /quit)
  th logs           지난 실행 로그 확인

→ 코디네이터가 즉흥적으로 판단. 아래 MODE A/B/C는 정해진 절차를 따르는 별도 워크플로.


========================================
Harness Protocol — MODE A / B / C
========================================
  MODE A  병렬 경쟁    워커 2개 동시 실행 → 교차검토 → 사람이 승자 선택
  MODE B  릴레이       진행 중인 작업(브랜치/상태 그대로)을 다른 에이전트가 이어받음
  MODE C  역할 분업    설계→구현→체크→검수를 에이전트별로 순서대로 (기본값)

공통: 시작 전 git 상태 확인, merge/push는 사용자 승인 필요, base 작업 트리는 안 건드림
(결과는 task 브랜치에 커밋, base 반영은 사람이 직접 merge).
정본 문서: design/harness_protocol/{MODES,WORKFLOW,ENGINEERING_POLICY,HARNESS_AGENTS}.md

명령:
  th protocol run --mode a "작업" --repo .        # MODE A 시작
  th protocol run --mode c "작업" --repo .        # MODE C 시작 (기본값)

  th protocol resume --task-id <ID> --run-dir <DIR> --repo . \
    --selection SELECT_WORKER_1                    # MODE A 선택 (또는 _2/_HYBRID/REWORK/CANCEL)

  th protocol resume --task-id <ID> --run-dir <DIR> --repo . \
    --from-stage IMPLEMENT                          # MODE C 중단 지점부터 이어가기

  th protocol relay --task-id <ID> --run-dir <DIR> --repo . \
    --next-agent codex                              # MODE B 인계

  th protocol status                               # 최근 실행 목록
  th protocol status --run-dir <DIR>               # stage별 결과/실패원인/토큰·비용

관찰: 기본으로 워커마다 tmux 창 자동 실행 (--no-visible로 끔). `tmux attach -t <session>`.

역할별 모델 오버라이드:
  HARNESS_MODE_C_IMPLEMENT_MODEL=gpt-5.6-sol th protocol run --mode c "..." --repo .
  또는 --set-role mode_c_implement.model=gpt-5.6-sol


========================================
Graft — 워커용 코드 그래프 (선택, 토큰 절약)
========================================
워커는 매번 빈 컨텍스트로 시작해 저장소를 grep으로 다시 탐색함. 켜면 워커 띄우기 전에
코드 그래프(tree-sitter, LLM 불필요)를 한 번 빌드하고 조회 명령을 프롬프트에 붙여줌.

켜기:
  [context_graph]                              # .team-harness/config.toml
  enabled = true

  TEAM_HARNESS_CONTEXT_GRAPH=1 th run "..."     # 이번 실행만 켜기

요약:
  - th run/repl/SDK, th protocol 전부 적용. 실행당 디렉터리당 1회만 빌드
  - 그래프는 작업 트리 밖(~/.team-harness/graft/)에 저장 → 저장소 안 건드림
  - graft 없거나 빌드 실패해도 경고만 남기고 정상 실행
  - 기본값 꺼짐. 힌트만큼(~250~300토큰) 프롬프트가 길어지니 효과는 직접 비교해서 판단
    (기능 끄고/켜고 th protocol run → th protocol status로 토큰 비교, 근거는 TH-D22)


========================================
막히면
========================================
에러 메시지 그대로 붙여서 알려줘.
