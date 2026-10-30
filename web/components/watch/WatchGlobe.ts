/** 지켜보기 지도의 Cesium 위성 지구본 그리기 (docs/specs/watch-globe.md).
 * - WatchMap.tsx가 넘긴 RenderInput(배·경로·부산항)을 지구본 위에 그린다. 지도 쪽은 이 파일이 지구본인지 평면인지 모른다 (types.ts WatchRenderer)
 * - 어떤 단계에서든 던지면(WebGL 없음, Cesium 파일 못 받음) 호출한 쪽이 Leaflet 평면 지도로 되돌린다 (spec EC-01, EC-02)
 * - 핀·클러스터·부산항·말풍선은 HTML 요소를 지구본 위 겹침 층에 띄우고 매 그리기 뒤(scene.postRender)에 화면 좌표로 옮긴다.
 *   기존 CSS(.watch-pin, .watch-cluster, .watch-tooltip)와 말풍선 HTML을 그대로 쓰려는 것이다 (spec "그리기 방식")
 * - 경로 선과 점은 Cesium 개체로 그리고 scene.pick으로 가리키기·누르기를 받는다
 * - requestRenderMode라 움직임이 있을 때만 다시 그린다 (spec EC-07). 바꾼 뒤에는 scene.requestRender()를 부른다 */
import type {Cartesian2, Cartesian3, Rectangle, Camera, PointPrimitive, Polyline, Scene} from 'cesium';
import {getGeography} from '@/lib/api';
import type {BaseKind, CreateRenderer, RenderInput, RenderPin, WatchMapCallbacks, WatchRenderer} from './types';

type CesiumModule = typeof import('cesium');

// --- 불러오기 ---
const CESIUM_BASE = '/cesium/';  // Workers·Assets·Widgets가 서 있는 주소 (scripts/copy-cesium-assets.mjs가 public/cesium/에 복사)
const CESIUM_CSS = `${CESIUM_BASE}Widgets/widgets.css`;

// --- 바탕 (spec "바탕" 표). 둘 다 EOX WGS84 타일이라 GeographicTilingScheme을 쓴다 ---
const SAT_URL = 'https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2020/default/WGS84/{z}/{y}/{x}.jpg';
const SAT_MAX_LEVEL = 14;  // spec 표: 위성 최대 14단계
const SAT_CREDIT = 'EOxCloudless by EOX IT Services GmbH (Contains modified Copernicus Sentinel data 2020), CC BY-NC-SA 4.0';
const MAP_URL = 'https://tiles.maps.eox.at/wmts/1.0.0/osm/default/WGS84/{z}/{y}/{x}.jpg';
const MAP_MAX_LEVEL = 17;  // spec 표: 지도 최대 17단계
const MAP_CREDIT = 'Data © OpenStreetMap contributors, Rendering © EOX';
const PROBE_TIMEOUT_MS = 5000;  // 0단계 타일 한 장을 이 시간 안에 못 받으면 그 바탕을 못 쓰는 것으로 본다 (spec EC-03)
const TILE_ERROR_LIMIT = 3;     // 타일 오류가 이만큼 쌓이면 못 쓰는 것으로 본다. 타일 한두 장의 일시 오류에는 바탕을 바꾸지 않는다
const GLOBE_COLOR = '#0b1a2e';  // 타일이 오기 전과 해안선 바탕일 때 보이는 바다색 구 (spec Q4 어두운 지도 칸)
const SPACE_COLOR = '#050b16';  // 구 바깥 배경
const COAST_STROKE = '#7aa2c8';  // 해안선 바탕의 육지 테두리
const COAST_FILL = '#1d3b5c';    // 해안선 바탕의 육지 칠

// --- 카메라. Leaflet의 minZoom 4 / MAX_ZOOM 13을 높이로 옮긴 값 ---
const MIN_ZOOM_M = 3000;          // 가장 가까이. Leaflet MAX_ZOOM 13(약 600m까지 갈라짐)에 해당하는 지면 위 높이
const MAX_ZOOM_M = 25_000_000;    // 가장 멀리. Leaflet minZoom 4(대륙 하나가 보임)보다 넓게 지구 전체가 보이는 높이
const MIN_FIT_HEIGHT_M = 1_500_000;  // 처음·모두 보기의 가장 낮은 높이. 배가 한곳에 몰려도 곡면이 보이게 한다 (spec Happy Path 1)
const FIT_PAD = 0.25;             // 모든 배 보기 여백 (Leaflet fitBounds.pad(.25))
const TRACK_PAD = 0.3;            // 한 배 경로로 이동할 때 여백 (Leaflet trackBounds.pad(.3))
const TRACK_MIN_HEIGHT_M = 50_000;   // 한 배 경로로 가도 이보다 낮게는 안 간다 (Leaflet zoom 9 상한)
const POINT_HEIGHT_M = 100_000;   // 경로 없는 배로 갈 때 가장 낮추지 않아도 되는 높이 (Leaflet zoom 8 하한)
const FLY_SECONDS = 1.2;          // 이동 시간. 움직임 줄이기면 0 (spec EC-06)
const MIN_PAD_RAD = 0.02;         // 여백이 0이 되지 않게 하는 최소 폭(약 130km). 배 한 척뿐이어도 범위가 생긴다

// --- 클러스터·말풍선 ---
const CLUSTER_PX = 44;  // WatchMap.tsx CLUSTER_PX와 같다. 화면에서 이 거리 안이면 한 클러스터로 묶는다
const NEAR_MAX_FACTOR = 1.1;  // 카메라 높이가 MIN_ZOOM_M의 이 배수 이하면 "가장 가까이"로 보고 묶지 않는다 (Leaflet atMax)
const TIP_NAMES = 5;    // 클러스터 말풍선에 이름을 보일 최대 척 수
const TIP_MARGIN = 4;   // 말풍선이 칸 가장자리에서 떨어져 있어야 하는 거리 (spec EC-09)
const PIN_SIZE = 34;
const CLUSTER_SIZE = 46;
const PORT_SIZE = 28;
const OFFSCREEN_PX = 80;  // 칸 밖으로 이만큼 넘으면 요소를 숨긴다

// --- 경로 ---
const TRACK_LIFT_M = 500;      // 선·점을 지면에서 띄운 높이. 구와 겹쳐 깜빡이는 것을 막는다
const TRACK_STEP_DEG = 0.25;   // 점 사이를 이 각도(약 28km)보다 촘촘히 나눠, 곧은 선이 구 속으로 파고들지 않게 한다
const HIT_WIDTH = 16;          // 보이지 않는 굵은 선 폭. 가는 선에 마우스를 맞추기 쉽게 한다 (Leaflet track-hit)
const HIT_ALPHA = 0.01;        // 0이면 Cesium이 집기에서 빼므로 거의 안 보이는 값을 쓴다
const PICK_BOX_PX = 8;         // scene.pick 허용 범위
const ALPHA_IDLE = 0.25, ALPHA_HOVER = 0.8, ALPHA_SELECT = 0.95;  // Leaflet paint()와 같은 선 투명도
const DOT_FAINT_ALPHA = 0.35;  // 고르지도 가리키지도 않은 배의 경로 점 (Leaflet .faint)

type Pt = {x: number; y: number};
/** 말풍선이 붙어 있는 대상. 핀은 key로 찾아 데이터가 바뀌어도 따라간다 */
type TipTarget = {kind: 'pin'; key: string} | {kind: 'cluster'; group: number[]};
/** 배 하나의 경로 개체 */
interface Track {line: Polyline; hit: Polyline; dots: PointPrimitive[]; color: InstanceType<CesiumModule['Color']>}

/** 글자 직접 이스케이프. 말풍선은 React가 아니라 HTML 글자로 만든다 (WatchMap.tsx esc와 같다) */
function esc(v: string) { return v.replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c] as string)); }

/** 움직임 줄이기 설정이면 true (spec EC-06) */
const reducedMotion = () => typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

/** Cesium 위젯 CSS를 한 번만 붙인다. 번들러가 아니라 public/cesium의 사본을 쓴다 */
function ensureCss() {
  if (document.querySelector(`link[href="${CESIUM_CSS}"]`)) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet'; link.href = CESIUM_CSS;
  document.head.appendChild(link);
}

/** 타일 한 장을 제한 시간 안에 받을 수 있는지 본다. 바탕을 바꿀지 정하는 근거 (spec EC-03) */
async function probe(url: string): Promise<boolean> {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), PROBE_TIMEOUT_MS);
  try { const r = await fetch(url.replace('{z}', '0').replace('{y}', '0').replace('{x}', '0'), {signal: ac.signal}); return r.ok; }
  catch { return false; }
  finally { clearTimeout(timer); }
}

/** 지구본 그리기. 실패하면 던진다 (호출한 쪽이 Leaflet으로 되돌린다) */
export const createGlobe: CreateRenderer = async (host, input, cb) => {
  if (typeof window === 'undefined') throw new Error('globe needs a browser');
  (window as unknown as {CESIUM_BASE_URL?: string}).CESIUM_BASE_URL = CESIUM_BASE;
  ensureCss();
  const Cesium = await import('cesium');
  return build(Cesium, host, input, cb);
};

/** Cesium 모듈이 준비된 뒤 위젯과 겹침 층을 만들고 WatchRenderer를 돌려준다 */
function build(C: CesiumModule, host: HTMLElement, first: RenderInput, cb: WatchMapCallbacks): WatchRenderer {
  // --- 뼈대: 위젯 칸과 HTML 겹침 층. 인라인 크기를 줘서 CSS가 없어도 칸을 채운다 ---
  if (getComputedStyle(host).position === 'static') host.style.position = 'relative';
  const root = document.createElement('div');
  root.className = 'watch-globe';
  root.style.cssText = 'position:absolute;inset:0;overflow:hidden';
  const overlay = document.createElement('div');
  overlay.className = 'globe-overlay';
  overlay.style.cssText = 'position:absolute;inset:0;overflow:hidden;pointer-events:none;z-index:2';
  host.append(root, overlay);
  host.dataset.renders = '0';

  let widget: InstanceType<CesiumModule['CesiumWidget']>;
  try {
    widget = new C.CesiumWidget(root, {
      baseLayer: false,                                  // Cesium ion 기본 위성 사진을 쓰지 않는다 (spec Out of Scope)
      terrainProvider: new C.EllipsoidTerrainProvider(),  // 3D 지형 없음
      requestRenderMode: true,                           // 움직임이 있을 때만 그린다 (spec EC-07)
      maximumRenderTimeChange: Infinity,                 // 시계가 흘러도 다시 그리지 않는다
      scene3DOnly: true,
      skyBox: false, skyAtmosphere: false,               // 별·대기 없음. 어두운 구 하나만
    });
  } catch (e) { overlay.remove(); root.remove(); delete host.dataset.renders; throw e; }

  const scene: Scene = widget.scene;
  const camera: Camera = widget.camera;
  const canvas = widget.canvas;
  scene.globe.baseColor = C.Color.fromCssColorString(GLOBE_COLOR);
  scene.backgroundColor = C.Color.fromCssColorString(SPACE_COLOR);
  scene.globe.showGroundAtmosphere = false;
  if (scene.sun) scene.sun.show = false;
  if (scene.moon) scene.moon.show = false;
  const ctl = scene.screenSpaceCameraController;
  ctl.minimumZoomDistance = MIN_ZOOM_M;
  ctl.maximumZoomDistance = MAX_ZOOM_M;

  let destroyed = false;
  let renders = 0;
  const render = () => { if (!destroyed) scene.requestRender(); };
  const ellipsoid = C.Ellipsoid.WGS84;
  const occluder = new C.EllipsoidalOccluder(ellipsoid, camera.positionWC);

  // --- 상태 ---
  let pins: RenderPin[] = [];
  let worlds: Cartesian3[] = [];
  let portWorld: Cartesian3 = C.Cartesian3.fromDegrees(first.port.lng, first.port.lat);
  let hovNow: number | null = null;   // 지금 가리키는 배 (목록·지도 어느 쪽이든)
  let selNow: number | null = null;   // 지금 고른 배
  let trackHover: number | null = null;  // 지도 경로 위에서 가리키는 배. 핀 위로 옮겨가면 비운다
  let openedKey: string | null = null;   // focus()로 열어 둔 말풍선의 핀. 다른 가리키기·선택·빈 곳 누름 전까지 안 닫힌다
  let tipFor: TipTarget | null = null;
  let groups: number[][] = [];
  let groupSig = '';
  let dataVersion = 0;
  let screen: (Pt | null)[] = [];
  let clusterEls: HTMLDivElement[] = [];
  const pinEls = new Map<string, HTMLDivElement>();
  const pinSigs = new Map<string, string>();
  const idxOf = (key: string) => pins.findIndex(p => p.key === key);

  // --- 바탕 ---
  const mkLayer = (url: string, maxLevel: number, credit: string) => new C.ImageryLayer(new C.UrlTemplateImageryProvider({
    url, tilingScheme: new C.GeographicTilingScheme(), maximumLevel: maxLevel, credit: new C.Credit(credit, true)}));
  const satLayer = mkLayer(SAT_URL, SAT_MAX_LEVEL, SAT_CREDIT);
  const mapLayer = mkLayer(MAP_URL, MAP_MAX_LEVEL, MAP_CREDIT);
  mapLayer.show = false;  // 위성이 안 될 때까지 지도 타일은 받지 않는다 (숨긴 층은 요청하지 않는다)
  widget.imageryLayers.add(satLayer);
  widget.imageryLayers.add(mapLayer);
  let base: BaseKind = 'sat';
  let satFailed = false, mapFailed = false;
  let coast: Awaited<ReturnType<CesiumModule['GeoJsonDataSource']['load']>> | null = null;
  let coastLoading = false;

  /** 해안선 바탕을 한 번 읽어 올린다 (spec EC-03 마지막 단계). 외부 호출 없음 */
  async function showCoast() {
    if (coast) { coast.show = true; render(); return; }
    if (coastLoading) return;
    coastLoading = true;
    try {
      const ds = await C.GeoJsonDataSource.load(getGeography() as object, {
        stroke: C.Color.fromCssColorString(COAST_STROKE), fill: C.Color.fromCssColorString(COAST_FILL), strokeWidth: 1});
      if (destroyed) return;
      coast = ds; ds.show = base === 'none';
      await widget.dataSources.add(ds);
      // 개체 모양은 몇 번 그리는 동안 비동기로 만들어진다. 가만히 있어도 처음 몇 번은 다시 그려 준다
      for (let k = 0; k < 8; k++) setTimeout(render, 150 * k);
    } catch { /* 해안선도 못 읽으면 바다색 구만 남는다 */ }
    finally { coastLoading = false; }
  }
  /** 바탕을 바꾸고 지도 쪽에 알린다 */
  function applyBase(b: BaseKind) {
    const changed = b !== base;
    base = b;
    satLayer.show = b === 'sat'; mapLayer.show = b === 'map';
    if (b === 'none') void showCoast(); else if (coast) coast.show = false;
    host.dataset.base = b;
    render();
    if (changed) cb.onBase?.(b);  // 바뀔 때만 알린다. 지도 쪽은 처음 바탕을 sat으로 안다
  }
  /** 바탕 하나가 못 쓰는 것으로 판정됐다. 위성 → 지도 → 해안선 순으로 내려간다 (spec EC-03) */
  function degrade(failed: 'sat' | 'map') {
    if (destroyed) return;
    if (failed === 'sat') satFailed = true; else mapFailed = true;
    if (base !== failed) return;  // 지금 보는 바탕이 아니면 다음에 고를 때만 영향
    if (failed === 'sat' && !mapFailed) { applyBase('map'); void probe(MAP_URL).then(ok => { if (!ok) degrade('map'); }); }
    else applyBase('none');
  }
  // 타일 오류가 쌓이면 내려간다. 일시 오류 한두 번은 넘긴다
  let satErrors = 0, mapErrors = 0;
  satLayer.imageryProvider.errorEvent.addEventListener(() => { if (++satErrors >= TILE_ERROR_LIMIT && !satFailed) degrade('sat'); });
  mapLayer.imageryProvider.errorEvent.addEventListener(() => { if (++mapErrors >= TILE_ERROR_LIMIT && !mapFailed) degrade('map'); });
  host.dataset.base = base;
  void probe(SAT_URL).then(ok => { if (!ok && !satFailed) degrade('sat'); });

  // --- HTML 겹침 요소 ---
  const tipEl = document.createElement('div');
  tipEl.className = 'watch-tooltip';
  // 상자 모양(배경·그림자)은 watch.css가 준다. 여기서는 배치와 폭만 정해 CSS가 없어도 깨지지 않게 한다
  tipEl.style.cssText = 'position:absolute;left:0;top:0;display:none;pointer-events:none;z-index:10;width:max-content;max-width:min(320px,calc(100% - 8px))';
  overlay.appendChild(tipEl);

  const portEl = document.createElement('div');
  portEl.className = 'port-marker';
  portEl.style.cssText = `position:absolute;left:0;top:0;width:${PORT_SIZE}px;height:${PORT_SIZE}px;display:grid;place-items:center;pointer-events:none;z-index:1`;
  portEl.innerHTML = '<span>⚑</span>';
  const portLabel = document.createElement('div');
  portLabel.className = 'port-tooltip';
  portLabel.textContent = '부산항';
  portLabel.title = first.port.name;
  // 깃발 아이콘(z 1)은 핀(3)·클러스터(5) 아래에 두지만, 이름표(z 8)는 늘 그 위에 띄운다. 배가 부산항 자리에 있어도 이름표가 안 가려지게 하려는 것으로,
  // Leaflet의 영구 툴팁 층이 핀 위에 뜨던 것과 같다. 말풍선(z 10)보다는 아래이고 pointer-events:none이라 아래 클러스터를 누르는 데 방해가 안 된다
  portLabel.style.cssText = 'position:absolute;left:0;top:0;pointer-events:none;z-index:8;white-space:nowrap';
  overlay.append(portEl, portLabel);

  /** 겹침 요소를 휠로 확대할 수 있게, 요소 위의 휠을 캔버스로 넘긴다 (요소가 캔버스 위에 있어 휠이 막히지 않게) */
  function forwardWheel(el: HTMLElement) {
    el.addEventListener('wheel', e => { e.preventDefault(); canvas.dispatchEvent(new WheelEvent('wheel', e)); }, {passive: false});
  }
  const baseElStyle = (size: number) => `position:absolute;left:0;top:0;width:${size}px;height:${size}px;pointer-events:auto;cursor:pointer;will-change:transform`;

  /** 핀 요소 하나. 같은 key면 요소를 재사용해 마우스가 올라가 있는 동안 데이터가 바뀌어도 가리키기가 끊기지 않게 한다 */
  function makePinEl(key: string): HTMLDivElement {
    const el = document.createElement('div');
    el.className = 'watch-pin';
    el.setAttribute('role', 'img');
    el.style.cssText = baseElStyle(PIN_SIZE) + ';z-index:3';
    el.addEventListener('mouseenter', () => {
      const i = idxOf(key); if (i < 0) return;
      if (openedKey && openedKey !== key) closeOpened();
      trackHover = null; hovNow = i; paint(); cb.onHover(i);
      showTip({kind: 'pin', key});
    });
    el.addEventListener('mouseleave', () => {
      const i = idxOf(key); if (i < 0) return;
      if (hovNow === i) hovNow = null;
      paint(); cb.onHover(null);
      if (openedKey !== key) hideTip();
    });
    el.addEventListener('click', () => { const i = idxOf(key); if (i >= 0) highlight(i); });
    forwardWheel(el);
    overlay.appendChild(el);
    return el;
  }
  /** 데이터와 요소를 맞춘다: 없어진 배 요소는 지우고, 모양이 바뀐 배만 안을 다시 쓴다 */
  function syncPins() {
    const keep = new Set(pins.map(p => p.key));
    for (const [k, el] of pinEls) if (!keep.has(k)) { el.remove(); pinEls.delete(k); pinSigs.delete(k); }
    for (const p of pins) {
      let el = pinEls.get(p.key);
      if (!el) { el = makePinEl(p.key); pinEls.set(p.key, el); }
      const sig = [p.letter, p.name, p.pinColor, p.seeded, p.verdict].join('\u0001');
      if (pinSigs.get(p.key) === sig) continue;
      pinSigs.set(p.key, sig);
      const label = `${p.letter} ${p.name}`;
      el.title = label; el.setAttribute('aria-label', label);
      // 지켜보기 버튼 때 넣은 위치(seed)면 seeded 클래스로 회색 + 점선 테두리 (spec Happy Path 2)
      el.innerHTML = `<span style="--pin:${esc(p.pinColor)}" class="pin-body ${p.verdict ?? ''} ${p.seeded ? 'seeded' : ''}"><b>${esc(p.letter)}</b></span>`;
    }
  }

  // --- 말풍선 ---
  const clusterTip = (g: number[]) => `<div class="watch-tip"><strong>배 ${g.length}척</strong><ul class="tip-names">${g.slice(0, TIP_NAMES)
    .map(k => `<li>${esc(`${pins[k].letter} ${pins[k].name}`)}</li>`).join('')}${g.length > TIP_NAMES ? `<li class="tip-more">외 ${g.length - TIP_NAMES}척</li>` : ''}</ul>`
    + '<span>눌러서 자세히 보기</span></div>';
  /** 말풍선 내용을 대상에 맞춰 채우고 보인다. 위치는 placeTip이 잡는다 */
  function showTip(target: TipTarget) {
    let html = '';
    if (target.kind === 'pin') { const i = idxOf(target.key); if (i < 0) return; html = pins[i].tipHtml; }
    else html = clusterTip(target.group);
    tipFor = target;
    tipEl.innerHTML = html;
    tipEl.style.display = 'block';
    placeTip();
  }
  function hideTip() { tipFor = null; tipEl.style.display = 'none'; }
  function closeOpened() { if (openedKey && tipFor?.kind === 'pin' && tipFor.key === openedKey) hideTip(); openedKey = null; }
  /** 말풍선을 대상 옆에 놓는다. 핀은 오른쪽, 클러스터는 위쪽이 기본이고 칸을 넘으면 반대쪽으로 뒤집는다 (spec EC-09) */
  function placeTip() {
    if (!tipFor) return;
    let anchor: Pt | null = null, above = false;
    if (tipFor.kind === 'pin') { const i = idxOf(tipFor.key); anchor = i >= 0 ? screen[i] ?? null : null; }
    else {
      const g = tipFor.group, ci = groups.findIndex(x => x.length === g.length && x.every((v, n) => v === g[n]));
      anchor = ci >= 0 ? clusterPos[ci] ?? null : null; above = true;
    }
    if (!anchor) { tipEl.style.visibility = 'hidden'; return; }
    tipEl.style.visibility = 'visible';
    const W = overlay.clientWidth, H = overlay.clientHeight, w = tipEl.offsetWidth, h = tipEl.offsetHeight;
    let x: number, y: number;
    if (above) {
      x = anchor.x - w / 2; y = anchor.y - CLUSTER_SIZE / 2 - h - TIP_MARGIN;
      if (y < TIP_MARGIN) y = anchor.y + CLUSTER_SIZE / 2 + TIP_MARGIN;
    } else {
      x = anchor.x + PIN_SIZE / 2 + TIP_MARGIN; y = anchor.y - h / 2;
      if (x + w > W - TIP_MARGIN) x = anchor.x - PIN_SIZE / 2 - TIP_MARGIN - w;
    }
    x = Math.max(TIP_MARGIN, Math.min(x, W - w - TIP_MARGIN));
    y = Math.max(TIP_MARGIN, Math.min(y, H - h - TIP_MARGIN));
    tipEl.style.transform = `translate(${Math.round(x)}px,${Math.round(y)}px)`;
  }

  // --- 경로 ---
  const lines = new C.PolylineCollection();
  const hits = new C.PolylineCollection();
  const dots = new C.PointPrimitiveCollection();
  scene.primitives.add(lines); scene.primitives.add(hits); scene.primitives.add(dots);
  let tracks: (Track | null)[] = [];

  /** 점 사이를 촘촘히 나눈 선 좌표. 경도는 날짜변경선을 짧은 쪽으로 건넌다 */
  function dense(pts: {lat: number; lng: number}[]): Cartesian3[] {
    const out: Cartesian3[] = [];
    for (let k = 0; k < pts.length; k++) {
      const a = pts[k];
      out.push(C.Cartesian3.fromDegrees(a.lng, a.lat, TRACK_LIFT_M));
      const b = pts[k + 1]; if (!b) break;
      const dLng = ((b.lng - a.lng + 540) % 360) - 180, dLat = b.lat - a.lat;
      const n = Math.ceil(Math.max(Math.abs(dLng), Math.abs(dLat)) / TRACK_STEP_DEG);
      for (let s = 1; s < n; s++) out.push(C.Cartesian3.fromDegrees(a.lng + dLng * s / n, a.lat + dLat * s / n, TRACK_LIFT_M));
    }
    return out;
  }
  /** 경로 개체를 다 지우고 현재 데이터로 다시 만든다 (배 10척 안팎이라 가볍다) */
  function buildTracks() {
    lines.removeAll(); hits.removeAll(); dots.removeAll();
    tracks = pins.map((p, i) => {
      if (p.track.length < 2) return null;
      const color = C.Color.fromCssColorString(p.trackColor);
      const positions = dense(p.track);
      const id = {ship: i};
      const line = lines.add({positions, width: 3, material: C.Material.fromType('Color', {color: color.withAlpha(ALPHA_IDLE)}), id});
      const hit = hits.add({positions, width: HIT_WIDTH, material: C.Material.fromType('Color', {color: color.withAlpha(HIT_ALPHA)}), id});
      // 현재 위치(마지막 점)는 핀이 대신하므로 뺀다
      const ds = p.track.slice(0, -1).map(t => dots.add({position: C.Cartesian3.fromDegrees(t.lng, t.lat, TRACK_LIFT_M), pixelSize: 8,
        color: C.Color.WHITE.withAlpha(DOT_FAINT_ALPHA), outlineColor: color.withAlpha(DOT_FAINT_ALPHA), outlineWidth: 2, id}));
      return {line, hit, dots: ds, color};
    });
  }
  /** 고른 배(진하게)와 가리키는 배(중간)에 맞춰 선·점·핀을 다시 칠한다. 선택 상태는 바꾸지 않는다 */
  function paint() {
    tracks.forEach((t, i) => {
      if (!t) return;
      const sel = i === selNow, hov = i === hovNow, strong = sel || hov;
      t.line.width = strong ? 4 : 3;
      (t.line.material.uniforms as {color: unknown}).color = t.color.withAlpha(sel ? ALPHA_SELECT : hov ? ALPHA_HOVER : ALPHA_IDLE);
      const a = strong ? 1 : DOT_FAINT_ALPHA;
      t.dots.forEach(d => { d.color = C.Color.WHITE.withAlpha(a); d.outlineColor = t.color.withAlpha(a); });
    });
    pins.forEach((p, i) => { const el = pinEls.get(p.key); if (!el) return; el.classList.toggle('hovered', i === hovNow); el.style.zIndex = i === hovNow ? '4' : '3'; });
    render();
  }
  /** 지도에서 배를 골랐다 (핀·경로 누름). 목록 쪽에 알린다 */
  function highlight(i: number | null) { selNow = i; cb.onSelect(i); paint(); }

  // --- 화면 좌표와 클러스터 ---
  let clusterPos: (Pt | null)[] = [];
  /** 카메라가 움직일 때마다(매 그리기 뒤) 핀 화면 좌표를 구하고, 묶음이 바뀌면 클러스터 요소를 다시 만들고, 모든 요소를 옮긴다 */
  function layout() {
    if (destroyed) return;
    occluder.cameraPosition = camera.positionWC;
    // 지구 뒤편의 배는 숨기고 묶음 계산에서도 뺀다 (spec EC-04)
    const toScreen = (w: Cartesian3): Pt | null => {
      if (!occluder.isPointVisible(w)) return null;
      const p: Cartesian2 | undefined = C.SceneTransforms.worldToWindowCoordinates(scene, w);
      return p ? {x: p.x, y: p.y} : null;
    };
    screen = worlds.map(toScreen);
    // 가장 가까이 당겼을 때는 묶지 않는다. 안의 배를 따로 볼 방법이 없어서다 (Leaflet atMax)
    const atMax = camera.positionCartographic.height <= MIN_ZOOM_M * NEAR_MAX_FACTOR;
    const used = new Set<number>(), gs: number[][] = [];
    pins.forEach((_, i) => {
      const a = screen[i];
      if (!a || used.has(i)) return;
      const g = [i]; used.add(i);
      if (!atMax) pins.forEach((__, j) => {
        const b = screen[j];
        if (!used.has(j) && b && Math.hypot(a.x - b.x, a.y - b.y) < CLUSTER_PX) { g.push(j); used.add(j); }
      });
      gs.push(g);
    });
    const sig = `${dataVersion}#${gs.map(g => g.join(',')).join('|')}`;
    if (sig !== groupSig) rebuildClusters(gs, sig);

    const W = overlay.clientWidth, H = overlay.clientHeight;
    const put = (el: HTMLElement, p: Pt | null, size: number) => {
      if (!p || p.x < -OFFSCREEN_PX || p.y < -OFFSCREEN_PX || p.x > W + OFFSCREEN_PX || p.y > H + OFFSCREEN_PX) { el.style.display = 'none'; return; }
      el.style.display = ''; el.style.transform = `translate(${Math.round(p.x - size / 2)}px,${Math.round(p.y - size / 2)}px)`;
    };
    const single = new Set<number>();
    groups.forEach(g => { if (g.length === 1) single.add(g[0]); });
    pins.forEach((p, i) => { const el = pinEls.get(p.key); if (el) put(el, single.has(i) ? screen[i] : null, PIN_SIZE); });
    clusterPos = groups.map(g => {
      if (g.length < 2) return null;
      // 평균 위치는 3차원 좌표를 더해 지표면으로 눌러 구한다. 날짜변경선을 걸쳐도 맞다
      const sum = new C.Cartesian3();
      g.forEach(k => C.Cartesian3.add(sum, worlds[k], sum));
      return toScreen(ellipsoid.scaleToGeodeticSurface(sum, new C.Cartesian3()) ?? worlds[g[0]]);
    });
    clusterEls.forEach((el, gi) => { if (el) put(el, clusterPos[gi], CLUSTER_SIZE); });

    const pp = toScreen(portWorld);
    put(portEl, pp, PORT_SIZE);
    if (pp) { portLabel.style.display = ''; portLabel.style.transform = `translate(${Math.round(pp.x - portLabel.offsetWidth / 2)}px,${Math.round(pp.y + PORT_SIZE / 2 + 4)}px)`; }
    else portLabel.style.display = 'none';
    placeTip();
  }
  /** 묶음이 바뀌었다: 클러스터 요소를 새로 만들고, 사라진 묶음에 붙은 말풍선을 닫는다 */
  function rebuildClusters(gs: number[][], sig: string) {
    clusterEls.forEach(el => el?.remove());
    groups = gs; groupSig = sig;
    clusterEls = gs.map(g => {
      if (g.length < 2) return null as unknown as HTMLDivElement;
      const el = document.createElement('div');
      el.className = 'watch-cluster';
      el.style.cssText = baseElStyle(CLUSTER_SIZE) + ';z-index:5';
      // 테두리는 안에서 가장 급한 판정 색, 아래 점은 안에 든 배들의 색(최대 4개) (Leaflet recluster와 같다)
      const urgent = g.some(k => pins[k].verdict === 'act') ? 'act' : g.some(k => pins[k].verdict === 'watch') ? 'watch' : '';
      const dotsHtml = g.slice(0, 4).map(k => `<em style="background:${esc(pins[k].pinColor)}"></em>`).join('');
      el.innerHTML = `<span class="cluster-body ${urgent}"><b>${g.length}</b><i>${dotsHtml}</i></span>`;
      const names = g.map(k => `${pins[k].letter} ${pins[k].name}`).join(', ');
      el.title = `배 ${g.length}척: ${names}`; el.setAttribute('aria-label', `배 ${g.length}척`); el.setAttribute('role', 'img');
      el.addEventListener('mouseenter', () => { closeOpened(); showTip({kind: 'cluster', group: g}); });
      el.addEventListener('mouseleave', () => { if (tipFor?.kind === 'cluster') hideTip(); });
      el.addEventListener('click', () => { closeOpened(); hideTip(); cb.onCluster(g); });
      forwardWheel(el);
      overlay.appendChild(el);
      return el;
    });
    // 묶음이 달라졌으니 클러스터 말풍선은 닫고, 열어 둔 핀이 이제 묶였으면 그것도 닫는다
    if (tipFor?.kind === 'cluster') hideTip();
    if (openedKey) { const i = idxOf(openedKey); if (i < 0 || !gs.some(g => g.length === 1 && g[0] === i)) closeOpened(); }
  }

  // --- 카메라 ---
  const lookDown = {heading: 0, pitch: -Math.PI / 2, roll: 0};
  /** 점들을 다 담는 사각형. 날짜변경선을 건너는 경로도 짧은 쪽으로 묶는다. 여백 비율 pad */
  function rectOf(points: {lat: number; lng: number}[], pad: number): Rectangle {
    const r = C.Rectangle.fromCartographicArray(points.map(p => C.Cartographic.fromDegrees(p.lng, p.lat)));
    const w = r.east >= r.west ? r.east - r.west : r.east - r.west + 2 * Math.PI;
    const dw = Math.max(w * pad, MIN_PAD_RAD), dh = Math.max((r.north - r.south) * pad, MIN_PAD_RAD);
    const lim = Math.PI / 2 * 0.98;
    if (w + 2 * dw >= 2 * Math.PI - 0.01) return new C.Rectangle(-Math.PI, Math.max(-lim, r.south - dh), Math.PI, Math.min(lim, r.north + dh));
    // Rectangle 생성자 순서는 (west, south, east, north)다
    return new C.Rectangle(C.Math.negativePiToPi(r.west - dw), Math.max(-lim, r.south - dh), C.Math.negativePiToPi(r.east + dw), Math.min(lim, r.north + dh));
  }
  /** 사각형이 다 들어오는 카메라 자리. minHeight보다 낮으면 그 높이로 올린다 */
  function destOf(rect: Rectangle, minHeight: number): Cartesian3 {
    const pos = camera.getRectangleCameraCoordinates(rect, new C.Cartesian3());
    const c = C.Cartographic.fromCartesian(pos);
    return c.height >= minHeight ? pos : C.Cartesian3.fromRadians(c.longitude, c.latitude, minHeight);
  }
  const heightOf = (d: Cartesian3) => C.Cartographic.fromCartesian(d).height;
  /** 카메라를 옮긴다. 움직임 줄이기면 바로, 아니면 날아간다. 끝난 뒤(다음 그리기 뒤) done을 부른다. 날던 중 끊기면 부르지 않는다 */
  function moveTo(destination: Cartesian3, done?: () => void) {
    camera.cancelFlight();
    const after = () => { if (!done) return; const off = scene.postRender.addEventListener(() => { off(); if (!destroyed) done(); }); render(); };
    if (reducedMotion()) { camera.setView({destination, orientation: lookDown}); render(); after(); return; }
    let fired = false;
    camera.flyTo({destination, orientation: lookDown, duration: FLY_SECONDS, complete: () => { if (!fired) { fired = true; after(); } }});
    render();
  }
  /** 모든 배와 경로와 부산항이 들어오는 카메라 자리 */
  const fitDest = () => destOf(rectOf([...pins.flatMap(p => [{lat: p.lat, lng: p.lng}, ...p.track]), {lat: first.port.lat, lng: first.port.lng}], FIT_PAD), MIN_FIT_HEIGHT_M);

  // --- 마우스: 경로 선·점 가리키기와 누르기 ---
  const handler = new C.ScreenSpaceEventHandler(canvas);
  let pressing = false;
  const shipAt = (p: Cartesian2): number | null => {
    const picked = scene.pick(p, PICK_BOX_PX, PICK_BOX_PX) as {id?: {ship?: number}} | undefined;
    return typeof picked?.id?.ship === 'number' ? picked.id.ship : null;
  };
  handler.setInputAction(() => { pressing = true; }, C.ScreenSpaceEventType.LEFT_DOWN);
  handler.setInputAction(() => { pressing = false; }, C.ScreenSpaceEventType.LEFT_UP);
  handler.setInputAction((m: {endPosition: Cartesian2}) => {
    if (pressing) return;  // 끌고 있는 중에는 집지 않는다
    const i = shipAt(m.endPosition);
    canvas.style.cursor = i !== null ? 'pointer' : '';
    if (i === trackHover) return;
    trackHover = i; hovNow = i; paint(); cb.onHover(i);
  }, C.ScreenSpaceEventType.MOUSE_MOVE);
  handler.setInputAction((m: {position: Cartesian2}) => {
    const i = shipAt(m.position);
    if (i !== null) { highlight(i); return; }
    // 빈 곳을 누르면 말풍선·상세 창·선택을 닫는다 (Leaflet map.on('click'))
    closeOpened(); hideTip(); cb.onCluster(null); highlight(null);
  }, C.ScreenSpaceEventType.LEFT_CLICK);

  // --- 그린 횟수 세기(테스트용)와 요소 옮기기. 요소는 매번 그린 직후에 옮겨 지구본과 어긋나지 않게 한다 ---
  const offPost = scene.postRender.addEventListener(() => { host.dataset.renders = String(++renders); layout(); });
  const ro = new ResizeObserver(() => { if (destroyed) return; widget.resize(); render(); });
  ro.observe(host);

  /** 입력 데이터로 핀·경로·부산항을 바꾼다. 카메라는 건드리지 않는다 (spec EC-05) */
  function setData(input: RenderInput) {
    pins = input.pins;
    worlds = pins.map(p => C.Cartesian3.fromDegrees(p.lng, p.lat));
    portWorld = C.Cartesian3.fromDegrees(input.port.lng, input.port.lat);
    portLabel.title = input.port.name;
    if (hovNow !== null && hovNow >= pins.length) hovNow = null;
    if (selNow !== null && selNow >= pins.length) selNow = null;
    if (trackHover !== null && trackHover >= pins.length) trackHover = null;
    syncPins(); buildTracks();
    dataVersion++;  // 묶음 모양이 같아도 안의 내용(색·판정)이 바뀌었을 수 있어 클러스터 요소를 다시 만든다
    // 떠 있는 핀 말풍선은 새 내용으로 바꾼다
    if (tipFor?.kind === 'pin') { if (idxOf(tipFor.key) < 0) { hideTip(); openedKey = null; } else showTip(tipFor); }
    layout(); paint();
  }
  setData(first);
  camera.setView({destination: fitDest(), orientation: lookDown});
  render();

  return {
    kind: 'globe',
    update: setData,
    focus(i) {
      if (i < 0 || i >= pins.length) return;
      closeOpened(); hideTip(); cb.onCluster(null); highlight(i);
      // 그 배가 다른 배와 떨어져 보이는(클러스터에서 빠지는) 가장 높은 카메라 높이.
      // 화면 지름 L픽셀이 시야 fov를 채우므로, 지면 d미터가 CLUSTER_PX 픽셀이 되는 높이는 d*L/(2*tan(fov/2)*CLUSTER_PX)
      const fov = (camera.frustum as {fov?: number}).fov ?? Math.PI / 3;
      const L = Math.max(canvas.clientWidth, canvas.clientHeight, 1);
      let nearest = Infinity;
      worlds.forEach((w, j) => { if (j !== i) nearest = Math.min(nearest, C.Cartesian3.distance(worlds[i], w)); });
      const apart = nearest === Infinity ? Infinity : nearest * L / (2 * Math.tan(fov / 2) * CLUSTER_PX);
      let dest: Cartesian3;
      const p = pins[i];
      // 경로 전체가 그 높이 안에 들어오면 경로로, 아니면 경로를 포기하고 배를 가운데 두고 갈라지는 높이로 (Leaflet focus와 같은 규칙)
      const trackDest = p.track.length > 1 ? destOf(rectOf(p.track, TRACK_PAD), TRACK_MIN_HEIGHT_M) : null;
      if (trackDest && heightOf(trackDest) <= apart) dest = trackDest;
      else dest = C.Cartesian3.fromDegrees(p.lng, p.lat, Math.max(MIN_ZOOM_M * 1.05, Math.min(apart * 0.9, POINT_HEIGHT_M)));
      moveTo(dest, () => {
        // 닿은 뒤에도 다른 배와 묶여 있으면 상세 창을, 혼자면 말풍선을 연다. 누른 배를 맨 위에 둔다
        const k = idxOf(p.key); if (k < 0) return;
        const g = groups.find(x => x.includes(k));
        if (g && g.length > 1) cb.onCluster([k, ...g.filter(x => x !== k)]);
        else { openedKey = p.key; showTip({kind: 'pin', key: p.key}); }
      });
    },
    fitAll() { cb.onCluster(null); moveTo(fitDest()); },
    hover(i) { hovNow = i; trackHover = null; paint(); },
    select(i) { selNow = i; paint(); },
    setBase(b) {
      if ((b === 'sat' && satFailed) || (b === 'map' && mapFailed)) return;  // 못 쓰는 바탕은 고르지 못한다
      applyBase(b);
    },
    resize() { if (destroyed) return; widget.resize(); render(); },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      ro.disconnect(); offPost(); handler.destroy();
      overlay.remove();
      delete host.dataset.renders; delete host.dataset.base;
      widget.destroy();
      // 캔버스가 DOM에서 빠져도 WebGL 문맥은 모아 치우기 전까지 남는다. 브라우저 문맥 수 한도를 아끼려고 바로 푼다 (spec EC-08)
      const gl = (canvas.getContext('webgl2') ?? canvas.getContext('webgl')) as WebGLRenderingContext | WebGL2RenderingContext | null;
      gl?.getExtension('WEBGL_lose_context')?.loseContext();
      root.remove();
    },
  };
}
