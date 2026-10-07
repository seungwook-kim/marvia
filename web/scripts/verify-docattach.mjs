/** 채팅 첨부 사진의 다음 동작(lib/docAttach.ts decideAttachment) 회귀 검사 (10-08 QA D4·D6·D7·D8).
 * lib/docAttach.ts를 그대로 옮겨 실행한다. 서버·키·외부 API 없이 돈다. verify-contract.mjs 끝에서 불린다.
 *   node scripts/verify-docattach.mjs   (web/ 에서)
 * B/L 번호는 전부 가공 값이다.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import Module from 'node:module';
import ts from 'typescript';

const root = process.cwd();
const readText = (f) => fs.readFileSync(f, 'utf8').replace(/\r\n/g, '\n');
const tsOptions = {compilerOptions: {module: ts.ModuleKind.CommonJS, esModuleInterop: true, target: ts.ScriptTarget.ES2022}};
Module._extensions['.ts'] = (m, f) => m._compile(ts.transpileModule(readText(f), tsOptions).outputText, f);
const file = path.join(root, 'lib/docAttach.ts');
const mod = new Module(file); mod.filename = file; mod.paths = Module._nodeModulePaths(path.dirname(file));
mod._compile(ts.transpileModule(readText(file), tsOptions).outputText, file);
const {decideAttachment, typedBls, flaggedLine} = mod.exports;

const f = (value, checks = []) => ({value, checks});
const BL = 'QAMV26100001';  // 가공 번호
const res = ({fields = {}, ...extra} = {}) => ({status: 'ok', needs_review: false, trace_id: 't', mrn: null,
  ocr: {lines: 1, rotation: 0, runs: 1, ocr_ms: 1},
  fields: {bl_no: f(BL), vessel: f('SEA STAR'), voyage: f('0925W'), eta: f('2026-10-20'), pol: f('X'), pod: f('Y'),
    item: f('Z'), etd: f('2026-10-10'), ...fields},
  bl_status: 'ok', important: ['vessel'], facts_saved: [], flagged: [], bl_candidates: [], ...extra});

// D7: 하이픈으로 끊어 쓴 같은 번호는 하나로 친다. 채팅이 알아듣게 붙인 번호로 바꿔 보낸다 (B/L이 두 개가 되지 않는다)
let d = decideAttachment(res(), 'QAMV-2610-0001 봐줘', 500);
assert.equal(d.message, `${BL} 봐줘`);
assert(d.hints && d.document);
// 띄어 쓴 같은 번호는 채팅이 B/L로 못 읽으므로 붙인 번호를 앞에 넣는다
d = decideAttachment(res(), 'QAMV 2610 0001 봐줘', 500);
assert(d.message.startsWith(`${BL} `));
// 소문자로 쓴 같은 번호: 그대로 보낸다 (두 번 넣지 않는다)
d = decideAttachment(res(), 'qamv26100001 운임 누가 내?', 500);
assert.equal(d.message.toUpperCase().split(BL).length - 1, 1);
// 다른 B/L을 썼으면 사진 번호를 붙이지 않고 알린다. 사진의 항차·날짜도 채우지 않는다
d = decideAttachment(res(), 'QAMV26109999 이거 어디야', 500);
assert.equal(d.message, 'QAMV26109999 이거 어디야');
assert(!d.message.includes(BL) && d.hints === null && d.document === null);
assert(/달라요/.test(d.note) && !d.note.includes(BL) && !d.note.includes('QAMV26109999'));
// 하이픈 쓴 다른 B/L도 다른 번호로 본다
d = decideAttachment(res(), 'QAMV-2610-9999 이거', 500);
assert(!d.message.includes(BL) && /달라요/.test(d.note));
// 날짜·전화·MRN·컨테이너 번호는 B/L이 아니다. 사진 B/L을 붙인다
for (const t of ['2026-10-20 도착이래', '20261020 도착', '051-000-0000 으로 전화', 'MRN 26QAMV0001I', 'TEMU1234567 컨테이너']) {
  d = decideAttachment(res(), t, 500);
  assert(d.message.startsWith(BL), t);
}
assert.deepEqual(typedBls('QAMV-2610-0001 2026-10-20 TEMU1234567').map(t => t.bl), [BL]);

// D6: ok인데 다른 칸이 검사에 걸렸으면 이름만 알린다 (값은 말하지 않는다)
d = decideAttachment(res({flagged: ['eta', 'contact_phone']}), '', 500);
assert(d.note.includes('확인이 필요한 칸: 입항 예정일, 연락처 전화'));
assert(!d.note.includes('2026-10-20'));
// 옛 응답(flagged 없음)은 fields로 같은 규칙을 계산한다. 값이 없는 칸(missing)은 넣지 않는다
const legacy = res({fields: {eta: f('2026-10-21', ['ambiguous']), item: f(null, ['missing'])}});
delete legacy.flagged;
assert(decideAttachment(legacy, '', 500).note.includes('확인이 필요한 칸: 입항 예정일.'));
assert.equal(flaggedLine([]), null);

// D4: 서로 다른 B/L이 여럿이면 자동 조회하지 않고 가린 값들을 보여 준다
d = decideAttachment(res({bl_status: 'uncertain', fields: {bl_no: f(BL, ['ambiguous'])}, bl_candidates: ['QAM******01', 'QAM******99']}), '', 500);
assert.equal(d.message, null);
assert(d.note.includes('여러 개') && d.note.includes('QAM******99') && !d.note.includes(BL));

// D8: 체험 모드 예시 서류(lib/fixtures/document.json)는 서버 규칙대로 ok라 사진 첨부가 조회를 보낸다
const demo = JSON.parse(readText(path.join(root, 'lib/fixtures/document.json')));
assert.equal(demo.bl_status, 'ok');
d = decideAttachment(demo, '', 500);
assert(d.message && d.message.startsWith(demo.fields.bl_no.value));
assert(d.note.includes('확인이 필요한 칸'));

console.log('PASS: docAttach (hyphen/space B/L, different B/L, flagged names, multiple B/L, demo fixture ok).');
