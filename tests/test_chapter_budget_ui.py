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
for(const name of ['panel','form','status','body','refresh','submit','error'])elements[name]=element();
elements.form.elements={author_confirmed:{checked:true}};
for(const [key,value] of Object.entries({max_provider_calls:'2',max_input_tokens:'1000',max_output_tokens:'100',max_active_seconds:'20'}))
 elements.form.elements[key]={value,disabled:false};
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
assert(ui.presentation(report).message.includes('预留额度'));
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
const pending=elements.form.listeners.submit({preventDefault(){}});
respond(1,{error:{message:'reply lost'}},false);await pending;
assert.equal(changes.at(-1),true);assert(elements.form.hidden);assert.equal(requests.length,2);
const reading=ui.refresh();respond(2,fresh());await reading;
assert.equal(requests.filter(x=>x.options.method==='POST').length,1);
assert.equal(changes.at(-1),false);
''')
