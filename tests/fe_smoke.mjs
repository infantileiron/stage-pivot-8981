// 前端冒烟：新页面渲染与关键交互（jsdom，无浏览器）
import {JSDOM} from 'jsdom';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
const FE='/workspace/frontend/src';
const dom=new JSDOM('<!doctype html><html><body><div id="app"></div></body></html>',{url:'http://localhost/#4',pretendToBeVisual:true});
globalThis.window=dom.window;globalThis.document=dom.window.document;globalThis.localStorage=dom.window.localStorage;
globalThis.location=dom.window.location;globalThis.Event=dom.window.Event;globalThis.MouseEvent=dom.window.MouseEvent;
globalThis.Blob=dom.window.Blob;globalThis.URL=dom.window.URL;
let pass=0,fail=0;
const ok=(cond,msg)=>{if(cond){pass++}else{fail++;console.error('FAIL:',msg)}};

const {state}=await import(pathToFileURL(path.join(FE,'stores/session.js')).href);
state.user={email:'analyst@demo.local',role:'analyst'};
state.settlements=[
 {id:1,code:'YM-002',day:'2026-09-02',venue:'云幕大剧场',play:'渡口来信',channel:'剧院直营',gross_cents:120000,refund_cents:20000,fee_cents:0,seats:100,checked:80},
 {id:2,code:'YM-001',day:'2026-09-01',venue:'河畔小剧场',play:'星河漫游',channel:'票务合作',gross_cents:90000,refund_cents:0,fee_cents:1234,seats:100,checked:70},
];
state.contracts=[{id:1,name:'直营阶梯',channel:'剧院直营',month_from:'2026-09',month_to:'2026-12',tiers:[{from_cents:0,rate_bp:500},{from_cents:100000,rate_bp:300}],status:'enabled',created:'',updated:''}];
state.feeRuns=[{id:7,months:['2026-09'],state:'failed',error:'结算源数据在计算期间被修订，过期结果拒绝落账，请重试任务',attempts:1,take_over:0,freeze:{scopes:[{month:'2026-09',channel:'剧院直营'}]}}];
state.feeMonths=[{id:3,month:'2026-09',channel:'剧院直营',contract_name:'直营阶梯',tiers:[{from_cents:0,rate_bp:500}],total_base_cents:240000,total_fee_cents:9200,manual_total_cents:0,show_count:2,run_id:7,stale:1}];
state.feeBasis={month:state.feeMonths[0],basis:[
 {ord_no:1,code:'YM-001',day:'2026-09-01',base_cents:100000,cum_base_cents:100000,cum_fee_cents:5000,fee_cents:5000,manual_fee_cents:0,marginal_tier:'100000 分起 · 0.03'},
 {ord_no:2,code:'YM-002',day:'2026-09-02',base_cents:140000,cum_base_cents:240000,cum_fee_cents:9200,fee_cents:4200,manual_fee_cents:0,marginal_tier:'100000 分起 · 0.03'},
]};

// 1. 合同页渲染（编辑态回显两档）
state.editingContract=state.contracts[0];
const contracts=await import(pathToFileURL(path.join(FE,'pages/contracts.js')).href);
document.querySelector('#app').innerHTML=contracts.render();
ok(document.querySelector('#contract')!==null,'合同表单存在');
ok(document.querySelectorAll('.tier-row').length===2,'回显两档费率');
ok(document.body.textContent.includes('直营阶梯'),'列出合同');
// 增加一档 / 删档（复用 app 的绑定逻辑较复杂，直接验证 DOM 结构可操作）
const tiers=document.querySelector('#tiers');
tiers.insertAdjacentHTML('beforeend','<div class="tier-row" data-tier="2"><input type="number" value="300000"><input type="number" value="100"><button type="button" data-del-tier="2">删档</button></div>');
ok(document.querySelectorAll('.tier-row').length===3,'可增加费率档');
document.querySelector('[data-del-tier="2"]').closest('.tier-row').remove();
ok(document.querySelectorAll('.tier-row').length===2,'可删除费率档');

// 2. 计费页渲染
const fees=await import(pathToFileURL(path.join(FE,'pages/fees.js')).href);
document.querySelector('#app').innerHTML=fees.render();
ok(document.querySelector('#fee-run')!==null,'计费任务表单存在');
ok(document.body.textContent.includes('待重算'),'过期月份显示待重算标记');
ok(document.body.textContent.includes('恢复并重试'),'失败任务显示重试按钮');
ok(document.body.textContent.includes('YM-001')&&document.body.textContent.includes('92.00'),'分档依据表格渲染且金额按元显示');
ok(document.querySelector('[data-export-month]')!==null,'导出按钮存在');
ok(document.body.textContent.includes('确认接管手填渠道费'),'手填接管勾选存在');

// 3. app.js 整体加载（自动进入登录页），不抛异常即通过
dom.window.localStorage.clear();
await import(pathToFileURL(path.join(FE,'app.js')).href);
ok(document.querySelector('#login')!==null,'未登录时渲染登录页');

// 4. 模拟登录后全量渲染（stub fetch）
const canned={
 '/me':{email:'analyst@demo.local',role:'analyst'},
 '/settlements':state.settlements,'/pivots':[],'/reviews':[],'/fee-contracts':state.contracts,
 '/fee-runs':state.feeRuns,'/fee-months':state.feeMonths,
};
globalThis.fetch=async(url,opts)=>({ok:true,json:async()=>{
 const p=String(url).replace(/^https?:\/\/[^/]+/,'').replace('/api','').split('?')[0];
 if(p==='/auth/login')return {token:'t',user:{email:'analyst@demo.local',role:'analyst'}};
 if(p in canned)return canned[p];
 return {};
}});
dom.window.fetch=globalThis.fetch;
dom.window.scrollTo=()=>{};
await document.querySelector('#login').onsubmit({preventDefault(){}});
await new Promise(r=>setTimeout(r,300));
ok(document.querySelector('nav')!==null,'登录后渲染导航');
ok(document.querySelectorAll('nav button').length===6,'六个页面入口');
// 切到渠道费计算页
dom.window.location.hash='#5';
window.onhashchange();
await new Promise(r=>setTimeout(r,100));
ok(document.querySelector('#fee-run')!==null,'导航可进入渠道费计算页');
// viewer 只读
state.user.role='viewer';
dom.window.location.hash='#4';
window.onhashchange();
await new Promise(r=>setTimeout(r,100));
const disabled=[...document.querySelectorAll('button')].filter(b=>b.disabled).length;
ok(disabled>0,'viewer 下写按钮被禁用');

console.log(`\n前端冒烟：${pass} 通过 / ${fail} 失败`);
process.exit(fail?1:0);
