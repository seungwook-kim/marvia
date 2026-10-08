/** 지켜보기 지도(components/WatchMap.tsx)의 순수 규칙: 위치 출처 표시와 같은 배 접기 (10-08 사용자 결정).
 * React·Leaflet 없이 값만 다뤄 scripts/verify-contract.mjs가 그대로 불러 검사한다. */
import type {PositionSource} from './api';

/** 위치 출처(watch_positions.source, migrations/031)를 사람 말로. 말풍선·클러스터 상세의 "위치 출처" 줄에 쓴다 */
export const POSITION_SOURCE_LABELS: Record<PositionSource, string> = {
  worker: '정기 수집',
  aisstream: '실시간 AIS',
  seed: '지켜보기를 켤 때 저장해 둔 위치(최대 몇 시간 전)',
  lookup: '화물 조회 때 받은 위치',
};

/** 출처 줄 글. 모르는 값이거나 옛 줄(null)이면 null을 돌려주고 화면은 그 줄을 뺀다 (없는 출처를 지어내지 않는다) */
export function positionSourceLabel(source: string | null | undefined): string | null {
  return source && Object.prototype.hasOwnProperty.call(POSITION_SOURCE_LABELS, source)
    ? POSITION_SOURCE_LABELS[source as PositionSource] : null;
}

/** 점선 테두리 회색 핀으로 그릴 출처인가 (10-08 사용자 결정). 지켜보기 버튼을 누를 때 넣은 첫 점(seed)은 ShipFinder 캐시라
 * 몇 시간 전 위치일 수 있어 다른 점과 구별한다. 마지막 점 출처로만 정해서, 정기 수집 점이 새로 오면 저절로 보통 핀으로 돌아간다 */
export const isSeedSource = (source: string | null | undefined) => source === 'seed';

/** 같은 배를 B/L 화물로도, 배만 지켜보기로도 켜 둔 MMSI 모음.
 * 두 줄은 같은 watch_positions 점을 읽어 늘 같은 자리에 있으니, 지도에는 화물 핀 하나만 그리고 배만 지켜보기 줄은 접는다.
 * 진행 중(active)이고 배가 정해진(vessel_mmsi) 화물만 센다: 반출이 끝난 화물은 지도에 없어 배만 지켜보기 핀이 그 배를 보여야 한다.
 * 화물 여러 건이 같은 배여도 화물 핀은 각자 그대로다 (다른 화물이라 클러스터로 묶이는 게 맞다). 접는 것은 배만 지켜보기 줄뿐이다 */
export function sharedVesselMmsis(cargo: {status: string; vessel_mmsi?: number | null}[], vessels: {mmsi: number}[]): Set<number> {
  const cargoShips = new Set(cargo.filter(c => c.status === 'active' && c.vessel_mmsi != null).map(c => c.vessel_mmsi as number));
  return new Set(vessels.map(v => v.mmsi).filter(m => cargoShips.has(m)));
}
