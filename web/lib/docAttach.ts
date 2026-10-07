/** 채팅 입력창에 붙인 서류 사진을 보낸 뒤 "무엇을 할지"를 코드로 정한다 (10-08 사용자 결정).
 *
 * 예전에는 읽은 값을 확인 화면(DocumentPanel)에 다시 보여 주고 사용자가 고친 값만 입력창에 넣었다. 10-08에 사용자가 이 규칙을 바꿨다:
 * Claude 앱처럼 사진은 입력창에 붙어만 있고, 보내기를 누르면 OCR이 B/L 번호를 찾아 바로 조회한다. 읽은 값은 다시 보여 주지 않는다.
 * 판단은 LLM이 아니라 이 파일의 코드가 한다. 재료는 서버가 코드로 정한 bl_status·important(routers/document.py)다.
 *   ok        → 사용자 글 + B/L(+MRN)을 채팅으로 보낸다. 서버가 B/L을 가린 뒤 조회한다(services/chat.mask_bls). 화면에는 가린 번호만 말한다
 *   uncertain → 조회하지 않는다. 직접 입력을 부탁하고, 사용자가 쓴 글이 있으면 B/L 없이 그 글만 보낸다
 *   none      → 쓴 글이 있으면 그 글만 보내고, 없으면 B/L·A/N 사진을 부탁한다
 * 중요한 칸(선명·항차·입항 예정일·출항일)은 값이 아니라 이름만 말한다. B/L 조건·연락처 저장(document-facts)은 확인 없이 하지 않는다.
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
  /** 검사를 통과한 항차·입항 예정일·출항일. ok일 때만 있다 (예전 확인 화면이 넘기던 DocHints와 같은 쓰임) */
  hints: AttachHints | null;
  /** 이번 조회에 함께 보낼 출항일. 서버가 화물 현황 표 출항 칸에 적는다 (routers/chat.py ChatDocument, 10-06) */
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

/** 마지막 글자에 받침이 있나 (한글 음절 U+AC00~U+D7A3). "이름과", "항차와"처럼 조사를 고르는 데만 쓴다 */
function hasFinal(word: string): boolean {
  const code = word.charCodeAt(word.length - 1) - 0xac00;
  return code >= 0 && code <= 11171 && code % 28 !== 0;
}

/** "선박 이름과 입항 예정일은 읽었어요." 같은 한 줄. 칸 이름만 말하고 값은 말하지 않는다 (10-08: 읽은 내용을 다시 보여 주지 않는다) */
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
    // 사용자가 같은 B/L을 이미 썼으면 두 번 넣지 않는다(두 번 넣으면 화물 두 개 비교로 읽힌다). 상한을 넘으면 사용자 글 뒤를 자른다
    const message = typed.toUpperCase().includes(bl) ? typed.slice(0, max) : `${head} ${typed.slice(0, Math.max(0, max - head.length - 1))}`.trim();
    const voyage = clean(r, 'voyage'), eta = clean(r, 'eta'), etd = clean(r, 'etd');
    return {
      message,
      note: `사진에서 B/L 번호(가린 값 ${maskBl(bl)})를 읽어 조회할게요.`,
      hints: voyage || eta || etd ? {voyage_no: voyage, forwarder_eta: eta, bl_no: bl, departed_on: etd} : null,
      document: etd ? {bl_no: bl, departed_on: etd} : null,
    };
  }
  if (status === 'uncertain') {
    // 틀린 번호로 남의 화물을 조회하지 않게 멈춘다. 번호는 가린 값만 보인다
    const seen = maskBl((r.fields.bl_no.value ?? '').replace(/\s/g, ''));
    return {message: typed || null, hints: null, document: null,
      note: withFound(`사진에서 B/L 번호로 보이는 값(${seen})을 찾았지만 확실하지 않아요. 맞으면 B/L 번호를 직접 입력해 주세요.`)};
  }
  return {message: typed || null, hints: null, document: null,
    note: withFound(typed ? '사진에서 B/L 번호를 찾지 못했어요.' : '사진에서 B/L 번호를 찾지 못했어요. B/L, A/N(도착 통지서) 사진을 올려 주세요.')};
}
