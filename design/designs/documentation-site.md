# 설계: 공식 문서 웹사이트 (Public Documentation Site)

**상태:** 승인됨 (구현 완료)  
**기록일:** 2026-07-13  
**적용 대상:** `team-harness`를 위한 독립적 문서 사이트. 이 저장소 내에 위치하며 `writeit.ai` 서브도메인에 배포됨.

이 문서는 훗날 이 문서를 읽는 사람이 문서 기술 스택이 *왜* 이렇게 구성되었는지 이해할 수 있도록 계획과 그 배후의 결정을 기록합니다.

공유된 핵심 원칙:

> **문서를 코드와 함께 버전 관리되는 자체 호스팅 가능한 정적 모듈로 제공하고, Next.js의 네이티브 방식으로 MDX를 작성하며, WriteIt 패밀리의 일관된 스타일을 적용하고, 키보드로 즉시 검색할 수 있도록 한다.**

---

## 목표 (Goals)

1. **사람 중심의 보기 쉬운 문서.** `team-harness`를 처음 접하는 개발자도 쉽게 이해할 수 있는 훌륭한 내비게이션, 본문 설명 및 예제 제공(단순한 옵션 나열 지양).
2. **자체 호스팅 가능한 모듈.** 별도의 유료 문서 SaaS 없이 `clone → build → host`가 가능한 정적 사이트. 문서가 코드와 함께 버전 관리되도록 *이 저장소 내*에 포함.
3. **`writeit.ai` 서브도메인 및 동일한 디자인.** CNAME을 통해 `team-harness.writeit.ai`로 접근 가능하며, 메인 사이트와 동일한 색상 체계를 사용하여 자연스러운 연결감 제공.
4. **Next.js 공식 문서 방식의 MDX 작성.** 라우트 역할을 하는 마크다운 파일에 리액트 컴포넌트를 직접 삽입할 수 있는 유연성.
5. **키보드 단축키 검색 (⌘K / Ctrl+K).** 문서 전체를 빠르게 탐색하는 명령 팔레트 검색 지원.

---

## 선행 사례 — `loopy-loop` 문서 사이트 복제

직접적인 선행 사례는 형제 프로젝트인 **`loopy-loop`**의 공식 문서 사이트([`loopy.writeit.ai`](https://loopy.writeit.ai))입니다. 해당 사이트는 네이티브 `@next/mdx` 패턴, Pagefind 정적 검색, WriteIt 색상 팔레트를 성공적으로 적용했습니다. 약 12개의 파일로 구성된 검증된 독립 모듈이므로, 스택을 새로 발명하는 대신 `loopy-loop/website/`의 구조를 가져와 team-harness 콘텐츠에 맞게 재구성했습니다.

따라서 아래의 모든 내용은 해당 선행 사례를 계승하며, team-harness 전용 결정 사항은 콘텐츠(결정 6)와 도메인뿐입니다.

## 결정 1 — 네이티브 `@next/mdx`, `page.mdx` 기반 라우팅

**네이티브 `@next/mdx`**로 구축합니다(각 `src/app/docs/**/page.mdx`가 라우트가 됨). Tailwind v4 + `@tailwindcss/typography`, `remark-gfm` + `rehype-slug` + `rehype-pretty-code` (Shiki), 수동 관리형 내비게이션 배열(`src/lib/docs/navigation.ts`), 3단 레이아웃(사이드바 / 본문 / 우측 목차) 및 이전-다음 페이지네이션, 완전한 정적 빌드를 위한 `output: 'export'`를 채택했습니다. 사내에서 이미 검증되었고 새로운 프레임워크 학습 부담이 없습니다.

## 결정 2 — 저장소 내부 `website/` 디렉터리에 위치

문서가 다루는 코드와 함께 버전 관리될 수 있도록 별도의 저장소나 비공개 모노레포가 아니라 `team-harness` 저장소의 `website/` 경로에 둡니다. `website/` 앱은 파이썬 패키지와 독립적인 자체 `package.json` 및 툴체인을 가지며, PyPI에는 배포되지 않고 오직 정적 사이트로만 빌드됩니다.

## 결정 3 — CNAME을 통한 GitHub Pages (`team-harness.writeit.ai`)

이 저장소의 정적 빌드 결과물을 **GitHub Pages**에 배포하고 CNAME을 통해 **`team-harness.writeit.ai`**로 서빙합니다. Pages를 사용하면 오픈소스 저장소와 함께 호스팅을 유지할 수 있고 서버가 필요 없으며 커스텀 도메인을 지원합니다. 범용적인 `docs.writeit.ai` 대신 제품 브랜드 서브도메인을 선택하여 도구 이름과 URL을 일치시켰습니다.

메커니즘: GitHub Actions 워크플로(`.github/workflows/docs-deploy.yml`)가 사이트를 빌드하고, 출력물에 대해 Pagefind를 실행한 뒤 `actions/deploy-pages`로 배포합니다. `output: 'export'` + `trailingSlash: true`는 `index.html`로 리졸브되는 디렉터리 스타일 URL을 제공하며, 서브도메인 루트에서 서빙되므로 `basePath`가 필요 없습니다. `.nojekyll` 파일은 Next의 `_next/` 에셋을 보호합니다. 커스텀 도메인은 최초 1회 수동 설정이 필요합니다(설정 순서는 `website/README.md` 참조).

## 결정 4 — Pagefind + `cmdk`를 통한 키보드(⌘K) 검색

**[Pagefind](https://pagefind.app/)**(빌드 후 단계에서 내보낸 HTML을 바탕으로 구축되는 정적 인덱스)를 사용하고, ⌘K / Ctrl+K로 바인딩된 **`cmdk`** 대화상자를 통해 검색을 제공합니다. 런타임 검색 서버 없이 자체 호스팅 제약 조건을 완벽히 충족합니다.

## 결정 5 — WriteIt 팔레트, 오픈 폰트 대체, 라이트 모드 우선

`writeit.ai`의 브랜드 색상(모래 `#f7ebbd`, 먹색 `#222433`, 녹색 `#5ca493`, 금색 `#ebaa1a`, 빨강 `#f34832`)을 `src/app/globals.css`의 shadcn 스타일 CSS 변수에 매핑했습니다. 도메인 잠금이 걸린 proxima-nova 대신 오픈 폰트인 **Hanken Grotesk**(`next/font` 사용)를 적용하여 완전한 자체 호스팅을 보장합니다. 라이트 전용인 메인 사이트에 맞춰 **라이트 모드를 우선 지원**합니다(다크 모드 토큰은 정의되어 있으나 토글 UI는 추후 연결 예정).

## 결정 6 — README 및 소스 문서 기반 콘텐츠 작성

콘텐츠는 기존의 `README.md`, `CLAUDE.md` 아키텍처 메모, 설계 문서를 바탕으로 MDX 페이지로 작성됩니다. 정보 아키텍처(각각 하나의 `page.mdx`, 순서는 `navigation.ts`에 정의):

| 라우트 | 소스 자료 |
|---|---|
| `/docs` — 소개 | README 개요, 태그라인, 핵심 가치 |
| `/docs/getting-started` | 설치, 사전 작업자 준비, `th init`, 첫 실행, 로그 |
| `/docs/concepts` | 조율자/작업자 분리, 요청 흐름, 도구 레지스트리, 템플릿, 컨텍스트, 스킬 |
| `/docs/configuration` | `config.toml`, 우선순위, 환경 변수, 프롬프트 파일, 출력, 재시도 |
| `/docs/workers` | 에이전트 템플릿 스키마, 내장 작업자, 커스텀 에이전트, 모델 + 추론 노력 |
| `/docs/providers` | `openai_compat` vs `codex`, 인증, OpenRouter 라우팅 |
| `/docs/skills` | 에이전트 스킬: 디렉터리, `SKILL.md`, 네이밍, 서브디렉터리 |
| `/docs/context-management` | 컨텍스트 추적, 자동 압축, `/compact`, `/clear` |
| `/docs/sdk` | `TeamHarness`, 생성자 매개변수, `TeamHarnessResult` |
| `/docs/cli-reference` | `th run`/`repl` 플래그, REPL 명령어, 키 조작, 터미널 기능 |
| `/docs/coordinator-tools` | 조율자의 에이전트 / 파일 시스템 / 셸 / 작업 도구 |
| `/docs/run-logs` | `run.json`, `todo.json`, `worker_sessions.json`, 작업자별 로그 |
| `/docs/troubleshooting` | 흔한 실패 유형, 마이그레이션 참고, 신뢰 모델 |

문서는 최신 API 상태를 반영해야 합니다(예: 조율자 기본 모델은 `gpt-5.6-sol`임).

---

## 기술 스택 요약

`Next.js` (App Router) · `@next/mdx` · `output: 'export'` + `trailingSlash: true` · `Tailwind v4` + `@tailwindcss/typography` · `remark-gfm` + `rehype-slug` + `rehype-pretty-code` (Shiki) · ⌘K 검색을 위한 `Pagefind` + `cmdk` · Hanken Grotesk 폰트와 WriteIt 팔레트 · `team-harness.writeit.ai` GitHub Pages 배포.

## 후속 작업 (이 저장소 PR 범위 외)

- 사이트 배포 후 `writeit.ai` 메인 사이트에 "Docs" 링크 추가.
- 라이트 모드 안정화 후 `next-themes`를 연결하여 다크 모드 토글 지원.
