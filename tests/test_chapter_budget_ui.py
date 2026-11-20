from pathlib import Path
import shutil
import subprocess

import pytest

ROOT=Path(__file__).resolve().parents[1]
pytestmark=pytest.mark.skipif(shutil.which('node') is None,reason='Node.js unavailable')


def run_js(assertions):
    harness=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const elements={},requests=[],changes=[];
function element(){return {hidden:false,disabled:false,textContent:'',value:'',checked:true,children:[],listeners:{},
 addEventListener(name,fn){this.listeners[name]=fn;},replaceChildren(...children){this.children=children;},
 reset(){},reportValidity(){return true;}};}
for(const name of ['panel','form','status','body','refresh','submit','error','money','money-note'])elements[name]=element();
elements.form.elements={author_confirmed:{checked:true},monetary_enabled:{checked:false}};
for(const [key,value] of Object.entries({max_provider_calls:'2',max_input_tokens:'1000',max_output_tokens:'100',max_active_seconds:'20'}))
 elements.form.elements[key]={value,disabled:false};
for(const key of ['budget_currency','budget_amount','budget_input_price','budget_cached_price','budget_output_price'])
 elements.form.elements[key]={value:'',disabled:true};
const sandbox={document:{querySelector:s=>elements[s.match(/data-chapter-budget-(.*)\]/)?.[1]],createElement:()=>element()},
 window:{NovelAgentUI:{requestHeaders:x=>x,parseResponse:r=>r.json(),showToast:()=>{},errorMessage:p=>p?.error?.message||'read failed'}},
 fetch:(url,options)=>new Promise(resolve=>requests.push({url,options,resolve}))};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),sandbox);
const ui=sandbox.window.NovelAgentChapterBudget;
const scope={projectId:'p',bookId:'b',chapter:2,contextSha:'a'.repeat(64),canFreeze:true};
const fresh=()=>({project_id:'p',book_id:'b',chapter:2,state:'ready',
 budget:{max_provider_calls:2,max_input_tokens:1000,max_output_tokens:100,max_active_seconds:'20',amount_limit:null},
 usage:{charged:{calls:1,input_tokens:300,output_tokens:10},unresolved_attempts:[],exceeded:[]},
 time:{active_seconds:'2',active_time_complete:true,active_status:'settled',active_limit_reached:false,author_wait_seconds:'100000',end_to_end_seconds:'100002'}});
const respond=(i,payload,ok=true)=>requests[i].resolve({ok,status:ok?200:409,json:async()=>payload});
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const moneyOption=()=>({available:true,model:'snapshot',service_tier:'default',input_reservation_per_call:400000,message:'review tariff',
 pricing_template:{provider:'openai',model:'snapshot',endpoint_type:'official',endpoint_fingerprint:'b'.repeat(64),
 input_token_semantics:'input_includes_cache',billing_policy:{schema_version:'fixture',service_tier:'default'}}});
(async()=>{
'''
    result=subprocess.run([shutil.which('node'),'-e',harness+assertions+
        '\n})().catch(e=>{console.error(e);process.exitCode=1;});',
        str(ROOT/'workbench/static/chapter-budget.js')],capture_output=True,text=True)
    assert result.returncode==0,result.stderr or result.stdout


def test_active_time_author_wait_unknown_and_cap_have_different_messages():
    run_js(r'''
const report=fresh();let view=ui.presentation(report);
assert(!view.blocked);assert(view.lines.some(x=>x.includes('作者等待：100000')));
assert(view.lines.some(x=>x.includes('金额未受控')));
report.time.active_status='unknown';assert(ui.presentation(report).blocked);
assert(ui.presentation(report).message.includes('计时'));
report.time.active_status='settled';report.usage.unresolved_attempts=[['e','a']];
view=ui.presentation(report);assert(view.blocked);
assert.match(view.message,/可能.*计费/);assert.match(view.message,/核对.*记录/);
assert.match(view.message,/预留.*(保留|继续占用|不释放)/);
report.usage.unresolved_attempts=[];report.usage.charged.calls=2;
assert(ui.presentation(report).message.includes('限额已用尽'));
''')


def test_navigation_discards_inflight_previous_chapter_budget():
    run_js(r'''
ui.setContext(scope,value=>changes.push(value));
ui.setContext({...scope,chapter:3},value=>changes.push(value));
respond(0,fresh());await tick();assert(!elements.body.children.length);
respond(1,{...fresh(),chapter:3});await tick();assert(elements.body.children.length);
assert.equal(changes.at(-1),false);assert.equal(requests.length,2);
assert(requests.every(x=>!x.options.method));
''')


def test_error_clears_previous_totals_and_cannot_enable_generation():
    run_js(r'''
ui.setContext(scope,value=>changes.push(value));respond(0,fresh());await tick();
assert(elements.body.children.length);const pending=ui.refresh();
assert(!elements.body.children.length);assert.equal(changes.at(-1),true);
respond(1,{error:{message:'damaged ledger'}},false);await pending;
assert(!elements.body.children.length);assert(elements.form.hidden);
assert.equal(changes.at(-1),true);assert.equal(elements.error.textContent,'damaged ledger');
''')


def test_mismatched_budget_identity_is_rejected():
    run_js(r'''
ui.setContext(scope,value=>changes.push(value));respond(0,{...fresh(),book_id:'foreign'});await tick();
assert.equal(changes.at(-1),true);assert(!elements.body.children.length);
assert(elements.error.textContent.includes('不属于'));
''')


def test_explicit_freeze_uses_current_context_and_double_submit_sends_once():
    run_js(r'''
ui.setContext(scope,value=>changes.push(value));respond(0,{...fresh(),state:'not_configured',budget:null});await tick();
const submit=()=>elements.form.listeners.submit({preventDefault(){}});
elements.form.elements.author_confirmed.checked=true;
const pending=submit();await submit();assert.equal(requests.length,2);
const payload=JSON.parse(requests[1].options.body);
assert.equal(payload.author_confirmed,true);assert.equal(payload.expected_context_sha256,scope.contextSha);
assert.equal(payload.configuration.max_provider_calls,2);assert.equal(payload.configuration.max_active_seconds,'20');
assert.equal(payload.configuration.amount_limit,null);
respond(1,fresh());await pending;assert(elements.form.hidden);
assert.equal(changes.at(-1),false);
''')


def test_lost_freeze_response_requires_read_and_never_automatically_reposts():
    run_js(r'''
ui.setContext(scope,value=>changes.push(value));respond(0,{...fresh(),state:'not_configured',budget:null});await tick();
elements.form.elements.author_confirmed.checked=true;
const pending=elements.form.listeners.submit({preventDefault(){}});
respond(1,{error:{message:'reply lost'}},false);await pending;
assert.equal(changes.at(-1),true);assert(elements.form.hidden);assert.equal(requests.length,2);
const reading=ui.refresh();respond(2,fresh());await reading;
assert.equal(requests.filter(x=>x.options.method==='POST').length,1);
assert.equal(changes.at(-1),false);
''')


def test_money_freeze_preserves_decimal_text_and_binds_server_identity():
    run_js(r'''
ui.setContext(scope,()=>{});respond(0,{...fresh(),state:'not_configured',budget:null,monetary_option:moneyOption()});await tick();
const f=elements.form.elements;f.monetary_enabled.checked=true;
elements.form.listeners.change({target:{name:'monetary_enabled'}});
assert(!f.budget_amount.disabled);assert.equal(f.author_confirmed.checked,false);
for(const [name,value] of Object.entries({budget_currency:'USD',budget_amount:'0.40045',budget_input_price:'1',
 budget_cached_price:'0.000000000000000001',budget_output_price:'2'}))f[name].value=value;
f.author_confirmed.checked=true;
const pending=elements.form.listeners.submit({preventDefault(){}});
const config=JSON.parse(requests[1].options.body).configuration;
assert.equal(config.amount_limit,'0.40045');assert.equal(config.currency,'USD');
assert.equal(config.pricing[0].model,'snapshot');assert.equal(config.pricing[0].endpoint_fingerprint,'b'.repeat(64));
assert.equal(config.pricing[0].cache_read_price_per_million,'0.000000000000000001');
respond(1,{...fresh(),budget:{...fresh().budget,...config}});await pending;
assert(elements.body.children.some(x=>x.textContent.includes('冻结价格：openai / snapshot')));
''')


def test_price_edit_and_refresh_clear_author_confirmation_and_money_availability():
    run_js(r'''
ui.setContext(scope,()=>{});respond(0,{...fresh(),state:'not_configured',budget:null,monetary_option:moneyOption()});await tick();
const f=elements.form.elements;f.monetary_enabled.checked=true;f.author_confirmed.checked=true;
elements.form.listeners.change({target:{name:'budget_input_price'}});assert.equal(f.author_confirmed.checked,false);
f.author_confirmed.checked=true;
const reading=ui.refresh();assert.equal(f.author_confirmed.checked,false);
respond(1,{...fresh(),state:'not_configured',budget:null,monetary_option:{available:false,message:'unsupported'}});await reading;
assert(elements.money.hidden);assert.equal(f.monetary_enabled.checked,false);assert(f.budget_input_price.disabled);
assert.equal(requests.filter(x=>x.options.method==='POST').length,0);
''')


def test_interrupted_money_freeze_displays_and_reuses_original_configuration():
    run_js(r'''
const budget={...fresh().budget,amount_limit:'1',currency:'USD',pricing:[{...moneyOption().pricing_template,currency:'USD',
 input_price_per_million:'1',cache_read_price_per_million:'0.1',output_price_per_million:'2'}]};
ui.setContext(scope,()=>{});respond(0,{...fresh(),state:'initializing',budget});await tick();
assert(!elements.form.hidden);assert(elements.form.elements.monetary_enabled.disabled);
assert(elements.body.children.some(x=>x.textContent.includes('冻结价格：')));
elements.form.elements.author_confirmed.checked=true;
const pending=elements.form.listeners.submit({preventDefault(){}});
assert.equal(JSON.parse(requests[1].options.body).configuration.amount_limit,'1');
assert.equal(JSON.parse(requests[1].options.body).configuration.pricing[0].model,'snapshot');
respond(1,{...fresh(),budget});await pending;
''')
