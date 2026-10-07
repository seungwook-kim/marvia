/** 감시·지도 화면 (docs/specs/cargo-watch.md, 10-02). 왼쪽 메뉴 "내 화물" 드롭다운의 두 번째 항목이다.
 * B/L 없이 배만 지켜보는 줄도 WatchMap이 같은 지도·목록에 회색 점으로 그린다 (10-08). 따로 그리던 목록은 같은 배를 두 번 보여 줘서 지웠다
 * 지도 아래에 부산항 최근 입항 추세 참고 카드(IMF PortWatch, 10-08)를 둔다. 며칠 늦은 맥락이라 판정과 섞지 않고 맨 아래에 둔다 */
import {WatchMap} from '@/components/WatchMap';
import {PortTrafficCard} from '@/components/PortTrafficCard';
export default function WatchPage(){return <div className="page"><WatchMap/><PortTrafficCard/></div>;}
