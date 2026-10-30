/** 지켜보기 지도 그리기 도구 고르기 (docs/specs/watch-globe.md "구조", EC-01, EC-02).
 * WebGL이 되면 Cesium 지구본(WatchGlobe)을, 안 되거나 Cesium을 못 불러오거나 만들다 오류가 나면 Leaflet 평면(WatchLeaflet)을 쓴다.
 * 어느 쪽이든 WatchRenderer 모양이라 WatchMap.tsx는 구분하지 않는다. 지구본 코드는 동적 import라 /watch에 들어갈 때만 내려받는다 */
import {createLeaflet} from './WatchLeaflet';
import type {CreateRenderer, WatchRenderer} from './types';

/** 브라우저가 WebGL을 켤 수 있는지. 못 켜면 Cesium 만들기에서 오류가 나므로 미리 본다.
 * lib/basemap.ts의 hasWebGL과 같은 방식이다(그 파일은 지도 4곳이 같이 쓰는 공용이라 건드리지 않고 작은 검사만 따로 둔다) */
export function supportsWebGL() {
  try {
    const c = document.createElement('canvas');
    return !!(c.getContext('webgl2') || c.getContext('webgl'));
  } catch { return false; }
}

const GLOBE_TIMEOUT_MS = 45_000;  // 지구본 만들기 전체의 마지막 안전판. WatchGlobe의 Cesium.js 불러오기 제한(30초)보다 길다

/** 지구본 만들기가 끝나지 않는 경우를 끊는다. 청크가 구문 오류로 안 읽히면 동적 import가 오류 없이 끝나지 않을 수 있어
 * (운영 빌드에서 Cesium 번들이 깨졌을 때 칸이 빈 채로 남았다), try/catch만으로는 Leaflet으로 되돌아갈 수 없다.
 * 시간이 지난 뒤 뒤늦게 만들어진 지구본은 칸에 남지 않게 바로 지운다 (spec EC-02) */
function withTimeout<T extends WatchRenderer>(task: Promise<T>, ms: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout>;
  let late = false;
  const guard = new Promise<never>((_, reject) => { timer = setTimeout(() => { late = true; reject(new Error('globe creation timed out')); }, ms); });
  task.then(r => { if (late) r.destroy(); }, () => { /* 시간 안에 실패한 것은 아래 race가 이미 알렸다 */ });
  return Promise.race([task, guard]).finally(() => clearTimeout(timer));
}

/** 칸(host)에 지도를 만든다. 지구본이 어떤 이유로든 실패하면 칸을 비우고 Leaflet으로 되돌린다.
 * 끝나면 host.dataset.renderer에 'globe' 또는 'leaflet'을 적어, 화면 검사(e2e)와 CSS가 어느 쪽인지 알 수 있게 한다 */
export const createWatchRenderer: CreateRenderer = async (host, input, cb) => {
  let r: WatchRenderer | null = null;
  if (supportsWebGL()) {
    try {
      r = await withTimeout((async () => {
        const {createGlobe} = await import('./WatchGlobe');
        return createGlobe(host, input, cb);
      })(), GLOBE_TIMEOUT_MS);
    } catch (e) {
      // EC-01, EC-02: 파일을 못 받았거나(오류, 시간 초과, window.Cesium 없음) Cesium이 오류를 냈다. 반쯤 만든 DOM을 치우고 평면 지도로 간다
      console.warn('지구본을 만들지 못해 평면 지도로 되돌려요:', e instanceof Error ? e.message : e);
      host.replaceChildren();
    }
  }
  if (!r) r = await createLeaflet(host, input, cb);
  host.dataset.renderer = r.kind;
  return r;
};
