# Marvia 앱 화면 개편 목업 브리프 (2026-10-07)

소개 랜딩(web/components/IntroContent.tsx, web/app/styles/landing.css)의 기업 톤을 로그인 후 앱 화면에 맞추기 위한 목업 기준이다.
리서치·디자인 감사·UX 분석 세 보고를 모아 정했다. 목업은 이 문서를 따른다.

## 1. 회의 결론

- 톤을 가르는 것은 색이 아니라 모서리, 글자 굵기, 부품 중복이다. 모서리를 낮추고(4/6/8px, 알약 없음) 굵기는 최대 700, 배지·버튼·탭을 한 벌로 합친다.
- 앱 셸은 shadcn dashboard 블록식(흰 사이드바 + 얇은 상단 바 + 1px 선 카드)으로, 채팅 + 결과는 Vercel Chatbot의 "대화 왼쪽, 결과 오른쪽" 분할로 간다.
- 로그인 후 첫 화면을 "오늘의 화물 보드"로 바꾼다. 매일 확인 여정을 한 화면에서 끝낸다.
- 사이드바는 7개에서 5개로 줄인다: 홈 / 물어보기 / 내 화물(표·지도 탭) / 알림·설정 / 관리(운영자).
- 판정 이유와 할 일은 접지 않고 위에 보인다. 긴급 화물은 할 일 체크리스트와 거래처 연락처를 같은 카드에 둔다.

## 2. 디자인 토큰 (모든 목업 공통, :root에 그대로)

| 토큰 | 값 |
|---|---|
| --text | #111827 |
| --text-2 | #4b5563 |
| --muted | #6b7280 |
| --line | #e5e7eb |
| --soft | #f5f7fa (앱 바탕) |
| --surface | #ffffff (카드) |
| --dark | #0f1b2d |
| --brand / --brand-hover / --brand-soft | #2962f5 / #1d4fd7 / #eef3ff |
| 순항 fine | 글자 #047857, 바탕 #ecfdf5, 띠 #10b981 |
| 주의 watch | 글자 #b45309, 바탕 #fffbeb, 띠 #f59e0b |
| 긴급 act | 글자 #b91c1c, 바탕 #fef2f2, 띠 #ef4444 |
| 모서리 | 4px(배지·칩) / 6px(버튼·입력) / 8px(카드·표) — 999px 알약 금지 |
| 그림자 | 카드 없음(1px 선만), 떠 있는 것만 0 1px 2px rgba(17,24,39,.05), 0 4px 12px rgba(17,24,39,.06) |
| 글꼴 | "Pretendard", "Noto Sans KR", "Malgun Gothic", -apple-system, sans-serif (외부 로드 없음) |
| 굵기 | 400 / 500 / 600 / 700만 |
| 크기 | 12(보조) 13(라벨) 14(표·본문 작게) 15(본문) 18(카드 제목) 24(화면 제목, 700, 자간 -.02em) |
| 눈썹글 | 12px / 600 / 자간 .08em / --brand |
| 여백 | 4 · 8 · 12 · 16 · 24 · 32 · 48 |

## 3. 공통 부품

- **앱 셸**: 왼쪽 사이드바 232px 흰색(오른쪽 1px 선), 로고 "Marvia" 18px/700, 메뉴 5개(아이콘 대신 작은 사각 점 또는 인라인 SVG), 선택 메뉴는 --brand-soft 바탕 + --brand 글자 + 모서리 6px. 상단 바 56px 흰색(아래 1px 선): 왼쪽 화면 이름, 오른쪽 "실시간 조회" 상태 점 + 사용자 이니셜 원(원형은 아바타만 허용).
- **버튼**: 기본 높이 40px, 큰 버튼 48px, 모서리 6px, 700. 주(파랑 채움) / 보조(흰 바탕 1px 선) / 위험(빨강 글자 1px 선). 보조 동작은 밑줄 링크.
- **판정 배지**: 모서리 4px, 13px/600, 의미색 바탕 + 글자 + 앞에 6px 사각 점. 순항/주의/긴급만.
- **칩(중립)**: 모서리 4px, 1px 선, 13px/500.
- **카드**: 흰 바탕, 1px --line, 모서리 8px, 안쪽 20~24px. 판정 카드는 윗선 3px 의미색 띠(랜딩 판정 카드와 같음).
- **표**: 머리줄 투명 + 아래 1px 선, 머리 글자 12px/600 --muted, 행 높이 44px, 행 사이 1px 선, 숫자는 tabular-nums.
- **탭**: 글자 탭 + 선택 시 아래 2px --brand 선.
- **지도**: 타일 없이 인라인 SVG로 단순하게(바다 #eaf1fb, 육지 #ffffff + 1px 선, 항로 점선, 배 마커). 실제 지도 라이브러리는 목업에서 쓰지 않는다.

## 4. 목업 목록

| 파일 | 화면 | 핵심 |
|---|---|---|
| 01-home-board.html | 홈 / 오늘의 화물 보드 | KPI 3칸(긴급·주의·순항), "어제 이후 바뀐 것" 카드(확인 버튼), 긴급·주의 화물 목록(지금 할 일), 아래 B/L 입력·판매 예정일·서류 올리기 |
| 02-chat-split.html | 물어보기(채팅 + 결과) | 왼쪽 대화(근거 칩, 되묻기 후보 칩, "N단계 · M초"), 입력창에 판매 예정일 칩, 오른쪽 결과: 판정 카드 + "왜 이 판정?" 3줄 + 지도 + 일정(ETA 3종) + 통관 단계 |
| 03-cargo-detail.html | 화물 상세(긴급 대응) | 긴급 판정 헤더, 할 일 체크리스트, 거래처(포워더·관세사) 바로 연락, 신호 충돌(conflicts) 설명, needs_broker 강조, 근거 목록(출처·조회 시각) |
| 04-my-cargo.html | 내 화물(표/지도 탭) | 신용장별 묶음 표에 판정·여유 일수·마지막 확인 칸 추가, 행 선택 시 일괄 다시 조회 바, 엑셀 받기, 지도 탭 미리보기 |
| 05-arrivals.html | 입항 예정 선박 | 필터 줄(기간·출발지·화물선만), 정렬 표 + 오른쪽 지도 연동(행 선택 = 지도 강조), 줄마다 지켜보기 아이콘 |

## 5. 목업 규칙

- 파일 하나로 끝낸다: HTML + 인라인 `<style>`, JS는 탭 전환 정도만(없어도 됨). 외부 CSS/JS/글꼴/이미지 로드 금지.
- 가볍게: 파일당 350줄 안팎, 30KB 이하.
- 데이터는 모두 가공값: B/L은 가린 형태(예: MRVD****0001), 배 이름은 DEMO/TEST 접두(예: DEMO PIONEER), 포워더 "데모포워딩", 연락처는 010-0000-0000 같은 자리표시값.
- 문구는 해요체. 판정 이름은 순항/주의/긴급. 실제로 없는 기능은 "(제안)"으로 표시한다.
- 너비 1440px 기준, 1024px까지 깨지지 않게. 접근성: 대비 4.5:1, 버튼은 button 태그.
- 공통 토큰·셸 CSS는 2·3절 값을 그대로 쓴다(파일마다 복사). 목업끼리 같은 셸이어야 한다.

## 6. 참고 레퍼런스

- shadcn/ui Blocks (dashboard-01): https://ui.shadcn.com/blocks
- next-shadcn-admin-dashboard (물류 대시보드): https://github.com/arhamkhnz/next-shadcn-admin-dashboard
- shadcn.io Map Package Tracking: https://www.shadcn.io/blocks/map-package-tracking
- Vercel Chatbot (Artifacts 분할): https://github.com/vercel/chatbot
- assistant-ui (채팅 속 결과 카드): https://github.com/assistant-ui/assistant-ui
- tablecn (고급 표): https://github.com/sadmann7/tablecn
- IBM Carbon Data Table (표 밀도·일괄 작업 바): https://carbondesignsystem.com/components/data-table/usage/
- Radix Themes (토큰 설계): https://www.radix-ui.com/themes/docs/overview/getting-started
