'use client';
/** 사이드바의 "대화" 목록. ChatGPT·Claude처럼 위에 "새 대화", 아래에 날짜별 묶음, 지금 보는 대화는 강조한다.
 * 목록·열기·삭제 상태는 모두 AppProvider에 있고 여기서는 그리기와 "정말 지울까요?" 확인 한 단계만 가진다.
 * 첫 쪽(50개)이 꽉 차면 아래 "지난 대화 더 보기"로 더 오래된 쪽을 이어 받는다 (10-08 QA F9, GET /api/conversations?before=).
 * 로그인했을 때만 Shell이 그린다. 넓은 화면은 사이드바 안에, 좁은 화면(900px 이하)은 상단바의 "대화" 버튼이 여는 패널 안에 같은 컴포넌트를 쓴다.
 */
import {useState} from 'react';
import {Plus, Trash2} from 'lucide-react';
import {useApp} from './AppProvider';
import {listConversationsPage} from '@/lib/api';
import type {ConversationSummary} from '@/lib/types';

/** 서버가 한 번에 주는 대화 수 (services/conversations.LIST_MAX). 첫 쪽이 이만큼 차 있으면 "더 보기"를 보인다 */
const PAGE_SIZE = 50;

const GROUPS = ['오늘', '어제', '지난 7일', '지난 30일', '그 이전'] as const;

/** 수정 시각을 사용자의 로컬 날짜 기준 묶음으로. 날짜 경계는 자정이다 */
function groupOf(iso: string, now: Date): typeof GROUPS[number] {
  const t = new Date(iso); if (Number.isNaN(t.getTime())) return '그 이전';
  const startOf = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((startOf(now) - startOf(t)) / 86_400_000);
  return days <= 0 ? '오늘' : days === 1 ? '어제' : days <= 7 ? '지난 7일' : days <= 30 ? '지난 30일' : '그 이전';
}

export function ConversationList({onPick}: {onPick?: () => void}) {
  const {conversations, activeId, loading, opening, newConversation, openConversation, removeConversation} = useApp();
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState('');
  const locked = loading || opening;  // 답을 기다리거나 불러오는 중에는 대화를 바꾸지 않는다
  // "더 보기"로 받은 더 오래된 쪽 (10-08 QA F9). 첫 쪽은 AppProvider가 갖고, 여기서는 그 뒤 쪽만 이어 붙인다
  const [older, setOlder] = useState<ConversationSummary[]>([]);
  const [olderCursor, setOlderCursor] = useState<string | null | undefined>(undefined);  // undefined: 아직 더 보기를 안 눌렀다
  const [moreBusy, setMoreBusy] = useState(false);

  // 첫 쪽에 들지 않은 줄만 붙인다 (첫 쪽이 새로 고쳐져 겹치거나, 지운 대화가 뒤쪽에 남지 않게)
  const firstIds = new Set(conversations.map(c => c.id));
  const all = [...conversations, ...older.filter(c => !firstIds.has(c.id))];
  // 서버 한 쪽은 50개라, 첫 쪽이 꽉 찼으면 더 있을 수 있다. 더 보기를 누른 뒤에는 서버가 준 커서로 정한다
  const canMore = olderCursor === undefined ? conversations.length >= PAGE_SIZE : olderCursor !== null;

  const now = new Date();
  const grouped = GROUPS.map(g => ({g, items: all.filter(c => groupOf(c.updated_at, now) === g)})).filter(x => x.items.length > 0);

  /** 다음 쪽을 받는다. 커서는 지금 목록의 마지막 줄("수정 시각|번호")이고 서버(services/conversations.make_cursor)와 같은 모양이다 */
  async function more() {
    const last = all[all.length - 1];
    if (!last) return;
    setMoreBusy(true); setError('');
    try {
      const page = await listConversationsPage(olderCursor ?? `${last.updated_at}|${last.id}`);
      setOlder(prev => [...prev, ...page.conversations.filter(c => !prev.some(p => p.id === c.id))]);
      setOlderCursor(page.next_before);
    }
    catch (e) { setError(e instanceof Error ? e.message : '대화를 더 불러오지 못했어요.'); }
    finally { setMoreBusy(false); }
  }

  function start() { setConfirmId(null); setError(''); newConversation(); onPick?.(); }
  async function open(c: ConversationSummary) {
    setConfirmId(null); setError('');
    if (c.id === activeId) { onPick?.(); return; }
    onPick?.();
    await openConversation(c.id);
  }
  async function remove(id: string) {
    setBusyId(id); setError('');
    try { await removeConversation(id); setConfirmId(null); setOlder(prev => prev.filter(c => c.id !== id)); }
    catch (e) { setError(e instanceof Error ? e.message : '대화를 지우지 못했어요. 잠시 뒤 다시 시도해 주세요.'); }
    finally { setBusyId(null); }
  }

  return <div className="conv-list">
    <button type="button" className="conv-new" onClick={start} disabled={locked}><span>새 대화</span><Plus size={16} aria-hidden="true"/></button>
    {error && <p role="alert" className="conv-error">{error}</p>}
    {grouped.length === 0 ? <p className="conv-empty">아직 저장된 대화가 없어요.</p>
      : grouped.map(({g, items}) => <section key={g} className="conv-group" aria-label={g}>
        <h3>{g}</h3>
        <ul>{items.map(c => {
          const active = c.id === activeId;
          return <li key={c.id} className={`conv-row${active ? ' active' : ''}`}>
            {confirmId === c.id
              ? <div className="conv-confirm" role="group" aria-label="대화 지우기 확인">
                  <span>이 대화를 지울까요?</span>
                  <button type="button" onClick={() => setConfirmId(null)} disabled={busyId === c.id}>취소</button>
                  <button type="button" className="conv-danger" onClick={() => void remove(c.id)} disabled={busyId === c.id}>{busyId === c.id ? '지우는 중' : '지우기'}</button>
                </div>
              : <>
                  <button type="button" className="conv-title" onClick={() => void open(c)} disabled={locked && !active} aria-current={active ? 'true' : undefined} title={c.title}>{c.title || '제목 없는 대화'}</button>
                  <button type="button" className="conv-delete" onClick={() => { setError(''); setConfirmId(c.id); }} disabled={locked} aria-label={`${c.title || '제목 없는 대화'} 지우기`}><Trash2 size={14}/></button>
                </>}
          </li>;
        })}</ul>
      </section>)}
    {canMore && <button type="button" className="conv-new" onClick={() => void more()} disabled={moreBusy}>
      <span>{moreBusy ? '불러오는 중' : '지난 대화 더 보기'}</span></button>}
  </div>;
}
