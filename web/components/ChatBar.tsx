'use client';
/** AI 입력창 (docs/specs/chat-search.md). 홈 화면의 주 기능이라 가운데 넓은 자리에 둔다.
 * 문장은 POST /api/chat/stream으로 가고, 서버가 B/L을 가린 뒤 LLM이 의도(화물 조회·후속 질문·입항 선박 검색)를 고른다.
 * 기다리는 동안은 서버가 알린 실제 처리 단계만 보인다 (docs/specs/chat-progress.md). 타이머로 단계를 만들지 않는다.
 * 서류 사진:
 *   Claude 앱처럼 입력창에 사진·PDF를 붙인다(클립 버튼, 붙여넣기, 끌어 놓기). 한 번에 하나, 새로 붙이면 바뀐다. 읽은 값을 다시 보여 주지 않는다.
 *   보내면 사용자 말풍선에 미리보기와 쓴 글이 보이고, 사진을 읽어 B/L이 확실할 때만 바로 조회한다(AppProvider.askWithAttachment,
 *   판단은 lib/docAttach.ts 코드). 사진은 /api/document에만 가고 저장하지 않는다. 글 없이 사진만 보내도 된다.
 * 화면을 내려 입력창이 안 보이면 "AI에게 이어서 물어보기" 버튼이 나타나 입력창으로 돌려보낸다.
 */
import {useEffect, useId, useRef, useState, type ChangeEvent, type ClipboardEvent, type DragEvent, type FormEvent, type KeyboardEvent, type ReactNode} from 'react';
import {createPortal} from 'react-dom';
import {ArrowUp, Check, FileText, LoaderCircle, MessageCircle, PackageSearch, Paperclip, Ship, Sparkles, X} from 'lucide-react';
import {VerdictBadge} from './Shared';
import {useApp, type ChatImage} from './AppProvider';
import {ChatMiniMap} from './ChatMiniMap';
import {ChatCargoTable} from './ChatCargoTable';
import {WatchOfferAction} from './WatchOfferAction';
import {ChatCandidateChips} from './ChatCandidateChips';
import {EvidencePopover} from './EvidencePopover';
import {ConversationEvidence} from './ConversationEvidence';
import {DOCUMENT_MAX_BYTES, isLive, QUOTA_MESSAGE, tour} from '@/lib/api';
import {onComposerRequest} from '@/lib/composer';
import {stageLabels, type ProgressStage} from '@/lib/progress';

const MAX = 500;  // routers/chat.py ChatRequest.message max_length

/** 처음 보는 사람을 위한 예시 문장. 누르면 입력창에 채우기만 한다(보내기는 사용자가). 실제 모드는 누구나 바로 결과를 볼 수 있는
 * 입항 선박 검색만 둔다 (데모 B/L을 실제 사용자에게 보여 주지 않는다). 데모 모드는 가공 B/L */
const LIVE_EXAMPLES = ['이번 주 일본에서 부산 들어오는 배 보여줘', '상하이에서 출발해서 부산 오는 배 있어?', '2주 안에 베트남에서 들어오는 배 알려줘', '3일 안에 부산 들어오는 배 전부'];
const DEMO_EXAMPLES = ['MRVDEMO1101 화물 괜찮아?', 'MRVDEMO1107 지금 조치해야 해?', '중국에서 부산 들어오는 배 보여줘'];
/** 데모 둘러보기(lib/tour.ts)에서는 isLive가 실행 중에 바뀌므로 그때그때 고른다 */
const examples = () => isLive ? LIVE_EXAMPLES : DEMO_EXAMPLES;

/** 사용자가 움직임 줄이기를 켰으면 부드러운 스크롤을 쓰지 않는다 (MDN prefers-reduced-motion) */
function scrollBehavior(): ScrollBehavior {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';
}

/** 입력창에 붙인 파일 하나. 사진이면 미리보기용 object URL, PDF면 빈 url(아이콘으로 보인다) */
interface Attachment { file: File; url: string; pdf: boolean }
/** 받는 파일: 사진과 PDF만 (lib/api.ts readDocument와 같은 검사). 서버는 앞 바이트로 다시 가른다 */
const isAcceptable = (f: File) => f.type.startsWith('image/') || f.type === 'application/pdf';

/** 말풍선 안의 첨부 미리보기. 새로고침 뒤에는 url이 비어(탭 저장소에 사진을 남기지 않는다) 파일 이름만 보인다 */
function ChatAttachmentPreview({image}: {image: ChatImage}) {
  return image.url && !image.pdf
    ? <img className="chat-attach-img" src={image.url} alt={`첨부한 사진 ${image.name}`}/>
    : <span className="chat-attach-file"><FileText size={16} aria-hidden="true"/>{image.name}</span>;
}

/** 답 글 속 http(s) 주소를 누를 수 있는 링크로 바꾼다. 단 코드가 붙인 줄만이다 (LLM 문장 속 주소까지 링크로 만들면,
 * 도구 결과·서류 글에 심은 주소가 답에 옮겨지면 누를 수 있는 링크가 된다). 링크로 만드는 줄은 머리가 아래 셋인 줄뿐이다:
 *   "웹 참고: 제목 (주소)"            services/chat.py WEB_NOTE. 주소는 서버가 가져온 검색 결과에서만 온다
 *   "확인처: ...", "진행 상황 직접 보기: ..."  services/referrals.py, 문구 원본은 skills/referrals/rules.md
 * 그 밖의 줄의 주소는 글자 그대로 둔다. 링크도 http·https만 만들고 새 탭으로 연다(noopener: 열린 쪽이 이 창을 못 건드리게).
 * 닫는 괄호는 주소에 넣지 않는다: "(https://...)"의 ")"가 주소 끝에 붙지 않게 */
const URL_PATTERN = /(https?:\/\/[^\s)]+)/g;
const LINK_LINE_PREFIXES = ['웹 참고: ', '확인처: ', '진행 상황 직접 보기: '];
function linkifyLine(line: string, key: number) {
  if (!LINK_LINE_PREFIXES.some(prefix => line.trimStart().startsWith(prefix))) return line;
  return line.split(URL_PATTERN).map((part, i) =>
    i % 2 === 1 ? <a key={`${key}-${i}`} href={part} target="_blank" rel="noopener noreferrer" className="chat-link">{part}</a> : part);
}
/** 서버가 답에 붙이는 근거 표시 [E1] / [E1, E2] / [E1][E2] (services/citations.py MARK_GROUP).
 * 이 화면에는 근거 칩을 그리는 부품이 없어 글자로 보이지 않게 앞 공백과 함께 걷어 낸다 */
const EVIDENCE_MARK = /\s*\[\s*E\d+(?:\s*[,，]\s*E\d+)*\s*\]/gi;
function linkify(text: string): ReactNode[] {
  // 줄마다 따로 본다. 줄바꿈 글자는 그대로 둔다(말풍선은 white-space로 줄을 보인다)
  return text.replace(EVIDENCE_MARK, '').split('\n').flatMap((line, i) => {
    const parts = linkifyLine(line, i);
    return [...(i === 0 ? [] : ['\n']), ...(Array.isArray(parts) ? parts : [parts])];
  });
}

export function ChatBar({idle = false, onShowDetail}: {idle?: boolean; onShowDetail?: () => void}) {
  const {ask, askWithAttachment, readingDoc, loading, stages, pendingSince, thread, result, request, usage, view} = useApp();
  // 판정 배지·할 일 한 줄은 "가장 최근 화물 답" 말풍선에만 붙인다. ChatTurn에는 판정이 없어 지금 보는 result(가장 최근 화물 답)에서 읽는다. 지어내지 않는다.
  // B/L 여러 개를 비교한 답은 표가 줄마다 배지를 가지고 있어 붙이지 않는다
  const verdictTurn = view === 'cargo' && result?.verdict ? thread.map((t, i) => t.role === 'assistant' && t.kind === 'cargo' && !(t.cargos && t.cargos.length > 1) ? i : -1).reduce((a, b) => Math.max(a, b), -1) : -1;
  // 선박 후보 칩은 "가장 최근 되묻기 답"에만 붙이고, 그 되묻기가 아직 풀리지 않았을 때만 보인다 (결과 칸이 같은 되묻기 그대로일 때).
  // 후보를 골라 판정이 나오면 결과가 바뀌어 칩이 사라진다
  const chipTurn = result?.status === 'insufficient' ? thread.map((t, i) => t.role === 'assistant' && t.agent?.vessel_candidates.length ? i : -1).reduce((a, b) => Math.max(a, b), -1) : -1;
  // 화물 카드가 실제로 보일 때만 "이 화물"로 안내한다. AppProvider.ask도 같은 조건으로 화물을 함께 보낸다
  const cargoShown = view === 'cargo' && !!result && !!request.bl_no;
  // 하루 토큰 한도를 다 쓰면 입력·서류 사진을 막는다. 서버도 429로 막는다 (docs/specs/token-quota.md)
  const blocked = !!usage?.exhausted;
  const [text, setText] = useState('');
  // 입력창에 붙인 서류 사진·PDF 하나. 보내면 AppProvider로 넘어가고 여기서는 비운다
  const [attachment, setAttachment] = useState<Attachment | null>(null);
  const [attachError, setAttachError] = useState('');
  const [dragging, setDragging] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const attachRef = useRef<Attachment | null>(null);
  attachRef.current = attachment;
  const [away, setAway] = useState(false);  // 입력창이 화면 밖이면 true
  // 다른 입력칸에 글을 쓰는 중이면 true. 휴대폰에서 떠 있는 "이어서 물어보기" 버튼이 입력칸을 덮지 않게 그동안 숨긴다
  const [typing, setTyping] = useState(false);
  // 입력창에 이미 글이 있을 때 예시·카드가 채우려던 문장. 덮어쓰지 않고 바꿀지 묻는다
  const [offer, setOffer] = useState<string | null>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const threadRef = useRef<HTMLDivElement>(null);
  const stick = useRef(true);  // 맨 아래를 보고 있으면 true. 위로 올려 읽는 중에는 새 메시지가 와도 끌어내리지 않는다

  // 새 메시지·답 대기 표시가 생기면 대화 칸만 맨 아래로 (위 대화를 읽는 중이면 그대로). 페이지 전체는 움직이지 않는다
  useEffect(() => {
    const el = threadRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [thread, loading, result]);
  function onScroll() {
    const el = threadRef.current;
    if (el) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  }

  // 입력창이 화면에서 벗어났는지 본다. 벗어나면 돌아가기 버튼을 띄운다
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const observer = new IntersectionObserver(([entry]) => setAway(!entry.isIntersecting), {threshold: 0});
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  // 다른 카드가 "AI에게 묻기"를 누르면 입력창으로 와서 글자를 채운다 (lib/composer.ts). 쓰던 글이 있으면 덮지 않고 묻는다
  const textRef = useRef(text);
  textRef.current = text;
  useEffect(() => onComposerRequest(t => {
    if (t !== undefined) offerText(t);
    goToComposer();
  }), []);  // eslint-disable-line react-hooks/exhaustive-deps

  // 입력칸(input·textarea·select)에 포커스가 있는지 본다. 화면 키보드가 올라온 동안 떠 있는 버튼을 숨기는 데만 쓴다
  useEffect(() => {
    const isField = (el: EventTarget | null) => el instanceof HTMLElement && el.matches('input, textarea, select, [contenteditable="true"]');
    const onIn = (e: FocusEvent) => setTyping(isField(e.target));
    const onOut = () => setTyping(isField(document.activeElement) && document.activeElement !== document.body);
    document.addEventListener('focusin', onIn);
    document.addEventListener('focusout', onOut);
    return () => { document.removeEventListener('focusin', onIn); document.removeEventListener('focusout', onOut); };
  }, []);

  /** 입력창이 비어 있으면 바로 채우고, 쓰던 글이 있으면 바꿀지 묻는다 */
  function offerText(t: string) {
    const next = t.slice(0, MAX);
    const now = textRef.current.trim();
    if (!now || now === next.trim()) { setText(next); setOffer(null); }
    else setOffer(next);
  }

  function goToComposer() {
    wrapRef.current?.scrollIntoView({behavior: scrollBehavior(), block: 'center'});
    inputRef.current?.focus({preventScroll: true});
  }

  // 화면을 떠날 때 보내지 않은 첨부의 미리보기 URL을 해제한다. 보낸 첨부의 URL은 AppProvider가 대화를 비울 때 해제한다
  useEffect(() => () => { if (attachRef.current?.url) URL.revokeObjectURL(attachRef.current.url); }, []);

  /** 파일 하나를 입력창에 붙인다. 이미 붙은 것이 있으면 바꾼다(한 번에 하나). 크기·종류는 보내기 전에 여기서 먼저 본다 */
  function attach(file: File | undefined) {
    if (!file || blocked) return;
    if (!isAcceptable(file)) { setAttachError('사진(JPG·PNG)이나 PDF만 올릴 수 있어요.'); return; }
    if (file.size > DOCUMENT_MAX_BYTES) { setAttachError('사진이 너무 커요. 10MB 이하로 올려 주세요.'); return; }
    const pdf = file.type === 'application/pdf';
    if (attachRef.current?.url) URL.revokeObjectURL(attachRef.current.url);  // 바꾸면 앞 미리보기는 해제
    const next = {file, url: pdf ? '' : URL.createObjectURL(file), pdf};
    attachRef.current = next;
    setAttachment(next);
    setAttachError('');
    inputRef.current?.focus();
  }
  /** 붙인 파일을 뺀다 (칩의 X 버튼) */
  function removeAttachment() {
    if (attachRef.current?.url) URL.revokeObjectURL(attachRef.current.url);
    attachRef.current = null;
    setAttachment(null);
    inputRef.current?.focus();
  }
  function onFile(e: ChangeEvent<HTMLInputElement>) {
    attach(e.target.files?.[0]);
    e.target.value = '';  // 같은 파일을 다시 골라도 change가 나게
  }
  /** 키보드 붙여넣기(Ctrl+V)로 사진을 붙인다. 글자와 함께 온 경우에는 글자 붙여넣기를 막지 않는다 */
  function onPaste(e: ClipboardEvent<HTMLTextAreaElement>) {
    const file = Array.from(e.clipboardData.files).find(isAcceptable);
    if (!file) return;
    if (!e.clipboardData.getData('text')) e.preventDefault();
    attach(file);
  }
  // 입력창 위로 파일을 끌어 놓기. 파일을 끌 때만 반응한다(글자 끌기는 그대로)
  const draggingFiles = (e: DragEvent) => Array.from(e.dataTransfer.types).includes('Files');
  function onDragOver(e: DragEvent) { if (!draggingFiles(e) || blocked) return; e.preventDefault(); setDragging(true); }
  function onDrop(e: DragEvent) { if (!draggingFiles(e)) return; e.preventDefault(); setDragging(false); attach(e.dataTransfer.files[0]); }

  async function send(message: string) {
    const m = message.trim();
    if (loading || blocked || m.length > MAX) return;  // 보내는 중이거나 한도를 다 썼으면 보내지 않는다
    if (attachment) {
      // 사진이 붙어 있으면 글이 비어 있어도 보낸다. 미리보기 URL은 말풍선이 이어 쓴다(해제는 AppProvider)
      const a = attachment;
      attachRef.current = null;  // 이제 AppProvider 몫이라 화면을 떠날 때 해제하지 않는다
      setAttachment(null); setText(''); setAttachError('');
      stick.current = true;
      await askWithAttachment(m, a.file, {url: a.url, name: a.file.name, pdf: a.pdf});
      return;
    }
    if (!m) return;
    setText('');
    stick.current = true;  // 내가 보낸 것은 항상 보이게
    const ok = await ask(m);
    // 실패하면 쓴 문장을 돌려준다. 그 사이 새로 쓴 글자가 있으면 덮지 않는다
    if (!ok) setText(t => t || m);
  }
  function submit(e: FormEvent) { e.preventDefault(); void send(text); }
  // Enter로 보내고, Shift+Enter는 줄바꿈 (한글 조합 중 Enter는 무시)
  function onKey(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(text); }
  }
  // 데모 둘러보기에서는 입력칸을 막고 예시를 누르면 바로 보낸다 (미리 만든 답, LLM 없음)
  function fill(example: string) { if (blocked) return; if (tour) { void send(example); return; } offerText(example); inputRef.current?.focus(); }

  // 판정 답 말풍선 안의 배지·할 일 한 줄. 대화가 비어 있는데 결과만 있는 경우(데모 첫 화면, 저장된 판정 열기)도 같은 모양으로 그린다
  const verdictBlock = result?.verdict ? <div className="chat-verdict"><VerdictBadge verdict={result.verdict} size="lg"/>{result.actions[0] && <p className="chat-todo"><span>지금 할 일</span>{result.actions[0]}</p>}<button type="button" className="chat-show-detail" onClick={onShowDetail}>결과 보기</button></div> : null;
  // 대화가 없고 답할 글만 있는 첫 화면(.chat-seed는 .chat-turn이 아니다: 실제 주고받은 말만 .chat-turn으로 센다): 결과 칸의 answer를 그대로 답 말풍선으로 보인다 (지어내지 않는다, 서버 값)
  const seedAnswer = thread.length === 0 && view === 'cargo' && result && result.status !== 'insufficient' && result.answer ? result : null;
  const empty = idle && !loading && thread.length === 0;
  // 이 대화의 근거 모음: 답 턴마다 붙은 근거를 순번(turn)과 함께 모은다. 0개면 버튼이 숨는다
  const convEvidence = thread.flatMap((t, i) => t.role === 'assistant' && t.evidence ? t.evidence.map(e => ({...e, turn: i})) : []);

  return <section className="ask" aria-label="AI에게 묻기">
    {/* 대화는 이 칸 안에서만 스크롤하고, 입력창은 아래에 고정한다. 페이지 전체는 움직이지 않는다 */}
    <div className="chat-scroll" ref={threadRef} onScroll={onScroll} role="log" aria-label="대화" tabIndex={0}>
      {convEvidence.length > 0 && <div className="chat-evidence-bar"><ConversationEvidence evidence={convEvidence}/></div>}
      {empty ? <div className="chat-empty">
        <span className="chat-empty-icon" aria-hidden="true"><MessageCircle size={22}/></span>
        <h2>어떤 화물을 확인할까요?</h2>
        <p>B/L 번호를 조회하면 배 위치와 통관 단계를 함께 보여드려요.</p>
        <div className="feature-tiles">
          <button type="button" onClick={() => isLive ? goToComposer() : fill(examples()[0])} disabled={blocked}><PackageSearch size={20}/>B/L 번호로 조회하기</button>
          <button type="button" onClick={() => fill(isLive ? examples()[0] : examples()[2])} disabled={blocked}><Ship size={20}/>입항 예정 선박 찾기</button>
          <button type="button" onClick={() => fileRef.current?.click()} disabled={blocked}><FileText size={20}/>서류 사진 올리기</button>
        </div>
      </div> : <ol className="chat-thread" aria-live="polite">
        {thread.map((t, i) => <li key={i} className={`chat-turn ${t.role}`}>
          <div className="chat-body"><div className="chat-bubble">{t.image && <ChatAttachmentPreview image={t.image}/>}{(t.role === 'assistant' || t.text) && <p>{t.role === 'assistant' ? <EvidencePopover text={t.text} citations={t.citations ?? []} evidence={t.evidence ?? []} renderText={linkify}/> : t.text}</p>}{i === verdictTurn && verdictBlock}</div>{t.role === 'assistant' && t.cargos && t.cargos.length > 1 ? <ChatCargoTable cargos={t.cargos}/> : t.role === 'assistant' && t.map && <ChatMiniMap map={t.map}/>}{t.role === 'assistant' && t.steps && t.steps.length > 0 && <StepsSummary steps={t.steps} tookMs={t.tookMs ?? 0}/>}{t.role === 'assistant' && t.watchOffer && <WatchOfferAction offer={t.watchOffer}/>}{i === chipTurn && t.agent && t.request && t.agent.trace_id === result?.trace_id && <ChatCandidateChips agent={t.agent} request={t.request} disabled={loading || blocked}/>}</div></li>)}
        {seedAnswer && <li className="chat-seed"><div className="chat-body"><div className="chat-bubble"><p>{seedAnswer.answer}</p>{verdictBlock}</div></div></li>}
      </ol>}
      {loading && pendingSince !== null && <Pending stages={stages} since={pendingSince} readingDoc={readingDoc}/>}
    </div>

    <div className="chat-dock">
      {blocked ? <p className="quota-note blocked" role="alert">{QUOTA_MESSAGE}</p>
        : usage?.near_limit && usage.remaining !== null ? <p className="quota-note" role="status">오늘 쓸 수 있는 AI 사용량이 얼마 남지 않았어요</p> : null}
      {offer !== null && <div className="fill-offer" role="status"><span>입력 중인 글이 있어요. 이 문장으로 바꿀까요? <strong>{offer}</strong></span>
        <button type="button" className="button secondary" onClick={() => { setText(offer); setOffer(null); inputRef.current?.focus(); }}>바꾸기</button>
        <button type="button" className="button text-button" onClick={() => { setOffer(null); inputRef.current?.focus(); }}>쓰던 글 유지하기</button></div>}
      {attachError && <p className="quota-note blocked" role="alert">{attachError}</p>}
      {/* 파일을 끌어 놓을 수 있는 자리는 입력창 전체다. 끄는 동안만 테두리를 강조한다 */}
      <div className={`composer-wrap ${dragging ? 'dragging' : ''}`} ref={wrapRef}
        onDragOver={onDragOver} onDragLeave={() => setDragging(false)} onDrop={onDrop}>
        <form className="composer" onSubmit={submit}>
          {/* 붙인 사진 칩 (Claude 앱처럼 입력칸 위). 읽은 값은 보여 주지 않는다 */}
          {attachment && <div className="composer-attach">
            <span className="attach-chip">
              {attachment.pdf ? <span className="attach-thumb pdf" aria-hidden="true"><FileText size={18}/></span>
                : <img className="attach-thumb" src={attachment.url} alt=""/>}
              <span className="attach-name">{attachment.file.name}</span>
              <button type="button" className="attach-remove" onClick={removeAttachment} aria-label={`첨부한 ${attachment.file.name} 빼기`}><X size={14}/></button>
            </span>
          </div>}
          <p className="sr-only" role="status">{attachment ? `파일을 첨부했어요: ${attachment.file.name}. 보내면 사진에서 B/L 번호를 찾아 조회해요.` : ''}</p>
          <label htmlFor="chat-input" className="sr-only">Marvia에게 질문하기</label>
          <textarea id="chat-input" ref={inputRef} value={text} onChange={e => setText(e.target.value)} onKeyDown={onKey} onPaste={onPaste} rows={1} maxLength={MAX} disabled={blocked || tour}
            placeholder={tour ? '데모에서는 아래 예시 질문만 눌러 볼 수 있어요' : attachment ? '사진에 대해 덧붙일 말을 쓰거나 그대로 보내 주세요' : cargoShown ? '이 화물에 대해 더 물어보세요' : 'B/L 번호를 입력해 주세요'}
            aria-describedby="chat-help" autoComplete="off" spellCheck={false}/>
          <div className="composer-foot">
            <button type="button" className="attach-trigger" onClick={() => fileRef.current?.click()} disabled={blocked || tour} aria-label="서류 사진이나 PDF 첨부하기" title="서류 사진이나 PDF 첨부하기"><Paperclip size={18}/></button>
            <input ref={fileRef} type="file" accept="image/*,application/pdf" className="sr-only" tabIndex={-1} aria-hidden="true" onChange={onFile}/>
            <div className="composer-send">
              {/* 글자 수는 쓰기 시작한 뒤에만 보인다. 빈 입력창에 숫자를 늘 두면 어수선하다 */}
              {text.length > 0 && <span className="mono chat-count" aria-label={`${text.length}자 입력, 최대 ${MAX}자`}>{text.length} / {MAX}</span>}
              <button type="submit" className="send" disabled={loading || blocked || (!text.trim() && !attachment)} aria-label="AI에게 질문 보내기">
                {loading ? <LoaderCircle size={20} className="spin"/> : <ArrowUp size={21}/>}</button>
            </div>
          </div>
        </form>
      </div>
      {/* 시작 문장: 대화가 비어 있을 때만. 누르면 입력창에 채우기만 한다 */}
      {(empty || tour) && <div className="chat-examples" aria-label="질문 예시">{examples().map(e =>
        <button key={e} type="button" onClick={() => fill(e)} disabled={blocked || (tour && loading)}>{e}<span aria-hidden="true">↗</span></button>)}</div>}
      <p id="chat-help" className="sr-only">Enter로 보내고 Shift + Enter로 줄을 바꿔요. 사진은 붙여넣기나 끌어 놓기로도 첨부할 수 있어요</p>
    </div>

    {/* body로 뺀다. 입력창이 화면 밖으로 밀렸을 때(휴대폰 상세 시트가 올라온 경우 등) 돌아가는 길 */}
    {away && !typing && createPortal(<button type="button" className="back-to-composer" onClick={goToComposer}><Sparkles size={15}/>AI에게 이어서 물어보기<span aria-hidden="true">↑</span></button>, document.body)}
  </section>;
}

/** 답이 온 뒤 접힌 한 줄 요약 "N단계 · M초". 누르면 거친 단계가 모두 완료로 펼쳐진다.
 * 단계 시간은 브라우저가 처음 본 시각 차이다. 스타일은 기존 pending-steps를 그대로 쓴다 */
function StepsSummary({steps, tookMs}: {steps: {stage: ProgressStage; at: number}[]; tookMs: number}) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return <div className="steps-summary">
    <button type="button" className="steps-toggle" aria-expanded={open} aria-controls={id} onClick={() => setOpen(o => !o)}>
      {steps.length}단계 · {Math.max(1, Math.round(tookMs / 1000))}초<span aria-hidden="true">{open ? '▾' : '▸'}</span></button>
    {open && <ol id={id} className="pending-steps" aria-label="답변 준비 단계">{steps.map(s =>
      <li key={s.stage} className="done"><span className="step-icon"><Check size={16}/></span><span className="step-name">{stageLabels[s.stage]}</span><span className="step-state">완료</span></li>)}</ol>}
  </div>;
}

/** 답을 기다리는 동안의 표시. 단계는 서버가 알린 것만, 시간은 브라우저가 실제로 잰 값이다 (docs/specs/chat-progress.md).
 * 마지막으로 받은 단계가 진행 중이고, 그 앞은 다음 단계가 시작됐으니 끝난 것이다. 앞으로 올 단계는 미리 그리지 않는다. */
function Pending({stages, since, readingDoc}: {stages: ProgressStage[]; since: number; readingDoc: boolean}) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const id = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(id); }, []);
  const seconds = Math.max(0, Math.floor((now - since) / 1000));
  return <div className="pending-card">
    <div className="pending-head">
      <strong>답을 준비하고 있어요</strong><span className="pending-dots" aria-hidden="true"><i/><i/><i/></span>
      <span className="mono pending-time">{seconds}초</span>
    </div>
    {/* 첨부 사진을 읽는 중. 브라우저가 실제로 보낸 POST /api/document를 기다리는 동안만 보인다. 읽는 데 20초쯤 걸린다(로컬 OCR + 소형 LLM) */}
    {readingDoc
      ? <ol className="pending-steps" aria-live="polite" aria-label="답변 준비 단계"><li className="current">
          <span className="step-icon"><LoaderCircle size={16} className="spin"/></span><span className="step-name">서류 사진 읽기 · 보통 20초쯤 걸려요</span><span className="step-state">진행 중</span></li></ol>
      : stages.length > 0
      ? <ol className="pending-steps" aria-live="polite" aria-label="답변 준비 단계">{stages.map((s, i) => {
          const done = i < stages.length - 1;
          return <li key={s} className={done ? 'done' : 'current'}>
            <span className="step-icon">{done ? <Check size={16}/> : <LoaderCircle size={16} className="spin"/>}</span>
            <span className="step-name">{stageLabels[s]}</span><span className="step-state">{done ? '완료' : '진행 중'}</span></li>;
        })}</ol>
      : <p className="pending-note" role="status">{isLive ? '진행 단계를 불러오는 중이에요.' : '체험 모드에서는 진행 단계를 보여 드리지 않아요.'}</p>}
    {/* 답이 올 자리를 흐릿한 줄로 먼저 잡아 둔다. 실제로 기다리는 동안만 보인다 */}
    <div className="pending-skeleton" aria-hidden="true"><span/><span/><span/></div>
  </div>;
}
