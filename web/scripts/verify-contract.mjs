/** Check local fixtures against a read-only AST snapshot of the actual Python contracts.
 * Also exercise replay context and date mismatch so fixed snapshots cannot mislead users.
 * No Python runtime, environment, original repository or external API is needed.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import Module from 'node:module';
import ts from 'typescript';
const root=process.cwd();
// Windows 작업 사본(core.autocrlf)은 줄끝이 CRLF라 LF 기준 정규식이 못 맞는다. 읽을 때 LF로 맞춘다
const readText=(f)=>fs.readFileSync(f,'utf8').replace(/\r\n/g,'\n');
const {definitions}=JSON.parse(readText('scripts/backend-contract.json'));
function validate(value,schema,at){
 if(schema.ref)return validate(value,definitions[schema.ref],at);
 if(schema.anyOf){for(const s of schema.anyOf){try{validate(value,s,at);return;}catch{}}throw new Error(`${at}: no union member matches`);}
 if(schema.enum){assert(schema.enum.includes(value),`${at}: invalid enum ${value}`);return;}
 if(schema.type==='null'){assert.equal(value,null,at);return;}
 if(schema.type==='array'){assert(Array.isArray(value),at);value.forEach((v,i)=>validate(v,schema.items,`${at}[${i}]`));return;}
 if(schema.type==='object'){assert(value&&typeof value==='object'&&!Array.isArray(value),at);if(schema.properties){assert.deepEqual(Object.keys(value).sort(),schema.required.slice().sort(),`${at}: exact fields`);for(const [key,s] of Object.entries(schema.properties))validate(value[key],s,`${at}.${key}`);}return;}
 assert.equal(typeof value,schema.type==='integer'?'number':schema.type,at);
 if(schema.type==='integer')assert(Number.isInteger(value),at);
 if(schema.format)assert(!Number.isNaN(Date.parse(value)),`${at}: date format`);
}
for(const id of ['ok','partial','insufficient','failed','unsupported','watch','act']){
 const r=JSON.parse(readText(`lib/fixtures/${id}.json`));validate(r,definitions.AgentResponse,id);
 if(r.status==='insufficient'){assert.equal(r.verdict,null);assert(r.missing_inputs.length||r.vessel_candidates.length);}
 if(r.verdict==='watch'||r.verdict==='act')assert(r.reasons.some(s=>s.triggered));
 if(r.days_margin!==null)assert(r.evidence.some(e=>e.field==='days_margin'&&Number(e.value)===r.days_margin));
}
validate(JSON.parse(readText('lib/fixtures/history.json')),definitions.HistoryResponse,'history');
// 서류 사진 응답과 화물 비서 기억 (schemas/document.py · schemas/memory.py). 칸이 하나라도 다르면 실패한다
const documentFixture=JSON.parse(readText('lib/fixtures/document.json'));validate(documentFixture,definitions.DocumentResponse,'document');
const memoryFixture=JSON.parse(readText('lib/fixtures/memory.json'));validate(memoryFixture,definitions.MemoryResponse,'memory');
// 화면에 가는 기억에는 가린 번호만 있다. 가리지 않은 예시 B/L(MRVDEMO + 숫자)이 섞이면 안 된다 (SDD 3.5)
assert(!/MRVDEMO\d{4}/.test(JSON.stringify(memoryFixture)),'memory fixture must not contain an unmasked B/L');
assert.equal(new Set(memoryFixture.items.map(i=>i.id)).size,memoryFixture.items.length,'memory item ids must be unique');
// In-memory transpilation runs exactly lib/api.ts; it does not create duplicate test code.
// api.ts가 다른 .ts(lib/auth.ts)를 불러도 같은 방식으로 옮겨 읽게 한다
const tsOptions={compilerOptions:{module:ts.ModuleKind.CommonJS,esModuleInterop:true,target:ts.ScriptTarget.ES2022}};
Module._extensions['.ts']=(m,f)=>m._compile(ts.transpileModule(readText(f),tsOptions).outputText,f);
const filename=path.join(root,'lib/api.ts');
const compiled=ts.transpileModule(readText(filename),{compilerOptions:{module:ts.ModuleKind.CommonJS,esModuleInterop:true,target:ts.ScriptTarget.ES2022}}).outputText;
const mod=new Module(filename);mod.filename=filename;mod.paths=Module._nodeModulePaths(path.dirname(filename));mod._compile(compiled,filename);
const api=mod.exports;process.env.NODE_ENV='development';const {demo}=api.getPresentationData();
const req=id=>structuredClone(demo.choices.find(c=>c.id===id).request);
api.selectFixture('partial');assert.equal((await api.getAgentResponse(req('partial'))).status,'partial');
const replay=api.getReplaySnapshot('fixture-failed-20261020');assert(replay);assert.equal((await api.getAgentResponse(replay.request)).status,'failed');
api.getReplaySnapshot('fixture-ok-20261020');const followup=await api.getAgentResponse({...req('ok'),follow_up_question:'근거는?'});assert.equal(followup.status,'ok');assert(followup.evidence.length>0);
await assert.rejects(()=>api.getAgentResponse({...req('ok'),sale_due_date:'2026-10-01'}),/저장된 예시와 조회 조건/);
await assert.rejects(()=>api.getAgentResponse({...req('ok'),bl_type:'HOUSE'}),/저장된 예시와 조회 조건/);
assert.deepEqual(await api.getHistory('different-session'),{items:[],available:true});
const candidates=JSON.parse(readText('lib/fixtures/insufficient.json')).vessel_candidates;
assert.equal((await api.getAgentResponse({...req('insufficient'),resolved_mmsi:candidates[0].mmsi})).status,'ok');
assert.equal((await api.getAgentResponse({...req('insufficient'),resolved_mmsi:candidates[1].mmsi,voyage_no:'EXAMPLE'})).status,'insufficient');
// 처리 단계 소식 (docs/specs/chat-progress.md). lib/progress.ts를 그대로 옮겨 실행한다
const progressFile=path.join(root,'lib/progress.ts');
const pm=new Module(progressFile);pm.filename=progressFile;pm._compile(ts.transpileModule(readText(progressFile),tsOptions).outputText,progressFile);
const {createSseParser,isStage,stageLabels}=pm.exports;
// 네트워크 조각은 이벤트 중간에서 잘려 온다. 끝난 이벤트만 꺼내고, 주석 줄(: ping)과 CRLF 줄바꿈도 견딘다
const push=createSseParser();
assert.deepEqual(push('event: progress\ndata: {"stage":"cus'),[]);
assert.deepEqual(push('toms"}\n\n: ping\n\nevent: result\r\ndata: {"kind":"message"}\r\n\r\n'),[{event:'progress',data:'{"stage":"customs"}'},{event:'result',data:'{"kind":"message"}'}]);
assert(isStage('judge')&&!isStage('made-up'));
// 화면 라벨과 서버 단계 이름이 같아야 한다. 한쪽에만 있으면 화면이 단계를 버리거나 서버가 못 보낸다
const backendStages=[...readText(path.join(root,'../backend/app/services/progress.py')).match(/STAGES = \(([\s\S]*?)\n\)/)[1].matchAll(/"([a-z_]+)"/g)].map(m=>m[1]);
assert.deepEqual(Object.keys(stageLabels).sort(),backendStages.slice().sort(),'progress stage names differ between web and backend');
// B/L 2~3개 비교 표 (ChatCargoTable). 줄의 칸 이름이 서버 ChatCargo와 같아야 하고, 여유 일수 문구는 VerdictCard와 같은 말이다
const chatSrc=readText(path.join(root,'../backend/app/routers/chat.py'));
const backendCargo=[...chatSrc.match(/class ChatCargo\(BaseModel\):[\s\S]*?\n\n\nclass/)[0].matchAll(/^    ([a-z_]+): /gm)].map(m=>m[1]);
const webCargo=[...readText(path.join(root,'lib/types.ts')).match(/export interface ChatCargo \{([\s\S]*?)\n\}/)[1].matchAll(/\b([a-z_]+)\??:/g)].map(m=>m[1]);
assert.deepEqual([...new Set(webCargo)].sort(),backendCargo.slice().sort(),'ChatCargo fields differ between web and backend');
assert(/cargos: list\[ChatCargo\] = \[\]/.test(chatSrc),'ChatResponse.cargos must default to an empty list');
// 대화로 지켜보기: 서버 ChatResponse의 watch_offer·watch_set 모양과 기본값(null)이 화면 타입과 같아야 한다
assert(/watch_offer: [A-Za-z]+ \| None = None/.test(chatSrc),'ChatResponse.watch_offer must default to None');
assert(/watch_set: [A-Za-z]+ \| None = None/.test(chatSrc),'ChatResponse.watch_set must default to None');
const typesSrc=readText(path.join(root,'lib/types.ts'));
const backendFields=(cls)=>{const m=chatSrc.match(new RegExp(`class ${cls}\\(BaseModel\\):([\\s\\S]*?)(?:\\n\\n\\n|$)`));assert(m,`${cls} missing in backend chat.py`);return [...m[1].matchAll(/^    ([a-z_]+): /gm)].map(x=>x[1]).sort();};
const webFields=(name)=>[...typesSrc.match(new RegExp(`export interface ${name} \\{([\\s\\S]*?)\\}`))[1].matchAll(/\b([a-z_]+)\??:/g)].map(x=>x[1]).sort();
assert.deepEqual(webFields('WatchOffer'),backendFields('WatchOffer'),'WatchOffer fields differ between web and backend');
assert.deepEqual(webFields('WatchSet'),backendFields('WatchSet'),'WatchSet fields differ between web and backend');
// 대화 목록: 고정된 계약의 칸 이름이 화면 타입과 같아야 한다. 백엔드 파일 없이도 돌 수 있게 화면 쪽만 정적으로 본다
assert.deepEqual(webFields('ConversationSummary'),['id','title','updated_at'].sort(),'ConversationSummary fields');
assert.deepEqual(webFields('ConversationMessage'),['created_at','extra','role','text'],'ConversationMessage fields');
assert.deepEqual(webFields('ConversationDetail'),['context','id','messages','result','title'],'ConversationDetail fields');
const apiSrc=readText(path.join(root,'lib/api.ts'));
for(const route of ["'/api/conversations'",'`/api/conversations/${encodeURIComponent(id)}`'])assert(apiSrc.includes(route),`api.ts must call ${route}`);
// 대화 id가 곧 chat session_id다: 새 대화는 새 id를 쓰고, 열면 그 id가 /api/chat의 session_id가 된다. 대화 글은 주소에 넣지 않는다 (id만)
assert(/export function setActiveSession/.test(apiSrc)&&/if \(activeSession\) return activeSession/.test(apiSrc),'api.ts must let a conversation set the session id');
const providerSrc=readText(path.join(root,'components/AppProvider.tsx'));
assert(!/\/\?c=\$\{[^}]*(text|message|thread)/.test(providerSrc),'conversation text must never be placed in the URL');
const fmtFile=path.join(root,'lib/format.ts');
const fm=new Module(fmtFile);fm.filename=fmtFile;fm.paths=Module._nodeModulePaths(path.dirname(fmtFile));fm._compile(ts.transpileModule(readText(fmtFile),tsOptions).outputText,fmtFile);
const {marginText}=fm.exports;
assert.equal(marginText({days_margin:3,sale_due_date:'2026-10-11'}),'3일 여유');
assert.equal(marginText({days_margin:-2,sale_due_date:'2026-10-11'}),'2일 늦음');
assert.equal(marginText({days_margin:0,sale_due_date:'2026-10-11'}),'0일 여유');
assert.equal(marginText({days_margin:null,sale_due_date:'2026-10-11'}),'확인 안 됨');
assert.equal(marginText({days_margin:null,sale_due_date:null}),'판매 예정일 없음');
// 로그인 뒤 돌아올 주소에 #이 남으면 ##access_token이 되어 supabase-js가 출입증을 못 읽는다 (lib/auth.ts returnAddress)
const authFile=path.join(root,'lib/auth.ts');
const am=new Module(authFile);am.filename=authFile;am.paths=Module._nodeModulePaths(path.dirname(authFile));am._compile(ts.transpileModule(readText(authFile),tsOptions).outputText,authFile);
const {parseParametersFromURL}=Module.createRequire(authFile)('@supabase/auth-js/dist/main/lib/helpers.js');
for(const href of ['http://localhost:3000/settings#','http://localhost:3000/?fixture=ok#evidence','http://localhost:3000/history']){
 const u=new URL(href);const back=am.exports.returnAddress(u);
 assert(!back.includes('#'),`return address keeps #: ${back}`);
 assert.equal(parseParametersFromURL(`${back}#access_token=T&refresh_token=R`).access_token,'T','supabase-js must read the token');
}
// 화면 글자 변환 (10-08 QA G4·G5). 날짜만 온 값에 없는 시각(09:00)을 붙이지 않고, 날짜 모양이 아니면 원문 그대로, 좌표는 부호대로 N/S·E/W
const {dateTime,day,coordText}=fm.exports;
assert.equal(dateTime('2026-10-20'),'2026년 10월 20일');
assert.equal(dateTime('2026-10-20',true),'10월 20일');
assert.equal(dateTime('2026-10-20T00:40:00Z'),'2026년 10월 20일 09:40');
assert.equal(dateTime('not-a-date'),'확인 안 됨');
assert.equal(day('2026-10-05'),'2026년 10월 5일');
assert.equal(day('2026-10-05T12:00:00+09:00'),'2026년 10월 5일');
assert.equal(day('2026-10'),'2026-10');
assert.equal(day(null),'확인 안 됨');
assert.equal(coordText(35.1,129.04),'35.10°N · 129.04°E');
assert.equal(coordText(-33.865,-70.5),'33.87°S · 70.50°W');
// 체험 모드는 운영자가 아니다 (10-08 QA G10). 운영자 메뉴(평가 지표·관리)는 서버가 운영자라고 할 때만 보인다
assert.equal((await api.getMe()).is_operator,false,'demo mode must not be an operator');
// 화면 예시 서류 응답의 계산 칸이 서버 규칙과 같아야 한다 (10-08 QA G14, D8과 함께 통과).
// bl_status·important는 서버 코드가 칸 값과 검사로 정한다(routers/document.py bl_status_of·important_of). 예시 값이 규칙과 어긋나면
// 체험 모드의 사진 첨부가 서버와 다르게 움직인다(예: "none"이면 조회를 안 한다). 여기서 같은 규칙으로 다시 계산해 대조한다
{
 const docSrc=readText(path.join(root,'../backend/app/routers/document.py'));
 const blOk=new RegExp(docSrc.match(/_BL_OK = re\.compile\(r"([^"]+)"\)/)[1]);
 const importantFields=[...docSrc.match(/IMPORTANT_FIELDS = \(([^)]*)\)/)[1].matchAll(/"([a-z_]+)"/g)].map(m=>m[1]);
 const f=documentFixture.fields;
 const status=!f.bl_no.value?'none':f.bl_no.checks.length||!blOk.test(f.bl_no.value)?'uncertain':'ok';
 const important=importantFields.filter(k=>f[k]?.value&&!f[k].checks.length);
 assert.equal(documentFixture.bl_status,status,`document fixture bl_status must follow routers/document.bl_status_of (expected ${status})`);
 assert.deepEqual(documentFixture.important,important,'document fixture important must follow routers/document.important_of');
}
// 채팅 첨부 사진의 다음 동작 (lib/docAttach.ts, 10-08 QA D4·D6·D7·D8). 따로 둔 검사 파일을 여기서 함께 돌린다
// 지켜보기 지도 규칙 (lib/watchMerge.ts, 10-08 사용자 결정): 위치 출처 글, seed 핀 구별, 같은 배 접기
{
 const wm=Module.createRequire(path.join(root,'lib/watchMerge.ts'))('./watchMerge.ts');
 assert.equal(wm.positionSourceLabel('worker'),'정기 수집');assert.equal(wm.positionSourceLabel('aisstream'),'실시간 AIS');
 assert.equal(wm.positionSourceLabel('lookup'),'화물 조회 때 받은 위치');assert(wm.positionSourceLabel('seed').includes('지켜보기를 켤 때'));
 for(const v of [null,undefined,'','other','toString'])assert.equal(wm.positionSourceLabel(v),null,`unknown source ${v} must hide the row`);
 assert.equal(wm.isSeedSource('seed'),true);for(const v of ['worker','aisstream','lookup',null])assert.equal(wm.isSeedSource(v),false);
 // 같은 배: 진행 중 화물의 배 번호와 같은 배만 지켜보기만 접는다. 반출 끝난 화물·배 모르는 화물은 접지 않는다
 const cargo=[{status:'active',vessel_mmsi:440999901},{status:'active',vessel_mmsi:440999901},{status:'done',vessel_mmsi:440999902},{status:'active',vessel_mmsi:null}];
 const shared=wm.sharedVesselMmsis(cargo,[{mmsi:440999901},{mmsi:440999902},{mmsi:440999903}]);
 assert.deepEqual([...shared],[440999901],'only vessel-only rows sharing an active cargo vessel are folded');
 assert.equal(wm.sharedVesselMmsis(cargo,[]).size,0);assert.equal(wm.sharedVesselMmsis([],[{mmsi:440999901}]).size,0);
}
await import('./verify-docattach.mjs');
console.log('PASS: 7 AgentResponse fixtures, history envelope, document and memory fixtures, null/enums/evidence, replay isolation, date/type mismatch, candidate resolution, session isolation, SSE parsing, stage names, chat cargo table fields, conversation list types, login return address, date/coordinate text, demo non-operator and document fixture bl_status/important.');
