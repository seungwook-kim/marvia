# Design direction

기준: `docs/mockups/v2/A-enterprise-navy.html`과 `docs/mockups/v2/BRIEF_V2.md`(안A, 엔터프라이즈 남색). 값은 `app/styles/enterprise.css`가 정한다.

## 방향

업무용 도구로 보이게 한다. 장식보다 표·숫자·근거가 먼저 읽혀야 한다.

- **짙은 남색 사이드바(`--nav #0f1b2d`) + 흰 본문(`--surface`), 바탕 `#f9fafb`.** 구분은 1px 회색 선, 그림자는 떠 있는 것(팝업·드로어)에만
- **모서리 4px 하나**(`--radius`). 굵기는 700까지. 그라데이션·빛·유리·이모지 없음
- **강조색은 짙은 파랑 하나(`--brand #1d4ed8`)**: 버튼, 링크, 선택된 메뉴처럼 누를 수 있는 것과 선택 표시에만
- **판정 등급(순항·주의·긴급) 색은 바탕을 칠하지 않고 작은 점 + 색 글자로만**(`--grade-fine/-watch/-act`). 성공 알림·사용량 안내·정지 배지에는 쓰지 않고 회색/파랑을 쓴다
- 글자 크기 다섯 단계(12·13·14·16·20). 본문 14px, 화면 제목 20px
- 글꼴은 번들된 Noto Sans KR(가변), 고정폭은 B/L 같은 번호용 IBM Plex Mono. 폰트 CDN을 부르지 않는다
- 아이콘은 lucide 선 아이콘만
- 소개 랜딩(`.lp`, `landing.css`)은 사진이 있는 밝은 화면이라 이 규칙과 따로 둔다

## 화면 색

설정 > "설정" 탭에서 고른다(`lib/theme.ts`). 이 브라우저에만 저장한다(localStorage).

| 값 | 뜻 |
|---|---|
| system | 운영체제의 밝게·어둡게를 따른다 |
| light / dark / comfort | 밝게 / 어둡게 / 눈 편하게(미색 바탕, 대비 한 단계 낮춤). 색 값은 `enterprise.css`의 `:root[data-theme=...]` |
| custom | 강조색·바탕색·사이드바색·글자 진하기를 직접 고르면 나머지 색을 계산해 CSS 변수로 넣는다 |

첫 화면이 깜빡이지 않게 `layout.tsx`의 작은 스크립트(`THEME_BOOT_SCRIPT`)가 그리기 전에 `data-theme`을 넣고, `ThemeSync`가 이후 변경을 따른다. 지도 색(`--map-*`)과 소개 랜딩은 화면 색과 상관없이 늘 밝게 그린다.

## 구조

| 자리 | 컴포넌트 | 비고 |
|---|---|---|
| 홈 헤드라인·입력 | `app/page.tsx`, `ChatBar.tsx` | 예시는 채우기만, 보내기는 사용자가. Enter 전송, Shift+Enter 줄바꿈, 한글 조합 중 Enter 무시, 500자. 서류 사진·PDF는 클립·붙여넣기·끌어놓기로 첨부 |
| 기다림 | `ChatBar.tsx` Pending | 서버가 SSE로 보낸 실제 단계만, 경과 시간은 브라우저 실측 (docs/specs/chat-progress.md) |
| 첫 진입 | `ChatBar.tsx` feature-tiles | 실제로 할 수 있는 작업만. API 없는 KPI는 만들지 않는다 |
| 입력으로 돌아가기 | `ChatBar.tsx` back-to-composer, `lib/composer.ts` | 입력창이 화면 밖이면 나타난다 |
| 결과 나눔 | `app/page.tsx` | 결과가 없으면 대화만 가운데, 결과가 생기면 대화(왼쪽) 40 : 상세(오른쪽) 60. 휴대폰에서는 상세가 아래 시트 |
| 상세 드로어 | `Drawer.tsx` | 대화별 근거 모음. 포커스 트랩, Esc, 뒤 화면 inert, 닫으면 포커스 복귀 |
| 오늘의 화물 | `TodayBoard.tsx` | 기억한 화물, 지켜보는 화물, 판정 변동, 카카오 연결 상태를 한 화면에 |
| 사이드바 | `Shell.tsx` | 오늘의 화물 / 내 화물(화물 조회, 지켜보는 화물) / 화물 현황 / 조회 이력 / 설정, 운영자에게만 평가 지표·관리(GET /api/me). 태블릿 아이콘 레일은 aria-label로 이름 유지 |
| 데모 둘러보기 | `TourGate.tsx`, `TourBanner.tsx` | 상단 띠로 데모 중임을 늘 보이고 "데모 끝내기"를 둔다 |

## 움직임

기업형 화면은 정적으로 둔다. `enterprise.css`가 올라오며 나타나기·차례 등장(`.enter`, `.stagger`, `.route-fade`)을 끄고, 색 바뀜과 드로어 열림, 실제 대기 중의 대기 표시만 남긴다. `prefers-reduced-motion: reduce`이면 남은 움직임도 끈다. 지도에 가짜 항적이나 위치 펄스는 없다.

## CSS 관리

`app/globals.css`의 `:root` 토큰이 기본이고, `layout.tsx`가 그 뒤에 화면별 CSS를 불러온다: chat, detail, compare-vessel, cargo-sheet, watch, settings, admin, entry, landing, features, enterprise, today. 뒤에 오는 파일이 앞의 같은 선택자를 덮는다. `enterprise.css`가 토큰 값과 공통 모양을 안A로 덮는 테마 파일이고, 동작(TSX)은 건드리지 않는다. 옛 토큰 이름(`--rail`, `--ink`, `--muted`, `--success/--warning/--danger`)은 새 이름(`--brand`, `--text`, `--text-2`, `--grade-*`)을 가리키도록 남아 있다.
