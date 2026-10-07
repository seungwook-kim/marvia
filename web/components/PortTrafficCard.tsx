'use client';
/** 부산항 최근 입항 추세 참고 카드 (IMF PortWatch). GET /api/port/busan/traffic을 읽어 한 줄로 보여 준다.
 * - 자료는 약 5일 늦게, 주 단위로 갱신된다. 그래서 "지금 붐빈다"가 아니라 "지난주 입항이 평소보다 많았다" 수준의 맥락이다.
 *   카드에 이 점과 "화물 판정에 쓰지 않는다"를 같이 적는다. 판정 카드와 헷갈리지 않게 등급 색도 쓰지 않는다
 * - 평균·비율·이름(busy/normal/quiet)은 서버(services/port_traffic.py)가 계산한다. 화면은 표시만 한다
 * - 서버가 없는 체험 모드이거나 자료를 한 번도 못 받았으면(status unavailable) 카드를 그리지 않는다
 * - 출처 "IMF PortWatch"는 IMF 이용 조건이라 늘 보인다 */
import {useEffect, useState} from 'react';
import {tour} from '@/lib/api';

const API_BASE = (process.env.NEXT_PUBLIC_API_BASE ?? '').trim().replace(/\/+$/, '');
const TIMEOUT_MS = 20_000; // 서버가 12시간 캐시를 갖고 있어 보통 바로 온다. 캐시가 빈 첫 호출만 위(IMF)까지 다녀온다

/** 서버 응답 모양 (backend/app/services/port_traffic.py summarize, _fallback) */
interface PortTraffic {
  status: 'ok' | 'partial' | 'unavailable';
  source: string;
  attribution: string;
  latest_date?: string | null;
  lag_days?: number | null;
  recent_days?: number;
  base_days?: number;
  avg7?: number | null;
  avg28?: number | null;
  ratio?: number | null;
  level?: 'busy' | 'normal' | 'quiet' | null;
}

const LEVEL_LABEL: Record<string, string> = {busy: '평소보다 많은 편', normal: '평소 수준', quiet: '평소보다 적은 편'};

/** 비율을 "+12%" 모양으로. 1.0이면 "0%" */
function percent(ratio: number): string {
  const p = Math.round((ratio - 1) * 100);
  return `${p > 0 ? '+' : ''}${p}%`;
}

export function PortTrafficCard() {
  const [data, setData] = useState<PortTraffic | null>(null);

  useEffect(() => {
    if (!API_BASE || tour) return; // 체험 모드·데모 둘러보기: 서버를 부르지 않고 가공 예시도 보여 주지 않는다. 실제 자료처럼 보일 수 있어서다
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
    fetch(`${API_BASE}/api/port/busan/traffic`, {signal: ctrl.signal})
      .then(res => (res.ok ? res.json() : null))
      .then((body: PortTraffic | null) => setData(body))
      .catch(() => setData(null)) // 참고 카드라 실패하면 조용히 숨긴다
      .finally(() => clearTimeout(timer));
    return () => { clearTimeout(timer); ctrl.abort(); };
  }, []);

  if (!data || data.status === 'unavailable' || data.avg7 == null) return null;
  const days = data.recent_days ?? 7;
  const base = data.base_days ?? 28;
  return (
    <section className="port-traffic" aria-label="부산항 최근 입항 추세 (참고)">
      <div className="port-traffic-main">
        <strong>부산항 최근 {days}일 컨테이너선 입항</strong>
        <span>
          하루 평균 {data.avg7.toLocaleString('ko-KR', {maximumFractionDigits: 1})}척
          {data.ratio != null && <> ({base}일 평균 대비 {percent(data.ratio)}{data.level ? `, ${LEVEL_LABEL[data.level]}` : ''})</>}
        </span>
      </div>
      <p className="port-traffic-meta">
        기준일 {data.latest_date ?? '알 수 없음'}
        {data.lag_days != null && <> (약 {data.lag_days}일 전 자료)</>}
        {data.status === 'partial' && <> · 최신 갱신에 실패해 이전에 받은 값이에요</>}
        {' · '}출처 {data.attribution || 'IMF PortWatch'}
      </p>
      <p className="port-traffic-note">
        며칠 늦게 나오는 참고 자료예요. 지금의 부두 혼잡도가 아니고, 화물 판정(순항·주의·긴급)에는 쓰지 않아요.
      </p>
    </section>
  );
}
