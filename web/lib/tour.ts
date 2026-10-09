/** 데모 둘러보기. 로그인 전 소개 화면의 "데모 둘러보기"로 들어오는 보기 전용 화면.
 * - 서버·DB·LLM을 부르지 않는다. 화면은 체험 모드 가공 데이터(lib/fixtures)와 아래 시뮬레이터만 쓴다
 * - 배 위치는 10초마다 한 칸(가상 시각 6시간) 움직인다. 실제 서비스는 수집 워커가 6시간마다 위치를 받도록 되어 있다(WORKER_INTERVAL_HOURS=6)
 * - 부산항에 닿으면 가상 시각으로 하루 머물다 목록에서 빠지고, 출발지로 돌아가 다시 온다
 * 켜고 끄는 값은 이 탭의 sessionStorage에만 둔다. 끄면 원래 로그인 화면으로 돌아간다 */
import watchMap from './fixtures/watch-map.json';
import type {CargoSummary, Verdict, WatchChange} from './types';
import type {WatchItem} from './api';

export const TOUR_KEY = 'marvia-tour';
/** 화면이 움직이는 간격(실제 시간)과 그 한 칸이 뜻하는 가상 시간 */
export const TOUR_TICK_MS = 10_000;
export const TOUR_TICK_HOURS = 6;
/** 실제 서비스의 위치 수집 간격 (worker/run.py DEFAULT_INTERVAL_HOURS, .env WORKER_INTERVAL_HOURS) */
export const REAL_INTERVAL_HOURS = 6;

export function tourRequested(): boolean {
  try { return window.sessionStorage.getItem(TOUR_KEY) === '1'; } catch { return false; }
}
export function enterTour() {
  try { window.sessionStorage.setItem(TOUR_KEY, '1'); } catch { /* 저장소를 못 쓰면 이번 페이지에서만 */ }
  window.location.assign('/today');
}
export function exitTour() {
  try { window.sessionStorage.removeItem(TOUR_KEY); } catch { /* 괜찮다 */ }
  window.location.assign('/');
}

// ---------- 시뮬레이터 ----------
const BUSAN = {lat: 35.08, lng: 129.08};
/** 데모 시각의 출발점: 들어온 순간을 2026-10-07 09:00(한국 시각)으로 본다 */
const SIM_EPOCH = Date.parse('2026-10-07T00:00:00Z');
const startedAt = typeof window === 'undefined' ? 0 : Date.now();
const DWELL = 4;        // 부산에서 4칸(가상 하루) 머문다
const STAGES_SEA = ['적하목록 제출', '입항 전'];
const STAGES_PORT = ['입항', '반입', '수입신고 수리'];

type Pt = {lat: number; lng: number};
type Base = {bl_masked: string; label: string | null; ship_name: string; goods_name?: string | null; ship_flag?: string | null};
const bases = (watchMap as {items: Base[]}).items;

/** 배마다 다른 항로(육지를 가로지르지 않게 바다 경유점을 둔다)와 속도(노트). 서쪽에서 오는 배는 제주 남쪽을 돌아 들어온다 */
const JEJU_S = {lat: 33.0, lng: 126.6}, KOREA_STRAIT = {lat: 34.55, lng: 128.75}, EAST_SEA = {lat: 36.6, lng: 130.2};
// 일본에서 오는 배는 시코쿠·규슈 남쪽(다네가시마·야쿠시마 남쪽)을 돌아 고토 열도 서쪽으로 올라와 대한해협 서수도로 들어온다.
// 예전 경유점은 규슈·쓰시마 위를, 타이완·샤먼발은 타이완·푸젠 해안 위를 가로질렀다 (10-09, 레포 해안선으로 점검)
const KYUSHU_S: Pt[] = [{lat: 32.4, lng: 133.0}, {lat: 30.0, lng: 131.3}, {lat: 29.9, lng: 130.3}, {lat: 30.6, lng: 129.5}, {lat: 31.8, lng: 128.6}, {lat: 33.0, lng: 128.2}, KOREA_STRAIT];
const ROUTES: {from: string; way: Pt[]; kn: number}[] = [
  {from: '칭다오', way: [{lat: 35.6, lng: 121.2}, {lat: 33.6, lng: 124.8}, JEJU_S, KOREA_STRAIT], kn: 14},
  {from: '상하이', way: [{lat: 31.0, lng: 122.6}, {lat: 32.2, lng: 125.0}, JEJU_S, KOREA_STRAIT], kn: 18},
  {from: '닝보', way: [{lat: 29.9, lng: 122.6}, {lat: 31.5, lng: 125.2}, JEJU_S, KOREA_STRAIT], kn: 16},
  {from: '다롄', way: [{lat: 38.6, lng: 122.0}, {lat: 36.5, lng: 123.6}, {lat: 33.8, lng: 125.0}, JEJU_S, KOREA_STRAIT], kn: 12},
  {from: '가오슝', way: [{lat: 22.55, lng: 120.1}, {lat: 23.5, lng: 119.2}, {lat: 25.2, lng: 120.1}, {lat: 26.6, lng: 121.4}, {lat: 30.5, lng: 125.0}, JEJU_S, KOREA_STRAIT], kn: 21},
  {from: '샤먼', way: [{lat: 24.2, lng: 118.6}, {lat: 25.2, lng: 120.1}, {lat: 26.6, lng: 121.4}, {lat: 30.0, lng: 124.5}, JEJU_S, KOREA_STRAIT], kn: 20},
  {from: '오사카', way: [{lat: 34.3, lng: 135.0}, {lat: 33.0, lng: 134.3}, ...KYUSHU_S], kn: 13},
  {from: '블라디보스토크', way: [{lat: 42.6, lng: 132.0}, {lat: 39.5, lng: 131.4}, EAST_SEA], kn: 15},
  {from: '톈진', way: [{lat: 38.8, lng: 118.3}, {lat: 38.3, lng: 121.0}, {lat: 37.3, lng: 123.0}, {lat: 34.5, lng: 124.6}, JEJU_S, KOREA_STRAIT], kn: 19},
  {from: '도쿄', way: [{lat: 34.9, lng: 139.9}, {lat: 34.3, lng: 139.2}, {lat: 33.1, lng: 136.0}, ...KYUSHU_S], kn: 11},
];
/** 부산 안에서 배마다 다른 정박 자리 (같은 점에 겹치지 않게) */
const BERTHS: Pt[] = [{lat: 35.10, lng: 129.04}, {lat: 35.06, lng: 129.13}, {lat: 35.12, lng: 128.98}, {lat: 35.03, lng: 129.07},
  {lat: 35.09, lng: 129.20}, {lat: 35.15, lng: 129.10}, {lat: 35.00, lng: 128.98}, {lat: 35.04, lng: 129.24}, {lat: 35.13, lng: 128.88}, {lat: 34.98, lng: 129.14}];

const NM_PER_DEG = 60;
function distNm(a: Pt, b: Pt) {
  const dLat = (b.lat - a.lat) * NM_PER_DEG, dLng = (b.lng - a.lng) * NM_PER_DEG * Math.cos(((a.lat + b.lat) / 2) * Math.PI / 180);
  return Math.hypot(dLat, dLng);
}
/** 경유점을 이은 항로에서 출발 후 d해리 지점 */
function along(path: Pt[], d: number): Pt {
  let left = d;
  for (let k = 1; k < path.length; k++) {
    const seg = distNm(path[k - 1], path[k]);
    if (left <= seg) { const f = seg ? left / seg : 0; return {lat: path[k - 1].lat + (path[k].lat - path[k - 1].lat) * f, lng: path[k - 1].lng + (path[k].lng - path[k - 1].lng) * f}; }
    left -= seg;
  }
  return path[path.length - 1];
}

// ---------- 실제 배처럼 보이게 (10-09 사용자 지적: 배가 너무 반듯하게 일자로 온다) ----------
// 예전에는 경유점 사이를 직선·일정 속도로만 옮겨 자로 그은 듯 보였다. 아래 셋을 더한다. 값은 배 번호(seed)로 정해져 같은 칸이면 늘 같은 위치다
//  1) 경유점을 Catmull-Rom 곡선으로 이어 부드럽게 꺾는다
//  2) 배마다 다른 작은 좌우 흔들림(최대 약 0.07도, 약 4해리). 출발·도착 근처에서는 0으로 줄여 항구·해안에 걸리지 않게 한다
//  3) 칸마다 속도가 85~110% 사이로 달라진다. 진행 방향(cog)은 실제로 움직인 방향으로 계산한다
//  4) 일부 배는 접안 전에 부산 앞 묘박지에서 몇 칸 기다린다(속력 0). 실제로 흔한 일이고, 화면의 묘박 대기 안내와도 맞는다

/** 부산 앞 묘박지(남쪽 바다). 배마다 조금씩 다른 자리에 닻을 내린다 */
const ANCHORAGE: Pt = {lat: 34.97, lng: 129.02};
const WOBBLE_DEG = 0.07;

/** 같은 입력이면 늘 같은 0~1 값 (배 번호·칸 번호로 흔들림·속도를 정한다) */
function noise(a: number, b: number) {
  const x = Math.sin(a * 127.1 + b * 311.7) * 43758.5453;
  return x - Math.floor(x);
}

/** 경유점을 Catmull-Rom 곡선으로 촘촘히 이은 점들. 경유점은 그대로 지난다 */
function smooth(way: Pt[], per = 10): Pt[] {
  if (way.length < 3) return way;
  const out: Pt[] = [];
  for (let k = 0; k < way.length - 1; k++) {
    const p0 = way[Math.max(0, k - 1)], p1 = way[k], p2 = way[k + 1], p3 = way[Math.min(way.length - 1, k + 2)];
    for (let s = 0; s < per; s++) {
      const t = s / per, t2 = t * t, t3 = t2 * t;
      const f = (a: number, b: number, c: number, d: number) => 0.5 * (2 * b + (-a + c) * t + (2 * a - 5 * b + 4 * c - d) * t2 + (-a + 3 * b - 3 * c + d) * t3);
      out.push({lat: f(p0.lat, p1.lat, p2.lat, p3.lat), lng: f(p0.lng, p1.lng, p2.lng, p3.lng)});
    }
  }
  out.push(way[way.length - 1]);
  return out;
}

/** 한 번의 항해. 칸 k에서의 위치·속력·방향을 준다. leg는 묘박 대기까지 넣은 항해 칸 수 */
function voyage(way: Pt[], berth: Pt, kn: number, seed: number, anchorTicks: number) {
  const anchor = {lat: ANCHORAGE.lat + (noise(seed, 1) - 0.5) * 0.06, lng: ANCHORAGE.lng + (noise(seed, 2) - 0.5) * 0.1};
  const toAnchor = smooth([...way, anchor]);
  const path = [...toAnchor, berth];
  const dAnchor = toAnchor.slice(1).reduce((sum, p, k) => sum + distNm(toAnchor[k], p), 0);
  const total = dAnchor + distNm(anchor, berth);
  // 칸마다 다른 속도로 간 누적 거리. 묘박 대기 칸은 위치를 묘박지에 묶는다
  const speed = (k: number) => kn * (0.85 + 0.25 * noise(seed, 10 + k));
  const run: number[] = [0];
  while (run[run.length - 1] < total && run.length < 400) run.push(run[run.length - 1] + speed(run.length - 1) * TOUR_TICK_HOURS);
  const sail = run.length - 1;                       // 묘박 없이 도착하는 칸
  const kAnchor = run.findIndex(d => d >= dAnchor);  // 묘박지에 닿는 칸
  const leg = Math.max(3, sail + anchorTicks);
  const distAt = (k: number) => {
    if (anchorTicks && k > kAnchor) k = k <= kAnchor + anchorTicks ? kAnchor : k - anchorTicks;
    if (anchorTicks && k === kAnchor) return dAnchor;
    return Math.min(total, run[Math.min(k, sail)]);
  };
  const point = (d: number): Pt => {
    const p = along(path, d);
    // 출발·도착 근처에서는 흔들림을 0으로: sin(pi * 진행률)
    const f = Math.min(1, d / Math.max(dAnchor, 1));
    const fade = Math.sin(Math.PI * f) * (d >= dAnchor ? 0 : 1);
    return {lat: p.lat + WOBBLE_DEG * fade * Math.sin(f * Math.PI * 3 + seed), lng: p.lng + WOBBLE_DEG * fade * Math.cos(f * Math.PI * 2.3 + seed * 1.7)};
  };
  const at = (k: number) => {
    const kk = Math.max(0, Math.min(k, leg));
    const here = point(distAt(kk)), prev = point(distAt(Math.max(0, kk - 1)));
    const moved = distNm(prev, here);
    const cog = moved > 0.1 ? (Math.atan2((here.lng - prev.lng) * Math.cos(here.lat * Math.PI / 180), here.lat - prev.lat) * 180 / Math.PI + 360) % 360 : 0;
    return {...here, sog: kk >= leg || kk === 0 ? 0 : Math.round(moved / TOUR_TICK_HOURS * 10) / 10, cog: Math.round(cog)};
  };
  return {leg, at};
}

/** 배마다 정해진 값: 항로·속도·항해 칸 수, 판매 여유(일), 중간에 생기는 지연(일), 처음 시작 칸 */
function plan(i: number) {
  const r = ROUTES[i % ROUTES.length];
  // 세 척에 한 척꼴로 부산 앞 묘박지에서 1~2칸(가상 6~12시간) 기다린다
  const v = voyage(r.way, BERTHS[i % BERTHS.length], r.kn, i + 1, i % 3 === 0 ? 1 + (i % 2) : 0);
  const leg = v.leg;
  // 도착이 몰리지 않게 배마다 주기 안의 다른 지점에서 시작한다 (i번째 배는 자기 주기의 i/전체 지점)
  const cycle = leg + DWELL + 1;
  return {b: bases[i], from: r.from, kn: r.kn, v, leg, slack: [5, 3, 1, 4, 0, 6, 2, 3, 5, 1][i % 10], delay: i % 3 === 1 ? 2 : 0, offset: Math.round((i / bases.length) * cycle) % cycle};
}
const PLANS = bases.map((_, i) => plan(i));

/** 지금이 몇 번째 칸인지 */
export function tourTick(now = Date.now()) { return Math.max(0, Math.floor((now - startedAt) / TOUR_TICK_MS)); }
/** 데모 가상 시각 */
export function tourClock(tick = tourTick()) { return new Date(SIM_EPOCH + tick * TOUR_TICK_HOURS * 3600_000); }

const iso = (d: Date) => d.toISOString();
const dateOnly = (d: Date) => iso(d).slice(0, 10);
function verdictOf(margin: number): Verdict { return margin < 0 ? 'act' : margin <= 2 ? 'watch' : 'fine'; }

interface ShipState { visible: boolean; item: WatchItem; summary: CargoSummary; verdict: Verdict }

/** i번 배의 tick 시점 상태. 항해(leg) → 부산 정박(DWELL) → 한 칸 쉬고(목록에서 빠짐) → 다시 출발 */
function shipAt(i: number, tick: number): ShipState {
  const {b, v, leg, slack, delay, offset} = PLANS[i];
  const cycle = leg + DWELL + 1;
  const t = (tick + offset) % cycle;
  const now = tourClock(tick);
  const hours = (n: number) => n * TOUR_TICK_HOURS * 3600_000;
  // 이번 주기의 도착 시각. 지연이 있는 배는 절반을 지나면 도착이 늦어진다
  const sailing = t < leg;
  const late = sailing && delay > 0 && t >= leg / 2 ? delay : 0;
  const arrive = new Date(now.getTime() + hours(Math.max(0, leg - t)) + late * 86_400_000);
  const sale = new Date(now.getTime() + hours(Math.max(0, leg - t)) + slack * 86_400_000);
  const margin = Math.round((sale.getTime() - arrive.getTime()) / 86_400_000);
  const here = v.at(Math.min(t, leg));
  const trail = Array.from({length: 5}, (_, j) => Math.min(t, leg) - (4 - j)).filter(k => k >= 0)
    .map(k => { const p = v.at(k); return {lat: p.lat, lng: p.lng, at: iso(new Date(now.getTime() - hours(Math.min(t, leg) - k))), sog: p.sog}; });
  const verdict: Verdict = sailing ? verdictOf(margin) : 'fine';
  const stage = sailing ? STAGES_SEA[t < leg / 2 ? 0 : 1] : STAGES_PORT[Math.min(STAGES_PORT.length - 1, t - leg)];
  const item: WatchItem = {
    bl_masked: b.bl_masked, label: b.label, ship_name: b.ship_name, status: 'active',
    verdict, stage, eta: iso(arrive), days_margin: margin,
    lat: here.lat, lng: here.lng, cog: here.cog, sog: sailing ? here.sog : 0, position_at: iso(now), track: trail,
    cargo_key: `tour-${i}`, goods_name: b.goods_name ?? b.label, ship_flag: b.ship_flag ?? null,
  };
  const summary: CargoSummary = {
    bl_masked: b.bl_masked, bl_type: 'HOUSE', label: b.goods_name ?? b.label, status: 'active',
    sale_due_date: dateOnly(sale), forwarder_eta: null, original_bl: null, to_order_bank: null, freight_term: null, incoterm: null,
    last_verdict: verdict, last_checked_at: iso(now), last_days_margin: margin, last_eta: dateOnly(arrive), last_stage: stage,
    pending: [], summary: null,
  };
  return {visible: t < leg + DWELL, item, summary, verdict};
}

/** 모든 배의 tick 시점 상태. 배끼리 가까우면 지도(WatchMap)의 클러스터가 숫자 하나로 묶고, 떨어지면 다시 각자 핀으로 보인다 */
function ships(tick: number) { return bases.map((_, i) => shipAt(i, tick)); }

export function tourWatchItems(tick = tourTick()): WatchItem[] { return ships(tick).filter(s => s.visible).map(s => s.item); }
export function tourCargo(tick = tourTick()): CargoSummary[] { return ships(tick).filter(s => s.visible).map(s => s.summary); }

let ackTick = -1;
/** "모두 확인"을 누른 칸. 그 뒤에 바뀐 것만 다시 보인다 (이 페이지 안에서만) */
export function tourAck() { ackTick = tourTick(); }

/** 최근 8칸(가상 2일) 동안 판정이 바뀐 것. 확인을 누른 뒤의 변동만 */
export function tourChanges(tick = tourTick()): WatchChange[] {
  const out: WatchChange[] = [];
  const from = Math.max(ackTick + 1, tick - 8, 1);
  for (let k = tick; k >= from; k--) {
    const a = ships(k - 1), c = ships(k);
    c.forEach((s, i) => {
      if (!s.visible || !a[i].visible || a[i].verdict === s.verdict) return;
      out.push({bl_masked: s.item.bl_masked, label: s.summary.label, from_verdict: a[i].verdict, to_verdict: s.verdict,
        changed_at: iso(tourClock(k)), reason: s.verdict === 'fine' ? '부산항 도착, 통관 진행' : s.item.days_margin! < 0 ? '입항 지연 예상, 판매 예정일 초과' : '입항 지연 예상, 여유 줄어듦'});
    });
  }
  return out.slice(0, 6);
}

// ---------- 데모에서 사용자가 지켜보기로 추가한 배 ----------
// 실제 위치를 모르므로 데모답게 새 출발 위치(항로의 출발지)에서 시작해 다른 배처럼 10초마다 움직이게 한다.
// 목적지·출발지 글에 항구 이름이 있으면 그 항로를, 없으면 배 번호로 항로를 고른다. 해제하면 지도에서도 빠진다 (이 페이지 메모리만)
const PORT_WORDS: [RegExp, string][] = [
  [/칭다오|QINGDAO|CNTAO/i, '칭다오'], [/상하이|SHANGHAI|CNSHA/i, '상하이'], [/닝보|NINGBO|CNNGB/i, '닝보'], [/다롄|DALIAN|CNDLC/i, '다롄'],
  [/가오슝|KAOHSIUNG|TWKHH/i, '가오슝'], [/샤먼|XIAMEN|CNXMN/i, '샤먼'], [/오사카|OSAKA|JPOSA/i, '오사카'], [/블라디보스토크|VLADIVOSTOK|RUVVO/i, '블라디보스토크'],
  [/톈진|TIANJIN|CNTSN|CNTXG/i, '톈진'], [/도쿄|TOKYO|JPTYO/i, '도쿄'],
];
const extras = new Map<number, {route: number; kn: number; addedTick: number}>();

export function tourAddVessel(mmsi: number, hint: string) {
  if (extras.has(mmsi)) return;
  const byWord = PORT_WORDS.find(([re]) => re.test(hint))?.[1];
  const route = byWord ? ROUTES.findIndex(r => r.from === byWord) : mmsi % ROUTES.length;
  extras.set(mmsi, {route, kn: ROUTES[route].kn + ((mmsi % 5) - 2), addedTick: tourTick()});
}
export function tourRemoveVessel(mmsi: number) { extras.delete(mmsi); }

/** 추가한 배의 지금 위치와 지나온 길. 추가한 칸을 출발로 보고, 도착 → 정박 → 다시 출발을 되풀이한다 */
export function tourVesselPosition(mmsi: number, tick = tourTick()) {
  const e = extras.get(mmsi);
  if (!e) return null;
  const r = ROUTES[e.route];
  const berth = BERTHS[(mmsi + 3) % BERTHS.length];
  // 화물 배와 같은 항해 모양(곡선·흔들림·속도 변화·묘박 대기)을 쓴다 (10-09)
  const v = voyage(r.way, {lat: berth.lat + .02, lng: berth.lng - .03}, e.kn, mmsi % 997, mmsi % 3 === 0 ? 1 : 0);
  const leg = v.leg;
  const t = Math.max(0, tick - e.addedTick) % (leg + DWELL);
  const now = tourClock(tick);
  const here = v.at(Math.min(t, leg));
  const track = Array.from({length: 5}, (_, j) => Math.min(t, leg) - (4 - j)).filter(k => k >= 0)
    .map(k => { const p = v.at(k); return {lat: p.lat, lng: p.lng, at: new Date(now.getTime() - (Math.min(t, leg) - k) * TOUR_TICK_HOURS * 3600_000).toISOString(), sog: p.sog}; });
  return {lat: here.lat, lng: here.lng, sog: t < leg ? here.sog : 0, cog: here.cog, position_at: now.toISOString(), track, from: r.from};
}
