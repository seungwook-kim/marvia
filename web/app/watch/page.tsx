/** 감시·지도 화면 (docs/specs/cargo-watch.md).
 * B/L 없이 배만 지켜보는 줄도 WatchMap이 같은 지도·목록에 회색 점으로 그린다.
 * 지도 아래 부산항 입항 추세 카드(IMF PortWatch)는 며칠 늦은 참고 자료라 판정과 섞지 않고 맨 아래에 둔다 */
import {WatchMap} from '@/components/WatchMap';
import {PortTrafficCard} from '@/components/PortTrafficCard';
export default function WatchPage(){return <div className="page"><WatchMap/><PortTrafficCard/></div>;}
