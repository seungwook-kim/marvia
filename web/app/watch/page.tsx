/** 감시·지도 화면 (docs/specs/cargo-watch.md, 10-02). 왼쪽 메뉴 "내 화물" 드롭다운의 두 번째 항목이다.
 * B/L 없이 배만 지켜보는 줄도 WatchMap이 같은 지도·목록에 회색 점으로 그린다 (10-08). 따로 그리던 목록은 같은 배를 두 번 보여 줘서 지웠다 */
import {WatchMap} from '@/components/WatchMap';
export default function WatchPage(){return <div className="page"><WatchMap/></div>;}
