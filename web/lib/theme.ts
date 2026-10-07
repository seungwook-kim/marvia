/** 화면 색 고르기 (10-07). 설정 > 화면 색에서 고르고, 바로 <html data-theme>에 반영한다.
 * - system: 운영체제 설정(prefers-color-scheme)을 따라 light/dark를 넣고, 설정이 바뀌면 따라 바꾼다
 * - light / dark / comfort(눈 편하게): 그대로 넣는다
 * 이 기기 브라우저에만 저장한다(localStorage). 저장소를 못 쓰면 system으로 본다. 색 값은 app/styles/enterprise.css */
export type ThemePref = 'system' | 'light' | 'dark' | 'comfort';
export const THEME_KEY = 'marvia-theme';
export const THEME_OPTIONS: {value: ThemePref; label: string; note: string; swatch: string[]}[] = [
  {value: 'system', label: '시스템 설정 따르기', note: '운영체제의 밝게·어둡게를 따라가요', swatch: ['#ffffff', '#0f1b2d']},
  {value: 'light', label: '밝게', note: '흰 바탕, 또렷한 글자', swatch: ['#ffffff', '#f3f4f6', '#0f1b2d']},
  {value: 'dark', label: '어둡게', note: '어두운 바탕, 밤이나 어두운 곳', swatch: ['#121a26', '#1a2433', '#080d15']},
  {value: 'comfort', label: '눈 편하게', note: '미색 바탕, 대비를 한 단계 낮춤', swatch: ['#faf7f0', '#ede7da', '#26313f']},
];

const isPref = (v: unknown): v is ThemePref => v === 'system' || v === 'light' || v === 'dark' || v === 'comfort';

export function readThemePref(): ThemePref {
  try { const v = window.localStorage.getItem(THEME_KEY); return isPref(v) ? v : 'system'; } catch { return 'system'; }
}

let unwatch: (() => void) | null = null;

/** 고른 값을 저장하고 바로 반영한다. system이면 운영체제 설정이 바뀔 때도 따라간다 */
export function applyTheme(pref: ThemePref, save = true) {
  if (save) { try { window.localStorage.setItem(THEME_KEY, pref); } catch { /* 저장 못 해도 이번 화면에는 반영한다 */ } }
  unwatch?.(); unwatch = null;
  const root = document.documentElement;
  if (pref !== 'system') { root.dataset.theme = pref; return; }
  const mq = window.matchMedia('(prefers-color-scheme: dark)');
  const set = () => { root.dataset.theme = mq.matches ? 'dark' : 'light'; };
  set();
  mq.addEventListener('change', set);
  unwatch = () => mq.removeEventListener('change', set);
}

/** 첫 화면이 깜빡이지 않게 그리기 전에 넣는 스크립트 (layout.tsx <head>). React 밖에서 돌아 위 함수를 쓸 수 없어 같은 규칙을 글로 둔다 */
export const THEME_BOOT_SCRIPT = `try{var p=localStorage.getItem('${THEME_KEY}');if(p!=='light'&&p!=='dark'&&p!=='comfort')p=matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';document.documentElement.dataset.theme=p}catch(e){}`;
