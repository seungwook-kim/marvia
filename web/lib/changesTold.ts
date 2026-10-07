/** 판정 변동을 대화 첫 말풍선으로 이미 알렸는지 기억한다.
 * 판정 변동은 사용자가 "오늘의 화물"에서 "모두 확인"을 누를 때까지 서버에 남는다. 그래서 새로 고칠 때마다 같은 변동을
 * 말풍선으로 또 알리지 않도록, 알린 변동(가린 B/L + 바뀐 시각)을 로그인한 사람별로 이 브라우저에 둔다. 최근 200개까지만 둔다 */
import type {WatchChange} from './types';

const KEY = 'marvia-changes-told';
const MAX = 200;
const keyOf = (c: WatchChange) => `${c.bl_masked}|${c.changed_at}`;

function read(uid: string): string[] {
  try {
    const v = JSON.parse(window.localStorage.getItem(KEY) ?? 'null');
    return v && v.uid === uid && Array.isArray(v.keys) ? v.keys : [];
  } catch { return []; }
}

/** 아직 말풍선으로 알리지 않은 변동만 */
export function untold(uid: string, changes: WatchChange[]): WatchChange[] {
  const told = new Set(read(uid));
  return changes.filter(c => !told.has(keyOf(c)));
}

export function markTold(uid: string, changes: WatchChange[]) {
  const keys = [...read(uid), ...changes.map(keyOf)].slice(-MAX);
  try { window.localStorage.setItem(KEY, JSON.stringify({uid, keys})); } catch { /* 못 저장하면 다음에 한 번 더 알린다 */ }
}
