# Bundled assets and libraries

- **Leaflet 1.9.4** - BSD-2-Clause. https://leafletjs.com / https://github.com/Leaflet/Leaflet/blob/main/LICENSE
- **MapLibre GL JS** - BSD-3-Clause. 지도 바탕(벡터)을 그린다. 워커 파일은 `scripts/copy-maplibre-worker.mjs`가 `public/maplibre/`로 복사하며 라이선스 문구는 파일 머리말에 있다. https://maplibre.org
- **@maplibre/maplibre-gl-leaflet** - ISC. MapLibre를 Leaflet 레이어로 얹는다. https://github.com/maplibre/maplibre-gl-leaflet
- **OpenFreeMap (positron 스타일) / OpenStreetMap 데이터** - 지도 바탕 스타일·타일. 화면이 tiles.openfreemap.org에서 받고, `public/basemap/`의 미리 구운 타일(`scripts/bake-basemap.mjs`)도 같은 스타일로 만든 것이다. 지도에 "© OpenStreetMap · OpenFreeMap"을 표시한다. https://openfreemap.org / https://www.openstreetmap.org/copyright
- **CesiumJS (npm `cesium` 1.146.0 고정)** - Apache-2.0. `/watch` 지켜보는 화물 위성 지구본에만 쓴다. `Workers`, `Assets`, `ThirdParty`, `Widgets`를 `scripts/copy-cesium-assets.mjs`가 `public/cesium/`으로 복사하며(`.gitignore`) `/watch`에 들어갈 때만 받는다. Cesium ion은 쓰지 않는다. https://github.com/CesiumGS/cesium
- **EOX Sentinel-2 cloudless 2020 (EOxCloudless)** - CC BY-NC-SA 4.0(비상업). `/watch` 지구본의 위성 바탕. 화면이 tiles.maps.eox.at에서 받는다. 출처 표시 문구: "EOxCloudless by EOX IT Services GmbH (Contains modified Copernicus Sentinel data 2020), CC BY-NC-SA 4.0". 광고·구독 없는 개인 포트폴리오라 쓰며 상업 전환 때는 다른 바탕(NASA GIBS)으로 바꾼다. https://s2maps.eu / https://creativecommons.org/licenses/by-nc-sa/4.0/
- **EOX OSM 지도 바탕** - OpenStreetMap 데이터(ODbL)를 EOX가 그린 타일. `/watch` 지구본의 두 번째 바탕이고 위성 바탕을 못 받거나 사용자가 "지도"를 고르면 쓴다. 출처 표시 문구: "Data © OpenStreetMap contributors, Rendering © EOX". https://maps.eox.at / https://www.openstreetmap.org/copyright
- **Natural Earth geography via world-atlas 2.0.2** - public domain. `countries-10m.json`에서 한국·북한·일본·중국·대만 지형을 GeoJSON으로 변환해 `lib/fixtures/geography.json`에 저장했습니다. 지도 바탕을 못 그릴 때의 해안선으로 씁니다(`/watch` 지구본은 바다색 구 위에 그립니다). https://www.naturalearthdata.com/about/terms-of-use/ / https://github.com/topojson/world-atlas
- **topojson-client** - ISC. 지형 변환에 사용했습니다. https://github.com/topojson/topojson-client
- **Noto Sans KR / IBM Plex Mono** - SIL Open Font License 1.1. Fontsource npm 패키지의 라이선스와 폰트를 로컬 번들로 사용합니다. Manrope 패키지는 의존성에 있으나 화면에서 불러오지 않습니다. https://fontsource.org
- **intfloat/multilingual-e5-small (ONNX int8, 리비전 614241f622f5)** - MIT (모델 카드 license: mit). 백엔드 채팅 검색(RAG)의 임베딩 모델이고 화면에는 번들되지 않는다. 파일은 레포에 커밋하지 않고 `scripts/fetch_rag_model.py`가 고정 리비전·sha256으로 받아 Docker 이미지에 넣는다(Render 무료 이미지는 제외). https://huggingface.co/intfloat/multilingual-e5-small
- **sentencepiece** - Apache-2.0. 위 임베딩 모델의 토크나이저를 읽는다(백엔드 `requirements-rag.txt`). https://github.com/google/sentencepiece
- **Lucide** - ISC. https://lucide.dev/license
- **Supabase JS** - MIT. 브라우저 로그인. https://github.com/supabase/supabase-js
- **Next.js / React** - MIT. https://nextjs.org / https://react.dev
- **소개 페이지 사진** - Unsplash License. 파일별 원본은 `public/landing/README.md`

지도에는 Leaflet·OpenStreetMap·OpenFreeMap·Natural Earth 출처를 표시하고, `/watch` 지구본에는 위 EOX 두 바탕의 출처 문구를 표시합니다. ShipFinder·UNI-PASS·해수부 실제 응답을 화면 fixture에 복사하지 않았습니다. 화면에 표시된 해당 출처명은 가공 fixture의 근거 유형입니다.

Next.js 공식 설치·라우팅 문서와 Leaflet 공식 레퍼런스를 확인해 구성했습니다:
https://nextjs.org/docs/app/getting-started/installation
https://leafletjs.com/reference.html
