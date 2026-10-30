/** End-to-end acceptance for the AI-composer home.
 * 1) Demo mode: `npm run dev -- --port 3100` must be running at MARVIA_TEST_URL (default http://localhost:3100; 127.0.0.1 makes the Next dev server block dev resources, so the first step times out). Fixtures only, no API.
 * 2) Live mode (optional): a production build made with NEXT_PUBLIC_API_BASE=http://127.0.0.1:3199 running at MARVIA_LIVE_TEST_URL.
 *    This script starts a fake API on :3199 itself, so the browser receives real SSE over the network (docs/specs/chat-progress.md).
 *    Without MARVIA_LIVE_TEST_URL the live checks are reported as SKIP, never as PASS.
 * Uses installed Chrome by default; PLAYWRIGHT_CHANNEL can override the browser channel.
 * All other off-origin HTTP is blocked and recorded: the app must run on local assets only.
 *    The /watch globe's satellite tiles (tiles.maps.eox.at) are allowed only in the /watch contexts and are answered with a 1x1 stub tile inside the test, so no real request leaves the machine.
 * /watch map (docs/specs/watch-globe.md): Cesium globe, Leaflet fallback without WebGL, tour mode. Needs WebGL (SwiftShader flags are passed at launch); without WebGL the globe checks are SKIP.
 * Covers: 7 cargo states, axe WCAG A/AA, zero page errors and external HTTP, candidate validation, MOF evidence filter,
 * history filter + replay + retry isolation, composer keys/IME/limits, real-stage waiting, drawers, roles.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import http from 'node:http';
import {chromium} from 'playwright';
import AxeBuilder from '@axe-core/playwright';

const base = process.env.MARVIA_TEST_URL ?? 'http://localhost:3100';
const live = process.env.MARVIA_LIVE_TEST_URL ?? '';
const MOCK_PORT = Number(process.env.MARVIA_MOCK_API_PORT ?? 3199);
const mockOrigin = `http://127.0.0.1:${MOCK_PORT}`;
// 헤드리스 Chrome은 GPU가 없어 WebGL이 꺼질 수 있다. /watch의 Cesium 지구본은 WebGL이 필요하므로 소프트웨어 렌더러(SwiftShader)를 허용한다 (docs/specs/watch-globe.md 테스트)
const browser = await chromium.launch({channel: process.env.PLAYWRIGHT_CHANNEL ?? 'chrome', headless: true,
  args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist']});
const failures = [], pageErrors = [], external = [], checks = [], skipped = [];
const interactionsOnly = process.argv.includes('--interactions');
// 스크린샷과 결과 JSON은 실행한 폴더가 아니라 이 스크립트 옆(web/scripts/screenshots, .gitignore의 screenshots/)에 쓴다.
// 레포 루트 등 다른 곳에서 돌려도 작업 사본에 파일이 흩어지지 않게 한다. MARVIA_SCREENSHOT_DIR로 바꿀 수 있다 (10-08 QA G13)
const SHOTS = process.env.MARVIA_SCREENSHOT_DIR || fileURLToPath(new URL('./screenshots/', import.meta.url));
const shot = name => path.join(SHOTS, name);
fs.mkdirSync(SHOTS, {recursive: true});
const check = (name, detail = {}) => { checks.push({name, ...detail}); console.log('PASS', name); };
const skip = (name, why) => { skipped.push({name, why}); console.log('SKIP', name, '-', why); };
async function ready(page) { await page.evaluate(() => document.fonts.ready); await page.waitForTimeout(150); }
async function cargo(page, id) { await page.goto(`${base}/?fixture=${id}`); await page.locator('[data-testid="result"][aria-busy="false"]').waitFor(); await ready(page); }
// ?fixture= 없이 연다. 개발용 fixture 고정이 없어야 문장 속 예시 B/L대로 결과가 바뀐다 (lib/api.ts selectFixture)
async function home(page) { await page.goto(`${base}/`); await page.locator('[data-testid="result"][aria-busy="false"]').waitFor(); await ready(page); }
async function overflow(page, width) { const w = await page.evaluate(() => document.documentElement.scrollWidth); assert(w <= width, `overflow ${w}>${width}`); }
async function audit(page, name) {
  const results = await new AxeBuilder({page}).withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze();
  for (const v of results.violations) failures.push({page: name, id: v.id, impact: v.impact, nodes: v.nodes.map(n => ({target: n.target, summary: n.failureSummary}))});
}
async function newContext(width, allowed = [base]) {
  const context = await browser.newContext({viewport: {width, height: 1000}, deviceScaleFactor: 1, reducedMotion: 'reduce'});
  // 지도 바탕(OpenFreeMap)이 쓰는 호스트 하나만 예외로 허용한다. 스타일, 벡터 타일, 글꼴, 아이콘이 모두 이 호스트다.
  // 못 불러와도 지도는 해안선으로 되돌아가야 하므로(lib/basemap.ts) 이 허용이 e2e를 네트워크에 묶지는 않는다. 다른 외부 주소는 그대로 실패시킨다.
  const origins = [...allowed.map(u => new URL(u).origin), 'https://tiles.openfreemap.org'];
  await context.route('**/*', route => {
    const url = new URL(route.request().url());
    if (url.protocol.startsWith('http') && !origins.includes(url.origin)) { external.push(url.origin); return route.abort(); }
    return route.continue();
  });
  return context;
}
/* ---------- /watch 지도 확인용 도우미 (docs/specs/watch-globe.md) ---------- */
// 체험 모드(fixture)에서 지도에 올라야 하는 배 수. 목록 파일에서 직접 센다: 진행 중(active)이고 위치가 있는 줄만 핀이 된다.
// 체험 모드는 배만 지켜보는 줄이 늘 비어 있다 (lib/watchVessel.ts vesselWatchEnabled). 반출 끝난(done) 줄은 지도에서 빠진다
const WATCH_FIXTURE = JSON.parse(fs.readFileSync(new URL('../lib/fixtures/watch-map.json', import.meta.url), 'utf8'));
const LOCATED_SHIPS = WATCH_FIXTURE.items.filter(i => i.status === 'active' && i.lat != null && i.lng != null).length;
// 데모 둘러보기가 배를 옮기는 간격. 코드의 값을 그대로 읽어 테스트가 따로 낡지 않게 한다
const TOUR_TICK_MS = Number(/TOUR_TICK_MS\s*=\s*([\d_]+)/.exec(fs.readFileSync(new URL('../lib/tour.ts', import.meta.url), 'utf8'))?.[1].replaceAll('_', '')) || 10_000;
// Cesium은 번들과 첫 그리기가 무거워 다른 화면보다 오래 기다린다
const GLOBE_TIMEOUT = 45_000;
// 위성 타일 대신 돌려줄 1x1 PNG. 실제 EOX 요청을 막고도 바탕 불러오기가 성공한 것처럼 보이게 한다 (외부 접속 0건 유지)
const TILE_PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=', 'base64');
/** 이 브라우저가 WebGL을 켤 수 있는지. 못 켜면 지구본 확인은 PASS가 아니라 SKIP으로 남긴다 */
async function webglAvailable() {
  const context = await browser.newContext(); const page = await context.newPage();
  const ok = await page.evaluate(() => { const c = document.createElement('canvas'); return Boolean(c.getContext('webgl2') || c.getContext('webgl')); });
  await context.close(); return ok;
}
/** /watch용 컨텍스트. tour: 데모 둘러보기 켜기(sessionStorage), noWebgl: WebGL을 끈 브라우저 흉내 (EC-01) */
async function watchContext({tour = false, noWebgl = false} = {}) {
  // 위성 바탕 호스트(tiles.maps.eox.at)는 /watch 컨텍스트에서만 허용 목록에 둔다 (docs/specs/watch-globe.md 바탕 표). 다른 화면은 그대로 막는다
  const context = await newContext(1440, [base, 'https://tiles.maps.eox.at']);
  // 나중에 건 route가 먼저 받는다: 위성 타일 주소는 여기서 가짜 타일로 답해 실제 네트워크로 나가지 않는다
  await context.route(/^https:\/\/tiles\.maps\.eox\.at\//, route => route.fulfill({status: 200, contentType: 'image/png', body: TILE_PNG}));
  if (tour) await context.addInitScript(() => { try { window.sessionStorage.setItem('marvia-tour', '1'); } catch { /* 저장소를 못 쓰면 데모가 안 켜진다 */ } });
  if (noWebgl) await context.addInitScript(() => {
    // Cesium과 우리 WebGL 확인이 모두 getContext로 묻는다. webgl 계열만 null을 돌려 "WebGL 없음"으로 만든다
    for (const proto of [window.HTMLCanvasElement?.prototype, window.OffscreenCanvas?.prototype]) {
      if (!proto?.getContext) continue;
      const original = proto.getContext;
      proto.getContext = function (type, ...rest) { return /webgl/i.test(String(type)) ? null : original.call(this, type, ...rest); };
    }
  });
  return context;
}
/** /watch를 열고 지도가 expected 렌더러(globe | leaflet)로 뜰 때까지 기다린다. 다른 렌더러가 뜨면 이유를 알 수 있게 실패시킨다 */
async function openWatch(page, expected) {
  await page.goto(`${base}/watch`);
  const host = page.locator('.watch-map [data-renderer]').first();
  await host.waitFor({state: 'attached', timeout: GLOBE_TIMEOUT});
  assert.equal(await host.getAttribute('data-renderer'), expected, `/watch map should render with ${expected}`);
  return host;
}
/** 지도 칸 안의 핀 수와 클러스터가 묶은 배 수 (page.evaluate와 waitForFunction이 같이 쓰도록 바깥 값에 기대지 않는다).
 * 클러스터 숫자는 <b>N</b>, 없으면 title "배 N척"에서 읽는다 */
const shipsOnMap = () => {
  const root = document.querySelector('.watch-map');
  // 화면에 그려진 요소만 센다. 지구본은 핀·클러스터 요소를 DOM에 계속 두고 묶인 배나 지구 뒤편 배는 display:none으로 숨긴다
  // (Leaflet은 아예 떼어 낸다). 숨은 요소까지 세면 같은 배가 핀과 클러스터에 두 번 잡힌다. 둘 다에서 맞도록 렌더된 것(getClientRects)만 본다
  const shown = el => el.getClientRects().length > 0;
  const pins = [...root.querySelectorAll('.watch-pin')].filter(shown).length;
  let clustered = 0, clusters = 0;
  for (const c of [...root.querySelectorAll('.watch-cluster')].filter(shown)) {
    const n = parseInt(c.querySelector('b')?.textContent ?? '', 10) || parseInt(/(\d+)/.exec(c.getAttribute('title') ?? '')?.[1] ?? '', 10);
    clustered += Number.isFinite(n) ? n : 0; clusters++;
  }
  return {pins, clustered, clusters, ports: [...root.querySelectorAll('.port-marker')].filter(shown).length};
};
/** 핀과 클러스터가 위치 있는 배를 빠짐없이 덮을 때까지 기다린 뒤 그 값을 돌려준다 (Cesium은 첫 그리기 뒤에야 핀이 놓인다).
 * want가 null이면 하나라도 놓일 때까지만 기다린다 (데모는 배 수가 시각마다 달라진다) */
async function coverage(page, want) {
  // Node 쪽에서 짧게 되풀이해 읽는다 (페이지 안에서 문자열을 코드로 바꾸지 않아 화면의 보안 정책과 부딪히지 않는다)
  let r = await page.evaluate(shipsOnMap);
  for (let waited = 0; waited < 15_000 && (want == null ? r.pins + r.clustered === 0 : r.pins + r.clustered !== want); waited += 250) {
    await page.waitForTimeout(250); r = await page.evaluate(shipsOnMap);
  }
  return r;  // 시간 안에 안 맞으면 호출한 쪽 assert가 실제 값을 보여 주며 실패한다
}
/** 첫 번째로 보이는 핀 위에 마우스를 올린다. 핀 한가운데 좌표로 움직여 실제로 그 자리에서 받는지(가려지지 않았는지)까지 본다 */
async function hoverFirstPin(page, selector) {
  const box = await page.locator(`${selector}:visible`).first().boundingBox();
  assert(box, `${selector}: a visible pin to hover`);
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, {steps: 6});
}
const turns = page => page.locator('.chat-turn').count();
const input = page => page.locator('#chat-input');
const activeId = page => page.evaluate(() => document.activeElement?.id ?? '');

/* ---------- Fake API for live mode (docs/specs/chat-progress.md AC-04, AC-05) ---------- */
const mockCalls = [];
const sleep = ms => new Promise(r => setTimeout(r, ms));
const chatReply = reply => ({kind: 'message', reply, agent: null, request: null, vessels: [], place: null, ports: [], days: null});
function startMock() {
  const server = http.createServer(async (req, res) => {
    const cors = {'access-control-allow-origin': req.headers.origin ?? '*', 'access-control-allow-headers': 'content-type, authorization, accept', 'access-control-allow-methods': 'GET, POST, PUT, DELETE'};
    if (req.method === 'OPTIONS') { res.writeHead(204, cors); return res.end(); }
    const url = new URL(req.url, mockOrigin);
    let body = '';
    for await (const chunk of req) body += chunk;
    const json = (status, data) => { res.writeHead(status, {...cors, 'content-type': 'application/json'}); res.end(JSON.stringify(data)); };
    mockCalls.push(`${req.method} ${url.pathname}`);
    if (url.pathname === '/api/me') return json(200, {user_id: 'u-test', email: null, is_operator: false, profile_complete: true});
    if (url.pathname === '/api/metrics') return json(403, {detail: '운영자만 쓸 수 있습니다.'});
    if (url.pathname === '/api/history') return json(200, {items: [], available: false});
    if (url.pathname === '/api/chat') return json(200, chatReply('대체 경로 답입니다.'));
    if (url.pathname === '/api/chat/stream') {
      const message = JSON.parse(body || '{}').message ?? '';
      if (message.includes('OLDSERVER')) return json(404, {detail: 'Not Found'});
      res.writeHead(200, {...cors, 'content-type': 'text/event-stream', 'cache-control': 'no-cache'});
      const send = (event, data) => res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
      send('progress', {stage: 'understand'}); await sleep(300);
      if (message.includes('BROKEN')) { send('error', {status: 500, detail: '답을 만드는 중 문제가 생겼습니다. 잠시 뒤 다시 시도해 주세요.'}); return res.end(); }
      res.write(': ping\n\n');
      send('progress', {stage: 'customs'}); await sleep(300);
      send('progress', {stage: 'identify'}); await sleep(2500);  // 화면이 진행 중 단계를 보여 주는 동안 기다린다
      send('result', chatReply('스트림 결과입니다.'));
      return res.end();
    }
    json(404, {detail: 'Not Found'});
  });
  return new Promise(resolve => server.listen(MOCK_PORT, '127.0.0.1', () => resolve(server)));
}

let mock;
try {
  /* ===== Demo mode: 7 cargo states and pages at two widths ===== */
  for (const width of [1440, 375]) {
    const context = await newContext(width);
    const page = await context.newPage(); page.on('pageerror', e => pageErrors.push(e.message));
    for (const [i, id] of (interactionsOnly ? [] : ['ok', 'partial', 'insufficient', 'failed', 'unsupported', 'watch', 'act']).entries()) {
      await cargo(page, id);
      assert.equal(await page.locator('[data-testid="result"]').getAttribute('data-status'), ['watch', 'act'].includes(id) ? 'ok' : id);
      if (['insufficient', 'failed', 'unsupported'].includes(id)) assert.equal(await page.locator('.verdict-hero').count(), 0, `${id}: no verdict card`);
      if (['watch', 'act'].includes(id)) assert.equal(await page.locator(`.verdict-hero.${id}`).count(), 1, `${id}: server verdict shown`);
      if (id === 'partial') assert.equal(await page.locator('.map-stale').count(), 1);
      if (['failed', 'unsupported'].includes(id)) assert.equal(await page.locator('.unknown-note').count(), 1, `${id}: unknown values stay empty`);
      if (['ok', 'partial', 'watch', 'act'].includes(id)) await page.locator('.leaflet-overlay-pane svg').waitFor();
      await overflow(page, width); await audit(page, `${id}-${width}`);
      await page.screenshot({path: shot(`${String(i + 1).padStart(2, '0')}-${id}-${width}.png`), fullPage: true}); check(`${id} ${width}px`);
    }
    for (const [i, route] of (interactionsOnly ? [] : ['history', 'metrics', 'settings', 'about', 'admin']).entries()) {
      await page.goto(`${base}/${route}`); if (route === 'history') await page.locator('.hist-list > li').first().waitFor(); await ready(page); await overflow(page, width); await audit(page, `${route}-${width}`);
      await page.screenshot({path: shot(`${String(i + 8).padStart(2, '0')}-${route}-${width}.png`), fullPage: true}); check(`${route} ${width}px`);
    }

    // 설정은 화면 안 탭 4개, 관리는 탭 2개: 탭마다 접근성 검사 (탭을 바꿔도 가로 넘침이 없어야 한다)
    await page.goto(`${base}/settings`); await ready(page);
    const settingsTabs = page.locator('.st-tabs .st-tab'); assert.equal(await settingsTabs.count(), 4, 'settings has 4 in-page tabs (조회 이력은 왼쪽 메뉴에 있음)');
    for (let t = 0; t < 4; t++) { await settingsTabs.nth(t).click(); await ready(page); await overflow(page, width); if (!interactionsOnly) await audit(page, `settings-tab${t}-${width}`); }
    await page.goto(`${base}/admin`); await ready(page);
    const adminTabs = page.getByRole('tab'); assert.equal(await adminTabs.count(), 2, 'admin has 2 in-page tabs');
    for (let t = 0; t < 2; t++) { await adminTabs.nth(t).click(); await ready(page); await overflow(page, width); if (!interactionsOnly) await audit(page, `admin-tab${t}-${width}`); }
    check(`settings 4 tabs + admin 2 tabs ${width}`);

    // AI 입력이 첫 화면의 중심이다: 입력창과 보내기 버튼이 스크롤 없이 보인다 (구현작업서 10절)
    await home(page);
    const box = await input(page).boundingBox(), send = await page.getByRole('button', {name: 'AI에게 질문 보내기'}).boundingBox();
    assert(box && send && box.y + box.height <= 1000 && send.y + send.height <= 1000, 'composer and send visible on first screen');
    assert.equal(await input(page).getAttribute('maxlength'), '500');
    check(`composer is the first-screen focus ${width}`);

    // 한글 조합 중 Enter와 Shift+Enter는 보내지 않는다. (예시 문장 채우기는 대화가 빈 첫 화면에만 있고, 데모는 첫 화면에 결과를 자동으로 열어 그 화면이 없다 → live 블록에서 확인)
    const before = await turns(page);
    await input(page).focus();
    await input(page).fill('MRVDEMO1101');
    await page.evaluate(() => document.getElementById('chat-input').dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', isComposing: true, bubbles: true, cancelable: true})));
    await input(page).press('Shift+Enter');
    await page.waitForTimeout(300);
    assert.equal(await turns(page), before, 'IME Enter / Shift+Enter must not send'); assert.match(await input(page).inputValue(), /\n/);
    check(`IME Enter and Shift+Enter do not send ${width}`);

    // Enter 전송, 기다리는 동안 중복 전송 차단과 "이전 결과" 안내, 서버 판정 표시
    await input(page).fill('MRVDEMO1107 지금 조치해야 해?'); await input(page).press('Enter');
    await page.locator('.pending-card').waitFor();
    assert(await page.getByRole('button', {name: 'AI에게 질문 보내기'}).isDisabled());
    assert(await page.locator('.stale-notice').isVisible(), 'previous result is labelled while waiting');
    assert.equal(await page.locator('.pending-steps').count(), 0, 'demo has no server stages and must not invent them');
    await page.locator('[data-testid="result"][aria-busy="false"] .verdict-hero.act').waitFor();
    assert.equal(await turns(page), before + 2);
    check(`Enter sends, duplicate blocked, no invented stages ${width}`);

    // 움직임 줄이기: 결과 등장 애니메이션이 꺼진다
    assert.equal(await page.evaluate(() => getComputedStyle(document.querySelector('.result')).animationName), 'none');
    check(`reduced motion removes entrance animation ${width}`);

    // 입력창은 항상 아래에 고정된다: 대화 칸을 맨 아래로 내려도, 페이지는 스크롤되지 않아도 입력창이 보이고 "이어서 물어보기" 버튼은 필요 없다
    await page.evaluate(() => document.querySelector('.chat-scroll').scrollTo(0, document.querySelector('.chat-scroll').scrollHeight));
    const pinned = await input(page).boundingBox(), vh = page.viewportSize().height;
    assert(pinned && pinned.y >= 0 && pinned.y + pinned.height <= vh, 'composer stays visible while the conversation is scrolled');
    assert.equal(await page.getByRole('button', {name: 'AI에게 이어서 물어보기'}).count(), 0, 'no back-to-composer button when composer is pinned');
    if (width === 1440) assert(await page.evaluate(() => document.documentElement.scrollHeight <= window.innerHeight + 1), 'page itself does not scroll at 1440x1000');
    await input(page).focus(); assert.equal(await activeId(page), 'chat-input');
    check(`composer stays pinned, no back-to-composer button ${width}`);

    // 되묻기: 아무것도 고르지 않으면 막고, 후보를 고르면 정상 결과
    await cargo(page, 'insufficient'); await page.getByRole('button', {name: '확인하고 판정하기'}).click(); assert(await page.locator('.candidate-section [role=alert]').isVisible());
    await page.locator('.cand-row').first().click(); assert(await page.getByRole('radio').first().isChecked(), 'clicking a candidate row selects its radio'); await page.getByRole('button', {name: '확인하고 판정하기'}).click(); await page.locator('[data-status="ok"][aria-busy="false"]').waitFor();
    check(`candidate validation + resolution ${width}`);

    // 부분 결과: 해수부 근거만 보기는 화면 필터이고 새 판정이 아니다. 원본 값 펼침
    await cargo(page, 'partial'); await page.getByRole('button', {name: '해수부 근거만 보기'}).click(); await page.locator('.more-fold[open] .filter-notice').waitFor({state: 'visible', timeout: 3000}); // 근거는 접힌 "펼쳐 보기" 안에 있어, 필터가 펼치고 안내를 보인다
    assert(await page.locator('.filter-notice').isVisible());
    assert.equal(await page.locator('.evidence-source').filter({hasNotText: '해양수산부'}).count(), 0);
    await page.getByRole('button', {name: '전체 보기'}).click(); await page.locator('.evidence-item summary').first().click(); assert(await page.locator('.evidence-item .evidence-detail').first().isVisible());
    check(`MOF filter + raw evidence ${width}`);

    // 지원 범위 밖: 질문 고치기는 입력창으로 보낸다
    await cargo(page, 'unsupported'); await page.getByRole('button', {name: '질문 고치기'}).click(); assert.equal(await activeId(page), 'chat-input');
    check(`unsupported returns to composer ${width}`);

    // 입항 선박 검색 → 표 5열 → 선명을 누르면 표 아래 카드(드로어 아님)가 그 배로 바뀐다. 여러 번 바꿔도 지도는 하나만 남는다
    await home(page); await input(page).fill('중국에서 부산 들어오는 배 보여줘'); await input(page).press('Enter');
    await page.locator('.vessel-results').waitFor();
    assert.equal(await page.locator('.vessel-results thead th').count(), 5);
    // 휴대폰: 상세는 아래 시트다. 입력창에 포커스가 있으면 접혀 있어서 손잡이로 편다
    const handle = page.locator('.sheet-handle');
    if (await handle.isVisible() && (await handle.getAttribute('aria-expanded')) === 'false') await handle.click();
    const links = page.locator('.ship-link'); assert((await links.count()) >= 2, 'at least two vessels to switch between');
    for (let n = 0; n < 4; n++) {
      const link = links.nth(n % 2), name = (await link.textContent()).trim();
      await link.click();
      await page.locator('.vessel-results .vd .vessel-detail-info').waitFor();
      assert.equal(await page.locator('.vd-head h3').textContent(), name, 'inline detail follows the clicked vessel');
      assert.equal(await link.getAttribute('aria-pressed'), 'true');
      assert.equal(await page.getByRole('dialog').count(), 0, 'vessel detail is inline, not a dialog');
      assert(!(await page.evaluate(() => document.querySelector('.app-shell')?.hasAttribute('inert'))), 'page is not inert for an inline card');
      if (n === 0 && width === 1440 && !interactionsOnly) await audit(page, `vessel-detail-${width}`);
    }
    await page.locator('.vd-more summary').click(); await page.locator('.vd-more dl').waitFor({state: 'visible', timeout: 3000}); // 펼침 애니메이션 때문에 즉시 확인하지 않고 기다린다
    assert(await page.locator('.vd-more[open] dl').isVisible(), 'detail 펼쳐 보기 opens');
    assert(await page.locator('.vessel-results .leaflet-container').count() <= 1, 'only the selected vessel keeps a map');
    check(`vessel search + inline detail switch/cleanup ${width}`);

    // 서류 사진: 입력창에 붙이면 칩만 보이고 확인 화면은 없다. 붙이기만 해서는 보내지 않고,
    // 보내면 사진에서 B/L을 읽어 바로 조회한다. 화면에는 가린 번호만 말한다 (데모 fixture: MRVDEMO1101, 가공값)
    const turnCount = await turns(page);
    // 휴대폰: 펼친 상세 시트가 입력창을 덮으므로 손잡이로 접는다 (넓은 화면에는 손잡이가 없다)
    if (await handle.isVisible() && (await handle.getAttribute('aria-expanded')) === 'true') await handle.click();
    await input(page).fill('');
    const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=', 'base64');
    const photo = {name: 'notice.png', mimeType: 'image/png', buffer: png};
    await page.locator('.composer input[type=file]').setInputFiles(photo); await page.locator('.attach-chip').waitFor();
    assert.equal(await page.getByRole('dialog').count(), 0, 'attaching opens no confirmation screen');
    assert.equal(await turns(page), turnCount, 'attaching alone never sends');
    if (width === 1440 && !interactionsOnly) await audit(page, `composer-attachment-${width}`);
    await page.getByRole('button', {name: '첨부한 notice.png 빼기'}).click();
    assert.equal(await page.locator('.attach-chip').count(), 0, 'chip remove button drops the attachment');
    await page.locator('.composer input[type=file]').setInputFiles(photo); await page.locator('.attach-chip').waitFor();
    await page.getByRole('button', {name: 'AI에게 질문 보내기'}).click();  // 글 없이 사진만 보내도 된다
    await page.locator('.chat-turn.user .chat-attach-img').last().waitFor();
    await page.waitForFunction(n => document.querySelectorAll('.chat-turn').length >= n + 3, turnCount);  // 사용자 + 코드 문장 + 답
    const note = await page.locator('.chat-turn.assistant').nth(-2).innerText();
    assert.match(note, /MRV\*+101/); assert.doesNotMatch(note, /MRVDEMO1101/, 'note shows only the masked B/L');
    await page.locator('.pending-card').waitFor({state: 'detached'});
    check(`composer attachment: chip, remove, send image-only, masked note + lookup ${width}`);

    // 이력: 필터, 저장된 판정 재생, 다시 조회해도 재생 스냅샷 조건 그대로
    await page.getByRole('link', {name: '조회 이력'}).first().click(); await page.locator('.hist-list > li').first().waitFor();
    await page.getByLabel('B/L 번호 검색').fill('ZZZZ'); assert.equal(await page.locator('.hist-list > li').count(), 0);
    await page.getByRole('button', {name: '조건 초기화'}).click(); await page.getByLabel('상태별로 거르기').selectOption('failed');
    await page.getByRole('button', {name: /저장된 판정 열기/}).click(); await page.locator('[data-status="failed"]').waitFor();
    await page.getByText('저장된 이력', {exact: true}).waitFor({state: 'attached'});
    if (await handle.isVisible() && (await handle.getAttribute('aria-expanded')) === 'false') await handle.click();
    assert(await page.getByText('저장된 이력', {exact: true}).isVisible(), 'replay chip is shown');
    await page.getByRole('button', {name: '다시 조회하기', exact: true}).click(); await page.locator('[data-testid="result"][aria-busy="false"]').waitFor();
    assert.equal(await page.locator('[data-testid="result"]').getAttribute('data-status'), 'failed');
    check(`history filter + replay + retry isolation ${width}`);
    await context.close();
  }

  // 중간 폭: 가로 넘침 없음 (큰 표만 자기 칸에서 스크롤)
  for (const width of interactionsOnly ? [] : [1024, 768, 390]) {
    const context = await newContext(width); const page = await context.newPage(); page.on('pageerror', e => pageErrors.push(e.message));
    await cargo(page, 'ok'); await overflow(page, width); await page.screenshot({path: shot(`20-ok-${width}.png`), fullPage: true});
    await input(page).fill('중국에서 부산 들어오는 배 보여줘'); await input(page).press('Enter'); await page.locator('.vessel-results').waitFor(); await overflow(page, width);
    check(`no page overflow ${width}px (cargo + vessel table)`); await context.close();
  }

  /* ===== /watch 지도 (docs/specs/watch-globe.md): 지구본 AC-01·02·06, Leaflet 되돌림 AC-03, 데모 둘러보기 AC-05 ===== */
  if (!(await webglAvailable())) skip('watch globe (Cesium canvas, pins, tooltip, idle, base toggle, tour)', 'this browser cannot create a WebGL context');
  else {
    // AC-01·AC-02·AC-06: 지구본이 뜨고, 핀이 위치 있는 배를 모두 덮고, 마우스·목록·단추가 되고, 가만히 두면 다시 그리지 않는다
    const context = await watchContext(); const page = await context.newPage(); page.on('pageerror', e => pageErrors.push(e.message));
    const host = await openWatch(page, 'globe'); await ready(page);
    assert.equal(await page.locator('.watch-map .watch-globe canvas').count(), 1, 'globe has exactly one Cesium canvas');
    assert.equal(await page.locator('.watch-map .leaflet-container').count(), 0, 'no Leaflet map while the globe is shown');
    const found = await coverage(page, LOCATED_SHIPS);
    assert.equal(found.pins + found.clustered, LOCATED_SHIPS, `pins (${found.pins}) + clustered ships (${found.clustered}) must cover the ${LOCATED_SHIPS} located ships in the fixture`);
    assert.equal(found.ports, 1, 'exactly one Busan port marker');
    check(`watch globe: canvas, ${LOCATED_SHIPS} located ships covered by pins + clusters, one port marker`);

    // 핀에 마우스를 올리면 말풍선이 뜬다 (선명, 가린 B/L 등 지금 Leaflet과 같은 칸)
    if (found.pins > 0) {
      await hoverFirstPin(page, '.globe-overlay .watch-pin');
      const tip = page.locator('.globe-overlay .watch-tooltip:visible .watch-tip').first();
      await tip.waitFor({state: 'visible', timeout: 5000});
      assert.match(await tip.innerText(), /TE\*{4}\d\d/, 'tooltip shows the masked B/L');
      check('watch globe: hovering a pin shows the tooltip');
    } else skip('watch globe: hovering a pin shows the tooltip', 'every ship is inside a cluster at the first camera');
    await page.mouse.move(2, 2, {steps: 4});
    await page.locator('.globe-overlay .watch-tooltip:visible').first().waitFor({state: 'hidden', timeout: 3000});

    // 목록 줄을 누르면 그 배로 간다: 혼자면 말풍선, 묶여 있으면 상세 창
    await page.locator('.watch-list li button').first().click();
    await page.locator('.globe-overlay .watch-tooltip:visible .watch-tip').or(page.locator('.cluster-panel')).first().waitFor({state: 'visible', timeout: 8000});
    if (await page.locator('.cluster-panel').count()) await page.locator('.cluster-panel [aria-label="닫기"]').click();
    check('watch globe: clicking a list row focuses the ship (tooltip or cluster panel)');

    // 모든 배 보기, 경로 끄고 켜기: 지구본은 그대로 동작한다
    await page.locator('.watch-fit').click();
    const trackToggle = page.locator('.watch-track-toggle input');
    await trackToggle.uncheck(); await trackToggle.check();
    assert.equal(await host.getAttribute('data-renderer'), 'globe', 'globe stays after fit-all and route toggle');
    check('watch globe: fit-all and route toggle keep the globe');

    // AC-06: 카메라가 멈춘 뒤 3초 동안 조작이 없으면 Cesium이 다시 그린 횟수(data-renders)가 늘지 않는다 (requestRenderMode)
    await page.mouse.move(2, 2, {steps: 4}); await page.waitForTimeout(2500);
    const renders = async () => Number(await host.getAttribute('data-renders'));
    const before = await renders(); assert(Number.isFinite(before) && before > 0, `data-renders is a positive count (got ${before})`);
    await page.waitForTimeout(3000);
    assert.equal(await renders(), before, 'idle globe must not render again');
    check('watch globe: idle page does not re-render (data-renders stays)');

    // 위성 / 지도 바탕 단추: 처음은 위성, 지도를 누르면 지도가 눌린 상태가 된다
    const group = page.locator('.watch-map [role="group"][aria-label="지도 바탕"]');
    const satBtn = group.locator('button', {hasText: /^위성$/}), mapBtn = group.locator('button', {hasText: /^지도$/});
    assert.equal(await satBtn.getAttribute('aria-pressed'), 'true', 'satellite is the first base');
    await mapBtn.click();
    assert.equal(await mapBtn.getAttribute('aria-pressed'), 'true', 'map base pressed after click');
    assert.equal(await satBtn.getAttribute('aria-pressed'), 'false', 'satellite base released');
    check('watch globe: base toggle satellite -> map');

    await page.waitForTimeout(500);
    await overflow(page, 1440); if (!interactionsOnly) await audit(page, 'watch-globe-1440');
    await page.screenshot({path: shot('40-watch-globe-1440.png'), fullPage: true}); check(`watch globe 1440px: no overflow${interactionsOnly ? '' : ', axe'}`);
    await context.close();

    // AC-05: 데모 둘러보기는 10초마다 위치만 바꾸고 지구본(캔버스)은 새로 만들지 않는다
    {
      const context = await watchContext({tour: true}); const page = await context.newPage(); page.on('pageerror', e => pageErrors.push(e.message));
      await page.goto(`${base}/watch`);
      await page.locator('html[data-tour="on"]').waitFor({state: 'attached', timeout: GLOBE_TIMEOUT});  // TourGate가 앱을 한 번 다시 그린 뒤 것만 센다
      await page.locator('.watch-map [data-renderer="globe"]').waitFor({state: 'attached', timeout: GLOBE_TIMEOUT});
      await page.waitForTimeout(2000);
      const canvas = await page.locator('.watch-map .watch-globe canvas').first().elementHandle();
      assert(canvas, 'tour mode: globe canvas exists');
      const first = await coverage(page, null); assert(first.pins + first.clustered > 0, 'tour mode: ships on the globe');
      await page.waitForTimeout(TOUR_TICK_MS + 1500);  // 한 칸보다 조금 더 기다려 위치 갱신을 한 번 겪는다
      assert(await canvas.evaluate(el => el.isConnected), 'tour tick must not recreate the globe canvas');
      assert.equal(await page.locator('.watch-map .watch-globe canvas').count(), 1, 'tour mode: still exactly one canvas');
      assert.equal(await page.locator('.watch-map [data-renderer]').first().getAttribute('data-renderer'), 'globe');
      const after = await coverage(page, null); assert(after.pins + after.clustered > 0, 'tour mode: ships still on the globe after a tick');
      check('watch globe tour mode: canvas survives a position tick, pins stay');
      await context.close();
    }
  }

  // AC-03·EC-01: WebGL이 없으면 지금 Leaflet 평면 지도로 그린다. 빈 칸이나 오류 화면 없이 핀과 말풍선이 같이 나온다
  {
    const context = await watchContext({noWebgl: true}); const page = await context.newPage(); page.on('pageerror', e => pageErrors.push(e.message));
    await openWatch(page, 'leaflet'); await ready(page);
    await page.locator('.watch-map .leaflet-container').waitFor({timeout: 15_000});
    assert.equal(await page.locator('.watch-map .watch-globe').count(), 0, 'no globe element in the fallback');
    const found = await coverage(page, LOCATED_SHIPS);
    assert.equal(found.pins + found.clustered, LOCATED_SHIPS, `fallback: pins (${found.pins}) + clustered ships (${found.clustered}) must cover the ${LOCATED_SHIPS} located ships`);
    assert(found.pins + found.clusters > 0, 'fallback: at least one pin or cluster');
    assert.equal(found.ports, 1, 'fallback: one port marker');
    if (found.pins > 0) {
      await hoverFirstPin(page, '.watch-map .watch-pin');
      await page.locator('.watch-map .leaflet-tooltip.watch-tooltip .watch-tip').first().waitFor({state: 'visible', timeout: 5000});
    }
    assert.equal(await page.locator('.watch-map [role="alert"]').count(), 0, 'fallback shows no error');
    await overflow(page, 1440);
    await page.screenshot({path: shot('41-watch-leaflet-fallback-1440.png'), fullPage: true});
    check('watch fallback without WebGL: Leaflet map with pins, port marker, tooltip');
    await context.close();
  }

  /* ===== Live mode against the fake API: roles, real SSE stages, fallback, error ===== */
  if (!live) {
    skip('live mode (roles, SSE stages, fallback, error, history unavailable)', 'MARVIA_LIVE_TEST_URL not set');
  } else {
    mock = await startMock();
    for (const width of [1440, 375]) {
      const context = await newContext(width, [live, mockOrigin]); const page = await context.newPage(); page.on('pageerror', e => pageErrors.push(e.message));
      await page.goto(live); await input(page).waitFor(); await ready(page);
      assert.equal(await page.getByRole('link', {name: '평가 지표'}).count(), 0); assert.equal(await page.getByRole('link', {name: '관리'}).count(), 0);
      assert.equal(await page.locator('.feature-tiles button').count(), 3, 'first visit shows three real tasks');
      assert.equal(await page.locator('[data-testid="result"]').count(), 0, 'live mode never auto-runs a query');
      await overflow(page, width); if (!interactionsOnly) await audit(page, `live-home-${width}`);
      await page.screenshot({path: shot(`30-live-home-${width}.png`), fullPage: true});
      check(`live: user menu hides operator pages, idle first visit ${width}`);

      // 예시는 채우기만 하고 보내지 않는다 (대화가 빈 첫 화면)
      await page.locator('.chat-examples button').first().click();
      assert((await input(page).inputValue()).length > 0, 'example fills the composer'); assert.equal(await turns(page), 0, 'example does not send');
      await input(page).fill('');
      check(`live: example fills only ${width}`);

      await page.goto(`${live}/metrics`); await page.getByText('운영자만 볼 수 있는 화면이에요.').waitFor();
      check(`live: direct /metrics is blocked for a normal user ${width}`);

      await page.goto(live); await input(page).waitFor();
      await input(page).fill('STREAM 테스트'); await input(page).press('Enter');
      await page.locator('.pending-steps li').nth(2).waitFor();
      const steps = await page.locator('.pending-steps li').evaluateAll(els => els.map(e => `${e.className}:${e.textContent.replace(/\s*(완료|진행 중)$/, '')}`));
      assert.deepEqual(steps, ['done:질문 이해', 'done:통관 정보 조회', 'current:선박 식별'], 'only server stages, last one in progress');
      if (width === 1440) await page.screenshot({path: shot('31-live-stages-1440.png')});
      await page.locator('.chat-turn.assistant', {hasText: '스트림 결과입니다.'}).waitFor();
      assert.equal(await page.locator('.pending-card').count(), 0);
      check(`live: SSE stages appear as the server sends them ${width}`);

      const calls = mockCalls.filter(c => c === 'POST /api/chat').length;
      await input(page).fill('OLDSERVER 질문'); await input(page).press('Enter');
      await page.locator('.chat-turn.assistant', {hasText: '대체 경로 답입니다.'}).waitFor();
      assert.equal(mockCalls.filter(c => c === 'POST /api/chat').length, calls + 1, 'falls back to /api/chat once');
      check(`live: missing stream endpoint falls back to /api/chat ${width}`);

      await input(page).fill('BROKEN 질문'); await input(page).press('Enter');
      await page.locator('.chat-turn.assistant', {hasText: '답을 만드는 중 문제가 생겼습니다.'}).waitFor();
      assert.equal(await input(page).inputValue(), 'BROKEN 질문', 'input restored after an error');
      check(`live: stream error is shown and input restored ${width}`);

      await page.goto(`${live}/history`); await page.getByText('이력을 불러올 수 없어요.').waitFor();
      check(`live: history available:false is not shown as empty ${width}`);
      await context.close();
    }
  }

  assert.deepEqual(pageErrors, [], 'Browser errors'); assert.deepEqual(external, [], 'Unexpected external HTTP');
  assert.equal(failures.length, 0, `Accessibility violations: see ${shot('verification.json')}`);
  console.log(`PASS: ${checks.length} checks, ${skipped.length} skipped; zero page errors and external HTTP requests. ${interactionsOnly ? 'Interaction-only run; accessibility not checked.' : 'Zero axe WCAG A/AA violations.'}`);
} finally {
  fs.writeFileSync(shot('verification.json'), JSON.stringify({checks, skipped, accessibilityViolations: failures, pageErrors, externalRequests: external}, null, 2));
  mock?.close();
  await browser.close();
}
