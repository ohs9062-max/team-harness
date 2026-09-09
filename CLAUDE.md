# CLAUDE.md

이 파일은 Claude Code(claude.ai/code)가 이 저장소의 코드를 다룰 때 필요한 지침을 제공합니다. 이는 **개발자 참고 문서**(명령어, 아키텍처, 릴리스 프로세스)입니다.

## 작업 협약 및 설계 결정 — 먼저 읽어보세요

- **[`AGENTS.md`](./AGENTS.md)**는 작업 협약서(여기서 변경 작업을 수행하는 방식)입니다. 중요한 작업을 시작하기 전에 반드시 읽으십시오.
- **[`design/decisions.md`](./design/decisions.md)**는 정본 아키텍처 결정 로그(Architecture Decision Log: TH-D1, TH-D2 등)이며, 시스템이 *왜* 이렇게 설계되었는지를 다룹니다. **새로운 아키텍처 결정은 여기에 기록**해야 하며, 의도된 결정(특히 **TH-D2** 1회성 작업자 및 **TH-D3** 정상 반환 ≠ 성공)을 해당 항목을 읽지 않고 임의로 "수정"하지 마십시오.
- **`design/designs/`**는 구속력 있는 공식 설계 문서이며, **`design/analysis/`**는 작업 메모입니다. 설계 및 결정 문서는 미래의 에이전트나 비전문가 인간도 사전 맥락 없이 이해할 수 있어야 합니다(AGENTS.md 규칙 2).
- team-harness는 **다른 프로젝트들이 의존하는 라이브러리**입니다(예: `loopy-loop`). `TeamHarnessResult`, 작업자 실행/라이프사이클 동작, 설정/템플릿 계약을 공개 API로 취급하십시오(AGENTS.md 규칙 3).

## 개발 명령어 (Development Commands)

```bash
uv sync --extra dev          # dev를 포함한 모든 의존성 설치
uv run ruff check src/       # 린트 검사
uv run ruff format src/      # 코드 포맷팅
uv run pyright src/           # 타입 검사
uv run pytest src/tests/ -v  # 전체 테스트 실행
uv run pytest src/tests/test_console.py -v                    # 단일 테스트 파일 실행
uv run pytest src/tests/test_skills.py::test_frontmatter_parsing -v  # 단일 테스트 실행
```

CI는 Python 3.12/3.13/3.14 환경에서 ruff lint, ruff format 검사, pyright, pytest를 실행합니다. 머지 전에 네 가지 항목이 모두 통과해야 합니다.

## 아키텍처 (Architecture)

team-harness는 다중 에이전트 조율(multi-agent orchestration) 하네스입니다. **조율자(Coordinator) LLM**(OpenAI 호환 API 또는 Codex 구독과 통신)이 사용자 작업을 받아 작업 단위로 분할하고, 서브프로세스로 실행되는 **작업자(Worker) CLI**(Codex, Antigravity, Claude Code, opencode, pi, OpenHands)에 실행을 위임합니다.

### 요청 흐름 (Request Flow)

```text
사용자 입력 → cli.py (_repl / _run)
  → harness.py (TeamHarness.run)
    → coordinator/loop.py (run → run_one_turn 루프)
      → coordinator/client.py (LLM과 대화)
      → LLM이 도구 호출 반환 → tools/registry.py (실행)
        → tools/agent_tools.py (spawn_agent, wait_for_agents, ...)
          → agents/spawner.py (worker CLI를 subprocess.exec로 실행)
          → agents/manager.py (AgentState 라이프사이클 추적)
        → tools/fs_tools.py (read_file, write_file, grep, ...)
        → tools/shell_tools.py (bash)
        → tools/todo_tools.py (todo_write, todo_read)
      → LLM이 도구 호출 없이 텍스트 응답을 반환할 때까지 루프 지속
```

### 핵심 아키텍처 개념

**조율자(Coordinator) vs 작업자(Workers)**: 조율자는 계획을 세우고 작업을 위임하는 LLM입니다. 작업자는 실제 작업을 수행하는 외부 CLI 프로세스(codex, antigravity, claude 등)입니다. 조율자는 직접 코드를 작성/수정하지 않고 오케스트레이션(조율)만 담당합니다.

**도구 레지스트리 (Tool Registry)**: `tools/registry.py`는 도구 이름 → (스키마, 비동기 함수)를 매핑합니다. 조율자 루프는 모든 스키마를 LLM에 전달한 후 도구 호출 이름에 따라 디스패치합니다. 도구 바인딩은 실행 시점의 클로저(manager, run_log, config)를 캡처하기 위해 `build_*_tool_bindings()` 팩토리 함수를 통해 실행(run)마다 생성됩니다.

**에이전트 템플릿 (Agent Templates)**: `agents/template.py`는 각 작업자 CLI가 어떻게 실행되는지(명령어, 플래그, 모델 주입, 세션 캡처 전략)를 정의합니다. 템플릿은 문자열 템플릿이 아니라 구조화된 형태(명령어 리스트 + 플래그 리스트)입니다. 설정은 내장 기본값과 `config.toml`의 사용자 정의 오버라이드를 병합합니다.

**콘솔 계층 구조 (Console Hierarchy)**: `ui/console.py`는 `ConsoleBase` (추상 클래스) → `SilentConsole` (SDK용), `PlainConsole` (비TTY 환경용), `TeamHarnessConsole` (라이브 패널, 스피너, 마크다운 렌더링을 지원하는 Rich TUI) 구조를 가집니다. 콘솔은 전체 시각적 라이프사이클을 구동합니다: `begin_turn → begin_streaming → stream_token → end_streaming → tool_call_start → end_turn`.

**컨텍스트 추적 (Context Tracking)**: `tracking/context.py`는 API 응답으로부터 토큰 사용량을 추적하고, 모델별 임계값에 도달하면 자동 압축(auto-compaction)을 트리거합니다. 압축은 별도의 LLM 호출을 통해 이전 대화 기록을 요약본으로 재작성합니다.

**에이전트 스킬 (Agent Skills)**: `skills/loader.py`는 `.agents/skills/` 디렉터리(상위 디렉터리를 순회하는 프로젝트 로컬 경로 및 `~/.agents/skills/` 전역 경로)에서 `SKILL.md` 파일을 탐색합니다. YAML 프론트매터를 파싱하여 이름과 설명을 가져옵니다. 스킬은 실행 가능한 코드가 아니라 조율자가 `read_file`을 통해 읽는 지침 문서입니다.

### 두 가지 진입점 (Entry Points)

- **CLI** (`cli.py`): `th run` (1회성 실행) 및 `th repl` (슬래시 명령어를 지원하는 대화형 루프). REPL은 `/clear`, `/compact`, `/agents`, `/log`, `/quit`을 지원합니다.
- **SDK** (`harness.py`): `TeamHarness(...).run(task)`가 `TeamHarnessResult`를 반환합니다. 기본적으로 `SilentConsole`을 사용합니다.

### 설정 우선순위 (Configuration Resolution)

CLI 플래그 → 환경 변수 (`TEAM_HARNESS_*`) → 로컬 `.team-harness/config.toml` → 전역 `~/.team-harness/config.toml` → 내장 기본값.

## 릴리스 절차 (Releasing)

**중요: 릴리스 작업을 하기 전에 항상 현재 상태를 먼저 확인하십시오:**

```bash
git fetch --tags
gh release list --limit 5          # 가장 최근에 배포된 릴리스는?
git tag --sort=-v:refname | head -5 # 어떤 태그가 존재하는가?
grep '^version' pyproject.toml      # 코드 내 버전은 무엇인가?
```

`pyproject.toml`의 버전은 최신 git 태그보다 반드시 높아야 합니다. 이미 존재하는 태그 버전은 재사용할 수 없으며, 새 버전으로 올려야 합니다.

**릴리스 파이프라인** (`.github/workflows/release.yml`):

`v*.*.*` 형식의 태그를 푸시하면 빌드 → PyPI 배포 → GitHub Release 생성이 자동으로 트리거됩니다. 파이프라인은 완전 자동화되어 있으므로, `gh release create`로 GitHub 릴리스를 수동 생성하지 마십시오. 수동으로 생성하면 CI의 `github-release` 단계가 "already exists" 오류로 실패합니다.

**올바른 릴리스 절차:**

1. 최신 태그 및 릴리스 확인 (위 명령어 사용)
2. `pyproject.toml`의 버전을 최신 태그보다 높은 버전으로 올림
3. `CHANGELOG.md`에 해당 버전에 대한 새 섹션 작성
4. 커밋: `git commit -m "chore: bump version to X.Y.Z"`
5. main에 푸시: `git push`
6. 태그 생성 및 푸시: `git tag vX.Y.Z && git push origin vX.Y.Z`
7. 이후 과정(빌드, PyPI 배포, GitHub Release 생성)은 CI가 자동 처리

`gh release create`를 **실행하지 마십시오**. CI가 처리합니다. 태그 푸시만이 유일한 트리거입니다.

## 코딩 컨벤션 (Conventions)

- **임포트(Imports)**: ruff가 한 줄에 하나씩 강제 정렬된 임포트를 적용합니다 (`force-single-line = true`). 라인당 임포트 하나.
- **비동기(Async)**: pytest는 `asyncio_mode = "auto"`를 사용합니다. 모든 비동기 테스트 함수는 `@pytest.mark.asyncio` 없이도 자동 실행됩니다.
- **콘솔 메서드**: `ConsoleBase`에 새 선택적 메서드를 추가할 때는 외부 서브클래스가 깨지지 않도록 `@abstractmethod` 대신 기본 no-op 구현을 제공해야 합니다 (`begin_compaction`/`end_compaction` 패턴 참조).
- **테스트 더블(Test doubles)**: `conftest.py`는 `DummyUI`(덕 타이핑 기반 콘솔 대역)를 제공합니다. `test_cli.py`는 테스트별 로컬 `FakeConsole` 클래스를 갖습니다. 새 콘솔 메서드를 추가할 때 둘 다 갱신해야 합니다.
- **에이전트 도구 바인딩**: `list[tuple[schema, fn]]`을 반환하는 `build_*_tool_bindings()` 팩토리 패턴을 사용하십시오. 새 도구 모듈에서 모듈 수준 전역 상태를 사용하지 마십시오.
