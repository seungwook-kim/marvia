/** 화면 색 고르기 (10-07). 설정 > 화면 색에서 고르고, 바로 <html data-theme>에 반영한다.
 * - system: 운영체제 설정(prefers-color-scheme)을 따라 light/dark를 넣고, 설정이 바뀌면 따라 바꾼다
 * - light / dark / comfort(눈 편하게): 그대로 넣는다 (색 값은 app/styles/enterprise.css)
 * - custom(직접 만들기): 사용자가 고른 강조색·바탕색·사이드바색·글자 진하기에서 나머지 색을 계산해 <html style>의 CSS 변수로 넣는다
 * 이 기기 브라우저에만 저장한다(localStorage). 저장소를 못 쓰면 system으로 본다 */
export type ThemePref = 'system' | 'light' | 'dark' | 'comfort' | 'custom';
export const THEME_KEY = 'marvia-theme';
/** 직접 만든 색의 원재료(편집기에 다시 보이는 값) */
export const CUSTOM_KEY = 'marvia-theme-custom';
/** 원재료에서 계산한 CSS 변수. 첫 화면 스크립트가 계산 없이 그대로 넣으려고 따로 저장한다 */
export const VARS_KEY = 'marvia-theme-vars';

export interface CustomTheme { brand: string; bg: string; nav: string; text: number }
export const DEFAULT_CUSTOM: CustomTheme = {brand: '#1d4ed8', bg: '#f9fafb', nav: '#0f1b2d', text: 100};

export const THEME_OPTIONS: {value: ThemePref; label: string; note: string; swatch: string[]}[] = [
  {value: 'system', label: '시스템 설정 따르기', note: '운영체제의 밝게·어둡게를 따라가요', swatch: ['#ffffff', '#0f1b2d']},
  {value: 'light', label: '밝게', note: '흰 바탕, 또렷한 글자', swatch: ['#ffffff', '#f3f4f6', '#0f1b2d']},
  {value: 'dark', label: '어둡게', note: '어두운 바탕, 밤이나 어두운 곳', swatch: ['#121a26', '#1a2433', '#080d15']},
  {value: 'comfort', label: '눈 편하게', note: '미색 바탕, 대비를 한 단계 낮춤', swatch: ['#faf7f0', '#ede7da', '#26313f']},
  {value: 'custom', label: '직접 만들기', note: '팔레트로 색을 골라요', swatch: ['#1d4ed8', '#f9fafb', '#0f1b2d']},
];

const isPref = (v: unknown): v is ThemePref => v === 'system' || v === 'light' || v === 'dark' || v === 'comfort' || v === 'custom';

export function readThemePref(): ThemePref {
  try { const v = window.localStorage.getItem(THEME_KEY); return isPref(v) ? v : 'system'; } catch { return 'system'; }
}

// ---------- 색 계산 ----------
type RGB = [number, number, number];
const HEX = /^#([0-9a-f]{6})$/i;
export const isHex = (v: string) => HEX.test(v);
function rgb(hex: string): RGB { const n = parseInt(hex.slice(1), 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; }
function hex([r, g, b]: RGB) { return '#' + [r, g, b].map(v => Math.round(Math.max(0, Math.min(255, v))).toString(16).padStart(2, '0')).join(''); }
/** a에서 b 쪽으로 t만큼 섞는다 (0이면 a, 1이면 b) */
function mix(a: string, b: string, t: number) { const x = rgb(a), y = rgb(b); return hex([0, 1, 2].map(i => x[i] + (y[i] - x[i]) * t) as RGB); }
/** WCAG 상대 밝기 */
export function luminance(h: string) {
  const [r, g, b] = rgb(h).map(v => { const c = v / 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
/** WCAG 대비 (1~21). 본문 글자는 4.5 이상이 기준 */
export function contrast(a: string, b: string) { const x = luminance(a), y = luminance(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); }
/** 바탕 위에서 더 잘 보이는 글자색(흰색 또는 먹색) */
const onColor = (bgHex: string) => (contrast(bgHex, '#ffffff') >= contrast(bgHex, '#111827') ? '#ffffff' : '#111827');

/** 고른 색 세 개와 글자 진하기(40~100)에서 화면 전체의 색 변수를 만든다 */
export function customVars(t: CustomTheme): Record<string, string> {
  const brand = isHex(t.brand) ? t.brand : DEFAULT_CUSTOM.brand;
  const bg = isHex(t.bg) ? t.bg : DEFAULT_CUSTOM.bg;
  const nav = isHex(t.nav) ? t.nav : DEFAULT_CUSTOM.nav;
  const s = Math.max(40, Math.min(100, t.text)) / 100;
  const dark = luminance(bg) < 0.25;
  const fg = dark ? '#ffffff' : '#000000';
  const surface = dark ? mix(bg, '#ffffff', 0.05) : mix(bg, '#ffffff', 0.7);
  const navDark = luminance(nav) < 0.4;
  const navFg = navDark ? '#ffffff' : '#000000';
  // 강조색이 바탕에서 너무 흐리면 글자·링크용으로 한 단계 바탕 반대쪽으로 민다
  const link = contrast(brand, surface) >= 4.5 ? brand : mix(brand, fg, 0.45);
  return {
    'color-scheme': dark ? 'dark' : 'light',
    '--bg': bg, '--canvas': bg, '--surface': surface,
    '--hover': mix(bg, fg, 0.06), '--soft': mix(bg, fg, 0.06), '--neutral-bg': mix(bg, fg, 0.06),
    '--border': mix(bg, fg, 0.12), '--border-strong': mix(bg, fg, 0.22),
    '--text': mix(bg, fg, 0.35 + 0.6 * s), '--ink': mix(bg, fg, 0.35 + 0.6 * s),
    '--text-2': mix(bg, fg, 0.3 + 0.5 * s), '--body': mix(bg, fg, 0.3 + 0.5 * s),
    '--muted': mix(bg, fg, 0.3 + 0.3 * s), '--quiet': mix(bg, fg, 0.3 + 0.3 * s),
    '--brand': link, '--brand-hover': mix(link, fg, 0.2), '--brand-soft': mix(surface, brand, 0.14),
    '--brand-fill': brand, '--on-brand': onColor(brand),
    '--nav': nav, '--nav-2': mix(nav, navFg, 0.08), '--nav-line': mix(nav, navFg, 0.14),
    '--nav-text': mix(nav, navFg, 0.78), '--nav-mute': mix(nav, navFg, 0.5), '--nav-strong': navFg === '#ffffff' ? '#ffffff' : '#111827',
    '--nav-on': contrast(brand, nav) >= 3 ? brand : mix(nav, navFg, 0.75),
    '--grade-fine': dark ? '#4ade80' : '#15803d', '--grade-watch': dark ? '#fbbf24' : '#b45309', '--grade-act': dark ? '#f87171' : '#b91c1c',
    '--grade-fine-solid': dark ? '#22c55e' : '#15803d', '--grade-watch-solid': '#d97706', '--grade-act-solid': '#dc2626',
  };
}

/** 고른 색으로 대비를 점검한다. 기준 미달 항목만 돌려준다 */
export function contrastWarnings(t: CustomTheme): string[] {
  const v = customVars(t);
  const out: string[] = [];
  if (contrast(v['--text'], v['--surface']) < 4.5) out.push(`본문 글자 대비 ${contrast(v['--text'], v['--surface']).toFixed(1)}:1 (4.5 이상 권장). 글자 진하기를 올려 주세요`);
  if (contrast(v['--muted'], v['--surface']) < 3) out.push(`보조 글자가 흐려요 (${contrast(v['--muted'], v['--surface']).toFixed(1)}:1)`);
  if (contrast(v['--on-brand'], v['--brand-fill']) < 4.5) out.push(`버튼 글자 대비 ${contrast(v['--on-brand'], v['--brand-fill']).toFixed(1)}:1. 강조색을 더 진하거나 더 밝게 골라 주세요`);
  if (contrast(v['--nav-text'], v['--nav']) < 4.5) out.push(`사이드바 글자 대비 ${contrast(v['--nav-text'], v['--nav']).toFixed(1)}:1`);
  return out;
}

export function readCustom(): CustomTheme {
  try {
    const raw = JSON.parse(window.localStorage.getItem(CUSTOM_KEY) ?? 'null');
    if (raw && isHex(raw.brand) && isHex(raw.bg) && isHex(raw.nav) && typeof raw.text === 'number') return raw;
  } catch { /* 망가진 값이면 기본값 */ }
  return {...DEFAULT_CUSTOM};
}

let unwatch: (() => void) | null = null;
let applied: string[] = [];

function setVars(vars: Record<string, string> | null) {
  const st = document.documentElement.style;
  for (const k of applied) st.removeProperty(k);
  applied = [];
  if (!vars) return;
  for (const [k, v] of Object.entries(vars)) { st.setProperty(k, v); applied.push(k); }
}

/** 고른 값을 저장하고 바로 반영한다. system이면 운영체제 설정이 바뀔 때도 따라간다. custom이면 custom 값(없으면 저장된 값)을 쓴다 */
export function applyTheme(pref: ThemePref, save = true, custom?: CustomTheme) {
  unwatch?.(); unwatch = null;
  const root = document.documentElement;
  if (pref === 'custom') {
    const c = custom ?? readCustom();
    const vars = customVars(c);
    setVars(vars);
    root.dataset.theme = 'custom';
    if (save) { try { window.localStorage.setItem(THEME_KEY, pref); window.localStorage.setItem(CUSTOM_KEY, JSON.stringify(c)); window.localStorage.setItem(VARS_KEY, JSON.stringify(vars)); } catch { /* 이번 화면에만 반영 */ } }
    return;
  }
  setVars(null);
  if (save) { try { window.localStorage.setItem(THEME_KEY, pref); } catch { /* 저장 못 해도 이번 화면에는 반영한다 */ } }
  if (pref !== 'system') { root.dataset.theme = pref; return; }
  const mq = window.matchMedia('(prefers-color-scheme: dark)');
  const set = () => { root.dataset.theme = mq.matches ? 'dark' : 'light'; };
  set();
  mq.addEventListener('change', set);
  unwatch = () => mq.removeEventListener('change', set);
}

/** 첫 화면이 깜빡이지 않게 그리기 전에 넣는 스크립트 (layout.tsx <head>). React 밖에서 돌아 위 함수를 쓸 수 없어 같은 규칙을 글로 둔다.
 * custom이면 저장해 둔 계산값(VARS_KEY)을 그대로 넣는다 */
export const THEME_BOOT_SCRIPT = `try{var d=document.documentElement,p=localStorage.getItem('${THEME_KEY}');if(p==='custom'){var v=JSON.parse(localStorage.getItem('${VARS_KEY}')||'null');if(v){for(var k in v)d.style.setProperty(k,v[k]);d.dataset.theme='custom'}else p=null}if(p!=='custom'){if(p!=='light'&&p!=='dark'&&p!=='comfort')p=matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';d.dataset.theme=p}}catch(e){}`;
