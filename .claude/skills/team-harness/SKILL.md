---
name: team-harness
description: 여러 AI 코딩 에이전트 CLI(Codex, Gemini, Claude Code, OpenHands, OpenCode, pi)가 단일 작업에서 한 팀으로 협업할 수 있도록 조율하는 CLI(`th`) 및 Python SDK인 team-harness를 설치, 구성 및 실행합니다. 사용자가 team-harness, `th` 명령, `th init` / `th run` / `th repl` / `th logs`를 언급하거나, team-harness 설치 및 설정을 요청하거나, Codex / Gemini / Claude / OpenHands를 함께 사용하는 다중 에이전트 실행을 원하거나, OpenRouter 또는 다른 OpenAI 호환 공급자를 가리키도록 설정하는 방법을 물을 때 이 스킬을 사용하십시오. 사용자가 "team-harness"를 명시적으로 말하지 않더라도 여러 AI 코딩 CLI를 한 팀으로 조율하거나, 여러 모델 공급자를 단일 워크플로에 연결하거나, 작업을 여러 에이전트 CLI로 분산(fan-out)하고 결과를 취합하기를 원할 때도 사용하십시오.
---

# team-harness 사용 가이드

team-harness는 하나 이상의 작업자(Worker) CLI(Codex, Gemini, Claude Code, OpenHands, OpenCode, pi) 위에서 LLM 조율자(Coordinator)를 실행하는 조율 계층입니다. 조율자는 작업을 분해하고, `spawn_agent` 도구를 통해 작업자를 생성하며, 작업자의 stream-json 출력을 모니터링하고 결과를 취합합니다. 사용자는 `th` CLI(대화형 REPL 또는 비대화형 실행)나 `team_harness` Python SDK를 통해 상호작용합니다.

사용자가 team-harness를 설치, 설정 또는 실행하도록 도울 때 언제든지 이 스킬을 사용하십시오. 아래 섹션들은 일반적인 초기 설정 흐름(설치 → 작업자 설치 → 설정 초기화 → 인증 → 실행) 순서로 구성되어 있습니다. 사용자가 이미 특정 단계를 완료한 경우 다음 단계로 건너뛰십시오.

## 빠른 시작 (Quickstart)

사용자가 가장 짧은 경로로 즉시 실행하기를 원할 때:

```bash
# 1. 설치
uv tool install team-harness            # 또는: pip install team-harness

# 2. PATH에 최소 하나의 작업자 CLI가 설치되어 있고 인증되어 있는지 확인
#    (codex, gemini, claude, openhands, opencode, pi 중 하나).

# 3. 프로젝트 로컬 설정 파일 생성
cd <대상 프로젝트 디렉토리>
th init

# 4. 실행
OPENROUTER_API_KEY="sk-or-..." th repl
#   또는
OPENROUTER_API_KEY="sk-or-..." th run "Write unit tests for src/utils.py"
```

일부 작업자만 설치되어 있는 경우 `--agents` 플래그로 제한하여 실행할 수 있습니다(예: `th run --agents codex,claude "..."`).

## 1. 설치 (Installation)

```bash
pip install team-harness
# 또는
uv tool install team-harness
```

`--upgrade` 플래그로 업그레이드할 수 있습니다 (`pip install --upgrade team-harness` / `uv tool install --upgrade team-harness`).

설치되는 기본 CLI 명령어는 `th`입니다. 기존의 `team-harness` 명령어도 별칭으로 계속 동작하며, `python -m team_harness`로도 실행할 수 있습니다.

## 2. 작업자 CLI 사전 준비 (Prerequisites)

작업자는 team-harness가 서브프로세스로 실행하는 로컬 CLI들입니다. 사용자가 모든 작업자를 설치할 필요는 **없으며**, 사용하려는 작업자만 있으면 됩니다. 실행할 작업자는 `--agents` 옵션으로 지정할 수 있습니다.

| 작업자 | 설치 링크 |
|---|---|
| `codex` | https://github.com/openai/codex |
| `gemini` | https://github.com/google-gemini/gemini-cli |
| `claude` | Claude Code (https://docs.anthropic.com/en/docs/claude-code) |
| `openhands` | `pip install openhands` (OpenHands-CLI 저장소에서 배포) |
| `opencode` | https://github.com/opencode-ai/opencode |
| `pi` | https://github.com/badlogic/pi-mono |

각 작업자는 자체 네이티브 인증을 사용합니다(예: `claude`는 Claude 자체 로그인을 사용하고, `codex`는 `codex login`으로 생성된 `~/.codex/auth.json`을 사용). 사용자가 `provider_env`를 명시적으로 설정하지 않는 한, team-harness는 조율자의 API 키를 작업자에게 자동 전달하지 않습니다(아래 "작업자를 OpenRouter로 라우팅하기" 참조).

사용자가 "조율자가 X를 실행할 수 없습니다"라고 보고하는 경우, 가장 먼저 확인할 사항은 team-harness 외부에서 수동으로 실행했을 때 `X`가 `PATH`에 있고 인증되어 있는지 여부입니다.

## 3. `th init`을 통한 프로젝트 설정

`th init`은 기본 설정 파일을 스캐폴딩하는 표준 방법입니다. 프로젝트 루트에서 실행하십시오:

```bash
th init                 # 현재 디렉토리에 ./.team-harness/ 생성
th init --global        # ~/.team-harness/ 에 전역 생성
th init --force         # config.toml 덮어쓰기 (사이드카 프롬프트 파일은 보존됨)
```

`th init`은 대상 `.team-harness/` 디렉터리 안에 4개의 파일을 생성합니다:

| 파일 | 용도 |
|---|---|
| `config.toml` | 조율자 및 에이전트별 설정 (공급자, 모델, 명령어, 플래그) |
| `coordinator_system_message.md` | 편집 가능한 조율자 기본 시스템 프롬프트 |
| `worker_suffix.md` | 생성되는 모든 작업자 프롬프트 끝에 추가되는 텍스트 (기본값 빈 파일) |
| `worker_footer.md` | 작업자 출력 요구사항 바닥글 (기본값 `{session_output_dir}` 플레이스홀더 포함) |

`th init --force`는 `config.toml`을 재생성하지만 세 개의 사이드카 프롬프트 파일은 **보존**하므로 사용자의 커스텀 프롬프트가 날아가지 않습니다. 누락된 사이드카 파일만 다시 생성됩니다.

프로젝트 레벨의 `.team-harness/`는 팀원 간 및 CI에서 프롬프트 동작의 재현성을 유지하기 위해 git에 커밋하는 것이 일반적입니다. **`config.toml`에 API 키를 직접 넣지 마십시오** — 환경 변수에 보관하십시오.

### 설정 우선순위 (Config Resolution Order)

1. CLI 플래그
2. 환경 변수
3. 로컬 `.team-harness/config.toml` (`--cwd`에서 상위 디렉터리로 탐색)
4. 전역 `~/.team-harness/config.toml`
5. 내장 기본값

목록(List) 설정은 덧붙이지 않고 대체합니다. 예를 들어 로컬 설정에서 `[coordinator].allowed_agents`를 지정하면 전역 목록에 추가되는 것이 아니라 완전히 대체됩니다.

## 4. 조율자 인증 (Authenticating the Coordinator)

조율자(작업을 분석하고 언제 작업자를 실행할지 결정하는 LLM)는 자체 API가 필요합니다. 세 가지 일반적인 방식이 있습니다:

### a) OpenRouter / OpenAI 호환 API (가장 일반적)

```bash
export OPENROUTER_API_KEY="sk-or-..."
th repl
```

또는 특정 공급자를 명시적으로 고정:

```bash
OPENAI_API_KEY="sk-..." TEAM_HARNESS_API_BASE="https://api.openai.com/v1" th repl
```

`config.toml` 설정 예시:

```toml
[coordinator]
provider = "openai_compat"
model = "anthropic/claude-sonnet-4.6"
api_base = "https://openrouter.ai/api/v1"
```

### b) Codex 구독 (실험적 기능)

`codex login`으로 생성된 인증 파일을 사용합니다:

```bash
TEAM_HARNESS_PROVIDER=codex th repl
```

또는 `config.toml` 설정 예시:

```toml
[coordinator]
provider = "codex"
model = "codex-mini-latest"
# codex_auth_path = "~/.codex/auth.json"   # 기본 경로가 아닐 때만 지정
```

Codex 인증 파일 탐색 순서: `codex_auth_path` → `TEAM_HARNESS_CODEX_AUTH_PATH` → `$CODEX_HOME/auth.json` → `~/.codex/auth.json`.

### 관련 환경 변수

- `TEAM_HARNESS_PROVIDER` — `openai_compat` 또는 `codex`
- `TEAM_HARNESS_MODEL` — 조율자 모델 ID
- `TEAM_HARNESS_API_BASE` — `openai_compat` 공급자의 기본 URL
- `TEAM_HARNESS_CODEX_AUTH_PATH` — Codex 인증 파일 경로
- `OPENROUTER_API_KEY` / `OPENAI_API_KEY`

## 5. team-harness 실행 방법

### REPL (대화형 모드)

```bash
th repl
```

유용한 REPL 명령어:

| 명령어 | 동작 |
|---|---|
| `/clear` / `/reset` | 대화 기록 초기화 (세션, 실행 로그, 에이전트는 유지) |
| `/compact [포커스]` | 이전 대화 기록 수동 압축 (선택적 포커스로 요약 방향 지정) |
| `/agents` | 에이전트 상태 테이블을 인라인으로 출력 |
| `/log` | 현재 실행 로그 파일의 경로 출력 |
| `/quit` | 실행 중인 에이전트 완료를 기다린 후 종료 |

REPL 키 입력: `Enter` 전송, `Shift+Enter` / `Alt+Enter` 줄바꿈, `Esc Esc` 입력 버퍼 비우기, `Ctrl+C` 종료 없이 입력만 취소, `Ctrl+D` 입력창이 비어 있을 때 종료, `Up`/`Down` 입력 히스토리 탐색. tmux에서 `Alt`/`Esc` 입력이 느리게 반응하면 `set -sg escape-time 0`을 설정하십시오.

### 비대화형 실행 (Headless)

```bash
th run "Write unit tests for src/utils.py using pytest"
th run -f task.txt        # 파일에서 프롬프트 읽기
```

자주 사용하는 플래그:

```text
--provider TEXT         openai_compat | codex
--model TEXT            조율자 모델 ID
--api-base TEXT         API 기본 URL
--api-key TEXT          openai_compat 인증 키
--agents TEXT           쉼표로 구분된 허용 목록 (예: "codex,gemini")
--max-retries INT       429/5xx 재시도 횟수 예산 (기본값 5)
--max-depth INT         중첩 하네스 깊이 제한 (기본값 3)
--system-prompt TEXT    조율자 시스템 프롬프트에 추가할 텍스트
--system-prompt-file PATH
--cwd PATH              실행 작업 디렉토리 (기본값 ".")
```

### 로그 확인 (Logs)

```bash
th logs                 # 가장 최근 실행 로그
th logs <run-id>        # 특정 실행 로그
```

각 실행 로그는 `~/.team-harness/runs/<run-id>/`에 저장됩니다:

- `run.json` — 델타 기반의 완전한 무손실 리플레이 가능 실행 로그
- `<agent-id>_stdout.log` / `<agent-id>_stderr.log` — 에이전트별 개별 표준 출력/에러 로그
- `todo.json` — 영속 작업 목록

또한 각 실행은 `<output_dir>/<run-id>/worker_sessions.json`에 작업자별 컴팩트 매니페스트(프롬프트, 상태, 타임스탬프, 로그 경로, 재개 메타데이터)를 기록합니다.

### Python SDK

```python
import asyncio
from team_harness import TeamHarness, TeamHarnessResult

async def main():
    harness = TeamHarness(
        api_key="sk-or-...",
        model="anthropic/claude-sonnet-4.6",
        agents=["codex", "gemini"],
    )
    result: TeamHarnessResult = await harness.run(
        "Write unit tests for src/utils.py using pytest"
    )
    print(result.text)
    for agent in result.agents:
        print(f"  {agent.id} ({agent.agent_type}): {agent.status}")

asyncio.run(main())
```

`TeamHarness(...)`는 CLI와 동일한 옵션을 지원합니다: `provider`, `model`, `api_base`, `api_key`, `codex_auth_path`, `agents`, `max_retries`, `max_depth`, `system_prompt`, `system_prompt_file`, `cwd`, 그리고 `console_mode` (`"silent" | "auto" | "plain" | "rich"` — SDK 사용 시 `"silent"` 권장).

`result`는 `TeamHarnessResult` 객체이며 `text`(최종 어시스턴트 응답), `agents`(`id`, `agent_type`, `status`, `exit_code`, `cwd`를 가진 `AgentSummary` 목록), `run_id`를 포함합니다. 오류 발생 시 `TeamHarnessError`가 발생하며, 실패하더라도 실행 로그는 정상 마감됩니다.

## 6. 일반적인 설정 레시피

### 작업자의 기본 모델 고정

`default_model`은 조율자가 명시적인 `model=...` 오버라이드 없이 이 작업자를 실행할 때 사용되는 모델입니다. `model_flag`는 하네스가 모델을 주입할 때 사용하는 argv 플래그입니다.

```toml
[agents.codex]
command = ["codex", "exec"]
default_model = "gpt-5.4"
```

상속된 기본값을 비활성화하려면 `default_model = false`로 설정하십시오.

### 추론 노력(Reasoning Effort) 고정 (codex / claude)

```toml
[agents.codex]
reasoning_effort = "high"        # codex: low | medium | high | xhigh
```

```toml
[agents.claude]
reasoning_effort = "high"        # claude: low | medium | high | max
```

`reasoning_effort_flag`는 에이전트별로 합리적인 기본값을 갖습니다(codex의 경우 `["-c", "model_reasoning_effort={effort}"]`, claude의 경우 `["--effort", "{effort}"]`). Gemini는 업스트림에서 이를 지원하지 않습니다.

### 작업자를 OpenRouter로 라우팅하기

조율자와 작업자의 인증은 독립적입니다. 작업자 CLI들을 동일한 OpenRouter 계정으로 라우팅하려면:

```bash
export OPENROUTER_API_KEY=sk-or-...
```

**Codex**는 `-c` 오버라이드를 통해 공급자 설정을 읽습니다:

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

**Claude Code**는 환경 변수에서 공급자 설정을 읽으므로 `provider_env`로 설정합니다:

```toml
[agents.claude]
default_model = "anthropic/claude-opus-4.6"

[agents.claude.provider_env]
ANTHROPIC_BASE_URL = "https://openrouter.ai/api"
ANTHROPIC_AUTH_TOKEN = "{env:OPENROUTER_API_KEY}"
ANTHROPIC_API_KEY = ""    # 네이티브 인증으로 폴백되지 않도록 비워두어야 함
```

`{env:OPENROUTER_API_KEY}`는 작업자 실행 시점에 `os.environ`에서 값을 리졸브합니다. 누락된 환경 변수는 빈 문자열로 대체되며 1회 경고가 출력됩니다.

**Gemini는 OpenRouter 라우팅을 지원하지 않습니다.** `gemini` CLI는 Google API와 직접 인증하며 OpenAI 호환 base-URL 모드를 지원하지 않습니다.

### 커스텀 작업자 CLI 추가하기

`[agents.<이름>]` 섹션을 추가하십시오. `command`만이 필수 필드입니다:

```toml
[agents.myagent]
command = ["my-custom-cli"]
shared_flags = ["--mode", "auto"]
model_flag = "--model"        # 모델 플래그가 없으면 false로 설정
prompt_flag = "-p"            # 프롬프트가 플래그로 전달되는 경우에만 설정
prompt_position = "after_command"   # 프롬프트가 argv 앞쪽에 위치해야 할 때 설정
```

환경 변수 기반 모델 주입(OpenHands 등)의 경우 `model_env_vars = ["LLM_MODEL"]`을 사용하고 `model_flag = false`로 설정하십시오.

`shared_flags` / `resume_prefix` / `resume_flags` 내부 플레이스홀더:

- `{session_id}` — 재개 세션 ID (재개 모드 전용)
- `{generated_uuid}` — 실행 시 하네스가 자동 생성하는 UUID (`claude` 에이전트의 `--session-id <uuid>` 형태에 사용)

세션 ID는 `[agents.<이름>.session_capture]`를 통해 작업자의 stream-json 출력에서 캡처할 수 있습니다:

```toml
[agents.codex.session_capture]
strategy = "stream_json_event"
match = { type = "thread.started" }
field_path = ["thread_id"]
```

새로 추가된 에이전트는 조율자의 `spawn_agent` 도구에 자동으로 노출됩니다.

## 7. 조율자 도구 목록 (동작 예측용)

조율자는 다음 도구들을 사용할 수 있습니다:

- **에이전트 관리:** `spawn_agent`, `kill_agent`, `agent_status`, `list_agents`, `wait_for_agents`, `wait_for_any`, `read_new_agent_output`
- **파일 시스템:** `read_file`, `write_file`, `append_file`, `edit_file`, `multi_edit_file`, `ls`, `glob`, `grep`, `read_new_file_content`
- **셸:** `bash`
- **작업 추적:** `todo_write`, `todo_read`

`bash`는 기본적으로 전체 명령에 120초 데드라인을 적용합니다. 장시간 실행되는 포그라운드 배치의 경우, 전체 배치를 포괄할 수 있는 양의 `timeout_seconds`를 지정하십시오. 타임아웃이나 취소 시 해당 명령의 전체 프로세스 그룹이 정리됩니다. 이 외부 데드라인은 호출된 프로그램 내부의 타임아웃과 별개입니다.

또한 `<cwd>/.agents/skills/` 및 `~/.agents/skills/` 아래의 에이전트 스킬을 탐색합니다(프로젝트 스킬이 전역 스킬보다 우선). 스킬 메타데이터는 시작 시 조율자에게 표시되며, 전체 지침은 필요할 때 `read_file`을 통해 읽어옵니다.

## 8. 자주 겪는 주의사항 (Common gotchas)

- **OpenHands 실행은 현재 자동 재개를 지원하지 않습니다.** `--json` 출력이 stream-json 형식으로 파싱되지 않기 때문입니다. 커스텀 `[agents.openhands]` 섹션은 업그레이드 후 새로 추가된 내장 `shared_flags`를 상속받습니다. 설정에서 우연히 이름이 겹친 커스텀 에이전트가 있다면 이름을 바꾸거나 상속 필드를 명시적으로 비우십시오 (`shared_flags = []`, `prompt_flag = false`, `model_env_vars = []`).
- **OpenHands `LLM_MODEL` 유출**: `LLM_MODEL` 주입을 위해 `--override-with-envs`가 필요하지만, 부모 셸의 `LLM_MODEL` / `LLM_API_KEY` / `LLM_BASE_URL`도 함께 가져옵니다. 결정론적인 실행을 위해 이를 unset하십시오.
- **Claude Code 모델 오버라이드**: `ANTHROPIC_MODEL`만 설정하는 것으로는 불충분합니다. 내부 코드 경로가 `ANTHROPIC_DEFAULT_SONNET_MODEL` / `ANTHROPIC_DEFAULT_OPUS_MODEL`을 직접 읽습니다. 내장 `claude` 템플릿의 `model_env_vars`는 세 가지를 모두 나열하고 있으므로 커스텀 시 이를 유지하십시오. 하네스는 저렴한 헬퍼가 haiku에서 계속 실행될 수 있도록 `ANTHROPIC_DEFAULT_HAIKU_MODEL`, `ANTHROPIC_SMALL_FAST_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL`을 의도적으로 건드리지 않습니다.
- **기존 `template = "..."` 문자열 형식은 제거되었습니다.** 설정에 남아 있으면 명확한 에러를 발생시킵니다. 해결책: `th init --force`로 `config.toml`을 재생성하십시오(사이드카 프롬프트 파일은 보존됨).
- **프롬프트 파일**은 UTF-8로 읽히며 최대 100KB로 제한됩니다. 초과하거나 비UTF-8이거나 읽을 수 없는 파일은 에러를 발생시킵니다.
- **신뢰 모델**: 스킬은 하네스 프로세스의 전체 권한으로 임의의 파이썬 코드를 실행할 수 있습니다. `bash` 도구는 샌드박스 없이 `stdin=/dev/null`로 실행됩니다. 스킬 디렉터리를 `PATH`처럼 취급하십시오 — 신뢰할 수 있는 작업과 스킬만 실행하십시오.

## 9. 추가 정보

저장소의 `README.md`가 가장 완전한 기준 문서입니다(전체 에이전트 템플릿 스키마, Codex 구독 상세 정보, 모델 우선순위 표, 터미널 기능 목록, 마이그레이션 안내 포함). 이 스킬 문서에서 다루지 않는 내용을 찾을 때는 임의로 추측하지 말고 README를 참조하십시오.

저장소: https://github.com/writeitai/team-harness
