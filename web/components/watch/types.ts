/** 지켜보기 지도 그리기 공용 약속 (docs/specs/watch-globe.md). WatchMap.tsx는 이 모양만 알고, 지구본(WatchGlobe)인지 평면(WatchLeaflet)인지 모른다 */

/** 지도에 올릴 배 한 척. WatchMap.tsx가 목록 줄에서 미리 계산해 넘긴다 (색·글자·말풍선 HTML 포함) */
export interface RenderPin {
  key: string;
  letter: string;            // 핀 글자 A, B, C...
  name: string;              // 선명 (클러스터 말풍선·title)
  lat: number; lng: number;  // 마지막 위치
  trackColor: string;        // 경로 선·점 색 (pinColor)
  pinColor: string;          // 핀 채움 색 (markColor: seed면 회색)
  seeded: boolean;           // 지켜보기 버튼 때 넣은 위치 → 점선 테두리 회색 핀 (.seeded)
  verdict: 'fine' | 'watch' | 'act' | null;
  tipHtml: string;           // 말풍선 HTML (이미 이스케이프됨, .watch-tip)
  track: {lat: number; lng: number}[];  // 오래된 순, 마지막이 현재 위치. showTrack이 false면 빈 배열
}
export interface RenderInput {
  pins: RenderPin[];
  port: {lat: number; lng: number; name: string};
  tour: boolean;             // 데모 둘러보기 (10초마다 update가 온다)
}
export type BaseKind = 'sat' | 'map' | 'none';
export interface WatchMapCallbacks {
  onHover(i: number | null): void;          // 지도에서 가리킨 배 (목록 스크롤·강조용)
  onSelect(i: number | null): void;         // 지도에서 고른 배 (null이면 선택 해제)
  onCluster(group: number[] | null): void;  // 클러스터를 눌렀다 (상세 창 열기), null이면 닫기
  onBase?(base: BaseKind): void;            // 바탕이 바뀌었다 (실패로 내려간 경우 포함)
}
export interface WatchRenderer {
  readonly kind: 'globe' | 'leaflet';
  update(input: RenderInput): void;  // 데이터만 바꾼다. 카메라·확대는 그대로
  focus(i: number): void;            // 그 배로 이동. 혼자면 말풍선, 묶여 있으면 onCluster([i, ...나머지])
  fitAll(): void;                    // 모든 배와 부산항이 들어오게
  hover(i: number | null): void;     // 목록에서 가리킴 → 지도 강조만 (onHover를 다시 부르지 않는다)
  select(i: number | null): void;    // 목록·상세 창에서 고름 → 지도 강조만
  setBase?(base: 'sat' | 'map'): void;
  resize(): void;
  destroy(): void;                   // WebGL 문맥까지 푼다
}
export type CreateRenderer = (host: HTMLElement, input: RenderInput, cb: WatchMapCallbacks) => Promise<WatchRenderer>;
