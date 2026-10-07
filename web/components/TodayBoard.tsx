'use client';
/** 오늘의 화물 (/today). 로그인한 사람 자신의 화물만 보이는 첫 화면이다.
 * 데이터는 모두 그 사람 것을 서버가 골라 준다(로그인 토큰 기준): 기억한 화물(GET /api/memory의 cargo), 지켜보는 화물(GET /api/watch),
 * 아직 확인하지 않은 판정 변동(GET /api/watch/changes, "모두 확인"을 누르면 POST /api/watch/changes/seen), 카카오 연결 상태(GET /api/kakao/status).
 * 체험 모드는 각 API가 돌려주는 가공 예시를 그대로 쓴다. 판정 등급은 서버가 저장한 last_verdict 그대로이고 화면이 다시 계산하지 않는다.
 * 모양은 docs/mockups/v2/A-enterprise-navy.html (styles/today.css) */
import {useEffect, useState, type FormEvent} from 'react';
import Link from 'next/link';
import {useRouter} from 'next/navigation';
import {RefreshCw} from 'lucide-react';
import {useApp} from './AppProvider';
import {VerdictBadge} from './Shared';
import {getKakaoStatus, getMemory, getWatchChanges, getWatchList, isLive, markWatchChangesSeen, tour} from '@/lib/api';
import {TOUR_TICK_MS} from '@/lib/tour';
import {dateTime, verdictLabels} from '@/lib/format';
import type {CargoSummary, KakaoStatus, Verdict, WatchChange} from '@/lib/types';
import type {WatchItem} from '@/lib/api';

const ORDER: Record<Verdict, number> = {act: 0, watch: 1, fine: 2};

/** "YYYY-MM-DD" 또는 ISO 시각을 "10-12"처럼 짧게. 없으면 줄표 */
function md(value: string | null | undefined) {
  if (!value) return '-';
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
  return m ? `${m[2]}-${m[3]}` : value;
}

function marginCell(c: CargoSummary) {
  if (c.last_days_margin === null || c.last_days_margin === undefined) return <span className="td-muted">-</span>;
  const n = c.last_days_margin;
  return <span className={n < 0 ? 'td-late' : undefined}>{n}</span>;
}

export function TodayBoard() {
  const {user, lookup, loading} = useApp();
  const router = useRouter();
  const [cargo, setCargo] = useState<CargoSummary[] | null>(null);
  const [watch, setWatch] = useState<WatchItem[]>([]);
  const [kakao, setKakao] = useState<KakaoStatus | null>(null);
  // 아직 확인하지 않은 판정 변동. 사용자가 "모두 확인"을 누를 때까지 남는다
  const [changes, setChanges] = useState<WatchChange[]>([]);
  const [acking, setAcking] = useState(false);
  const [ackError, setAckError] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [loadedAt, setLoadedAt] = useState<Date | null>(null);
  const [bl, setBl] = useState('');
  const [sale, setSale] = useState('');
  const [formError, setFormError] = useState('');

  async function load() {
    setBusy(true); setError('');
    try {
      const [mem, list, ch] = await Promise.all([getMemory(), getWatchList().catch(() => null), getWatchChanges().catch(() => [] as WatchChange[])]);
      setCargo(mem.cargo ?? []);
      setWatch(list?.items ?? []);
      setChanges(ch);
      setLoadedAt(new Date());
    } catch (e) {
      setError(e instanceof Error ? e.message : '화물 정보를 불러오지 못했어요.');
      setCargo(null);  // 0건으로 보이지 않게 비워 둔다 (숫자 칸은 '-')
      setLoadedAt(null);
    } finally { setBusy(false); }
    if (isLive) getKakaoStatus().then(setKakao).catch(() => setKakao(null));
  }
  /** 판정 변동을 모두 확인 처리한다. 서버가 지켜보는 화물 전체의 변동 표시를 지운다 */
  async function ack() {
    setAcking(true); setAckError('');
    try { await markWatchChangesSeen(); setChanges([]); }
    catch (e) { setAckError(e instanceof Error ? e.message : '확인 처리를 하지 못했어요. 잠시 뒤 다시 눌러 주세요.'); }
    finally { setAcking(false); }
  }
  // 로그인한 사람이 바뀌면 다시 읽는다 (앞 사람의 화물이 남지 않게 먼저 비운다)
  useEffect(() => { setCargo(null); setWatch([]); setChanges([]); void load(); }, [user?.id]);  // eslint-disable-line react-hooks/exhaustive-deps
  // 데모 둘러보기: 시뮬레이터가 10초마다 움직이므로 같이 다시 읽는다 (lib/tour.ts)
  useEffect(() => { if (!tour) return; const id = setInterval(() => void load(), TOUR_TICK_MS); return () => clearInterval(id); }, []);  // eslint-disable-line react-hooks/exhaustive-deps

  const active = (cargo ?? []).filter(c => c.status === 'active');
  const count = (v: Verdict) => active.filter(c => c.last_verdict === v).length;
  const needs = active.filter(c => c.last_verdict === 'act' || c.last_verdict === 'watch')
    .sort((a, b) => ORDER[a.last_verdict!] - ORDER[b.last_verdict!] || (a.last_days_margin ?? 99) - (b.last_days_margin ?? 99));
  const shipOf = (c: CargoSummary) => watch.find(w => w.bl_masked === c.bl_masked)?.ship_name ?? null;

  async function quick(e: FormEvent) {
    e.preventDefault();
    const no = bl.trim().toUpperCase();
    if (!no) { setFormError('B/L 번호를 넣어 주세요.'); return; }
    setFormError('');
    router.push('/');  // 결과는 물어보기 화면에 보인다
    await lookup({bl_no: no, bl_type: null, sale_due_date: sale || null, session_id: ''});  // 구분 모름: 서버가 하우스·마스터를 차례로 본다
  }

  return <div className="today">
    <header className="td-head">
      <div>
        <h1>오늘의 화물</h1>
        <span className="td-ts">{loadedAt ? `${dateTime(loadedAt.toISOString())} 기준` : error ? '불러오지 못함' : '불러오는 중'}{!isLive && ' · 체험 데이터'}</span>
      </div>
      <div className="td-actions">
        <button type="button" className="button secondary" onClick={() => void load()} disabled={busy}><RefreshCw size={14} className={busy ? 'spin' : undefined}/>새로고침</button>
        <Link className="button primary" href="/">물어보기</Link>
      </div>
    </header>

    {error && <p role="alert" className="td-error">{error}</p>}

    <section className="td-kpi" aria-label="판정별 화물 수">
      <div><span className="td-l">진행 중 화물</span><strong>{cargo ? active.length : '-'}<small>건</small></strong><span className="td-d">지켜보는 화물 {watch.filter(w => w.status === 'active').length}건</span></div>
      {(['act', 'watch', 'fine'] as Verdict[]).map(v => <div key={v}>
        <span className="td-l"><VerdictBadge verdict={v}/></span>
        <strong>{cargo ? count(v) : '-'}<small>건</small></strong>
        <span className="td-d">{v === 'act' ? '판매 예정일 초과·임박' : v === 'watch' ? '확인 필요' : '정상 진행'}</span>
      </div>)}
    </section>

    <div className="td-grid">
      <div className="td-main">
        <section className="td-panel" aria-labelledby="td-changes">
          <div className="td-ptitle"><h2 id="td-changes">판정 변동</h2><span className="td-c">확인 전 {changes.length}건</span>
            {changes.length > 0 && <button type="button" className="button secondary td-ack" onClick={() => void ack()} disabled={acking}>{acking ? '처리 중' : '모두 확인'}</button>}</div>
          {ackError && <p role="alert" className="td-error td-pad">{ackError}</p>}
          {changes.length === 0
            ? <p className="td-empty">확인하지 않은 판정 변동이 없어요.</p>
            : <div className="table-scroll"><table>
                <thead><tr><th style={{width: 120}}>시각</th><th>화물</th><th style={{width: 170}}>판정 변경</th><th>사유</th></tr></thead>
                <tbody>{changes.map((c, i) => <tr key={`${c.bl_masked}-${i}`}>
                  <td className="td-num">{dateTime(c.changed_at, true)}</td>
                  <td>{c.label ?? c.bl_masked}<span className="td-sub">{c.bl_masked}</span></td>
                  <td><span className="td-change"><VerdictBadge verdict={c.from_verdict}/><span aria-hidden="true">→</span><VerdictBadge verdict={c.to_verdict}/></span>
                    <span className="sr-only">{verdictLabels[c.from_verdict]}에서 {verdictLabels[c.to_verdict]}로</span></td>
                  <td>{c.reason ?? '-'}</td>
                </tr>)}</tbody>
              </table></div>}
        </section>

        <section className="td-panel" aria-labelledby="td-needs">
          <div className="td-ptitle"><h2 id="td-needs">조치 필요 화물</h2><span className="td-c">{needs.length}건 · 여유 일수 오름차순</span>
            <Link className="td-link" href="/cargo">화물 현황 전체</Link></div>
          {cargo === null ? <p className="td-empty">{error ? '화물을 불러오지 못했어요. 새로고침을 눌러 주세요.' : '불러오는 중이에요.'}</p>
            : needs.length === 0 ? <p className="td-empty">긴급·주의 화물이 없어요. 진행 중인 화물은 {active.length}건이에요.</p>
            : <div className="table-scroll"><table>
                <thead><tr><th style={{width: 84}}>판정</th><th>화물 · B/L</th><th>선박</th><th style={{width: 84}}>도착 예정</th><th style={{width: 96}}>판매 예정일</th><th className="td-r" style={{width: 72}}>여유(일)</th><th>통관 단계</th><th style={{width: 120}}>마지막 확인</th></tr></thead>
                <tbody>{needs.map(c => <tr key={c.bl_masked}>
                  <td><VerdictBadge verdict={c.last_verdict}/></td>
                  <td>{c.label ?? '이름 없는 화물'}<span className="td-sub">{c.bl_masked}</span></td>
                  <td>{shipOf(c) ?? <span className="td-muted">-</span>}</td>
                  <td className="td-num">{md(c.last_eta)}</td>
                  <td className="td-num">{md(c.sale_due_date)}</td>
                  <td className="td-num td-r">{marginCell(c)}</td>
                  <td>{c.last_stage ?? <span className="td-muted">-</span>}</td>
                  <td className="td-num td-muted">{c.last_checked_at ? dateTime(c.last_checked_at, true) : '-'}</td>
                </tr>)}</tbody>
              </table></div>}
        </section>
      </div>

      <aside className="td-side">
        <section className="td-panel" aria-labelledby="td-quick">
          <div className="td-ptitle"><h2 id="td-quick">빠른 조회</h2></div>
          <form className="td-form" onSubmit={e => void quick(e)}>
            <label htmlFor="td-bl">B/L 번호</label>
            <input id="td-bl" value={bl} onChange={e => setBl(e.target.value)} placeholder="예: MRVD00000001" autoComplete="off"/>
            <label htmlFor="td-sale">판매 예정일</label>
            <input id="td-sale" type="date" value={sale} onChange={e => setSale(e.target.value)}/>
            {formError && <p role="alert" className="td-error">{formError}</p>}
            <div className="td-form-btns"><button type="submit" className="button primary" disabled={loading}>조회</button></div>
          </form>
        </section>

        <section className="td-panel" aria-labelledby="td-alert">
          <div className="td-ptitle"><h2 id="td-alert">알림 연결</h2><Link className="td-link" href="/settings?tab=kakao">설정</Link></div>
          <dl className="td-kv">
            <div><dt>카카오 알림톡</dt><dd>{!isLive ? '체험 모드' : kakao === null ? '확인 안 됨' : !kakao.configured ? '서버 미설정' : kakao.linked ? '연결됨' : '미연결'}</dd></div>
            {kakao?.last_sent_at && <div><dt>마지막 발송</dt><dd>{dateTime(kakao.last_sent_at, true)}</dd></div>}
          </dl>
        </section>

        <section className="td-panel" aria-labelledby="td-done">
          <div className="td-ptitle"><h2 id="td-done">순항 화물</h2><span className="td-c">{count('fine')}건</span></div>
          {count('fine') === 0 ? <p className="td-empty">순항 화물이 없어요.</p>
            : <dl className="td-kv">{active.filter(c => c.last_verdict === 'fine').slice(0, 6).map(c =>
                <div key={c.bl_masked}><dt>{c.label ?? c.bl_masked}</dt><dd>{c.last_days_margin === null ? '-' : `여유 ${c.last_days_margin}일`}</dd></div>)}</dl>}
        </section>
      </aside>
    </div>
  </div>;
}
