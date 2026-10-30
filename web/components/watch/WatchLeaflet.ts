/** 지켜보기 지도를 Leaflet 평면으로 그린다 (docs/specs/watch-globe.md). 예전 WatchMap.tsx의 지도 그리기 코드를 WatchRenderer 모양으로 옮긴 것이다.
 * 지구본(WatchGlobe)을 못 켜는 브라우저(WebGL 없음)나 Cesium을 못 불러온 경우의 되돌림 길이라 동작은 예전 지도와 같게 둔다 (EC-01, EC-02).
 * - 배 핀, 클러스터, 말풍선, 경로 선·점, 고른 배·가리키는 배 강조, 부산항 표식을 그린다
 * - 목록·상세 창·선택 상태는 WatchMap.tsx 것이다. 여기서는 콜백(cb)으로 알리기만 한다
 * - update()는 지도를 지우고 다시 그리되 보던 중심·확대를 그대로 둔다. 예전 지도가 데이터가 바뀔 때마다 그랬다 */
import {addBasemap, drawLocalGeography} from '@/lib/basemap';
import type {CreateRenderer, RenderInput, WatchMapCallbacks, WatchRenderer} from './types';

// 화면에서 이 거리(px) 안에 있는 배끼리 한 클러스터로 묶는다. 핀 폭(34px)보다 조금 넓게
const CLUSTER_PX = 44;
// 최대 확대. 10이면 44px가 약 5km라 같은 정박지의 배가 끝까지 묶여 하나가 안 보였다. 13이면 약 600m까지 갈라진다
const MAX_ZOOM = 13;
const TIP_NAMES = 5;  // 클러스터 말풍선에 이름을 보일 최대 척 수

/** Leaflet 말풍선은 React가 아니라 글자를 직접 이스케이프한다 (WatchMap.tsx의 esc와 같은 규칙. 핀 말풍선은 거기서 이미 만들어 넘기고, 여기서는 클러스터 말풍선에만 쓴다) */
function esc(v: string) { return v.replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c] as string)); }

/** Leaflet 지도를 만든다. 라이브러리는 브라우저에서만 불러온다(동적 import). 서버 렌더링 때는 실행되지 않는다 */
export const createLeaflet: CreateRenderer = async (host: HTMLElement, first: RenderInput, cb: WatchMapCallbacks): Promise<WatchRenderer> => {
  const L = await import('leaflet');
  /** 마지막으로 보던 지도 중심·확대. 지도를 다시 그려도 화면이 튀지 않게 한다 */
  let view: {center: [number, number]; zoom: number} | null = null;
  let input = first;
  let focusFn: (i: number) => void = () => {};
  let fitAllFn: () => void = () => {};
  let hoverFn: (i: number | null) => void = () => {};
  let selectFn: (i: number | null) => void = () => {};
  let resizeFn: () => void = () => {};
  let teardown: (() => void) | null = null;
  let destroyed = false;

  /** 지도 한 번 그리기. 반환하는 함수는 지우기(보던 위치 기억 + 정리) */
  function build(): () => void {
    const {pins, port: portPos, tour} = input;
    const map = L.map(host, {zoomControl: true, scrollWheelZoom: true, minZoom: 4, maxZoom: MAX_ZOOM});
    const points = pins.flatMap(p => [L.latLng(p.lat, p.lng), ...p.track.map(t => L.latLng(t.lat, t.lng))]);
    const port = L.latLng(portPos.lat, portPos.lng);
    // 중심을 먼저 정한 뒤 선을 올린다 (VesselMap과 같은 이유: 범위를 모르면 Leaflet이 멈춘다)
    map.fitBounds(L.latLngBounds([...points, port]).pad(.25), {maxZoom: 8, animate: false});
    // 다시 그릴 때(데모의 10초 갱신, 경로 보기 켜고 끄기)는 보던 확대·위치를 그대로 둔다
    if (view) map.setView(view.center, view.zoom, {animate: false});
    // 바탕은 세계 지도, 못 쓰면 해안선 (lib/basemap.ts). 데모는 10초마다 다시 그려 깜빡이지 않게 레포의 해안선 바탕을 바로 쓴다
    const base = tour ? (drawLocalGeography(L, map), {dispose() {}}) : addBasemap(L, map, () => { drawLocalGeography(L, map); });
    map.attributionControl.setPrefix(false);
    // 부산항 마커는 배 핀보다 아래층에 두고, 이름표(툴팁 층)는 늘 핀 위에 떠서 배가 같은 자리에 있어도 가려지지 않는다
    L.marker(port, {icon: L.divIcon({className: 'port-marker', html: '<span>⚑</span>', iconSize: [28, 28], iconAnchor: [14, 14]}), interactive: false, zIndexOffset: -1000})
      .addTo(map).bindTooltip(portPos.name, {permanent: true, direction: 'bottom', offset: [0, 12], className: 'port-tooltip'});

    // 경로 동그라미는 배 핀·클러스터보다 아래 층에 둔다. 같은 층이면 위도 순으로 겹쳐 클러스터를 가려 누를 수 없다
    map.createPane('watch-track').style.zIndex = '550';  // overlay(선) 400 < 경로 점 550 < 마커 600
    // 경로: 지난 위치를 오래된 순으로 선으로 잇고, 현재 위치를 뺀 지점마다 작은 동그라미 (마지막 점은 큰 핀).
    // 평소에는 모든 배 경로를 흐리게 깔고, 고른 배(핀·목록·클러스터 상세에서 누름)의 경로만 진하게 보인다.
    // 경로 점에는 말풍선을 달지 않는다
    const trackLines: (L.Polyline | null)[] = [];
    const trackHits: (L.Polyline | null)[] = [];  // 보이지 않는 굵은 선. 가는 선에 마우스를 맞추기 쉽게 한다
    const trackDots: L.Marker[][] = [];
    let selNow: number | null = null;  // 지금 고른 배 (WatchMap의 상태와 같은 값, 지도 쪽에서 바로 쓴다)
    let hovNow: number | null = null;  // 지금 가리키는 배
    pins.forEach((p, i) => {
      const track = p.track;
      if (track.length < 2) { trackLines.push(null); trackHits.push(null); trackDots.push([]); return; }
      const c = p.trackColor;
      const pts = track.map(t => [t.lat, t.lng] as L.LatLngTuple);
      trackLines.push(L.polyline(pts, {color: c, weight: 3, opacity: .25, interactive: false}).addTo(map));
      const hit = L.polyline(pts, {color: c, weight: 16, opacity: 0, className: 'track-hit'}).addTo(map);
      hit.on('mouseover', () => mapHover(i)).on('mouseout', () => mapHover(null))
        .on('click', e => { L.DomEvent.stopPropagation(e); highlight(i); });
      trackHits.push(hit);
      trackDots.push(track.slice(0, -1).map(t =>
        L.marker([t.lat, t.lng], {icon: L.divIcon({className: 'track-dot faint', html: `<span style="--pin:${c}"></span>`, iconSize: [12, 12], iconAnchor: [6, 6]}),
          keyboard: false, interactive: true, pane: 'watch-track'}).addTo(map)
          .on('mouseover', () => mapHover(i)).on('mouseout', () => mapHover(null))
          .on('click', e => { L.DomEvent.stopPropagation(e); highlight(i); })));
    });
    // 선·점·핀의 모양을 고른 배(진하게)와 가리키는 배(중간)에 맞춰 다시 칠한다. 선택은 바꾸지 않는다
    const paint = () => {
      trackLines.forEach((line, i) => line?.setStyle(i === selNow ? {opacity: .95, weight: 4} : i === hovNow ? {opacity: .8, weight: 4} : {opacity: .25, weight: 3}));
      trackDots.forEach((dots, i) => dots.forEach(d => d.getElement()?.classList.toggle('faint', i !== selNow && i !== hovNow)));
      markers.forEach((m, i) => m.getElement()?.classList.toggle('hovered', i === hovNow));
      if (selNow !== null) trackLines[selNow]?.bringToFront();
      else if (hovNow !== null) trackLines[hovNow]?.bringToFront();
    };
    // sel이 null이면 전부 흐리게. 고른 배는 선을 굵고 진하게, 점을 또렷하게. 지도에서 눌렀을 때만 쓴다(바깥에 알림)
    const highlight = (sel: number | null) => { selNow = sel; cb.onSelect(sel); paint(); };
    // 지도에서 가리키면 바깥(옆 목록)이 그 줄로 스크롤하고 강조한다. 목록에서 가리키는 경우는 hoverFn이 지도 쪽만 칠한다
    const mapHover = (i: number | null) => { hovNow = i; cb.onHover(i); paint(); };

    // 배 핀은 지도에 바로 올리지 않고 묶음 레이어에 둔다. 확대 단계마다 가까운 배끼리 클러스터로 다시 묶는다
    const markers = pins.map((p, i) => {
      const icon = L.divIcon({className: 'watch-pin', iconSize: [34, 34], iconAnchor: [17, 17],
        // 지켜보기 버튼 때 넣은 위치(seed)면 회색 채움 + 점선 테두리(seeded). 판정 테두리는 그대로 둔다
        html: `<span style="--pin:${p.pinColor}" class="pin-body ${p.verdict ?? ''} ${p.seeded ? 'seeded' : ''}"><b>${p.letter}</b></span>`});
      return L.marker([p.lat, p.lng], {icon, title: `${p.letter} ${p.name}`, alt: `${p.letter} ${p.name}`, riseOnHover: true})
        .bindTooltip(p.tipHtml, {direction: 'auto', offset: [18, 0], className: 'watch-tooltip', opacity: 1})
        .on('click', e => { L.DomEvent.stopPropagation(e); highlight(i); })
        .on('mouseover', () => mapHover(i)).on('mouseout', () => mapHover(null));
    });
    const layer = L.layerGroup().addTo(map);

    // 목록에서 연 말풍선은 마우스를 떼도 저절로 안 닫힌다. 한 번에 하나만 열어 두고,
    // 다른 배를 고르거나, 다른 마커에 마우스를 올리거나, 지도 빈 곳을 누르면 닫는다 (말풍선이 쌓이지 않게)
    let opened: L.Marker | null = null;
    const closeOpened = () => { opened?.closeTooltip(); opened = null; };
    markers.forEach(m => m.on('mouseover', () => { if (opened && opened !== m) closeOpened(); }));
    map.on('click', () => { closeOpened(); cb.onCluster(null); highlight(null); });

    let groups: number[][] = [];
    const recluster = () => {
      closeOpened();
      layer.clearLayers();
      const pt = pins.map(p => map.latLngToLayerPoint([p.lat, p.lng]));
      const used = new Set<number>();
      groups = [];
      // [왜] 더 확대할 수 없는 단계에서 묶으면 안의 배를 따로 볼 방법이 없다. 그때는 핀이 겹쳐도 하나씩 올린다
      const atMax = map.getZoom() >= map.getMaxZoom();
      pins.forEach((_, i) => {
        if (used.has(i)) return;
        const g = [i]; used.add(i);
        if (!atMax) pins.forEach((__, j) => { if (!used.has(j) && pt[i].distanceTo(pt[j]) < CLUSTER_PX) { g.push(j); used.add(j); } });
        groups.push(g);
      });
      groups.forEach(g => {
        if (g.length === 1) { layer.addLayer(markers[g[0]]); return; }
        const center = L.latLng(g.reduce((a, k) => a + pins[k].lat, 0) / g.length, g.reduce((a, k) => a + pins[k].lng, 0) / g.length);
        // 테두리는 안에서 가장 급한 판정 색. 아래 점은 안에 든 배들의 색(최대 4개)
        const urgent = g.some(k => pins[k].verdict === 'act') ? 'act' : g.some(k => pins[k].verdict === 'watch') ? 'watch' : '';
        const dots = g.slice(0, 4).map(k => `<em style="background:${pins[k].pinColor}"></em>`).join('');
        const names = g.map(k => `${pins[k].letter} ${pins[k].name}`).join(', ');
        L.marker(center, {icon: L.divIcon({className: 'watch-cluster', iconSize: [46, 46], iconAnchor: [23, 23],
          html: `<span class="cluster-body ${urgent}"><b>${g.length}</b><i>${dots}</i></span>`}), title: `배 ${g.length}척: ${names}`, alt: `배 ${g.length}척`, riseOnHover: true, zIndexOffset: 1000})
          .on('click', e => { L.DomEvent.stopPropagation(e); closeOpened(); cb.onCluster(g); })
          // 이름은 한 줄에 한 척, 많으면 앞의 TIP_NAMES척만 보이고 나머지는 "외 N척"으로 줄인다 (전체는 눌러서 여는 상세 창에)
          .bindTooltip(`<div class="watch-tip"><strong>배 ${g.length}척</strong><ul class="tip-names">${g.slice(0, TIP_NAMES).map(k => `<li>${esc(`${pins[k].letter} ${pins[k].name}`)}</li>`).join('')}${g.length > TIP_NAMES ? `<li class="tip-more">외 ${g.length - TIP_NAMES}척</li>` : ''}</ul><span>눌러서 자세히 보기</span></div>`, {direction: 'top', offset: [0, -22], className: 'watch-tooltip', opacity: 1})
          .addTo(layer);
      });
      paint();  // 다시 올린 핀에도 강조를 입힌다
    };
    map.on('zoomend', recluster);
    map.on('zoomend', paint);
    recluster();

    // 목록·상세 창에서 배를 고르면 그 배의 경로 전체(경로가 없으면 현재 위치)로 확대한다.
    // 확대한 뒤에도 다른 배와 묶여 있으면 그 클러스터의 상세 창을, 혼자면 그 배 말풍선을 연다
    focusFn = i => {
      closeOpened(); cb.onCluster(null); highlight(i);
      let finished = false;
      const after = () => {
        if (finished) return; finished = true;
        const g = groups.find(x => x.includes(i));
        // 묶인 채로 끝났으면 상세 창으로 보이되 누른 배를 맨 위에 둔다. 최대 확대에서는 묶지 않으므로 그때는 말풍선이 열린다
        if (g && g.length > 1) cb.onCluster([i, ...g.filter(x => x !== i)]);
        else { markers[i].openTooltip(); opened = markers[i]; }
      };
      map.once('moveend', after);
      setTimeout(after, 700);  // 이미 그 자리라 지도가 안 움직이면 moveend가 오지 않는다
      // 그 배가 다른 배와 떨어져 보이는(클러스터에서 빠지는) 가장 낮은 확대 단계를 찾는다.
      // 경로 전체가 들어오는 단계가 그보다 낮으면 경로를 포기하고 배를 가운데 두고 그 단계로 확대한다.
      // 경로 범위에만 맞추면 클러스터 안의 배를 목록에서 눌러도 그 배로 가지 않는다
      const here = L.latLng(pins[i].lat, pins[i].lng);
      const apartAt = (z: number) => pins.every((o, j) => j === i
        || map.project(here, z).distanceTo(map.project(L.latLng(o.lat, o.lng), z)) >= CLUSTER_PX);
      let apart = map.getMaxZoom();
      for (let z = map.getMinZoom(); z <= map.getMaxZoom(); z++) { if (apartAt(z)) { apart = z; break; } }
      const track = pins[i].track;
      const trackBounds = track.length > 1 ? L.latLngBounds(track.map(t => L.latLng(t.lat, t.lng))).pad(.3) : null;
      const trackZoom = trackBounds ? Math.min(map.getBoundsZoom(trackBounds), 9) : 0;
      if (trackBounds && trackZoom >= apart) map.fitBounds(trackBounds, {maxZoom: 9, animate: true});
      else map.setView(here, Math.max(apart, 8), {animate: true});
    };
    fitAllFn = () => { map.fitBounds(L.latLngBounds([...points, port]).pad(.25), {maxZoom: 8, animate: true}); };
    // 목록에서 가리키거나 고른 배: 지도 선·핀만 칠하고 바깥에는 다시 알리지 않는다
    hoverFn = i => { hovNow = i; paint(); };
    selectFn = i => { selNow = i; paint(); };
    resizeFn = () => { map.invalidateSize(); };

    return () => {
      const c = map.getCenter(); view = {center: [c.lat, c.lng], zoom: map.getZoom()};
      focusFn = () => {}; fitAllFn = () => {}; hoverFn = () => {}; selectFn = () => {}; resizeFn = () => {};
      cb.onHover(null); base.dispose();
      // 확대 애니메이션 중에 지우면 Leaflet 1.9가 250ms 뒤 남은 타이머(_onZoomTransitionEnd)로 지운 지도를 건드려 "_zoom of null" 오류가 난다.
      // 데모는 10초마다 다시 그려 자주 겹친다. 애니메이션을 멈추고 그 타이머가 아무것도 하지 않게 표시를 내린 뒤 지운다
      map.stop(); (map as unknown as {_animatingZoom?: boolean})._animatingZoom = false; map.remove();
    };
  }

  teardown = build();
  return {
    kind: 'leaflet',
    // 데이터가 바뀌면 예전처럼 지도를 지우고 다시 그린다. 보던 위치는 view에 기억해 둔다. 고른 배 표시는 예전처럼 풀린다
    update(next) {
      if (destroyed) return;
      teardown?.(); input = next; cb.onSelect(null); teardown = build();
    },
    focus: i => focusFn(i),
    fitAll: () => fitAllFn(),
    hover: i => hoverFn(i),
    select: i => selectFn(i),
    resize: () => resizeFn(),
    destroy() { if (destroyed) return; destroyed = true; teardown?.(); teardown = null; },
  };
};
