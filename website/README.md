# team-harness 공식 문서 사이트

[team-harness](https://github.com/writeitai/team-harness)의 공식 문서 웹사이트이며, **[team-harness.writeit.ai](https://team-harness.writeit.ai)**에 배포됩니다.

이 사이트는 독립적인 정적 사이트입니다. Next.js App Router 기반 앱으로 각 페이지는 MDX로 작성되며, 정적 HTML로 익스포트되어 GitHub Pages를 통해 서빙됩니다. 기술 스택 선정 배경은 [`design/designs/documentation-site.md`](../design/designs/documentation-site.md)를 참조하십시오.

## 기술 스택 (Stack)

- **Next.js** (App Router) + **`@next/mdx`** — 각 `src/app/docs/**/page.mdx` 파일이 하나의 라우트가 됩니다.
- **Tailwind v4** + **`@tailwindcss/typography`** — 본문 텍스트 스타일링, `src/app/globals.css`에서 WriteIt 브랜드 테마 적용.
- **`rehype-pretty-code`** (Shiki 코드 하이라이팅), **`rehype-slug`** (제목 앵커), **`remark-gfm`**.
- **Pagefind** — **`cmdk`** ⌘K 명령 팔레트를 통해 제공되는 정적 검색 엔진.
- `output: 'export'` → 서버 없는 완전한 정적 사이트.

## 로컬 개발 (Develop)

```bash
cd website
npm install
npm run dev        # http://localhost:3000
```

> 검색(⌘K)은 프로덕션 빌드에서만 결과를 반환합니다. Pagefind 인덱스는 `postbuild` 단계에서 생성되므로 `npm run dev` 중에는 존재하지 않습니다.

## 빌드 (Build)

```bash
npm run build      # next build (-> out/) 이후 pagefind가 out/ 디렉터리를 인덱싱함
```

정적 사이트는 `website/out/`에 생성됩니다. `npm run preview`를 통해 (검색 인덱스가 동작하는) 프로덕션 빌드를 미리 볼 수 있습니다. `npm run typecheck`는 `tsc --noEmit`을 실행하며, Next가 생성하는 타입에 의존하므로 빌드 후에 실행하십시오.

## 페이지 추가 및 수정 (Add or edit a page)

1. `src/app/docs/<route>/page.mdx` 파일을 생성합니다. export된 `metadata` 객체(`title`, `description`)로 시작하고, 단일 `#` H1을 작성한 후 본문을 작성합니다. `##`/`###` 제목을 사용하면 "이 페이지의 목차(On this page)"에 자동으로 반영됩니다.
2. `src/lib/docs/navigation.ts`에 페이지를 추가합니다. 이 배열이 사이드바 순서 및 이전/다음 페이지네이션의 유일한 기준(Single Source of Truth)입니다.

## 배포 (Deploy)

`.github/workflows/docs-deploy.yml`은 `website/**` 경로가 수정되어 `main` 브랜치에 푸시될 때마다 GitHub Pages로 자동 빌드 및 배포합니다(Pull Request에서는 빌드 검사만 실행). `public/.nojekyll`은 GitHub Pages가 Next의 `_next/` 정적 에셋을 제외하지 않도록 방지합니다.

### 최초 설정 (첫 배포 전에 1회 필요)

워크플로가 아티팩트를 발행하더라도, Pages 사이트와 커스텀 도메인은 저장소에서 최초 1회 설정해야 합니다. GitHub Actions 기반 배포에서는 `public/CNAME` 파일만으로는 도메인이 자동 구성되지 않습니다:

1. **Settings → Pages → Build and deployment → Source:** 에서 **GitHub Actions**를 선택합니다.
2. **Settings → Pages → Custom domain:** 에 `team-harness.writeit.ai`를 입력하고 저장합니다(이 설정이 실제로 도메인을 연결하며, 커밋된 `CNAME` 파일은 의도를 기록하는 용도입니다).
3. **DNS** (`writeit.ai` 도메인 관리 영역): `team-harness.writeit.ai CNAME writeitai.github.io.` 레코드를 추가합니다.
4. DNS 전파가 완료되면, Settings → Pages에서 **Enforce HTTPS**를 활성화합니다.

커스텀 도메인이 연결되기 전까지 사이트는 `https://writeitai.github.io/team-harness/`에서 서비스되며, 이 경우 루트 상대 경로인 `/_next/`와 `/pagefind/` URL이 올바르게 리졸브되지 않습니다. 따라서 링크를 공유하기 전에 위 설정을 먼저 완료하십시오.
