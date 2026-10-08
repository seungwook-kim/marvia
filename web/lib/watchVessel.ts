/** B/L 없이 배(MMSI)만 지켜보기의 API 클라이언트 (백엔드 routers/watch.py /api/watch/vessels).
 *
 * 출입증 붙이기·주소 규칙은 api.ts의 call과 같다(accessToken, NEXT_PUBLIC_API_BASE). 오류는 서버가 준 안내 문장(detail)을 그대로 올린다.
 * 판정·통관은 없다. 배 이름·국적·목적지·도착 예정은 채팅에서 사용자가 본 글 그대로 보낸다 (서버가 길이만 자른다).
 */
import {accessToken} from './auth';
import {tour, type PositionSource} from './api';
import {tourAddVessel, tourRemoveVessel, tourVesselPosition} from './tour';

const API_BASE = (process.env.NEXT_PUBLIC_API_BASE ?? '').trim().replace(/\/+$/, '');
/** 서버가 없으면(체험 모드) 계정이 없어 지켜볼 수 없다 */
export const vesselWatchEnabled = API_BASE !== '';
const TIMEOUT_MS = 15_000;

/** 채팅에서 본 배. dest·eta는 보인 글 그대로(없으면 생략). 입항 예정 목록처럼 MMSI가 없는 곳은 mmsi 대신 callSign을 준다 (서버가 찾는다) */
export interface VesselRef { mmsi?: number | null; callSign?: string | null; name: string; flag?: string | null; dest?: string | null; eta?: string | null }

/** 지켜보는 배 한 척. 위치는 수집 전이면 null이다. 판정 칸은 일부러 없다 */
export interface WatchedVessel {
  mmsi: number; name: string; flag: string | null; dest: string | null; eta: string | null; created_at: string | null;
  lat: number | null; lng: number | null; cog: number | null; sog: number | null; position_at: string | null;
  /** 마지막 점의 위치 출처 (services/vessel_watch.list_vessels, migrations/031). 옛 줄·데모는 null 또는 없음 */
  position_source?: PositionSource | null;
  track: {lat: number; lng: number; at: string; sog: number | null; source?: PositionSource | null}[];
}

async function request(path: string, init: RequestInit = {}): Promise<Response> {
  if (tour) throw new Error('데모에서는 쓸 수 없어요.');  // 데모에서는 서버를 부르지 않는다
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const headers = new Headers(init.headers);
    const token = await accessToken();
    if (token) headers.set('authorization', `Bearer ${token}`);
    if (init.body) headers.set('content-type', 'application/json');
    const res = await fetch(`${API_BASE}${path}`, {...init, headers, signal: controller.signal});
    if (!res.ok) {
      let detail = '';
      try { const b = await res.json(); if (typeof b?.detail === 'string') detail = b.detail; } catch { /* JSON이 아니면 기본 문장 */ }
      // 하루 선박 조회 한도(503)는 서버 안내 문장을 그대로, 문장이 없으면 같은 뜻의 기본 문장을 보인다
      throw new Error(res.status === 401 ? '로그인이 필요해요. 로그인한 뒤 다시 시도해 주세요.' : detail || (res.status === 503 ? '오늘 선박 조회 한도를 다 썼어요. 내일 다시 시도해 주세요.' : '') || '배 지켜보기를 처리하지 못했어요. 잠시 뒤 다시 시도해 주세요.');
    }
    return res;
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw new Error('답이 너무 늦어져 중단했어요. 다시 시도해 주세요.');
    throw e;
  } finally { clearTimeout(timer); }
}

// 켜고 끈 일을 같은 화면의 다른 버튼·목록에 알린다 (WatchToggle이 AppProvider로 하는 것과 같은 목적의 자체 구독)
type Listener = () => void;
const listeners = new Set<Listener>();
export const onVesselWatchChange = (fn: Listener) => { listeners.add(fn); return () => { listeners.delete(fn); }; };
const notify = () => listeners.forEach(fn => fn());

// 데모 둘러보기(lib/tour.ts): 서버를 부르지 않고 이 페이지 메모리에서만 켜고 끈다. 새로 고치면 비워진다
const tourVessels = new Map<number, WatchedVessel>();
let tourNextMmsi = 990000001;  // 호출부호만 있는 배(입항 예정 목록)에 데모용 번호를 붙인다

/** GET /api/watch/vessels. 체험 모드면 빈 목록 */
export async function listWatchedVessels(): Promise<WatchedVessel[]> {
  // 데모: 추가한 배는 시뮬레이터가 정한 가상 위치·지나온 길로 지도에 나온다 (lib/tour.ts tourVesselPosition)
  if (tour) return [...tourVessels.values()].map(v => { const p = tourVesselPosition(v.mmsi); return p ? {...v, lat: p.lat, lng: p.lng, sog: p.sog, cog: p.cog, position_at: p.position_at, track: p.track} : v; });
  if (!vesselWatchEnabled) return [];
  return ((await (await request('/api/watch/vessels')).json()) as {items: WatchedVessel[]}).items;
}

/** POST /api/watch/vessels. 같은 배를 다시 올리면 보인 값만 갱신된다. 5척 한도면 서버 안내 문장으로 실패한다.
 * 호출부호로 올렸을 때 서버가 정한 MMSI를 돌려준다 (해제할 때 쓴다). 서버가 번호를 못 정하면 안내 문장으로 실패한다 */
export async function watchVessel(v: VesselRef): Promise<number> {
  if (tour) {
    if (tourVessels.size >= 5 && !(v.mmsi && tourVessels.has(v.mmsi))) throw new Error('배는 5척까지 지켜볼 수 있어요. (데모)');
    const mmsi = v.mmsi ?? tourNextMmsi++;
    tourVessels.set(mmsi, {mmsi, name: v.name, flag: v.flag ?? null, dest: v.dest ?? null, eta: v.eta ?? null, created_at: new Date().toISOString(),
      lat: null, lng: null, cog: null, sog: null, position_at: null, track: []});
    tourAddVessel(mmsi, [v.dest, v.name, v.flag].filter(Boolean).join(' '));
    notify();
    return mmsi;
  }
  if (!vesselWatchEnabled) throw new Error('체험 모드에서는 배를 지켜볼 수 없어요.');
  const body = v.mmsi ? {mmsi: v.mmsi} : {call_sign: v.callSign ?? null};
  const res = await request('/api/watch/vessels', {method: 'POST', body: JSON.stringify({...body, name: v.name, flag: v.flag ?? null, dest: v.dest ?? null, eta: v.eta ?? null})});
  const mmsi = ((await res.json()) as {mmsi: number}).mmsi;
  notify();
  return mmsi;
}

/** DELETE /api/watch/vessels/{mmsi}. 켜져 있지 않아도 오류가 아니다 */
export async function unwatchVessel(mmsi: number): Promise<void> {
  if (tour) { tourVessels.delete(mmsi); tourRemoveVessel(mmsi); notify(); return; }
  if (!vesselWatchEnabled) return;
  await request(`/api/watch/vessels/${mmsi}`, {method: 'DELETE'});
  notify();
}
