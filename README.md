# Team Harness

다른 코딩 하네스(Codex, Claude Code, Grok Build, Antigravity, OpenCode, pi, OpenHands)를 위한 조율(Coordination) 계층.

<br>

<p align="center">
  <img src=".github/assets/images/logo.png" alt="team-harness logo" width="400">
</p>

## 어떤 일을 하나요? (What does it do?)

다음과 같은 프롬프트를 실행할 수 있습니다:

```text
MVP 달성을 위해 아직 누락된 주요 구성 요소가 무엇인지 알려줘.

이를 위해 에이전트 팀을 구성해줘.

다음 작업을 담당할 에이전트 팀을 만들어:
- CODEX, CLAUDE, ANTIGRAVITY를 사용하여 분석 수행
    - 분석을 최대한 철저히 수행하고 그 결과를 전용 디렉터리 내의 새 파일에 출력할 것
- 최종 보고서 작성
    - 이전 에이전트들의 모든 분석을 읽고, 최종 결과와 의견을 SUMMARY.md에 정리할 것
```

Team Harness는 Codex, Claude Code, Antigravity CLI 간의 협업을 조율합니다.

Claude Code의 에이전트 팀 기능으로도 유사한 결과를 얻을 수 있습니다.
하지만 Team Harness를 사용하면 **원하는 어떤 모델이든 연결**할 수 있으며, 기본 시스템 프롬프트도 훨씬 쉽게 미세 조정할 수 있습니다.

## 설치 (Installation)

```bash
pip install team-harness
# 또는
uv tool install team-harness
```

최신 버전으로 업그레이드:

```bash
pip install --upgrade team-harness
# 또는
uv tool install --upgrade team-harness
```

## 사전 준비 사항 (Prerequisites)

작업자(Worker) CLI는 별도로 설치하고 인증해야 합니다. 모든 작업자를 설치할 필요는 없으며, 보유하고 있는 작업자만 사용하도록 `--agents codex,antigravity` 옵션으로 실행을 제한할 수 있습니다.
OpenHands는 `pip install openhands`로 설치합니다(PyPI 배포 패키지 이름은 OpenHands-CLI 저장소에서 제공하는 `openhands`임).

| 작업자 | 설치 안내 문서 |
|---|---|
| `codex` | [Codex CLI](https://github.com/openai/codex) |
| `claude` | [Claude Code](https://docs.anthropic.com/en/docs/claude-code) |
| `grok` | [Grok Build CLI](https://docs.x.ai/build/cli/headless-scripting) (`XAI_API_KEY` 또는 `grok login`) |
| `antigravity` | [Antigravity CLI](https://antigravity.google/docs/cli-overview) |
| `openhands` | [OpenHands CLI](https://github.com/OpenHands/OpenHands-CLI) |
| `opencode` | [opencode](https://github.com/opencode-ai/opencode) |
| `pi` | [pi](https://github.com/badlogic/pi-mono) |

## 빠른 시작 (Quick start)

```bash
# 프로젝트 루트 디렉토리에서 실행
cd <대상 프로젝트 디렉토리>

# ./.team-harness/ 디렉터리에 프로젝트 로컬 설정 생성
# config.toml, coordinator_system_message.md, worker_suffix.md, worker_footer.md 생성
th init
```

### Codex 로그인이 되어 있는 경우
```bash
TEAM_HARNESS_PROVIDER=codex th repl
```
또는 `<your project>/.team-harness/config.toml` 파일에서 `provider = "codex"` 설정

### API 키를 사용할 경우
```bash
OPENROUTER_API_KEY="sk-or-..." th repl
```
또는
```bash
OPENAI_API_KEY="sk-..." TEAM_HARNESS_API_BASE="https://openai.com/api/v1" th repl
```

### 비대화형 실행 (Headless)

```bash
# 단일 명령어 실행
th run "Write unit tests for src/utils.py using pytest"

# 파일에서 프롬프트 읽기
th run -f task.txt
```

### 로그 확인 (Viewing Logs)

```bash
# 실행 로그 보기
th logs
th logs <run-id>
```

## Python SDK

파이썬 코드에서 프로그래밍 방식으로 team-harness를 사용할 수 있습니다:

```python
import asyncio
from pathlib import Path
from team_harness import CallerContext, TeamHarness, TeamHarnessResult

async def main():
    harness = TeamHarness(
        api_key="sk-or-...",
        model="anthropic/claude-sonnet-4",
        agents=["codex", "antigravity"],
        # 선택적 임베딩 계약: 전체 실행을 호출자 소유의 루트 아래에 유지하고
        # 에이전트에게 외부 세션 식별자를 제공합니다.
        caller_context=CallerContext(
            trace_root=Path("/abs/session/traces/attempt-42"),
            parent_assignment_path=Path("/abs/session/attempt-42/assignment.json"),
            parent_attempt_id="attempt-42",
            root_session_id="session-root",
            session_id="session-leaf",
            session_depth=1,
            workflow_role="inner",
        ),
    )
    result: TeamHarnessResult = await harness.run(
        "Write unit tests for src/utils.py using pytest"
    )
    print(result.text)
    for agent in result.agents:
        print(f"  {agent.id} ({agent.agent_type}): {agent.status}")

asyncio.run(main())
```

모든 CLI 옵션은 생성자 매개변수로 지원됩니다:

```python
harness = TeamHarness(
    provider="codex",           # 또는 "openai_compat" (기본값)
    model="codex-mini-latest",
    api_base="https://openrouter.ai/api/v1",
    api_key="sk-or-...",
    codex_auth_path="~/.codex/auth.json",
    agents=["codex", "antigravity"], # 또는 "codex,antigravity"
    max_retries=5,
    retry_base_delay_s=1.0,
    retry_max_delay_s=30.0,
    max_depth=3,
    rate_limit_circuit_breaker=True,
    rate_limit_default_cooldown_s=900,
    system_prompt="추가 지침",
    system_prompt_file="prompt.txt",
    agent_models={"codex": "gpt-5.5"},
    agent_reasoning_efforts={"codex": "high"},
    output_dir="./_outputs",
    cwd="./project",
    console_mode="silent",      # "silent" | "auto" | "plain" | "rich"
)
```

`agent_models` 및 `agent_reasoning_efforts`는 지정된 에이전트 유형의 템플릿 기본값을 오버라이드합니다. `model=...`로 지정된 조율자 모델은 변경되지 않으며, 생성 시 전달된 `model` 인자가 해당 작업자에 대해 여전히 최우선합니다.

`output_dir`은 SDK 실행을 위한 `[coordinator].output_dir`을 오버라이드합니다. 각 실행은 여전히 team-harness 실행 ID로 이름 지어진 하위 디렉터리를 생성합니다.

`run()` 메서드는 다음 정보를 담은 `TeamHarnessResult`를 반환합니다:

- `text` -- 최종 어시스턴트 응답
- `agents` -- `AgentSummary` 목록 (id, agent_type, status, exit_code, cwd)
- `run_id` -- 고유 실행 식별자
- `run_json_path` -- 명시적인 정본 조율자 실행 레코드 경로
- `session_output_dir` -- 작업자/세션 아티팩트 디렉터리
- `coordinator_input_path` -- 첫 공급자 호출 전에 캡처된 생성 시스템/사용자 입력 경로

오류 발생 시 `TeamHarnessError`가 발생합니다. 구조화된 실패 시 `error.detail`에 동일한 세 경로가 노출됩니다. 임베딩 호출자는 호출자 계약을 선택하기 전에 `get_capabilities()`를 검사할 수 있습니다. 실행 로그는 실패하더라도 항상 정상 마감됩니다.

기능 명칭이 패키지 버전 대신 호환성 경계가 됩니다. 호출자 계약 v1은 `caller_run_record_v1`, `coordinator_input_v1`, `spawn_assignment_v1`, `nested_caller_context_v1`을 제공합니다:

```python
from team_harness import get_capabilities

required = {
    "caller_run_record_v1",
    "coordinator_input_v1",
    "spawn_assignment_v1",
    "nested_caller_context_v1",
}
capabilities = get_capabilities()
if not capabilities.supports(*required):
    raise RuntimeError("설치된 team-harness에 필요한 호출자 계약이 누락되었습니다")
```

`nested_caller_context_v1`은 조율자가 내장 `type="harness"`를 동적으로 선택할 때 적용됩니다. 해당 자식 조율자는 외부 루프 세션, 깊이, 시도, 역할 및 관련 상태 경로를 유지하며, 자체 직접 할당 및 하네스 아티팩트 서브트리를 받고 부모 하네스 실행 ID를 기록합니다.

## 설정 (Configuration)

`th`는 내장 기본값만으로도 바로 동작합니다. 명시적인 설정 파일을 생성하려면:

```bash
# 현재 저장소를 위한 프로젝트 로컬 설정 생성
th init

# ~/.team-harness/config.toml 아래에 전역 설정 생성
th init --global

# 기존 설정 파일 덮어쓰기
th init --force
th init --global --force
```

전역 설정은 사용자 전역 기본값을 위한 것입니다. 프로젝트 설정은 저장소 전용 설정을 위한 것이며 보안 비밀을 포함해서는 안 됩니다. API 키는 환경 변수에 보관하십시오.

전역 설정 예시:

```toml
[coordinator]
provider = "openai_compat"
model = "gpt-5.5"
api_base = "https://openrouter.ai/api/v1"
coordinator_system_message_file = "coordinator_system_message.md"
worker_suffix_file = "worker_suffix.md"
worker_footer_file = "worker_footer.md"
system_prompt = ""
output_dir = "_outputs"

# 작업자 에이전트는 구조화된 명령어로 정의됩니다: 기본 `command` 리스트,
# 항상 적용되는 `shared_flags`, 이전 세션을 재개할 때만 적용되는 `resume_flags`.
# `session_capture` 하위 테이블은 하네스가 작업자의 stream-json 출력에서 공급자의
# 세션 ID를 추출하여 나중에 재개할 수 있도록 하는 방법을 정의합니다.
#
# 생략된 모든 필드는 해당 에이전트 유형의 내장 기본값에서 상속되므로,
# 관심 있는 부분만 오버라이드하면 됩니다.
# 주석이 포함된 완전한 샘플을 다시 생성하려면 `th init --force`를 실행하십시오.

[agents.codex]
command = ["codex", "exec"]
shared_flags = [
    "--dangerously-bypass-approvals-and-sandbox",
    "--skip-git-repo-check",
    "--json",
]
resume_prefix = ["resume"]
resume_flags = ["{session_id}"]
model_flag = "--model"
default_model = "gpt-5.5"
deduplicate_flags = [
    "--dangerously-bypass-approvals-and-sandbox",
    "--skip-git-repo-check",
    "--json",
]
reasoning_effort_flag = ["-c", "model_reasoning_effort={effort}"]
# reasoning_effort = "high"   # 레벨을 고정하려면 주석 해제

[agents.codex.session_capture]
strategy = "stream_json_event"
match = { type = "thread.started" }
field_path = ["thread_id"]

[agents.claude]
command = ["claude"]
shared_flags = [
    "-p",
    "--dangerously-skip-permissions",
    "--output-format", "stream-json",
    "--verbose",
]
resume_flags = ["--resume", "{session_id}"]
model_flag = "--model"
model_env_vars = [
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
]
deduplicate_flags = [
    "-p",
    "--dangerously-skip-permissions",
    "--verbose",
]
reasoning_effort_flag = ["--effort", "{effort}"]
# default_model = "claude-sonnet-4-6"   # 기본값을 고정하려면 주석 해제
# reasoning_effort = "high"             # 값: low|medium|high|max

# claude를 OpenRouter로 라우팅하려면 provider_env 블록 주석 해제.
# [agents.claude.provider_env]
# ANTHROPIC_BASE_URL = "https://openrouter.ai/api"
# ANTHROPIC_AUTH_TOKEN = "{env:OPENROUTER_API_KEY}"
# ANTHROPIC_API_KEY = ""

[agents.claude.session_capture]
strategy = "stream_json_event"
match = { type = "system", subtype = "init" }
field_path = ["session_id"]

[agents.grok]
command = ["grok"]
shared_flags = [
    "--always-approve",
    "--output-format", "streaming-json",
    "--no-auto-update",
]
resume_flags = ["--resume", "{session_id}"]
prompt_flag = "-p"
model_flag = "--model"
default_model = "grok-4.5"
deduplicate_flags = ["--always-approve", "--no-auto-update"]
reasoning_effort_flag = ["--reasoning-effort", "{effort}"]

[agents.grok.session_capture]
strategy = "stream_json_event"
match = { type = "end" }
field_path = ["sessionId"]

[agents.antigravity]
command = ["agy"]
shared_flags = [
    "--dangerously-skip-permissions",
    "--print-timeout", "60m",
]
prompt_flag = "--print"
resume_flags = ["--conversation", "{session_id}"]
model_flag = false
deduplicate_flags = [
    "--dangerously-skip-permissions",
]

[agents.openhands]
command = ["openhands"]
shared_flags = ["--headless", "--json", "--override-with-envs"]
prompt_flag = "-t"
model_env_vars = ["LLM_MODEL"]

[agents.opencode]
command = ["opencode"]

[agents.pi]
command = ["pi", "--print", "--no-session"]

[agents.harness]
command = ["th", "run"]
model_flag = "--model"
```

OpenHands는 현재 team-harness에서 자동 재개를 지원하지 않습니다. `--json` 출력 형식이 stream-json으로 파싱되지 않기 때문입니다.
`--override-with-envs`는 `LLM_MODEL` 주입을 위해 필요하지만, 셸의 `LLM_MODEL`, `LLM_API_KEY`, `LLM_BASE_URL`도 가져오므로 결정론적 동작을 위해 필요한 경우 unset하십시오.

Grok Build(`grok`)는 `--always-approve`, `--output-format streaming-json`, `--no-auto-update`로 비대화형 실행됩니다. 세션 ID는 최종 NDJSON `end` 이벤트(`sessionId`)에서 캡처되며 `--resume <id>`로 재개합니다.

Antigravity는 `agy --print`를 사용하여 비대화형 서브프로세스로 실행됩니다. 대화 ID를 이미 알고 있는 호출자는 `--conversation <id>`를 통해 재개 모드를 사용할 수 있습니다.

### 프롬프트 설정

`th init`은 대상 `.team-harness/` 디렉터리에 4개의 파일을 생성합니다:

| 파일 | 용도 |
|---|---|
| `config.toml` | 모든 조율자 및 에이전트 설정 |
| `coordinator_system_message.md` | 편집 가능한 조율자 기본 시스템 프롬프트 |
| `worker_suffix.md` | 생성되는 모든 작업자 프롬프트에 자동 추가되는 텍스트 |
| `worker_footer.md` | 기본 작업자 출력 요구사항 템플릿 |

프롬프트 관련 설정 키:

| 키 | 용도 |
|---|---|
| `coordinator_system_message_file` | 조율자 기본 프롬프트 파일 경로 |
| `worker_suffix_file` | 모든 작업자 프롬프트에 추가되는 접미사 파일 경로 |
| `worker_footer_file` | 접미사 뒤에 추가되는 작업자 바닥글 템플릿 경로 |
| `system_prompt` | 조율자 기본 프롬프트 뒤에 인라인으로 추가되는 확장 텍스트 |

프롬프트 파일은 UTF-8로 읽히며 최대 100KB로 제한됩니다.

### 프로젝트 수준 설정

`th init`은 `./.team-harness/config.toml`, `coordinator_system_message.md`, `worker_suffix.md`, `worker_footer.md`를 생성합니다. 로컬 설정 탐색은 `--cwd`에서 상위로 올라가며 가장 가까운 조상 설정이 전역 설정을 오버라이드합니다.

목록(List) 설정은 덧붙이지 않고 대체합니다.

`[coordinator].output_dir`은 실행별 조율자 및 작업자 아티팩트가 기록되는 위치를 제어합니다. 각 실행은 `<output_dir>/<run_id>/`를 생성합니다.

조율자 재시도 동작 제어 키:

```toml
max_retries = 5
retry_base_delay_s = 1.0
retry_max_delay_s = 30.0
```

작업자의 하드 속도 제한(Rate-limit) 처리:

```toml
rate_limit_circuit_breaker = true
rate_limit_default_cooldown_s = 900
```

작업자가 실패한 429 또는 거부된 속도 제한 이벤트로 종료되면, 동일한 에이전트 템플릿 패밀리의 후속 생성은 공급자 리셋 시점까지 단락(차단)됩니다.

### 설정 우선순위

1. CLI 플래그
2. 환경 변수
3. 로컬 `.team-harness/config.toml`
4. 전역 `~/.team-harness/config.toml`
5. 내장 기본값

관련 환경 변수:
- `TEAM_HARNESS_PROVIDER`
- `TEAM_HARNESS_MODEL`
- `TEAM_HARNESS_API_BASE`
- `TEAM_HARNESS_CODEX_AUTH_PATH`
- `OPENROUTER_API_KEY` 또는 `OPENAI_API_KEY`

### 커스텀 에이전트 유형 추가

구조화된 명령어로 `[agents.<이름>]` 섹션을 추가하십시오. 유일한 필수 필드는 `command`입니다.

```toml
[agents.myagent]
command = ["my-custom-cli"]
shared_flags = ["--mode", "auto"]
model_flag = "--model"   # CLI에 모델 플래그가 없으면 `false`로 설정
```

새로운 유형은 조율자의 `spawn_agent` 도구에 자동으로 표시됩니다.

`shared_flags`, `resume_prefix`, `resume_flags` 내부 플레이스홀더:
- `{session_id}` — 재개 세션 ID로 치환됨 (재개 모드 전용).
- `{generated_uuid}` — 생성 시 하네스가 생성한 UUID로 치환됨 (`claude`의 `--session-id <uuid>` 형태에 유용).

### 기본 모델 설정

- **`default_model`** — 조율자가 명시적인 `model=...`을 전달하지 않았을 때 사용되는 모델.
- **`model_flag`** — argv에 모델을 주입하는 CLI 플래그 이름 (예: `"--model"`).

우선순위:
1. 조율자의 명시적 `spawn_agent(model="…")` (최우선)
2. `[agents.<이름>].default_model`
3. 작업자 CLI 자체의 내부 기본값 (폴백)

### 추론 노력 (Reasoning effort)

추론 노력 조절을 지원하는 작업자 CLI 설정:

- **`reasoning_effort`** — 설정할 값 (예: `"high"`).
- **`reasoning_effort_flag`** — `{effort}` 플레이스홀더를 포함한 argv 토큰 형태.

| 작업자 | `reasoning_effort_flag` | 허용되는 값 |
|---|---|---|
| codex | `["-c", "model_reasoning_effort={effort}"]` | `low`, `medium`, `high`, `xhigh` |
| claude | `["--effort", "{effort}"]` | `low`, `medium`, `high`, `max` |
| grok | `["--reasoning-effort", "{effort}"]` | `low`, `medium`, `high` |
| antigravity | (업스트림 미지원) | — |

조율자는 `spawn_agent(effort="…")`로 생성별 수준을 오버라이드할 수 있습니다.

### 작업자를 OpenRouter로 연결하기

조율자를 구동하는 동일한 OpenRouter 계정으로 작업자 CLI들을 라우팅할 수 있습니다.
먼저 셸에서 환경 변수를 설정합니다:

```bash
export OPENROUTER_API_KEY=sk-or-...
```

#### Codex via OpenRouter

```toml
[agents.codex]
command = ["codex", "exec"]
shared_flags = [
    "--dangerously-bypass-approvals-and-sandbox",
    "--skip-git-repo-check",
    "--json",
    "-c", "model_provider=openrouter",
    "-c", 'model_providers.openrouter.name="openrouter"',
    "-c", 'model_providers.openrouter.base_url="https://openrouter.ai/api/v1"',
    "-c", 'model_providers.openrouter.env_key="OPENROUTER_API_KEY"',
]
default_model = "openai/gpt-5.3-codex"
```

#### Claude Code via OpenRouter

```toml
[agents.claude]
default_model = "anthropic/claude-opus-4.6"

[agents.claude.provider_env]
ANTHROPIC_BASE_URL = "https://openrouter.ai/api"
ANTHROPIC_AUTH_TOKEN = "{env:OPENROUTER_API_KEY}"
ANTHROPIC_API_KEY = ""   # 네이티브 인증 폴백 방지를 위해 반드시 비워둠
```

### 기존 단일 문자열 템플릿에서의 마이그레이션

이전 버전의 `template = "codex exec ... {prompt}"` 단일 문자열 형태는 **제거**되었습니다. 해당 설정이 남아 있으면 오류가 발생합니다.
가장 빠른 마이그레이션 방법:

```bash
th init --force    # 완전한 구조화 샘플 재생성 (사이드카 프롬프트 파일은 보존됨)
```

### 인증 (Authentication)

- `provider = "openai_compat"`: OpenRouter 또는 OpenAI 호환 API 키 사용.
- `provider = "codex"`: `codex login`으로 생성된 인증 파일 사용.
- 각 작업자 CLI는 자체 네이티브 인증을 사용합니다.
- 하네스는 사용자가 명시적으로 환경 변수 오버라이드를 전달하지 않는 한 조율자 API 키를 작업자에게 전달하지 않습니다.

## CLI 플래그 (CLI flags)

```text
th run [OPTIONS] [TASK]

옵션:
  -f, --file PATH            인자 대신 파일에서 작업 내용 읽기
  --provider TEXT            조율자 공급자: "openai_compat" 또는 "codex"
  --model TEXT               조율자 모델 오버라이드 (예: "anthropic/claude-sonnet-4")
  --api-base TEXT            조율자 API 기본 URL 오버라이드
  --api-key TEXT             openai_compat 조율자 API 키 오버라이드
  --codex-auth-path TEXT     Codex auth.json 위치 오버라이드
  --agents TEXT              쉼표로 구분된 허용 에이전트 목록 (예: "codex,antigravity")
  --max-retries INT          429/5xx 오류 API 재시도 예산 (기본값: 5)
  --max-depth INT            중첩 하네스 깊이 제한 (기본값: 3)
  --system-prompt TEXT       시스템 프롬프트에 추가할 텍스트
  --system-prompt-file PATH  파일에서 시스템 프롬프트 확장 내용 읽기
  --cwd PATH                 실행 작업 디렉토리 (기본값: ".")
```

`th repl`도 동일한 옵션을 받습니다 (`-f`/`--file` 및 `TASK` 인자 제외).

## REPL 명령어 (REPL commands)

| 명령어 | 설명 |
|---|---|
| `/clear` | 대화 기록 및 컨텍스트 추적 초기화 (새로 시작) |
| `/reset` | `/clear`의 별칭 |
| `/compact [focus]` | 다음 턴을 위해 이전 대화를 요약본으로 수동 압축 |
| `/quit` | 안전한 종료: 실행 중인 에이전트 완료 대기 후 종료 |
| `/agents` | 현재 에이전트 상태 테이블 인라인 출력 |
| `/log` | 현재 실행 로그 파일의 경로 출력 |

## 컨텍스트 관리 (Context management)

- 상태 표시줄은 누적 비용이 아니라 최신 API 사용량 기준의 현재 컨텍스트 점유율을 보여줍니다.
- 모델별 임계값에 도달하면 새 조율자 턴 전에 자동 압축(auto-compaction)이 선제적으로 실행됩니다.
- 자동 압축은 마지막 메시지 역할이 `user`일 때만 실행되므로, 도구 교환 중간에 압축되지 않습니다.
- `/compact <focus>`를 통해 요약이 강조할 내용을 편향시킬 수 있습니다.
- `/clear`는 세션, 실행 로그, 에이전트 상태는 유지하면서 대화만 새로 시작할 때 사용합니다.

## 터미널 시각 기능 (Terminal features)

Rich 콘솔 모드(stdout이 TTY일 때 기본 활성화)의 시각 기능:

- **스피너 애니메이션** — 조율자가 생각하는 동안(토큰 스트리밍 전) 상태 표시줄에 애니메이션 점자 스피너 표시.
- **iTerm2 탭 진행률** — iTerm2에서 실행 시 터미널 탭에 진행 표시기 노출.
- **사용자 프롬프트 스타일링** — 제출된 사용자 프롬프트를 어두운 배경색과 흰색 글씨로 표시하여 모델 출력과 시각적 구분.
- **에이전트 이모지** — 에이전트 유형별 이모지 표시 (예: 🔷 codex, 🚀 antigravity, 🟣 claude).
- **경로 하이라이트** — 도구 호출 인자 및 결과의 파일 경로를 시안(cyan) 색상으로 강조.

## REPL 키 조작 (REPL editing keys)

| 키 | 동작 |
|---|---|
| `Enter` | 현재 입력 제출 |
| `Shift+Enter` | 줄바꿈 삽입 (멀티라인 편집) |
| `Alt+Enter` | 줄바꿈 삽입 (대체 키) |
| `Esc Esc` | 전체 입력 버퍼 비우기 |
| `Ctrl+C` | REPL 종료 없이 현재 입력만 취소 |
| `Ctrl+D` | 입력 버퍼가 비어 있을 때 REPL 종료 |
| `Up` / `Down` | 세션 내 입력 히스토리 탐색 |

긴 텍스트를 붙여넣을 때 줄바꿈이 4개 이상이면 편집 중에는 `[Pasted text #N +M lines]`로 축약 표시되며, 제출 시 전체 텍스트가 자동으로 복원됩니다.

## 조율자 도구 (Coordinator tools)

조율자 모델이 사용할 수 있는 도구 목록:

**에이전트 관리:** `spawn_agent`, `kill_agent`, `agent_status`, `list_agents`, `wait_for_agents`, `wait_for_any`, `read_new_agent_output`

**파일 시스템:** `read_file`, `write_file`, `append_file`, `edit_file`, `multi_edit_file`, `ls`, `glob`, `grep`, `read_new_file_content`

`read_file` 및 `read_new_file_content`는 호출당 파일 콘텐츠 기준 최대 32,768자 및 UTF-8 인코딩 후 최대 32KiB로 제한됩니다. 소용량 판독은 그대로 반환됩니다. 대용량 임의 접근 판독의 경우 반환 결과에 정확한 문자 범위와 다음 `offset_chars`가 명시되며, 조율자는 다음 페이지를 요청할 수 있습니다. 이를 통해 프롬프트가 경로 기반으로 유지되고 단 한 번의 실수로 모델 컨텍스트 전체가 소진되는 것을 방지합니다.

**셸:** `bash`

**작업 추적:** `todo_write`, `todo_read`

`bash` 도구는 전체 명령 데드라인(기본 120초)을 갖는 포그라운드 명령을 실행합니다. 장기 배치의 경우 양의 `timeout_seconds`를 전달할 수 있습니다. 타임아웃 또는 취소 시 Team Harness는 해당 명령의 프로세스 그룹 전체를 종료하고 수거합니다.

## 에이전트 스킬 (Agent Skills)

team-harness는 [Agent Skills](https://agentskills.io) 표준을 지원합니다.

스킬은 YAML 프론트매터(name + description)와 마크다운 지침이 포함된 `SKILL.md` 파일이 있는 디렉터리입니다. 조율자는 시작 시 스킬 메타데이터를 확인하고, 작업에 필요할 때 `read_file` 도구로 전체 지침을 읽어옵니다.

### 스킬 디렉터리 위치

| 위치 | 범위 |
|---|---|
| `<cwd>/.agents/skills/` | 프로젝트 로컬 (루트까지 상위 디렉터리도 탐색) |
| `~/.agents/skills/` | 사용자 전역 |

프로젝트 스킬이 동일한 이름의 사용자 전역 스킬보다 우선합니다. `.agents/skills/` 경로는 Codex CLI 규칙과 일치하므로 Codex용으로 작성된 스킬이 그대로 동작합니다.

### 스킬 생성 예시

```bash
mkdir -p .agents/skills/my-skill
cat > .agents/skills/my-skill/SKILL.md << 'EOF'
---
name: my-skill
description: 파일을 요약하고 간략한 보고서를 작성합니다. 사용자가 요약이나 개요를 요청할 때 사용하십시오.
---

# 내 스킬

## 단계

1. `read_file`을 사용하여 대상 파일 읽기
2. 핵심 사항 요약
3. 간략한 보고서 작성

## 참고

- 요약은 500단어 이내로 유지
- 실행 가능한 인사이트에 집중
EOF
```

### 스킬 명명 규칙

- 1-64자, 소문자, 숫자, 하이픈만 허용
- 하이픈으로 시작하거나 끝날 수 없으며, 연속된 하이픈 금지
- 디렉터리 이름이 정본 스킬 이름이 됨

### 선택적 하위 디렉터리

| 디렉터리 | 용도 |
|---|---|
| `scripts/` | 에이전트가 실행할 수 있는 코드 |
| `references/` | 필요 시 로드되는 추가 문서 |
| `assets/` | 템플릿, 데이터 파일, 스키마 |

## 실행 로그 (Run logs)

각 실행은 `~/.team-harness/runs/<run-id>/` 아래에 다음 파일을 생성합니다:

- `run.json` — 델타 기반의 전체 실행 로그 (무손실 리플레이 가능)
- `todo.json` — 영속 작업 목록

`caller_context`가 지정된 SDK 실행은 `<caller trace_root>/<run-id>/` 아래에 정본 실행 레코드와 모든 세션 아티팩트를 함께 보관합니다.

각 실행은 `<output_dir>/<run-id>/worker_sessions.json` 매니페스트도 생성합니다:

```text
<output_dir>/<run-id>/workers/<worker-label>__<agent-id>/stdout.jsonl
<output_dir>/<run-id>/workers/<worker-label>__<agent-id>/stderr.log
<output_dir>/<run-id>/agents/<agent-id>/agent_assignment.json
```

동일한 라이브 하네스 실행 중 종료된 작업자를 이어갈 때는 `spawn_agent(mode="resume", resume_from_agent_id="<agent-id>", ...)`를 사용하십시오.

## 신뢰 모델 (Trust model)

- **스킬**은 하네스 프로세스의 전체 권한으로 임의의 파이썬 코드를 실행할 수 있습니다. 스킬 디렉터리를 `PATH`처럼 신뢰하십시오.
- **`bash` 도구**는 `stdin=/dev/null`로 샌드박스 없이 실행됩니다.
- **작업자 CLI**는 할당된 작업 디렉터리에서 파일을 읽고 쓸 수 있는 별도의 로컬 프로세스입니다.
- 하네스는 설정된 API 엔드포인트로만 조율자 작업 내용과 도구 출력을 전송합니다.

이 도구는 신뢰할 수 있는 로컬 자동화를 위해 설계되었습니다. 신뢰할 수 없는 작업이나 스킬을 실행하지 마십시오.

## 마이그레이션 (Migration)

기본 CLI 명령어는 이제 `th`입니다:

- `team-harness`는 호환성 별칭으로 계속 동작합니다.
- `pip install team-harness`는 변경되지 않습니다.
- `python -m team_harness`는 변경되지 않습니다.
- 설정, 실행 로그, 스킬은 계속 `~/.team-harness/` 아래에 유지됩니다.
- 기존 설정 파일은 업그레이드로 인해 수정되지 않습니다.

## 개발 (Development)

```bash
uv sync --extra dev
uv run ruff check src/        # 린트 검사
uv run ruff format src/        # 포맷팅
uv run pyright src/             # 타입 검사
uv run pytest src/tests/ -v    # 테스트 실행
```

## 라이선스 (License)

Apache-2.0 — [LICENSE](LICENSE) 참조.
