// CesiumJS 실행 파일을 public/cesium/로 복사한다 (components/watch/WatchGlobe.ts, docs/specs/watch-globe.md).
// 이유: Cesium은 웹 워커, 기본 이미지, 서드파티 스크립트, 위젯 CSS를 CESIUM_BASE_URL('/cesium/') 아래의 고정 주소에서 불러온다.
// 번들러(Turbopack)는 이 파일들을 묶지 못하므로 node_modules의 Build/Cesium을 그대로 내려 준다(본체 Cesium.js 포함). CDN은 쓰지 않는다.
// 버전이 바뀌어도 npm install 뒤 predev/prebuild가 다시 복사한다.
import {copyFileSync, cpSync, existsSync, mkdirSync, rmSync} from 'node:fs';
const from = new URL('../node_modules/cesium/Build/Cesium/', import.meta.url), to = new URL('../public/cesium/', import.meta.url);
if (!existsSync(from)) { console.error('cesium is not installed: run npm install first'); process.exit(1); }
// 옛 버전 파일이 남지 않게 지우고 새로 복사한다
rmSync(to, {recursive: true, force: true});
mkdirSync(to, {recursive: true});
for (const d of ['Workers', 'Assets', 'ThirdParty', 'Widgets']) cpSync(new URL(d, from), new URL(d, to), {recursive: true});
// Cesium.js 본체도 복사해 /cesium/Cesium.js로 내려 준다. 번들러(Turbopack)가 묶으면 줄인 결과에서 템플릿 문자열의 8진 이스케이프가 깨져
// 운영 빌드의 청크가 구문 오류로 안 읽히므로, 번들하지 않고 <script>로 불러온다 (WatchGlobe.ts loadCesium). Apache-2.0 라이선스는 web/THIRD_PARTY.md
copyFileSync(new URL('Cesium.js', from), new URL('Cesium.js', to));
console.log('cesium assets copied to public/cesium/');
