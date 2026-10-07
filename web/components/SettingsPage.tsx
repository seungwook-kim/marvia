'use client';
/** 설정·내 정보 화면. 시안(4-1~4-5, 3-5)이 다섯 화면으로 나눈 것을 /settings 안의 하위 탭으로 옮겼다.
 * 하위 이동은 제목 아래의 밑줄 탭 줄이 맡고, 고른 탭 밑의 막대가 미끄러지듯 따라간다.
 * 판단 기준은 읽기 전용 (SDD 5.13). 설정 키·파일 경로·모델 설정은 일반 사용자에게 내지 않는다 (10-02 화면 용어 점검).
 * 카카오에서 돌아오면(?kakao=) 연결 상태가 있는 "설정" 탭을 연다. KakaoPanel이 그 값을 읽고 주소에서 지우므로 여기서는 지우지 않는다.
 * 탭 종류는 TABS가 정하고, 각 탭 안의 화면은 기존 컴포넌트(ProfileForm·HistoryTable·MemoryPanel·KakaoPanel·AccountPanel)를 그대로 쓴다 (동작·접근성 유지).
 */
import {useCallback, useEffect, useId, useLayoutEffect, useRef, useState} from 'react';
import {Info, LockKeyhole} from 'lucide-react';
import {useApp} from './AppProvider';
import {ProfileForm} from './ProfileForm';
import {HistoryTable} from './HistoryTable';
import {MemoryPanel} from './MemoryPanel';
import {KakaoPanel} from './KakaoPanel';
import {AccountPanel} from './AccountPanel';
import {applyTheme, contrastWarnings, DEFAULT_CUSTOM, isHex, readCustom, readThemePref, THEME_OPTIONS, type CustomTheme, type ThemePref} from '@/lib/theme';

type Tab = 'profile' | 'history' | 'memory' | 'rules' | 'account';
const TABS: {key: Tab; label: string; lead: string}[] = [
  {key: 'profile', label: '회원 정보', lead: '내 이름과 연락처를 확인하고 고칠 수 있어요.'},
  {key: 'history', label: '조회 이력', lead: '최근 조회한 내용을 요약으로 볼 수 있어요. 자세한 근거는 다시 조회하면 확인할 수 있어요.'},
  {key: 'memory', label: '기억한 화물', lead: '메모와 기억한 화물을 확인하고 정리할 수 있어요.'},
  {key: 'rules', label: '설정', lead: '화면 색과 알림 연결은 언제든 바꿀 수 있어요. 판단 기준은 볼 수만 있어요.'},
  {key: 'account', label: '계정과 이력', lead: '되돌릴 수 없는 작업은 한 번 더 확인한 뒤 진행해요.'},
];

export function SettingsPage(){
  const [tab, setTab] = useState<Tab>('rules');
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const [bar, setBar] = useState<{left: number; width: number} | null>(null);
  // 주소의 ?tab= 또는 카카오 복귀(?kakao=)를 처음 한 번만 읽는다. 서버 그림과 어긋나지 않게 마운트 뒤에 읽는다
  useEffect(() => {
    const p = new URLSearchParams(window.location.search);
    const want = p.get('tab');
    if (p.get('kakao') !== null) setTab('rules');
    else if (TABS.some(t => t.key === want)) setTab(want as Tab);
  }, []);
  // 고른 탭 밑의 막대 위치. 탭 줄이 가로로 밀리는 좁은 화면에서도 같은 줄 안에서 계산하므로 함께 움직인다
  const place = useCallback(() => {
    const el = tabRefs.current[TABS.findIndex(t => t.key === tab)];
    if (el) setBar({left: el.offsetLeft, width: el.offsetWidth});
  }, [tab]);
  useLayoutEffect(() => {
    place();
    window.addEventListener('resize', place);
    return () => window.removeEventListener('resize', place);
  }, [place]);
  useEffect(() => { void document.fonts?.ready.then(place); }, [place]);
  const cur = TABS.find(t => t.key === tab)!;
  return <div className="page st-page">
    <header className="st-head"><h1>내 정보와 설정</h1></header>
    <nav className="st-tabs" aria-label="설정·내 정보 구역">{TABS.map((t, i) =>
      <button key={t.key} ref={el => { tabRefs.current[i] = el; }} type="button" className={`st-tab${t.key === tab ? ' on' : ''}`} aria-current={t.key === tab ? 'page' : undefined} onClick={() => setTab(t.key)}>{t.label}</button>)}
      {bar && <span className="st-tab-bar" aria-hidden="true" style={{transform: `translateX(${bar.left}px)`, width: bar.width}}/>}</nav>
    {tab !== 'memory' && <p className="st-lead-line">{cur.lead}</p>}
    <div className="st-panel" key={tab}>
      {tab === 'profile' && <ProfileTab/>}
      {tab === 'history' && <HistoryTable/>}
      {tab === 'memory' && <MemoryPanel/>}
      {tab === 'rules' && <RulesTab/>}
      {tab === 'account' && <AccountPanel/>}
    </div>
  </div>;
}

/** 회원 정보: 입력 폼 카드 하나. 로그인 계정(이메일)은 상자 없이 옆에 글로만 둔다. 로그인이 켜진 실제 모드에서만 저장된 정보가 있다 */
function ProfileTab(){
  const {authEnabled, user} = useApp();
  const usable = authEnabled && !!user;
  if (!usable) return <p className="st-note">{authEnabled ? '로그인하면 회원 정보를 확인하고 고칠 수 있어요.' : '지금은 로그인 기능이 꺼져 있어 회원 정보를 받지 않아요.'}</p>;
  return <div className="st-cols profile">
    <section className="st-card" aria-labelledby="st-profile-title"><h2 id="st-profile-title">내 회원 정보</h2><p className="st-sub">이름, 휴대전화, 주소. 본인에게만 원문이 보이고, 운영자에게는 가린 값만 보여요.</p><ProfileForm mode="edit"/></section>
    <section className="st-plain"><h2>로그인 계정</h2><p className="st-email">{user.email ?? user.name ?? user.id}</p></section>
  </div>;
}

/** 설정: 판단 기준(읽기 전용), 신호 우선순위, 카카오 알림, 운영자만 보는 모델 설정. 폭이 넓으면 칸이 늘어난다 */
function RulesTab(){
  const {data, isOperator} = useApp();
  const s = data.settings;
  return <div className="st-cols rules">
    <ThemeCard/>
    <section className="st-card"><div className="st-card-head"><h2>이상 신호 기준</h2>
      <span className="st-icon-tip" role="img" aria-label="보기만 가능" title="보기만 가능해요"><LockKeyhole size={18}/></span>
      {isOperator && <span className="st-icon-tip" role="img" aria-label={`출처 ${s.source}, ${s.read_at} 확인`} title={`출처: ${s.source} (${s.read_at} 확인)`}><Info size={18}/></span>}</div>
      <p className="st-sub">Marvia가 이상 신호로 보는 기준이에요. 운영팀이 관리하고, 여기서는 바꿀 수 없어요.</p>
      <dl className="st-rows">{s.thresholds.map(t => <div key={t.key} className="st-row"><dt>{t.label}</dt><dd>{t.value} {t.unit}</dd></div>)}</dl></section>
    <section className="st-card"><h2>서로 다른 신호, 무엇을 믿나요?</h2>
      <p className="st-sub">정보끼리 어긋나면 위에 있는 것을 먼저 믿어요. 실제로 고른 도착 예정은 각 조회 결과에서 확인해 주세요.</p>
      <ol className="st-priority">{s.priority.map((p, i) => <li key={p}><span>0{i + 1}</span>{p}</li>)}</ol></section>
    <KakaoPanel/>
    {isOperator && <ModelCard rows={[['판정 모델', s.model], ['평가 모델', s.judge_model], ['프롬프트 버전', s.prompt_version], ['요청당 토큰 상한', s.token_limit], ['일일 비용 상한', s.daily_budget]]}/>}
  </div>;
}

/** 화면 색 (10-07). 고르면 바로 화면 전체에 반영되고 이 기기에 저장된다 (lib/theme.ts). 로그인 없이도 쓸 수 있다 */
function ThemeCard(){
  const [pref, setPref] = useState<ThemePref>('system');
  const [custom, setCustom] = useState<CustomTheme>(DEFAULT_CUSTOM);
  useEffect(() => { setPref(readThemePref()); setCustom(readCustom()); }, []);
  function pick(v: ThemePref) { setPref(v); applyTheme(v, true, v === 'custom' ? custom : undefined); }
  /** 색을 움직이는 동안 바로 화면에 반영하고 저장한다. HEX 칸에 쓰다 만 값은 화면에만 남기고 반영하지 않는다 */
  function edit(next: Partial<CustomTheme>) {
    const c = {...custom, ...next};
    setCustom(c);
    if (isHex(c.brand) && isHex(c.bg) && isHex(c.nav)) { setPref('custom'); applyTheme('custom', true, c); }
  }
  const valid = isHex(custom.brand) && isHex(custom.bg) && isHex(custom.nav);
  const warnings = valid ? contrastWarnings(custom) : [];
  const preview = valid ? [custom.brand, custom.bg, custom.nav] : THEME_OPTIONS[4].swatch;
  return <section className="st-card st-theme">
    <h2>화면 색</h2>
    <p className="st-sub">오래 보면 눈이 피로할 수 있어요. 고르면 바로 바뀌고, 이 기기에 저장돼요. "직접 만들기"에서는 팔레트로 색을 골라요.</p>
    <fieldset className="theme-picker"><legend className="sr-only">화면 색 고르기</legend>
      {THEME_OPTIONS.map(o => <label key={o.value} className="theme-opt">
        <input type="radio" name="marvia-theme" value={o.value} checked={pref === o.value} onChange={() => pick(o.value)}/>
        <span className="theme-swatch" aria-hidden="true">{(o.value === 'custom' ? preview : o.swatch).map((c, i) => <i key={`${c}-${i}`} style={{background: c}}/>)}</span>
        <strong>{o.label}</strong><small>{o.note}</small>
      </label>)}
    </fieldset>
    {pref === 'custom' && <div className="theme-editor" role="group" aria-label="직접 만들기 색 조절">
      <ColorField label="강조색 (버튼·선택 표시)" value={custom.brand} onChange={v => edit({brand: v})}/>
      <ColorField label="바탕색" value={custom.bg} onChange={v => edit({bg: v})}/>
      <ColorField label="사이드바색" value={custom.nav} onChange={v => edit({nav: v})}/>
      <label className="theme-field"><span>글자 진하기 {custom.text}%</span>
        <input type="range" min={40} max={100} step={1} value={custom.text} onChange={e => edit({text: Number(e.target.value)})} aria-valuetext={`${custom.text}%`}/></label>
      <div className="theme-presets"><span>빠른 시작</span>
        {PRESETS.map(p => <button key={p.label} type="button" onClick={() => edit(p.theme)}>{p.label}</button>)}
        <button type="button" onClick={() => edit(DEFAULT_CUSTOM)}>기본값으로</button></div>
      {!valid ? <p className="theme-warn" role="status">색은 #과 6자리(예: #1D4ED8)로 넣어 주세요.</p>
        : warnings.length ? <ul className="theme-warn" role="status">{warnings.map(w => <li key={w}>{w}</li>)}</ul>
        : <p className="theme-ok" role="status">글자와 버튼이 잘 읽히는 대비예요.</p>}
    </div>}
  </section>;
}

/** 직접 만들기의 빠른 시작 예시. 여기서 출발해 조금씩 바꾸면 된다 */
const PRESETS: {label: string; theme: CustomTheme}[] = [
  {label: '남색 기본', theme: DEFAULT_CUSTOM},
  {label: '숲', theme: {brand: '#15803d', bg: '#f4f7f2', nav: '#1f3a2c', text: 95}},
  {label: '먹', theme: {brand: '#374151', bg: '#f5f5f4', nav: '#1c1917', text: 100}},
  {label: '한밤', theme: {brand: '#60a5fa', bg: '#0f172a', nav: '#020617', text: 90}},
  {label: '종이', theme: {brand: '#9a3412', bg: '#f6f1e7', nav: '#3b2f25', text: 85}},
];

/** 팔레트(운영체제 색 고르기 창)와 HEX 칸. 팔레트는 움직이는 동안 바로 반영된다 */
function ColorField({label, value, onChange}: {label: string; value: string; onChange: (v: string) => void}){
  const id = useId();
  return <div className="theme-field"><span id={id}>{label}</span>
    <div className="theme-color">
      <input type="color" value={isHex(value) ? value : '#000000'} onChange={e => onChange(e.target.value)} aria-labelledby={id}/>
      <input type="text" value={value} maxLength={7} spellCheck={false} aria-label={`${label} HEX 값`}
        onChange={e => { const v = e.target.value.trim(); onChange(v.startsWith('#') ? v : `#${v}`); }}/>
    </div>
  </div>;
}

/** 운영자용 모델·프롬프트 설정. isOperator일 때만 그려진다(위 RulesTab). 값이 있는 줄만 보이고, 하나도 없으면 한 줄로 알린다.
 * 전에는 다섯 줄이 모두 "확인되지 않음"으로 채워져 고장처럼 보였다 (10-02 UI 점검 V7). 값을 지어내지 않는다 */
function ModelCard({rows}:{rows:[string,unknown][]}){
  const known = rows.filter(([, v]) => v !== null && v !== undefined && v !== '');
  return <section className="st-card"><h2>모델과 프롬프트</h2>{known.length
    ? <dl className="st-rows">{known.map(([label, value]) => <div className="st-row" key={label}><dt>{label}</dt><dd>{String(value)}</dd></div>)}</dl>
    : <p className="st-note">운영 설정 정보가 없어요. 서버에서 설정 값을 받으면 여기에 나와요.</p>}</section>;
}
