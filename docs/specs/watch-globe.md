# Spec: 지켜보기 지도를 위성 지구본으로 (WatchMap)

상태: 구현 완료. 아래 "정할 것" Q1~Q4는 2026-10-30에 제안대로 확정했고, SDD 5.13 지도 줄과 docs/specs/cargo-watch.md "지도 바탕" 절에도 반영했다. 로컬에서 typecheck, 계약 검사, e2e(체험 모드)를 통과했다. live 모드 e2e, 프로덕션 빌드, 배포 확인은 아직 하지 않았다.

## Why

- **상황**: 지켜보는 화물 화면(`/watch`)은 사용자가 지켜보기를 켠 배 여러 척을 한 지도에 보여 준다. 사용자가 알고 싶은 것은 정확한 좌표가 아니라 "이 정도 왔구나"(부산까지 남은 거리)와 지나온 길이다.
- **문제**: 지금 지도는 평면(Leaflet + OpenFreeMap positron)이라 바다를 건너오는 배를 멀리서 볼 때 어디쯤인지 감이 약하다. 배가 여러 바다에 흩어지면 메르카토르 왜곡으로 거리감도 틀어진다.
- **비교**: 같은 모의 배를 ① 지금 평면, ② MapLibre 지구본, ③ Cesium 위성 지구본으로 그린 목업을 보고 사용자가 ③을 골랐다. 재난 에이전트(typhoon-quake-agent)의 God's Eye View 화면과 같은 엔진·바탕이다.

## Goal

- **해결 목표**: `/watch`의 지도 하나만 CesiumJS 위성 지구본으로 바꾸고, 지금 지도가 하는 일(핀, 말풍선, 경로, 클러스터, 목록 연동, 데모 움직임)은 그대로 둔다.
- **성공 기준**: 아래 AC 전부. 바꾼 뒤에도 `npm run typecheck`, `npm run test:e2e`, `npm run test:contracts` 통과.
- **Out of Scope**
  - 다른 지도 셋은 바꾸지 않는다: 채팅 미니 지도(`ChatMiniMap`), 판정 결과 지도(`MapPanel`), 입항 선박 지도(`VesselMap`). 작고 여러 개가 뜨거나(브라우저 WebGL 문맥 수 한도) 부산 근처 확대가 목적이라 평면이 맞다.
  - 3D 지형, 3D 건물, Cesium ion(가입·토큰). 쓰지 않는다.
  - 백엔드·API·DB 변경. 화면이 쓰는 값(`GET /api/watch`, `GET /api/watch/vessels`)은 그대로다.
  - 출발항 기준 "전체 항해의 몇 %" 표시. 출발항 데이터가 없어 새 결정이 필요하다.

## What

**Happy Path**
1. `/watch`를 열면 지켜보는 배와 부산항이 모두 들어오는 범위로 위성 지구본이 뜬다. 곡면이 보이도록 처음 카메라 높이에 아래 한계를 둔다.
2. 배마다 지금과 같은 글자 핀(A, B, C…)이 뜬다. 색, 판정 테두리, 지켜보기 버튼으로 넣은 위치(seed)의 점선 회색 핀도 그대로다.
3. 핀에 마우스를 올리면 지금과 같은 말풍선(선명, 판정, 가린 B/L, 화물 이름, 통관 단계, 도착 예정, 위치 받은 시각, 위치 출처)이 뜬다.
4. 배마다 6시간 간격 경로를 그 배 색 선과 작은 점으로 그린다. 평소엔 흐리게, 고른 배와 가리키는 배만 진하게.
5. 화면에서 가까운 배끼리는 숫자 클러스터로 묶고, 누르면 지금과 같은 상세 창이 뜬다.
6. 오른쪽 목록과 지도가 서로 강조된다. 목록 줄을 누르면 그 배로 날아가고, 혼자면 말풍선, 묶여 있으면 상세 창을 연다.
7. "모든 배 보기", "6시간 간격 경로 보기"는 지금처럼 동작한다.
8. 오른쪽 위 작은 단추로 바탕을 위성 / 지도로 바꿀 수 있다 (재난 에이전트 미리보기와 같은 두 바탕).

**Edge Cases**

| # | 상황 | 처리 |
|---|---|---|
| EC-01 | 브라우저가 WebGL을 못 켬 | 지구본을 만들지 않고 지금 Leaflet 평면 지도로 그린다. 기능은 지금과 같다 |
| EC-02 | Cesium 파일을 못 불러옴(네트워크·용량) | EC-01과 같이 Leaflet 평면 지도로 되돌린다 |
| EC-03 | 위성 타일(EOX)이 제한 시간 안에 안 오거나 오류 | 지도 바탕(EOX osm)으로 바꾸고, 그것도 실패하면 바탕 없이 바다색 구에 레포 해안선(`lib/fixtures/geography.json`)을 그린다. 핀·경로·말풍선은 바탕과 상관없이 그린다 |
| EC-04 | 배가 지구 뒤편에 있음(카메라 반대쪽) | 핀과 말풍선을 숨긴다. 클러스터 계산에서도 뺀다 |
| EC-05 | 데모 둘러보기가 10초마다 위치를 바꿈 | 지구본은 한 번만 만들고 핀·경로 위치만 바꾼다. 카메라는 그대로 둔다(지금처럼 화면이 튀지 않게) |
| EC-06 | 움직임 줄이기 설정 | 카메라 이동을 애니메이션 없이 바로 바꾼다 |
| EC-07 | 화면을 가만히 둠 | 움직임이 있을 때만 다시 그린다(`requestRenderMode`). 탭을 열어 두기만 해도 GPU를 계속 쓰지 않게 |
| EC-08 | 페이지를 떠남 | 지구본을 지워 WebGL 문맥을 푼다 |
| EC-09 | 핀 말풍선이 지도 오른쪽·아래 끝에 걸림 | 말풍선을 반대쪽으로 뒤집어 칸 안에 둔다 |
| EC-10 | 위치가 있는 배가 0척 | 지금처럼 지도 없이 안내 문구만. 지구본을 만들지 않는다 |

## How

**구조**

- `web/components/WatchMap.tsx`: 데이터 읽기, 목록, 클러스터 상세 창, 단추는 그대로 둔다. 지도 그리기만 떼어 낸다.
- `web/components/watch/WatchGlobe.ts`(새): Cesium으로 그리기. 위 Happy Path 2~8.
- `web/components/watch/WatchLeaflet.ts`(새): 지금 `WatchMap.tsx`의 Leaflet 그리기 코드를 옮긴 것. EC-01, EC-02 되돌림용. 동작을 바꾸지 않는다.
- 두 파일은 같은 모양의 함수(지도 만들기, 데이터 바꾸기, 배로 이동, 모두 보기, 강조, 지우기)를 내보내고, `WatchMap.tsx`는 어느 쪽인지 모른다.

**그리기 방식**

- 핀·클러스터·부산항 표식은 **HTML 요소**로 지구본 위에 띄우고, 매 그리기 뒤(`scene.postRender`)에 화면 좌표로 옮긴다. 지금 CSS(`.watch-pin`, `.pin-body`, `.seeded`, `.watch-cluster`, `.watch-tooltip`)와 말풍선 HTML(`tip()`), 키보드 접근(title, alt)을 그대로 쓰려는 것이다. 핀은 최대 10척 + 클러스터라 매번 옮겨도 가볍다.
- 경로 선과 경로 점은 Cesium 개체(polyline, point)로 그리고, 마우스 아래 개체 집기(`scene.pick`)로 가리키기·누르기를 받는다.
- 클러스터는 지금 규칙을 그대로 옮긴다: 화면 거리 `CLUSTER_PX` 안이면 묶고, 가장 가까이 당겼을 때는 묶지 않는다. 확대 단계 대신 카메라 높이를 쓴다.

**바탕 (재난 에이전트 결정 0013·0022와 같은 출처)**

| 순서 | 바탕 | 주소 | 출처 표기 |
|---|---|---|---|
| 1 | EOX Sentinel-2 cloudless 2020 위성 (WGS84, 최대 14단계) | `tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2020/default/WGS84/{z}/{y}/{x}.jpg` | EOxCloudless by EOX IT Services GmbH (Contains modified Copernicus Sentinel data 2020), CC BY-NC-SA 4.0 |
| 2 | EOX OSM 지도 (WGS84, 최대 17단계) | `tiles.maps.eox.at/wmts/1.0.0/osm/default/WGS84/{z}/{y}/{x}.jpg` | Data © OpenStreetMap contributors, Rendering © EOX |
| 3 | 바다색 구 + 레포 해안선 | 외부 호출 없음 | Natural Earth |

- EOX 위성은 **비상업 전용**(CC BY-NC-SA 4.0)이다. Marvia는 광고·구독 없는 개인 포트폴리오라 쓸 수 있다. 상업 전환 때는 재난 에이전트처럼 NASA GIBS로 바꾼다.
- 외부로 나가는 지도 주소가 `tiles.openfreemap.org` 하나에서 `tiles.maps.eox.at`까지 둘이 된다. SDD 5.13 지도 줄을 같이 고친다.

**Cesium 불러오기**

- npm `cesium`을 정확한 버전으로 고정해 설치한다(작성 시점 최신 1.146.0). CDN은 쓰지 않는다. 지금도 글꼴·지도 자료를 로컬로 번들한다(`next.config.ts`).
- `Build/Cesium`의 `Cesium.js`(본체), `Workers`, `Assets`, `ThirdParty`, `Widgets`를 `public/cesium/`으로 복사하는 스크립트를 `scripts/copy-maplibre-worker.mjs`처럼 predev/prebuild에 붙이고, `public/cesium/`은 `.gitignore`에 넣는다. `window.CESIUM_BASE_URL = '/cesium/'`.
- 본체는 번들러에 넣지 않는다. `WatchGlobe.ts`의 `loadCesium`이 `/cesium/Cesium.js`를 `<script>` 하나로 한 번만 불러와(30초 제한) 전역 `window.Cesium`을 쓴다. npm `cesium`은 타입(`import type`)으로만 쓴다.
  - 이유: Turbopack이 npm `cesium`을 묶어 줄이면 템플릿 문자열의 8진 이스케이프가 깨져, 운영 빌드의 그 청크가 "Octal escape sequences are not allowed in template strings"로 안 읽힌다. 개발 모드(`npm run dev`)에서는 멀쩡해 e2e가 놓쳤다. 공식 미리 빌드 파일은 이 문제가 없다.
  - 파일 못 받음, 시간 초과, 불러온 뒤 `window.Cesium` 없음, 지구본 만들기 중 오류는 모두 EC-02로 Leaflet이 이어받고 `data-renderer="leaflet"`가 붙는다. 청크가 안 읽혀 동적 import가 끝나지 않는 경우에 대비해 지구본 만들기 전체에도 45초 안전판이 있다(`components/watch/index.ts`).
- `/watch` 화면 코드(`WatchGlobe.ts`)는 동적 import 한다. `Cesium.js`는 `/watch`에 들어갈 때만 받으므로 다른 화면 첫 로딩은 무거워지지 않는다.
- 운영 빌드에서 확인한다: `npm run build` 뒤 `.next/static/chunks/*.js`를 모두 `new Function(src)`로 읽어 구문 오류가 없어야 하고, e2e에 `/cesium/Cesium.js`를 막았을 때 Leaflet으로 되돌아가는 확인이 있다.
- Viewer 옵션: `baseLayer: false`, 타원체 지형, 위젯(타임라인, 애니메이션, 지오코더, 홈, 장면 모드, 도움말, 전체 화면, infoBox, 선택 표시) 모두 끔. `requestRenderMode: true`.

**문서·라이선스**

- `web/THIRD_PARTY.md`: CesiumJS(Apache-2.0), EOX 두 바탕과 표기 문구를 더한다.
- `docs/specs/cargo-watch.md` "지도 바탕" 절, SDD 5.13 지도 줄, `web/README.md` 지도 설명을 고친다.

**테스트**

- `scripts/verify.mjs`에 `/watch` 확인을 더한다(지금은 `/watch` 지도를 보지 않는다): 지구본 캔버스가 뜨고 핀 수가 위치 있는 배 수(체험 예시 기준)와 같다. WebGL을 끈 실행에서는 Leaflet 지도(`.leaflet-container`)가 뜬다.
- `npm run typecheck`.

## AC (Given-When-Then)

**AC-01 · 지구본으로 뜬다**
- GIVEN: 위치가 있는 지켜보기 배가 1척 이상, WebGL 가능
- WHEN: `/watch`를 연다
- THEN: Cesium 지구본이 뜨고, 배마다 글자 핀 하나와 부산항 표식이 보이며, 위성 바탕 출처 문구가 보인다

**AC-02 · 지금 기능 유지**
- GIVEN: AC-01 상태
- WHEN: 핀에 마우스를 올리고, 목록 줄을 누르고, 클러스터를 누르고, 경로 보기를 끈다
- THEN: 지금 Leaflet 지도와 같은 말풍선 칸, 같은 이동·강조, 같은 상세 창이 뜨고 경로가 사라진다

**AC-03 · WebGL이 없으면 평면 지도**
- GIVEN: WebGL을 끈 브라우저
- WHEN: `/watch`를 연다
- THEN: 지금 Leaflet 지도가 뜨고 AC-02가 그대로 성립한다. 빈 칸이나 오류 화면이 없다

**AC-04 · 바탕 실패 되돌림**
- GIVEN: EOX 위성 요청을 일부러 실패시킨다
- WHEN: `/watch`를 연다
- THEN: 지도 바탕으로, 그것도 막으면 해안선 구로 바뀌고, 핀·경로는 계속 보인다

**AC-05 · 데모 둘러보기**
- GIVEN: 데모 둘러보기 모드
- WHEN: 10초마다 배 위치가 바뀐다
- THEN: 지구본을 새로 만들지 않고 핀·경로만 움직인다. 카메라는 사용자가 둔 자리 그대로다

**AC-06 · 가만히 두면 다시 그리지 않는다**
- GIVEN: AC-01 상태에서 아무 조작도 하지 않는다
- WHEN: 10초 동안 둔다
- THEN: 그 사이 Cesium 장면을 다시 그린 횟수가 0이다 (`scene.postRender` 횟수로 확인)

**AC-07 · 다른 지도는 그대로**
- GIVEN: 채팅 미니 지도, 판정 결과 지도, 입항 선박 지도
- WHEN: 이 변경 뒤 각 화면을 연다
- THEN: 지금과 같은 Leaflet 평면 지도이고 기존 e2e가 통과한다

## 정할 것 (구현 전에 사용자 확인)

| # | 질문 | 제안 | 결정 |
|---|---|---|---|
| Q1 | WebGL·Cesium 실패 때 지금 Leaflet 지도로 되돌릴까, 안내 문구만 보일까 | 되돌린다. 코드는 지금 것을 옮기기만 해서 새로 짤 부분이 적다 | 제안대로 |
| Q2 | 위성 / 지도 바탕 전환 단추를 둘까 | 둔다. 위성에는 지명이 없어 처음 보는 사람은 지도 바탕이 필요하다 | 제안대로 |
| Q3 | 목록·핀에 "부산까지 남은 거리"를 더할까 | 이번 범위에서 뺀다. `/api/watch` 응답에 그 칸이 없어 API 변경이 필요하다. 원하면 별도 Spec | 제안대로 |
| Q4 | 어두운 지구본과 밝은 화면 카드가 붙는 모양 | 지도 칸만 어둡게 두고 칸 둘레와 위쪽 단추 줄은 지금 밝은 카드 그대로 | 제안대로 |
