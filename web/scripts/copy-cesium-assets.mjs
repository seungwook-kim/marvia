// CesiumJS 실행 파일을 public/cesium/로 복사한다 (components/watch/WatchGlobe.ts, docs/specs/watch-globe.md).
// 이유: Cesium은 웹 워커, 기본 이미지, 서드파티 스크립트, 위젯 CSS를 CESIUM_BASE_URL('/cesium/') 아래의 고정 주소에서 불러온다.
// 번들러(Turbopack)는 이 파일들을 묶지 못하므로 node_modules의 Build/Cesium을 그대로 내려 준다. CDN은 쓰지 않는다.
// 버전이 바뀌어도 npm install 뒤 predev/prebuild가 다시 복사한다.
import {cpSync, existsSync, mkdirSync, rmSync} from 'node:fs';
const from = new URL('../node_modules/cesium/Build/Cesium/', import.meta.url), to = new URL('../public/cesium/', import.meta.url);
if (!existsSync(from)) { console.error('cesium is not installed: run npm install first'); process.exit(1); }
// 옛 버전 파일이 남지 않게 지우고 새로 복사한다
rmSync(to, {recursive: true, force: true});
mkdirSync(to, {recursive: true});
for (const d of ['Workers', 'Assets', 'ThirdParty', 'Widgets']) cpSync(new URL(d, from), new URL(d, to), {recursive: true});
// Cesium.js 본체는 번들러가 npm 패키지에서 가져오므로 복사하지 않는다 (Apache-2.0 라이선스는 web/THIRD_PARTY.md)
console.log('cesium assets copied to public/cesium/');
