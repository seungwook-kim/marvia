'use client';
/** 데모 둘러보기 안내 줄 (10-07). 화면 맨 위에 늘 보인다: 데모라는 것, 10초마다 움직인다는 것, 실제 서비스는 6시간마다라는 것, 데모 시각, 끝내기 */
import {useEffect, useState} from 'react';
import {REAL_INTERVAL_HOURS, TOUR_TICK_HOURS, TOUR_TICK_MS, exitTour, tourClock} from '@/lib/tour';

const fmt = (d: Date) => new Intl.DateTimeFormat('ko-KR', {timeZone: 'Asia/Seoul', month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false}).format(d);

export function TourBanner() {
  const [now, setNow] = useState(() => tourClock());
  useEffect(() => { const id = setInterval(() => setNow(tourClock()), 1000); return () => clearInterval(id); }, []);
  return <div className="tour-banner" role="note" aria-label="데모 안내">
    <strong>데모 화면</strong>
    <span>예시 데이터로 둘러보는 보기 전용 화면이에요. 질문은 아래 예시 버튼으로만 할 수 있어요.</span>
    <span className="tour-tick">배 위치: {TOUR_TICK_MS / 1000}초마다 {TOUR_TICK_HOURS}시간씩 움직여요 · 실제 서비스는 {REAL_INTERVAL_HOURS}시간마다 위치를 받도록 되어 있어요</span>
    <span className="tour-clock" aria-live="off">데모 시각 {fmt(now)}</span>
    <button type="button" className="tour-exit" onClick={exitTour}>데모 끝내기</button>
  </div>;
}
