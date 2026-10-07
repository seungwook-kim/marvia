/** 채팅 입력창에 붙인 서류 사진을 보낸 뒤 "무엇을 할지"를 코드로 정한다.
 *
 * 사진은 입력창에 붙어만 있고, 보내기를 누르면 OCR이 B/L 번호를 찾아 바로 조회한다. 읽은 값은 다시 보여 주지 않는다.
 * 판단은 LLM이 아니라 이 파일의 코드가 한다. 재료는 서버가 코드로 정한 bl_status·important(routers/document.py)다.
 *   ok        → 사용자 글 + B/L(+MRN)을 채팅으로 보낸다. 서버가 B/L을 가린 뒤 조회한다(services/chat.mask_bls). 화면에는 가린 번호만 말한다.
 *               사용자가 사진과 다른 B/L을 썼으면 사진 번호를 붙이지 않고 그 사실을 알린다(하이픈·띄어쓰기는 빼고 비교한다).
 *               다른 칸이 검사에 걸렸으면 "확인이 필요한 칸: …" 이름만 덧붙인다 (flagged)
 *   uncertain → 조회하지 않는다. 직접 입력을 부탁하고, 사용자가 쓴 글이 있으면 B/L 없이 그 글만 보낸다.
 *               B/L이 여러 개(bl_candidates)면 가린 값들을 보여 주고 고르게 한다
 *   none      → 쓴 글이 있으면 그 글만 보내고, 없으면 B/L·A/N 사진을 부탁한다
 * 중요한 칸(선명·항차·입항 예정일·출항일)은 값이 아니라 이름만 말한다.
 * B/L 조건·화물 인도 연락처는 확인 화면 없이 서버가 저장한다(services/doc_facts.py, 로그인 + B/L 검사 통과일 때만).
 * 화면은 저장한 칸 이름(facts_saved)만 "운임 조건·인코텀즈를 저장해 두었어요"처럼 말하고 값은 말하지 않는다. 값은 채팅에서 물으면 서버 코드가 답한다.
 */
import {maskBl} from './format';
import type {ChatDocument, DocumentResponse} from './types';

/** 되묻기 화면에 미리 채울 값과 출항일. AppProvider DocHints와 같은 모양이다 (순환 import를 피하려고 여기서 따로 적는다) */
export interface AttachHints { voyage_no: string | null; forwarder_eta: string | null; bl_no: string; departed_on: string | null }

export interface AttachDecision {
  /** 채팅으로 보낼 문장. null이면 채팅을 부르지 않는다 */
  message: string | null;
  /** 답보다 먼저 대화에 넣을 코드 문장. 읽은 값은 담지 않는다(B/L은 가린 값만). null이면 없음 */
  note: string | null;
  /** 검사를 통과한 항차·입항 예정일·출항일. ok일 때만 있다 (DocHints와 같은 쓰임) */
  hints: AttachHints | null;
  /** 이번 조회에 함께 보낼 출항일. 서버가 화물 현황 표 출항 칸에 적는다 (routers/chat.py ChatDocument) */
  document: ChatDocument | null;
}

// schemas/agent.py _BL_PATTERN + services/chat.py _BL(숫자 하나 이상). 서버 routers/document._BL_OK와 같다
const BL_OK = /^(?=[A-Z0-9]*\d)[A-Z0-9]{8,20}$/;
const MRN_OK = /^[0-9]{2}[A-Z0-9]{8}[IE]$/;  // schemas/agent.py _MRN_PATTERN
const DATE_OK = /^\d{4}-\d{2}-\d{2}$/;
type Important = NonNullable<DocumentResponse['important']>[number];
const IMPORTANT: Important[] = ['vessel', 'voyage', 'eta', 'etd'];  // routers/document.IMPORTANT_FIELDS
const IMPORTANT_NAMES: Record<Important, string> = {vessel: '선박 이름', voyage: '항차', eta: '입항 예정일', etd: '출항일'};

/** 서버가 bl_status를 주지 않는 옛 응답(또는 데모 fixture)이면 같은 규칙으로 다시 계산한다 (routers/document.bl_status_of) */
function blStatusOf(r: DocumentResponse): 'ok' | 'uncertain' | 'none' {
  if (r.bl_status) return r.bl_status;
  const bl = r.fields.bl_no;
  if (!bl.value) return 'none';
  return bl.checks.length || !BL_OK.test(bl.value) ? 'uncertain' : 'ok';
}

/** 검사를 통과한 중요한 칸 이름. 서버 값이 없으면 같은 규칙으로 계산한다 (routers/document.important_of) */
function importantOf(r: DocumentResponse): Important[] {
  if (r.important) return r.important;
  return IMPORTANT.filter(k => { const f = r.fields[k]; return !!f?.value && !f.checks.length; });
}

type Saved = NonNullable<DocumentResponse['facts_saved']>[number];
// 순서는 서버 응답 순서를 따르지 않고 이 순서로 말한다 (원본 B/L·은행 지시식은 서버 doc_facts.CONDITIONS 순서)
const SAVED_ORDER: Saved[] = ['freight_term', 'incoterm', 'original_bl', 'to_order_bank', 'contact'];
const SAVED_NAMES: Record<Saved, string> = {freight_term: '운임 조건', incoterm: '인코텀즈', original_bl: '원본 B/L 여부',
  to_order_bank: '은행 지시식 여부', contact: '화물 인도 연락처'};

/** "운임 조건·인코텀즈를 저장해 두었어요. 채팅에서 물으면 알려 드려요." 저장한 칸 이름만 말한다. 없으면 null */
export function savedLine(keys: Saved[] | undefined): string | null {
  const names = SAVED_ORDER.filter(k => keys?.includes(k)).map(k => SAVED_NAMES[k]);
  if (!names.length) return null;
  const last = names[names.length - 1];
  return `${names.join('·')}${hasFinal(last) ? '을' : '를'} 저장해 두었어요(서류 사진에서 읽은 값, 확인 전). 채팅에서 물으면 알려 드려요.`;
}

/** 마지막 글자에 받침이 있나 (한글 음절 U+AC00~U+D7A3). "이름과", "항차와"처럼 조사를 고르는 데만 쓴다 */
function hasFinal(word: string): boolean {
  const code = word.charCodeAt(word.length - 1) - 0xac00;
  return code >= 0 && code <= 11171 && code % 28 !== 0;
}

/** "선박 이름과 입항 예정일은 읽었어요." 같은 한 줄. 칸 이름만 말하고 값은 말하지 않는다 (읽은 내용을 다시 보여 주지 않는다) */
export function importantLine(keys: Important[]): string | null {
  const names = keys.map(k => IMPORTANT_NAMES[k]);
  if (!names.length) return null;
  const last = names[names.length - 1];
  const head = names.slice(0, -1);
  const joined = head.length ? `${head.slice(0, -1).map(n => `${n}, `).join('')}${head[head.length - 1]}${hasFinal(head[head.length - 1]) ? '과' : '와'} ${last}` : last;
  return `${joined}${hasFinal(last) ? '은' : '는'} 읽었어요.`;
}

/** 검사를 통과한 값만 돌려준다. 날짜 칸은 YYYY-MM-DD 모양일 때만 */
function clean(r: DocumentResponse, key: 'voyage' | 'eta' | 'etd'): string | null {
  const f = r.fields[key];
  const v = f?.value?.trim();
  if (!v || f?.checks.length) return null;
  return key === 'voyage' || DATE_OK.test(v) ? v : null;
}

type Flagged = NonNullable<DocumentResponse['flagged']>[number];
// 확인이 필요한 칸 이름 (routers/document.FLAGGABLE_FIELDS 순서). 값은 말하지 않고 이름만 말한다
const FLAGGED_ORDER: Flagged[] = ['vessel', 'voyage', 'eta', 'etd', 'pol', 'pod', 'item', 'original_bl', 'to_order_bank',
  'freight_term', 'incoterm', 'contact_company', 'contact_role', 'contact_phone'];
const FLAGGED_NAMES: Record<Flagged, string> = {vessel: '선박 이름', voyage: '항차', eta: '입항 예정일', etd: '출항일',
  pol: '선적항', pod: '양하항', item: '품명', original_bl: '원본 B/L 여부', to_order_bank: '은행 지시식 여부',
  freight_term: '운임 조건', incoterm: '인코텀즈', contact_company: '연락처 회사', contact_role: '연락처 역할', contact_phone: '연락처 전화'};

/** 값을 읽었지만 검사에 걸린 칸 이름. 서버 값이 없으면 같은 규칙으로 계산한다 (routers/document.flagged_of) */
function flaggedOf(r: DocumentResponse): Flagged[] {
  if (r.flagged) return r.flagged;
  return FLAGGED_ORDER.filter(k => { const f = r.fields[k]; return !!f?.value && !!f.checks.length; });
}

/** "확인이 필요한 칸: 입항 예정일, 연락처 전화. 서류에서 한 번 더 봐 주세요." 이름만 말한다. 없으면 null (10-08 QA D6) */
export function flaggedLine(keys: Flagged[]): string | null {
  const names = FLAGGED_ORDER.filter(k => keys.includes(k)).map(k => FLAGGED_NAMES[k]);
  return names.length ? `확인이 필요한 칸: ${names.join(', ')}. 서류에서 한 번 더 봐 주세요.` : null;
}

const CONTAINER_OK = /^[A-Z]{3}[UJZ]\d{7}$/;  // ISO 6346 컨테이너 번호. B/L 모양과 겹치지만 B/L이 아니다

/** 20YYMMDD 모양의 실제 날짜인가 (services/chat._is_date_digits와 같은 기준). 날짜를 B/L로 치지 않는다 */
function isDateDigits(v: string): boolean {
  if (!/^20\d{6}$/.test(v)) return false;
  const y = +v.slice(0, 4), m = +v.slice(4, 6), d = +v.slice(6);
  const dt = new Date(Date.UTC(y, m - 1, d));
  return dt.getUTCFullYear() === y && dt.getUTCMonth() === m - 1 && dt.getUTCDate() === d;
}

/** 사용자가 쓴 글 속 B/L 모양 번호. raw는 쓴 그대로, bl은 하이픈을 뺀 대문자다 (10-08 QA D7).
 * 하이픈으로 끊어 쓴 번호(MRVA-2026-090001)도 같은 번호로 본다. 다만 하이픈이 든 숫자뿐인 덩어리는 날짜·전화라 뺀다.
 * 날짜·MRN·컨테이너 번호는 B/L이 아니다 */
export function typedBls(text: string): {raw: string; bl: string}[] {
  const out: {raw: string; bl: string}[] = [];
  for (const m of text.matchAll(/[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*/g)) {
    const raw = m[0];
    const bl = raw.replace(/-/g, '').toUpperCase();
    if (!BL_OK.test(bl) || isDateDigits(bl) || MRN_OK.test(bl) || CONTAINER_OK.test(bl)) continue;
    if (raw.includes('-') && !/[A-Z]/.test(bl)) continue;
    out.push({raw, bl});
  }
  return out;
}

/** 서류 응답과 사용자가 쓴 글로 다음 동작을 정한다. max는 채팅 문장 상한(routers/chat.py ChatRequest.message 500자) */
export function decideAttachment(r: DocumentResponse, text: string, max: number): AttachDecision {
  const typed = text.trim();
  const status = blStatusOf(r);
  const found = importantLine(importantOf(r));
  const withFound = (line: string) => found ? `${line}\n${found}` : line;

  if (status === 'ok') {
    const bl = (r.fields.bl_no.value ?? '').replace(/\s/g, '').toUpperCase();
    // MRN은 세관에 아직 없는 화물을 해수부 입항 신고로 찾을 때 쓴다. 서버가 코드로 뽑고 LLM에는 안 보낸다 (docs/specs/mrn-fallback.md)
    const mrn = (r.mrn ?? '').replace(/\s/g, '').toUpperCase();
    const head = `${bl}${MRN_OK.test(mrn) ? ` MRN ${mrn}` : ''}`;
    const saved = savedLine(r.facts_saved);  // 서버는 B/L 검사를 통과했을 때만 저장하므로 ok 줄에만 붙는다
    const typedList = typedBls(typed);
    // 10-08 QA D7: 같은 번호인지는 하이픈·띄어쓰기를 뺀 값으로 비교한다 (예전에는 글자 그대로 포함 여부만 봐서 B/L이 두 개가 됐다)
    const same = typedList.some(t => t.bl === bl) || typed.replace(/[\s-]/g, '').toUpperCase().includes(bl);
    const other = typedList.find(t => t.bl !== bl);
    if (!same && other) {
      // 사용자가 사진과 다른 B/L을 썼다. 사진 번호를 붙이면 두 화물 비교로 읽히고, 어느 쪽을 물은 건지 코드가 정할 수 없다.
      // 사용자가 쓴 번호를 따르고 사진 번호는 붙이지 않는다. 사진의 항차·날짜도 다른 화물 것이라 채우지 않는다
      return {
        message: typed.slice(0, max), hints: null, document: null,
        note: `사진의 B/L(가린 값 ${maskBl(bl)})과 입력하신 B/L(가린 값 ${maskBl(other.bl)})이 달라요. 입력하신 번호로 조회할게요. `
          + `사진의 번호로 조회하려면 그 번호를 직접 입력해 주세요.${saved ? `\n${saved}` : ''}`,
      };
    }
    // 하이픈으로 끊어 쓴 같은 번호는 붙인 번호로 바꾼다. 채팅은 붙여 쓴 번호만 B/L로 알아듣는다 (services/chat.py _BL)
    let normalized = typed;
    for (const t of typedList) if (t.bl === bl && t.raw !== t.bl) normalized = normalized.replace(t.raw, t.bl);
    // 사용자가 같은 B/L을 이미 썼으면 두 번 넣지 않는다(두 번 넣으면 화물 두 개 비교로 읽힌다). 상한을 넘으면 사용자 글 뒤를 자른다.
    // 띄어 쓴 같은 번호(MRVA 2026 090001)는 채팅이 B/L로 못 알아들어 붙인 번호를 앞에 넣는다
    const message = typedList.some(t => t.bl === bl)
      ? normalized.slice(0, max)
      : `${head} ${normalized.slice(0, Math.max(0, max - head.length - 1))}`.trim();
    const voyage = clean(r, 'voyage'), eta = clean(r, 'eta'), etd = clean(r, 'etd');
    const flagged = flaggedLine(flaggedOf(r));
    return {
      message,
      note: [`사진에서 B/L 번호(가린 값 ${maskBl(bl)})를 읽어 조회할게요.`, flagged, saved].filter(Boolean).join('\n'),
      hints: voyage || eta || etd ? {voyage_no: voyage, forwarder_eta: eta, bl_no: bl, departed_on: etd} : null,
      document: etd ? {bl_no: bl, departed_on: etd} : null,
    };
  }
  if (status === 'uncertain') {
    // 틀린 번호로 남의 화물을 조회하지 않게 멈춘다. 번호는 가린 값만 보인다
    const many = r.bl_candidates ?? [];
    if (many.length > 1) {
      // 10-08 QA D4: 정정본·MASTER+HOUSE처럼 서로 다른 B/L이 여럿이면 어느 번호인지 서류만으로 모른다. 서버가 준 가린 값만 보인다
      return {message: typed || null, hints: null, document: null,
        note: withFound(`사진에 B/L 번호가 여러 개 있어요(가린 값 ${many.join(', ')}). 어느 번호인지 몰라 조회하지 않았어요. 조회할 B/L 번호를 직접 입력해 주세요.`)};
    }
    const seen = maskBl((r.fields.bl_no.value ?? '').replace(/\s/g, ''));
    return {message: typed || null, hints: null, document: null,
      note: withFound(`사진에서 B/L 번호로 보이는 값(${seen})을 찾았지만 확실하지 않아요. 맞으면 B/L 번호를 직접 입력해 주세요.`)};
  }
  return {message: typed || null, hints: null, document: null,
    note: withFound(typed ? '사진에서 B/L 번호를 찾지 못했어요.' : '사진에서 B/L 번호를 찾지 못했어요. B/L, A/N(도착 통지서) 사진을 올려 주세요.')};
}
