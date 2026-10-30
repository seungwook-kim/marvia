'use client';
/** 지켜보는 화물 (docs/specs/cargo-watch.md). 내 화물 B/L마다 그 배의 마지막 위치를 한 지도에 서로 다른 마커로 보여 준다.
 * - 위치는 watch_positions(017)의 점이다. 수집 워커는 ShipFinder 한도 때문에 꺼져 있어, 조회할 때(services/cargo._keep_position)와
 *   지켜보기를 켤 때(services/watch.seed_position) 쌓인 점이 나온다. 서버가 없는 체험 모드만 예시 데이터다
 * - 반출이 끝난 화물(status done)은 지도에서 빼고 목록 아래에 개수만 알린다
 * - 마커에 마우스를 올리면 선명, 가린 B/L, 운송 화물 이름표, 판정, 통관 단계, 도착 예정, 위치 수신 시각이 뜬다.
 *   운송 화물 이름은 사용자가 붙인 이름표를 먼저 쓰고, 없으면 UNI-PASS 품명을 쓴다 (SDD 3.5)
 * - 배마다 6시간 간격 위치 이력(track)을 그 배 색의 선으로 잇고, 찍힌 지점마다 작은 동그라미를 단다. 마우스를 올리면 시각과 속력
 * - 배끼리 화면에서 가까우면 숫자 클러스터로 묶고, 최대 확대에서는 묶지 않는다(규칙은 watch/WatchLeaflet.ts, watch/WatchGlobe.ts).
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
 * - 지도 그리기는 components/watch/ 의 도구(WatchRenderer)가 맡는다. WebGL이 되면 Cesium 위성 지구본, 안 되면 Leaflet 평면 (docs/specs/watch-globe.md).
 *   이 파일은 데이터 읽기, 목록, 클러스터 상세 창, 단추만 맡고 어느 도구인지 모른다. 데모 둘러보기·체험 모드도 같은 길로 지구본을 쓴다
 * 판정은 하지 않는다. 표시만 한다. */
import {useEffect, useMemo, useRef, useState, type CSSProperties} from 'react';
import {EyeOff, LocateFixed, Maximize2, X} from 'lucide-react';
import {errorStatus, getPresentationData, getWatchList, tour, type WatchItem, type WatchList} from '@/lib/api';
import {TOUR_TICK_MS} from '@/lib/tour';
import {listWatchedVessels, onVesselWatchChange, unwatchVessel, type WatchedVessel} from '@/lib/watchVessel';
import {isSeedSource, positionSourceLabel, sharedVesselMmsis} from '@/lib/watchMerge';
import {dateTime, verdictLabels} from '@/lib/format';
import {VerdictBadge} from '@/components/Shared';
import {createWatchRenderer, supportsWebGL} from '@/components/watch';
import type {BaseKind, RenderInput, WatchMapCallbacks, WatchRenderer} from '@/components/watch/types';

// 화물마다 다른 마커 색. 판정 색(초록·노랑·빨강)과 겹치지 않게 고른다. 색이 모자라면 처음부터 다시 쓰고 글자로 구분한다
const PALETTE = ['#1d4ed8', '#7c3aed', '#0f766e', '#c2410c', '#be185d', '#4d7c0f', '#0369a1', '#a21caf', '#b45309', '#334155'];
const markerLetter = (i: number) => String.fromCharCode(65 + (i % 26));
const colorOf = (i: number) => PALETTE[i % PALETTE.length];
// 배만 지켜보는 줄의 점 색. 판정이 없다는 뜻의 회색이고, 화물 색(PALETTE)과도 판정 색과도 겹치지 않는다
const VESSEL_COLOR = '#9ca3af';
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
/** 지도 말풍선은 React가 아니라 HTML 글이라 글자를 직접 이스케이프한다 */
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

  /** 지금 화면의 줄들을 지도 그리기 도구(WatchRenderer)가 받는 모양으로 바꾼다.
   * 색·글자·말풍선 HTML은 여기서 미리 만들어 넘긴다. 도구는 지구본인지 평면인지 몰라도 되게 하려는 것이다 (docs/specs/watch-globe.md) */
  const buildInput = (): RenderInput => {
    const {demo} = getPresentationData();
    return {
      pins: active.map((item, i) => ({
        key: keyOf(item, i), letter: markerLetter(i), name: item.ship_name, lat: item.lat, lng: item.lng,
        trackColor: pinColor(item, i), pinColor: markColor(item, i), seeded: isSeedSource(item.position_source),
        verdict: item.verdict ?? null, tipHtml: tip(item, markerLetter(i)),
        track: showTrack ? (item.track ?? []).map(t => ({lat: t.lat, lng: t.lng})) : [],
      })),
      port: {lat: demo.port.lat, lng: demo.port.lng, name: demo.port.name},
      tour,
    };
  };
  // 효과 안에서 늘 최신 화면 값으로 입력을 만들 수 있게 매 렌더에 맞춰 둔다
  const buildRef = useRef(buildInput);
  buildRef.current = buildInput;
  const renderer = useRef<WatchRenderer | null>(null);
  const inputVer = useRef(0);  // 데이터가 바뀐 횟수. 지도를 만드는 동안 바뀌었는지 보려고 센다
  const creating = useRef<Promise<unknown>>(Promise.resolve());  // 지도 만들기·지우기를 한 줄로 세운다 (같은 칸에 두 번 만들지 않게)
  const [kind, setKind] = useState<'globe' | 'leaflet' | null>(null);  // 만들어진 도구. 만드는 중이면 null
  const [glCapable, setGlCapable] = useState(false);  // WebGL이 되는 브라우저라 지구본을 시도하는 중. 로딩 동안 어두운 바탕을 깔려고 쓴다
  const [base, setBase] = useState<BaseKind>('sat');  // 지구본의 지금 바탕

  // 지도에서 일어난 일을 이 컴포넌트의 상태에 잇는다. 상태 설정 함수와 ref만 쓰므로 한 번 만들어 두면 된다
  const callbacks = useMemo<WatchMapCallbacks>(() => ({
    // 지도에서 가리키면 옆 목록이 그 줄로 스크롤한다. 목록에서 가리킬 때는 이 콜백이 오지 않는다
    onHover: i => {
      setHovered(i);
      const row = i !== null ? listEl.current?.children[i] as HTMLElement | undefined : undefined;
      const box = listEl.current;
      if (row && box && box.scrollHeight > box.clientHeight + 1) {
        const r = row.getBoundingClientRect(), b = box.getBoundingClientRect();
        if (r.top < b.top || r.bottom > b.bottom) {
          const calm = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
          box.scrollTo({top: box.scrollTop + (r.top < b.top ? r.top - b.top - 8 : r.bottom - b.bottom + 8), behavior: calm ? 'auto' : 'smooth'});
        }
      }
    },
    onSelect: setSelected,
    onCluster: setCluster,
    onBase: setBase,
  }), []);

  // 데이터가 바뀌면(목록 새로 읽기, 데모의 10초 갱신, 경로 보기 켜고 끄기) 지도를 새로 만들지 않고 데이터만 넘긴다. 지구본은 카메라를 그대로 둔다 (EC-05)
  // 고른 배·가리키는 배·클러스터 창은 배 번호(배열 순서)로 들고 있어서, 배가 들어오고 나가면 번호가 다른 배를 가리킬 수 있다.
  // 그래서 이전 입력의 key를 기억해 두고, 새 입력에서 같은 key의 번호로 옮긴다. key가 사라졌으면 풀고, 클러스터 창은 구성이 달라졌으면 닫는다
  const prevKeys = useRef<string[]>([]);
  useEffect(() => {
    inputVer.current += 1;
    const input = buildRef.current();
    const keys = input.pins.map(p => p.key);
    const before = prevKeys.current;
    prevKeys.current = keys;
    const r = renderer.current;
    if (!r) return;
    r.update(input);
    const remap = (i: number | null) => i === null ? null : keys.indexOf(before[i] ?? '') >= 0 ? keys.indexOf(before[i]) : null;
    const sel = remap(selected), hov = remap(hovered);
    if (sel !== selected) { r.select(sel); setSelected(sel); }
    if (hov !== hovered) { r.hover(hov); setHovered(hov); }
    if (cluster && cluster.some(k => before[k] === undefined || keys[k] !== before[k])) setCluster(null);
  }, [list, vessels, showTrack]);  // eslint-disable-line react-hooks/exhaustive-deps
  // 위치가 있는 배가 있으면 지도를 한 번 만들고, 0척이 되거나 화면을 떠나면 지운다 (EC-08, EC-10). 어느 도구를 쓸지는 createWatchRenderer가 정한다 (EC-01, EC-02)
  const mapShown = !!list && active.length > 0;
  useEffect(() => {
    const el = host.current;
    if (!mapShown || !el) return;
    let disposed = false;
    let made: WatchRenderer | undefined;
    let ro: ResizeObserver | undefined;
    const ver = inputVer.current;
    setGlCapable(supportsWebGL());
    // 앞선 만들기·지우기가 끝난 뒤에 시작한다. 개발 모드의 효과 두 번 실행에서도 같은 칸에 지도가 두 개 생기지 않는다
    const run = creating.current.then(async () => {
      if (disposed) return;
      const r = await createWatchRenderer(el, buildRef.current(), callbacks);
      if (disposed) { r.destroy(); return; }
      made = r; renderer.current = r; setKind(r.kind);
      if (inputVer.current !== ver) r.update(buildRef.current());  // 만드는 사이 데이터가 바뀌었다
      ro = new ResizeObserver(() => r.resize());
      ro.observe(el);
    }).catch(e => { console.error('지켜보기 지도를 만들지 못했어요:', e); });
    creating.current = run;
    return () => {
      disposed = true; ro?.disconnect(); made?.destroy(); renderer.current = null;
      setKind(null); setBase('sat'); setHovered(null); setSelected(null); setCluster(null);
    };
  }, [mapShown, callbacks]);

  /** 목록 줄을 가리키는 동안 지도에서도 그 배를 강조한다 */
  const hoverShip = (i: number | null) => { renderer.current?.hover(i); setHovered(i); };
  /** 목록 줄·상세 창 버튼으로 그 배에 간다. 지도가 아직 없으면 목록 강조만 한다 */
  const focusShip = (i: number) => { if (renderer.current) renderer.current.focus(i); else setSelected(i); };
  /** 지구본 바탕 전환 (Q2). 어느 바탕이 켜졌는지는 지구본이 정한다(이미 실패한 바탕은 무시). 단추는 지구본이 onBase로 알려 준 값만 따라간다 */
  const chooseBase = (b: 'sat' | 'map') => { renderer.current?.setBase?.(b); };


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
            <button type="button" className="watch-fit" onClick={() => { setCluster(null); renderer.current?.fitAll(); }} title="모든 배 보기" aria-label="모든 배 보기"><Maximize2 size={16} strokeWidth={1.9}/>모든 배 보기</button>
            <label className="watch-track-toggle"><input type="checkbox" checked={showTrack} onChange={e => setShowTrack(e.target.checked)}/>6시간 간격 경로 보기</label>
          </div>
          {seededShown && <p className="wm-legend"><span className="watch-chip seeded wm-legend-pin" aria-hidden="true"/>점선 테두리 회색 핀: 지켜보기 버튼을 누를 때 넣은 위치예요(정기 수집 전, 몇 시간 전일 수 있음)</p>}
          <div className={`watch-map${kind === 'globe' || (kind === null && glCapable) ? ' is-globe' : ''}`}>
            <div ref={host} className="leaflet-host"/>
            {/* 위성 / 지도 바탕 전환 (Q2). 지구본일 때만 보인다. 바탕을 다 못 쓰면(none) 단추를 막는다 */}
            {kind === 'globe' && <div className="globe-base" role="group" aria-label="지도 바탕">
              {(['sat', 'map'] as const).map(b => <button key={b} type="button" aria-pressed={base === b} disabled={base === 'none'} onClick={() => chooseBase(b)}>{b === 'sat' ? '위성' : '지도'}</button>)}
            </div>}
            {cluster && <div className="cluster-panel" role="dialog" aria-label={`이 자리에 있는 배 ${cluster.length}척`} onKeyDown={e => { if (e.key === 'Escape') setCluster(null); }}>
              <div className="cluster-head"><strong>이 자리에 있는 배 {cluster.length}척</strong>
                <button type="button" className="icon-button" onClick={() => setCluster(null)} aria-label="닫기" autoFocus><X size={16}/></button></div>
              <ul>{cluster.map(k => { const item = active[k]; const move = motion(item); return <li key={keyOf(item, k)}>
                <div className="cluster-item-head"><span className={`watch-chip ${seededClass(item)}`.trim()} style={{background: markColor(item, k)}}>{markerLetter(k)}</span>
                  <span className="watch-main"><strong>{item.ship_name}</strong>{move && <small>{move}</small>}</span>
                  <button type="button" className="cluster-go" onClick={() => focusShip(k)} aria-label={`${item.ship_name} 위치로 지도 이동`} title="지도에서 보기"><LocateFixed size={18} strokeWidth={1.9} aria-hidden="true"/></button></div>
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
                onMouseEnter={() => hoverShip(i)} onMouseLeave={() => hoverShip(null)} onFocus={() => hoverShip(i)} onBlur={() => hoverShip(null)} aria-pressed={selected === i} onClick={() => focusShip(i)} aria-label={`${markerLetter(i)} ${item.ship_name} 지도에서 보기`}>
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
              <button type="button" onMouseEnter={() => hoverShip(k)} onMouseLeave={() => hoverShip(null)} onFocus={() => hoverShip(k)} onBlur={() => hoverShip(null)}
                onClick={() => focusShip(k)} aria-label={`${item.ship_name} 배만 지켜보기, ${markerLetter(k)} 화물과 같은 배. 지도에서 보기`}>
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
