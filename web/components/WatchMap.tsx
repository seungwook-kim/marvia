'use client';
/** 지켜보는 화물 (docs/specs/cargo-watch.md). 내 화물 B/L마다 그 배의 마지막 위치를 한 지도에 서로 다른 마커로 보여 준다.
 * - 위치는 watch_positions(017)의 점이다. 수집 워커는 ShipFinder 한도 때문에 꺼져 있어, 조회할 때(services/cargo._keep_position)와
 *   지켜보기를 켤 때(services/watch.seed_position) 쌓인 점이 나온다. 서버가 없는 체험 모드만 예시 데이터다
 * - 반출이 끝난 화물(status done)은 지도에서 빼고 목록 아래에 개수만 알린다
 * - 마커에 마우스를 올리면 선명, 가린 B/L, 운송 화물 이름표, 판정, 통관 단계, 도착 예정, 위치 수신 시각이 뜬다.
 *   운송 화물 이름은 사용자가 붙인 이름표를 먼저 쓰고, 없으면 UNI-PASS 품명을 쓴다 (SDD 3.5)
 * - 배마다 6시간 간격 위치 이력(track)을 그 배 색의 선으로 잇고, 찍힌 지점마다 작은 동그라미를 단다. 마우스를 올리면 시각과 속력
 * - 배끼리 화면에서 가까우면(CLUSTER_PX 안) 숫자 클러스터로 묶는다. 확대 단계가 바뀔 때마다 다시 묶는다.
 *   클러스터를 누르면 상세 창에 그 안의 배와 말풍선 내용 전부가 뜬다. 새 라이브러리(leaflet.markercluster) 없이 직접 묶는다
 * - B/L 없이 배만 지켜보는 줄(GET /api/watch/vessels, lib/watchVessel.ts)도 같은 지도·목록에 올린다.
 *   B/L이 없으니 판정·통관·여유 칸은 비우고, 등급 없는 회색 점(VESSEL_COLOR)으로 그린다
 *   해제 버튼도 이 목록 줄 옆에 있다
 * - 같은 배(MMSI)를 B/L 화물로도, 배만 지켜보기로도 켜 두면 핀은 화물 하나만 그린다 (10-08 사용자 결정, lib/watchMerge.ts).
 *   두 줄은 같은 위치 점을 읽어 늘 같은 자리에 있어서, 둘 다 그리면 가짜 "2척" 클러스터가 된다. 배만 지켜보기 줄은 목록에
 *   "위 화물과 같은 배"로 남겨 해제 버튼을 누를 수 있게 하고, 화물 말풍선에 배만 지켜보기도 켜져 있다고 적는다
 * - 마지막 점의 위치 출처(position_source, migrations/031)를 말풍선 "위치 출처" 줄로 보인다. 지켜보기 버튼을 누를 때 넣은 위치(seed)는
 *   ShipFinder 캐시라 몇 시간 전일 수 있어 핀·목록 칩을 점선 테두리 회색으로 그린다 (10-08 사용자 결정).
 *   마지막 점 출처로만 정해서 정기 수집 점이 새로 오면 저절로 보통 핀으로 돌아간다. 경로 선·점은 원래 색 그대로다
 * 판정은 하지 않는다. 표시만 한다. */
import {useEffect, useRef, useState, type CSSProperties} from 'react';
import {EyeOff, LocateFixed, Maximize2, X} from 'lucide-react';
import {addBasemap, drawLocalGeography} from '@/lib/basemap';
import {errorStatus, getPresentationData, getWatchList, tour, type WatchItem, type WatchList} from '@/lib/api';
import {TOUR_TICK_MS} from '@/lib/tour';
import {listWatchedVessels, onVesselWatchChange, unwatchVessel, type WatchedVessel} from '@/lib/watchVessel';
import {isSeedSource, positionSourceLabel, sharedVesselMmsis} from '@/lib/watchMerge';
import {dateTime, verdictLabels} from '@/lib/format';
import {VerdictBadge} from '@/components/Shared';

// 화물마다 다른 마커 색. 판정 색(초록·노랑·빨강)과 겹치지 않게 고른다. 색이 모자라면 처음부터 다시 쓰고 글자로 구분한다
const PALETTE = ['#1d4ed8', '#7c3aed', '#0f766e', '#c2410c', '#be185d', '#4d7c0f', '#0369a1', '#a21caf', '#b45309', '#334155'];
const markerLetter = (i: number) => String.fromCharCode(65 + (i % 26));
const colorOf = (i: number) => PALETTE[i % PALETTE.length];
// 배만 지켜보는 줄의 점 색. 판정이 없다는 뜻의 회색이고, 화물 색(PALETTE)과도 판정 색과도 겹치지 않는다
const VESSEL_COLOR = '#9ca3af';
// 화면에서 이 거리(px) 안에 있는 배끼리 한 클러스터로 묶는다. 핀 폭(34px)보다 조금 넓게
const CLUSTER_PX = 44;
const TIP_NAMES = 5;  // 클러스터 말풍선에 이름을 보일 최대 척 수
/** 지도·목록 한 줄. B/L 화물(WatchItem)이거나, 같은 모양으로 바꾼 배만 지켜보는 줄(vesselOnly, mmsi)이다.
 * alsoVesselOnly: 이 화물의 배를 배만 지켜보기로도 켜 뒀다. sameShip: 진행 중 화물과 같은 배라 핀을 그리지 않는 배만 지켜보기 줄 */
type MapItem = WatchItem & {vesselOnly?: boolean; mmsi?: number; dest?: string | null; eta_text?: string | null;
  alsoVesselOnly?: boolean; sameShip?: boolean};
/** 위치가 있어 지도에 올릴 수 있는 줄 */
type Located = MapItem & {lat: number; lng: number};

/** 배만 지켜보는 줄을 지도 줄 모양으로. B/L이 없으니 판정·통관·여유·가린 B/L은 비운다.
 * eta는 사용자가 채팅에서 본 글 그대로라(services/vessel_watch.py) 날짜가 아닐 수 있어 eta_text에 따로 둔다 */
function fromVessel(v: WatchedVessel): MapItem {
  return {bl_masked: '', label: null, ship_name: v.name, status: 'active', verdict: null, stage: null, eta: null, days_margin: null,
    lat: v.lat, lng: v.lng, cog: v.cog, sog: v.sog, position_at: v.position_at, position_source: v.position_source ?? null, track: v.track, ship_flag: v.flag,
    vesselOnly: true, mmsi: v.mmsi, dest: v.dest, eta_text: v.eta};
}
/** 목록 key. 배만 지켜보는 줄은 가린 B/L이 비어 있어 서로 겹친다. MMSI로 가른다.
 * 화물은 지문(cargo_key)을 먼저 쓴다. 가린 B/L은 서로 다른 B/L이 같은 값일 수 있어, 지문이 없으면(체험 예시) 줄 번호를 붙여 겹치지 않게 한다 (10-08 QA G11) */
const keyOf = (item: MapItem, i: number) => item.vesselOnly ? `vessel-${item.mmsi}` : item.cargo_key ? `cargo-${item.cargo_key}` : `cargo-${item.bl_masked}-${i}`;
/** 점·칩 색. 배만 지켜보는 줄은 회색, 화물은 순서대로 PALETTE */
const pinColor = (item: MapItem, i: number) => item.vesselOnly ? VESSEL_COLOR : colorOf(i);
/** 핀·칩 칠하기. 마지막 점이 지켜보기 버튼 때 넣은 위치(seed)면 회색 채움 + 점선 테두리(seeded), 아니면 pinColor 그대로.
 * 경로 선·점은 이 값이 아니라 pinColor를 쓴다 (경로는 출처와 상관없이 그 배 색) */
const markColor = (item: MapItem, i: number) => isSeedSource(item.position_source) ? VESSEL_COLOR : pinColor(item, i);
const seededClass = (item: MapItem) => isSeedSource(item.position_source) ? 'seeded' : '';
/** 보인 글 그대로의 도착 예정. 날짜로 읽히면 화면 형식으로, 아니면 글 그대로 */
const etaText = (v: string | null | undefined) => !v ? '확인 안 됨' : Number.isNaN(new Date(v).valueOf()) ? v : dateTime(v, true);

/** 자동 확인 안내 한 줄 (10-08 QA E1-1·E1-9, services/watch.watch_list의 auto_paused·known_vessel).
 * 멈춤이 먼저다: 잇달아 실패해 멈췄으면 다시 조회해야 풀린다. 배를 모르면 워커가 아예 다시 판정하지 않는다.
 * 옛 서버 응답·체험 예시에는 칸이 없다(undefined). 그때는 아무것도 보이지 않는다 */
function autoNotice(item: MapItem): string | null {
  if (item.vesselOnly || item.status !== 'active') return null;
  if (item.auto_paused) return '자동 확인이 멈췄어요. 다시 조회하면 다시 시작해요.';
  if (item.known_vessel === false) return '배가 정해지지 않아 자동 확인을 하지 않아요. 조회에서 배를 고르면 시작해요.';
  return null;
}

/** "위치 출처" 줄. 출처를 모르면(옛 줄·체험 예시) 줄을 뺀다 (lib/watchMerge.ts positionSourceLabel) */
function sourceRow(item: MapItem): [string, string][] {
  const label = positionSourceLabel(item.position_source);
  return label ? [['위치 출처', label]] : [];
}
/** 말풍선과 클러스터 상세 창이 같이 쓰는 항목 */
function rowsOf(item: MapItem): [string, string][] {
  if (item.vesselOnly) return [
    ['구분', '배만 지켜보는 중 (B/L 없음, 판정 없음)'],
    ['국적', item.ship_flag ?? '-'],
    ['목적지', item.dest ?? '-'],
    ['도착 예정', etaText(item.eta_text)],
    ['위치 받은 시각', dateTime(item.position_at, true)],
    ...sourceRow(item),
  ];
  const notice = autoNotice(item);
  return [
    ['화물 이름', item.label ?? item.goods_name ?? '이름 없음'],  // 이름표가 없으면 UNI-PASS 품명
    ['B/L', item.bl_masked],
    ['국적', item.ship_flag ?? '-'],  // 값이 없으면 대시
    ['통관 단계', item.stage ?? '확인 안 됨'],
    ['도착 예정', item.eta ? dateTime(item.eta, true) : '확인 안 됨'],
    ['위치 받은 시각', dateTime(item.position_at, true)],
    ...sourceRow(item),
    ...(item.alsoVesselOnly ? [['배만 지켜보기', '배만 지켜보기도 켜져 있어요'] as [string, string]] : []),
    ...(notice ? [['자동 확인', notice] as [string, string]] : []),
  ];
}
function motion(item: MapItem) {
  return item.sog != null ? `${item.sog}노트${item.cog != null ? ` · ${Math.round(item.cog)}°` : ''}` : null;
}
/** Leaflet 말풍선은 React가 아니라 글자를 직접 이스케이프한다 */
function esc(v: string) { return v.replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c] as string)); }
function tip(item: MapItem, letter: string) {
  const move = motion(item);
  // 말풍선 머리에 배 이름과 판정 배지. 판정이 없으면 배지를 그리지 않는다
  const badge = item.verdict ? `<span class="verdict-badge ${item.verdict}">${esc(verdictLabels[item.verdict])}</span>` : '';
  return `<div class="watch-tip"><header><strong>${esc(letter)} · ${esc(item.ship_name)}</strong>${badge}</header>${move ? `<span>${esc(move)}</span>` : ''}`
    + `<dl>${rowsOf(item).map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join('')}</dl></div>`;
}

export function WatchMap() {
  const [list, setList] = useState<WatchList | null>(null);
  const [showTrack, setShowTrack] = useState(true);  // 경로 보기 켜고 끄기
  const [cluster, setCluster] = useState<number[] | null>(null);  // 누른 클러스터 안의 배 번호들. 상세 창에 보인다
  const [selected, setSelected] = useState<number | null>(null);  // 지금 고른 배. 목록 카드를 강조한다 (보기 전용 상태)
  const [hovered, setHovered] = useState<number | null>(null);  // 마우스·키보드가 가리키는 배. 선택과 별개인 임시 강조 (지도 ↔ 목록 양방향)
  const host = useRef<HTMLDivElement>(null);
  const listEl = useRef<HTMLOListElement>(null);
  const hover = useRef<(i: number | null, fromMap?: boolean) => void>(() => {});
  const focus = useRef<(i: number) => void>(() => {});
  const fitAll = useRef<() => void>(() => {});
  /** 마지막으로 보던 지도 중심·확대. 지도를 다시 그려도 화면이 튀지 않게 한다 */
  const view = useRef<{center: [number, number]; zoom: number} | null>(null);
  // 배만 지켜보는 줄. 못 읽으면 빈 목록으로 두고 화물 지도는 그대로 그린다. 체험 모드는 늘 빈 목록이다
  const [vessels, setVessels] = useState<WatchedVessel[]>([]);
  // 배만 지켜보는 줄의 해제. 해제 버튼은 이 목록 줄 옆에 있다
  const [unwatching, setUnwatching] = useState<number | null>(null);
  const [unwatchError, setUnwatchError] = useState('');
  async function unwatch(mmsi: number) {
    setUnwatching(mmsi); setUnwatchError('');
    try { await unwatchVessel(mmsi); }  // 성공하면 onVesselWatchChange가 목록을 다시 읽는다
    catch (e) { setUnwatchError(e instanceof Error ? e.message : '지켜보기를 해제하지 못했어요.'); }
    finally { setUnwatching(null); }
  }
  /** 배만 지켜보는 줄 옆 해제 버튼. 줄 전체가 버튼이라 안에 넣지 못하고 옆에 둔다 */
  const unwatchButton = (item: MapItem) => item.vesselOnly && item.mmsi ?
    <button type="button" className="wm-unwatch icon-button" onClick={() => void unwatch(item.mmsi!)} disabled={unwatching === item.mmsi}
      aria-label={`${item.ship_name} 지켜보기 해제`} title="지켜보기 해제"><EyeOff size={16}/></button> : null;
  // 목록을 못 읽은 이유. 이게 있으면 뼈대 화면 대신 안내와 다시 시도 버튼을 보인다. 401이면 로그인이 풀린 것이다 (10-08 QA G1)
  const [loadError, setLoadError] = useState<{message: string; auth: boolean} | null>(null);
  const [attempt, setAttempt] = useState(0);  // "다시 불러오기"를 누를 때마다 올려 아래 읽기를 다시 돈다
  useEffect(() => {
    let alive = true; setLoadError(null);
    // 실패를 잡지 않으면 뼈대 화면이 끝없이 돌고 브라우저에 unhandledRejection이 남는다
    getWatchList().then(l => { if (alive) setList(l); })
      .catch(e => { if (alive) setLoadError({message: e instanceof Error ? e.message : '지켜보는 화물을 불러오지 못했어요.', auth: errorStatus(e) === 401}); });
    return () => { alive = false; };
  }, [attempt]);
  // 데모 둘러보기: 배 위치가 10초마다 움직인다 (lib/tour.ts). 실제 모드는 들어올 때 한 번 읽는다
  useEffect(() => {
    if (!tour) return;
    // 화물 목록과 함께, 데모에서 지켜보기로 추가한 배의 가상 위치도 다시 읽는다. 한 번 실패해도 보던 지도는 그대로 둔다
    const id = setInterval(() => {
      getWatchList().then(setList).catch(() => { /* 다음 갱신에서 다시 읽는다 */ });
      listWatchedVessels().then(setVessels).catch(() => {});
    }, TOUR_TICK_MS);
    return () => clearInterval(id);
  }, []);
  useEffect(() => {
    let alive = true;
    const load = () => listWatchedVessels().then(l => { if (alive) setVessels(l); }).catch(() => { /* 화물 지도만 보인다 */ });
    void load();
    const off = onVesselWatchChange(() => void load());  // 채팅·아래 목록에서 켜고 끄면 다시 읽는다
    return () => { alive = false; off(); };
  }, []);

  // 지도에는 위치가 있는 줄만 올린다. 위치 수집 전 줄은 아래에 이름만 보여 준다 (GET /api/watch). B/L 화물 뒤에 배만 지켜보는 줄.
  // 진행 중 화물과 같은 배인 배만 지켜보기 줄(sameShip)은 핀·경로를 그리지 않고 목록에만 남긴다 (10-08 사용자 결정)
  const shared = sharedVesselMmsis(list?.items ?? [], vessels);
  const items: MapItem[] = [
    ...(list?.items ?? []).map(c => c.status === 'active' && c.vessel_mmsi != null && shared.has(c.vessel_mmsi) ? {...c, alsoVesselOnly: true} : c),
    ...vessels.map(fromVessel).map(v => shared.has(v.mmsi!) ? {...v, sameShip: true} : v),
  ];
  const active = items.filter((i): i is Located => !i.sameShip && i.status === 'active' && i.lat != null && i.lng != null);
  const waiting = items.filter(i => !i.sameShip && i.status === 'active' && (i.lat == null || i.lng == null));
  // 접힌 줄. 위치가 있으면 같은 배 화물 핀(pinOf)을 가리키는 목록 줄로, 없으면 "아직 위치를 받지 못했어요"에 같이 둔다
  const folded = items.filter(i => i.sameShip);
  const pinOf = (item: MapItem) => active.findIndex(a => !a.vesselOnly && a.vessel_mmsi === item.mmsi);
  const foldedOnMap = folded.filter(f => pinOf(f) >= 0);
  const foldedWaiting = folded.filter(f => pinOf(f) < 0);
  const seededShown = active.some(i => isSeedSource(i.position_source));
  const done = list?.items.filter(i => i.status === 'done').length ?? 0;

  useEffect(() => {
    if (!active.length) return;
    let disposed = false;
    setSelected(null);
    let cleanup: (() => void) | undefined;
    void import('leaflet').then(L => {
      if (disposed || !host.current) return;
      const {demo} = getPresentationData();
      const css = getComputedStyle(document.documentElement);
      const color = (n: string) => css.getPropertyValue(n).trim();
      const map = L.map(host.current, {zoomControl: true, scrollWheelZoom: true, minZoom: 4, maxZoom: 10});
      const points = active.flatMap(i => [L.latLng(i.lat, i.lng), ...(showTrack ? (i.track ?? []).map(t => L.latLng(t.lat, t.lng)) : [])]);
      const port = L.latLng(demo.port.lat, demo.port.lng);
      // 중심을 먼저 정한 뒤 선을 올린다 (VesselMap과 같은 이유: 범위를 모르면 Leaflet이 멈춘다)
      map.fitBounds(L.latLngBounds([...points, port]).pad(.25), {maxZoom: 8, animate: false});
      // 다시 그릴 때(데모의 10초 갱신, 경로 보기 켜고 끄기)는 보던 확대·위치를 그대로 둔다
      if (view.current) map.setView(view.current.center, view.current.zoom, {animate: false});
      // 바탕은 세계 지도, 못 쓰면 해안선 (lib/basemap.ts). 데모는 10초마다 다시 그려 깜빡이지 않게 레포의 해안선 바탕을 바로 쓴다
      const base = tour ? (drawLocalGeography(L, map), {dispose() {}}) : addBasemap(L, map, () => { drawLocalGeography(L, map); });
      map.attributionControl.setPrefix(false);
      // 부산항 마커는 배 핀보다 아래층에 두고, 이름표(툴팁 층)는 늘 핀 위에 떠서 배가 같은 자리에 있어도 가려지지 않는다
      L.marker(port, {icon: L.divIcon({className: 'port-marker', html: '<span>⚑</span>', iconSize: [28, 28], iconAnchor: [14, 14]}), interactive: false, zIndexOffset: -1000})
        .addTo(map).bindTooltip('부산항', {permanent: true, direction: 'bottom', offset: [0, 12], className: 'port-tooltip'});

      // 경로 동그라미는 배 핀·클러스터보다 아래 층에 둔다. 같은 층이면 위도 순으로 겹쳐 클러스터를 가려 누를 수 없다
      map.createPane('watch-track').style.zIndex = '550';  // overlay(선) 400 < 경로 점 550 < 마커 600
      // 경로: 지난 위치를 오래된 순으로 선으로 잇고, 현재 위치를 뺀 지점마다 작은 동그라미 (마지막 점은 큰 핀).
      // 평소에는 모든 배 경로를 흐리게 깔고, 고른 배(핀·목록·클러스터 상세에서 누름)의 경로만 진하게 보인다.
      // 경로 점에는 말풍선을 달지 않는다
      const trackLines: (L.Polyline | null)[] = [];
      const trackHits: (L.Polyline | null)[] = [];  // 보이지 않는 굵은 선. 가는 선에 마우스를 맞추기 쉽게 한다
      const trackDots: L.Marker[][] = [];
      let selNow: number | null = null;  // 지금 고른 배 (state와 같은 값, 지도 쪽에서 바로 쓴다)
      let hovNow: number | null = null;  // 지금 가리키는 배
      active.forEach((item, i) => {
        const track = showTrack ? (item.track ?? []) : [];
        if (track.length < 2) { trackLines.push(null); trackHits.push(null); trackDots.push([]); return; }
        const c = pinColor(item, i);
        const pts = track.map(t => [t.lat, t.lng] as L.LatLngTuple);
        trackLines.push(L.polyline(pts, {color: c, weight: 3, opacity: .25, interactive: false}).addTo(map));
        const hit = L.polyline(pts, {color: c, weight: 16, opacity: 0, className: 'track-hit'}).addTo(map);
        hit.on('mouseover', () => hover.current(i, true)).on('mouseout', () => hover.current(null, true))
          .on('click', e => { L.DomEvent.stopPropagation(e); highlight(i); });
        trackHits.push(hit);
        trackDots.push(track.slice(0, -1).map(t =>
          L.marker([t.lat, t.lng], {icon: L.divIcon({className: 'track-dot faint', html: `<span style="--pin:${c}"></span>`, iconSize: [12, 12], iconAnchor: [6, 6]}),
            keyboard: false, interactive: true, pane: 'watch-track'}).addTo(map)
            .on('mouseover', () => hover.current(i, true)).on('mouseout', () => hover.current(null, true))
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
      // i가 null이면 전부 흐리게. 고른 배는 선을 굵고 진하게, 점을 또렷하게
      const highlight = (sel: number | null) => { selNow = sel; setSelected(sel); paint(); };
      // 지도에서 가리키면(fromMap) 옆 목록이 그 줄로 스크롤하고, 목록에서 가리키면 지도 선·핀이 강조된다
      hover.current = (i, fromMap) => {
        hovNow = i; setHovered(i); paint();
        const row = i !== null && fromMap ? listEl.current?.children[i] as HTMLElement | undefined : undefined;
        const box = listEl.current;
        if (row && box && box.scrollHeight > box.clientHeight + 1) {
          const r = row.getBoundingClientRect(), b = box.getBoundingClientRect();
          if (r.top < b.top || r.bottom > b.bottom) {
            const calm = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
            box.scrollTo({top: box.scrollTop + (r.top < b.top ? r.top - b.top - 8 : r.bottom - b.bottom + 8), behavior: calm ? 'auto' : 'smooth'});
          }
        }
      };

      // 배 핀은 지도에 바로 올리지 않고 묶음 레이어에 둔다. 확대 단계마다 가까운 배끼리 클러스터로 다시 묶는다
      const markers = active.map((item, i) => {
        const letter = markerLetter(i);
        const icon = L.divIcon({className: 'watch-pin', iconSize: [34, 34], iconAnchor: [17, 17],
          // 지켜보기 버튼 때 넣은 위치(seed)면 회색 채움 + 점선 테두리(seeded, 10-08 사용자 결정). 판정 테두리는 그대로 둔다
          html: `<span style="--pin:${markColor(item, i)}" class="pin-body ${item.verdict ?? ''} ${seededClass(item)}"><b>${letter}</b></span>`});
        return L.marker([item.lat, item.lng], {icon, title: `${letter} ${item.ship_name}`, alt: `${letter} ${item.ship_name}`, riseOnHover: true})
          .bindTooltip(tip(item, letter), {direction: 'auto', offset: [18, 0], className: 'watch-tooltip', opacity: 1})
          .on('click', e => { L.DomEvent.stopPropagation(e); highlight(i); })
          .on('mouseover', () => hover.current(i, true)).on('mouseout', () => hover.current(null, true));
      });
      const layer = L.layerGroup().addTo(map);

      // 목록에서 연 말풍선은 마우스를 떼도 저절로 안 닫힌다. 한 번에 하나만 열어 두고,
      // 다른 배를 고르거나, 다른 마커에 마우스를 올리거나, 지도 빈 곳을 누르면 닫는다 (말풍선이 쌓이지 않게)
      let opened: L.Marker | null = null;
      const closeOpened = () => { opened?.closeTooltip(); opened = null; };
      markers.forEach(m => m.on('mouseover', () => { if (opened && opened !== m) closeOpened(); }));
      map.on('click', () => { closeOpened(); setCluster(null); highlight(null); });

      let groups: number[][] = [];
      const recluster = () => {
        closeOpened();
        layer.clearLayers();
        const pts = active.map(it => map.latLngToLayerPoint([it.lat, it.lng]));
        const used = new Set<number>();
        groups = [];
        active.forEach((_, i) => {
          if (used.has(i)) return;
          const g = [i]; used.add(i);
          active.forEach((__, j) => { if (!used.has(j) && pts[i].distanceTo(pts[j]) < CLUSTER_PX) { g.push(j); used.add(j); } });
          groups.push(g);
        });
        groups.forEach(g => {
          if (g.length === 1) { layer.addLayer(markers[g[0]]); return; }
          const center = L.latLng(g.reduce((a, k) => a + active[k].lat, 0) / g.length, g.reduce((a, k) => a + active[k].lng, 0) / g.length);
          // 테두리는 안에서 가장 급한 판정 색. 아래 점은 안에 든 배들의 색(최대 4개)
          const urgent = g.some(k => active[k].verdict === 'act') ? 'act' : g.some(k => active[k].verdict === 'watch') ? 'watch' : '';
          const dots = g.slice(0, 4).map(k => `<em style="background:${markColor(active[k], k)}"></em>`).join('');
          const names = g.map(k => `${markerLetter(k)} ${active[k].ship_name}`).join(', ');
          L.marker(center, {icon: L.divIcon({className: 'watch-cluster', iconSize: [46, 46], iconAnchor: [23, 23],
            html: `<span class="cluster-body ${urgent}"><b>${g.length}</b><i>${dots}</i></span>`}), title: `배 ${g.length}척: ${names}`, alt: `배 ${g.length}척`, riseOnHover: true, zIndexOffset: 1000})
            .on('click', e => { L.DomEvent.stopPropagation(e); closeOpened(); setCluster(g); })
            // 이름은 한 줄에 한 척, 많으면 앞의 TIP_NAMES척만 보이고 나머지는 "외 N척"으로 줄인다 (전체는 눌러서 여는 상세 창에)
            .bindTooltip(`<div class="watch-tip"><strong>배 ${g.length}척</strong><ul class="tip-names">${g.slice(0, TIP_NAMES).map(k => `<li>${esc(`${markerLetter(k)} ${active[k].ship_name}`)}</li>`).join('')}${g.length > TIP_NAMES ? `<li class="tip-more">외 ${g.length - TIP_NAMES}척</li>` : ''}</ul><span>눌러서 자세히 보기</span></div>`, {direction: 'top', offset: [0, -22], className: 'watch-tooltip', opacity: 1})
            .addTo(layer);
        });
        paint();  // 다시 올린 핀에도 강조를 입힌다
      };
      map.on('zoomend', recluster);
      map.on('zoomend', paint);
      recluster();

      // 목록·상세 창에서 배를 고르면 그 배의 경로 전체(경로가 없으면 현재 위치)로 확대한다.
      // 확대한 뒤에도 다른 배와 묶여 있으면 그 클러스터의 상세 창을, 혼자면 그 배 말풍선을 연다
      focus.current = i => {
        closeOpened(); setCluster(null); highlight(i);
        let finished = false;
        const after = () => {
          if (finished) return; finished = true;
          const g = groups.find(x => x.includes(i));
          // 최대로 확대해도 붙어 있는 배(같은 부두 등)는 상세 창으로 보이되, 누른 배를 맨 위에 둔다
          if (g && g.length > 1) setCluster([i, ...g.filter(x => x !== i)]);
          else { markers[i].openTooltip(); opened = markers[i]; }
        };
        map.once('moveend', after);
        setTimeout(after, 700);  // 이미 그 자리라 지도가 안 움직이면 moveend가 오지 않는다
        // 그 배가 다른 배와 떨어져 보이는(클러스터에서 빠지는) 가장 낮은 확대 단계를 찾는다.
        // 경로 전체가 들어오는 단계가 그보다 낮으면 경로를 포기하고 배를 가운데 두고 그 단계로 확대한다.
        // 경로 범위에만 맞추면 클러스터 안의 배를 목록에서 눌러도 그 배로 가지 않는다
        const here = L.latLng(active[i].lat, active[i].lng);
        const apartAt = (z: number) => active.every((o, j) => j === i
          || map.project(here, z).distanceTo(map.project(L.latLng(o.lat, o.lng), z)) >= CLUSTER_PX);
        let apart = map.getMaxZoom();
        for (let z = map.getMinZoom(); z <= map.getMaxZoom(); z++) { if (apartAt(z)) { apart = z; break; } }
        const track = showTrack ? (active[i].track ?? []) : [];
        const trackBounds = track.length > 1 ? L.latLngBounds(track.map(t => L.latLng(t.lat, t.lng))).pad(.3) : null;
        const trackZoom = trackBounds ? Math.min(map.getBoundsZoom(trackBounds), 9) : 0;
        if (trackBounds && trackZoom >= apart) map.fitBounds(trackBounds, {maxZoom: 9, animate: true});
        else map.setView(here, Math.max(apart, 8), {animate: true});
      };
      fitAll.current = () => { setCluster(null); map.fitBounds(L.latLngBounds([...points, port]).pad(.25), {maxZoom: 8, animate: true}); };

      const ro = new ResizeObserver(() => map.invalidateSize());
      ro.observe(host.current);
      cleanup = () => { const c = map.getCenter(); view.current = {center: [c.lat, c.lng], zoom: map.getZoom()}; hover.current = () => {}; setHovered(null); ro.disconnect(); base.dispose();
        // 확대 애니메이션 중에 지우면 Leaflet 1.9가 250ms 뒤 남은 타이머(_onZoomTransitionEnd)로 지운 지도를 건드려 "_zoom of null" 오류가 난다.
        // 데모는 10초마다 다시 그려 자주 겹친다. 애니메이션을 멈추고 그 타이머가 아무것도 하지 않게 표시를 내린 뒤 지운다
        map.stop(); (map as unknown as {_animatingZoom?: boolean})._animatingZoom = false; map.remove(); };
    });
    return () => { disposed = true; cleanup?.(); };
  }, [list, vessels, showTrack]);  // eslint-disable-line react-hooks/exhaustive-deps

  const total = active.length + waiting.length;
  // 왼쪽 지도 카드(위에 기준 시각·모든 배 보기·경로 체크), 오른쪽 "내 화물" 목록 카드. 휴대폰은 지도 → 목록 순으로 쌓는다
  return <section className="watch wm" aria-labelledby="watch-title">
    <div className="wm-head"><h2 id="watch-title">지켜보는 화물 지도</h2></div>
    {!list && loadError ? <div className="watch-empty" role="alert">
        <p>{loadError.auth ? '로그인이 풀려 지켜보는 화물을 불러오지 못했어요. 다시 로그인한 뒤 열어 주세요.' : loadError.message}</p>
        {!loadError.auth && <button type="button" className="button secondary" onClick={() => setAttempt(n => n + 1)}>다시 불러오기</button>}
      </div>
      : !list ? <div className="wm-skeleton" role="status" aria-label="지켜보는 화물을 불러오고 있어요."><div className="wm-sk-map"/><div className="wm-sk-list"><i/><i/><i/><i/></div></div>
      : !total ? <div className="watch-empty">지켜보는 화물이 없어요. B/L 조회 결과나 선박 옆의 지켜보기 버튼을 누르면 여기에 나와요.</div>
      : <div className="wm-body">
        {active.length > 0 && <div className="wm-map-card">
          <div className="wm-toolbar">
            <span className="watch-updated">{list.updated_at ? `${dateTime(list.updated_at, true)} 기준` : ''}</span>
            <button type="button" className="watch-fit" onClick={() => fitAll.current()} title="모든 배 보기" aria-label="모든 배 보기"><Maximize2 size={16} strokeWidth={1.9}/>모든 배 보기</button>
            <label className="watch-track-toggle"><input type="checkbox" checked={showTrack} onChange={e => setShowTrack(e.target.checked)}/>6시간 간격 경로 보기</label>
          </div>
          {seededShown && <p className="wm-legend"><span className="watch-chip seeded wm-legend-pin" aria-hidden="true"/>점선 테두리 회색 핀: 지켜보기 버튼을 누를 때 넣은 위치예요(정기 수집 전, 몇 시간 전일 수 있음)</p>}
          <div className="watch-map">
            <div ref={host} className="leaflet-host"/>
            {cluster && <div className="cluster-panel" role="dialog" aria-label={`이 자리에 있는 배 ${cluster.length}척`} onKeyDown={e => { if (e.key === 'Escape') setCluster(null); }}>
              <div className="cluster-head"><strong>이 자리에 있는 배 {cluster.length}척</strong>
                <button type="button" className="icon-button" onClick={() => setCluster(null)} aria-label="닫기" autoFocus><X size={16}/></button></div>
              <ul>{cluster.map(k => { const item = active[k]; const move = motion(item); return <li key={keyOf(item, k)}>
                <div className="cluster-item-head"><span className={`watch-chip ${seededClass(item)}`.trim()} style={{background: markColor(item, k)}}>{markerLetter(k)}</span>
                  <span className="watch-main"><strong>{item.ship_name}</strong>{move && <small>{move}</small>}</span>
                  <button type="button" className="cluster-go" onClick={() => focus.current(k)} aria-label={`${item.ship_name} 위치로 지도 이동`} title="지도에서 보기"><LocateFixed size={18} strokeWidth={1.9} aria-hidden="true"/></button></div>
                <dl>{rowsOf(item).map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
              </li>; })}</ul>
            </div>}
          </div>
        </div>}
        <aside className="wm-side">
          <div className="wm-side-head"><h3>내 화물</h3>
            <span className="wm-count">진행 중 {active.length + waiting.length}건</span></div>
          <ol ref={listEl} className="watch-list" aria-label="지켜보는 화물">
            {active.map((item, i) => <li key={keyOf(item, i)} style={{'--i': Math.min(i, 5)} as CSSProperties}>
              <button type="button" className={`${selected === i ? 'selected' : ''} ${hovered === i ? 'hovered' : ''}`.trim()}
                onMouseEnter={() => hover.current(i)} onMouseLeave={() => hover.current(null)} onFocus={() => hover.current(i)} onBlur={() => hover.current(null)} aria-pressed={selected === i} onClick={() => focus.current(i)} aria-label={`${markerLetter(i)} ${item.ship_name} 지도에서 보기`}>
                <span className={`watch-chip ${seededClass(item)}`.trim()} style={{background: markColor(item, i)}}>{markerLetter(i)}</span>
                {item.vesselOnly ? <span className="watch-main">
                  <strong className="watch-goods">{item.ship_name}</strong>
                  <span className="watch-ship">배만 지켜보는 중{item.ship_flag ? ` · ${item.ship_flag}` : ''}</span>
                  <span className="watch-stage">도착 예정 {etaText(item.eta_text)}</span>
                  {selected === i && <span className="sr-only">지도에서 선택됨</span>}
                </span> : <span className="watch-main">
                  <strong className="watch-goods">{item.label ?? item.goods_name ?? '이름 없음'}</strong>
                  <span className="watch-ship">{item.ship_name}{item.ship_flag ? ` · ${item.ship_flag}` : ''}</span>
                  <span className="watch-stage">통관 {item.stage ?? '확인 안 됨'}</span>
                  {autoNotice(item) && <span className="watch-stage">{autoNotice(item)}</span>}
                  {selected === i && <span className="sr-only">지도에서 선택됨</span>}
                </span>}
                {/* 배만 지켜보는 줄은 판정·B/L이 없어 배지 자리를 비운다. 그 자리에 해제 버튼이 겹쳐 온다(wm-unwatch) */}
                {item.vesselOnly ? <span className="watch-side"/> : <span className="watch-side"><VerdictBadge verdict={item.verdict}/><small className="watch-bl">{item.bl_masked}</small></span>}
              </button>
              {unwatchButton(item)}
            </li>)}
            {/* 화물과 같은 배인 배만 지켜보기 줄. 핀은 화물 것 하나라 누르면 그 핀으로 간다. 해제 버튼은 그대로 둔다 */}
            {foldedOnMap.map(item => { const k = pinOf(item); return <li key={keyOf(item, k)} className="wm-same-ship">
              <button type="button" onMouseEnter={() => hover.current(k)} onMouseLeave={() => hover.current(null)} onFocus={() => hover.current(k)} onBlur={() => hover.current(null)}
                onClick={() => focus.current(k)} aria-label={`${item.ship_name} 배만 지켜보기, ${markerLetter(k)} 화물과 같은 배. 지도에서 보기`}>
                <span className="watch-chip" style={{background: VESSEL_COLOR}}>{markerLetter(k)}</span>
                <span className="watch-main">
                  <strong className="watch-goods">{item.ship_name}</strong>
                  <span className="watch-ship">배만 지켜보는 중</span>
                  <span className="watch-stage">위 화물과 같은 배 ({markerLetter(k)} 핀)</span>
                </span>
                <span className="watch-side"/>
              </button>
              {unwatchButton(item)}
            </li>; })}
          </ol>
          <p role="alert" className="request-error wm-unwatch-error">{unwatchError}</p>
          {waiting.length + foldedWaiting.length > 0 && <div className="wm-waiting"><strong>아직 위치를 받지 못했어요</strong>
            <ul>{[...waiting, ...foldedWaiting].map((i, n) => <li key={keyOf(i, n)}className={i.vesselOnly ? 'wm-waiting-vessel' : undefined}><span>{i.label ?? i.goods_name ?? i.ship_name}</span><small>{i.vesselOnly ? `${i.ship_name} · 배만 지켜보는 중` : `${i.ship_name} · ${i.bl_masked}`}</small>{i.sameShip && <small>위 화물과 같은 배</small>}{i.alsoVesselOnly && <small>배만 지켜보기도 켜져 있어요</small>}{autoNotice(i) && <small>{autoNotice(i)}</small>}{unwatchButton(i)}</li>)}</ul>
            <p>다음 조회나 위치 확인 뒤 지도에 나와요.</p></div>}
          {done > 0 && <p className="watch-done">반출이 끝난 화물 {done}건은 지도에서 뺐어요.</p>}
        </aside>
      </div>}
  </section>;
}
