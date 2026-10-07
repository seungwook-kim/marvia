/** 마지막으로 받은 GET /api/me 결과를 로그인한 사람(user id)별로 이 브라우저에 기억한다 (10-07).
 * 왜: 새로고침할 때마다 서버 답이 오기 전 1~2초 동안 운영자 메뉴(평가 지표·관리)가 사라졌다 나타나고,
 * "회원 정보를 확인하고 있어요" 화면이 떴다. 기억한 값으로 먼저 그리고, 서버 답이 오면 그 값으로 맞춘다.
 * 화면 표시용일 뿐이다. 운영자 API·정지 여부는 서버가 매번 다시 확인한다. 다른 사람으로 로그인하면 쓰지 않는다 */
export interface MeSnapshot { is_operator: boolean; profile_complete: boolean; suspended: boolean }
const KEY = 'marvia-me';

export function readMe(userId: string): MeSnapshot | null {
  try {
    const v = JSON.parse(window.localStorage.getItem(KEY) ?? 'null');
    if (v && v.uid === userId && typeof v.is_operator === 'boolean' && typeof v.profile_complete === 'boolean') {
      return {is_operator: v.is_operator, profile_complete: v.profile_complete, suspended: !!v.suspended};
    }
  } catch { /* 저장소를 못 쓰거나 망가진 값이면 기억 없음 */ }
  return null;
}

export function saveMe(userId: string, me: MeSnapshot) {
  try { window.localStorage.setItem(KEY, JSON.stringify({uid: userId, ...me})); } catch { /* 저장 못 해도 화면은 서버 값으로 그린다 */ }
}

export function clearMe() {
  try { window.localStorage.removeItem(KEY); } catch { /* 괜찮다 */ }
}
