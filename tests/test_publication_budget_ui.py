"""Budget state must block quality execution while keeping drafts readable."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT=Path(__file__).resolve().parents[1]
pytestmark=pytest.mark.skipif(shutil.which('node') is None,reason='Node.js unavailable')


def run_js(assertions):
    harness=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const elements={},requests=[],saved=new Map();
function element(){return {hidden:false,disabled:false,textContent:'',checked:false,href:'',listeners:{},
 addEventListener(name,fn){this.listeners[name]=fn;},append(){},replaceChildren(){}};}
const get=id=>elements[id] || (elements[id]=element());
const sandbox={document:{getElementById:get,createElement:()=>element()},URLSearchParams,
 location:{search:'?project_id=p&chapter=2&review_id=r'},crypto:{randomUUID:()=> 'fixed-key'},
 localStorage:{getItem:k=>saved.get(k)||null,setItem:(k,v)=>saved.set(k,v),removeItem:k=>saved.delete(k)},
 fetch:(url,options)=>new Promise(resolve=>requests.push({url,options,resolve}))};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),sandbox);
const input={project_id:'p',book_id:'b',chapter:2,review_id:'r',review_state:'confirmed',final_draft:'original draft',
 expected_draft_revision:1,expected_review_revision:2,expected_draft_sha256:'a'.repeat(64),expected_facts_sha256:'b'.repeat(64)};
const budget=()=>({project_id:'p',book_id:'b',chapter:2,state:'ready',budget:{budget_sha256:'c'.repeat(64)},
 time:{binding:{budget_sha256:'c'.repeat(64)},active_status:'settled',active_limit_reached:false}});
const respond=(i,payload,status=200)=>requests[i].resolve({ok:status===200,status,json:async()=>payload});
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
'''
    result=subprocess.run([shutil.which('node'),'-e',harness+assertions+
        '\n})().catch(e=>{console.error(e);process.exitCode=1;});',
        str(ROOT/'workbench/static/chapter-publication.js')],capture_output=True,text=True)
    assert result.returncode==0,result.stderr or result.stdout


@pytest.mark.parametrize('state',['unknown','running','exhausted'])
def test_initial_budget_pause_and_readonly_refresh_never_run_quality(state):
    run_js(r'''
respond(0,input);await tick();const blocked=budget();
''' + ("blocked.time.active_limit_reached=true;" if state=='exhausted' else
       f"blocked.time.active_status='{state}';") + r'''
respond(1,blocked);await tick();
assert(get('preview').disabled && get('commit').disabled);assert.equal(get('prose').textContent,'original draft');
get('confirmed').checked=true;
const reading=get('reload').listeners.click();respond(2,input);await tick();respond(3,budget());await reading;
assert(!get('preview').disabled);assert(get('commit').disabled);assert(!get('confirmed').checked);
assert(requests.every(x=>!x.options.method));
''')


def test_budget_read_failure_blocks_checks_and_preserves_draft():
    run_js(r'''
respond(0,input);await tick();respond(1,{error:{code:'chapter_time_evidence_invalid',message:'invalid'}},409);await tick();
assert(get('preview').disabled && get('commit').disabled);assert.equal(get('prose').textContent,'original draft');
assert(get('error').textContent.includes('计时记录'));assert(!get('reload').disabled);
''')


def test_verified_author_import_uses_local_review_without_requesting_model_budget():
    run_js(r'''
respond(0,{...input,execution_mode:'local_author_review'});await tick();
assert(!get('preview').disabled);assert(get('commit').disabled);
assert.equal(requests.length,1);assert.equal(get('prose').textContent,'original draft');
const pending=get('preview').listeners.click();
respond(1,{...input,execution_mode:'local_author_review'});await tick();
assert(requests[2].url.endsWith('/publication-preview'));
assert(!requests.some(r=>r.url.endsWith('/budget')));
respond(2,{error:{code:'chapter_commit_conflict',message:'changed'}},409);await pending;
assert(get('commit').disabled);assert.equal(get('prose').textContent,'original draft');
''')


def test_exhaustion_during_quality_clears_confirmation_and_disables_new_checks():
    run_js(r'''
respond(0,input);await tick();respond(1,budget());await tick();assert(!get('preview').disabled);
const pending=get('preview').listeners.click();respond(2,input);await tick();respond(3,budget());await tick();
assert.equal(requests[4].options.method,'POST');assert(requests[4].url.endsWith('/publication-preview'));
respond(4,{error:{code:'chapter_active_time_exceeded',message:'exhausted'}},409);await pending;
assert(get('preview').disabled && get('commit').disabled);assert(!get('confirmed').checked);
assert(get('preview-panel').hidden);assert(get('error').textContent.includes('主动执行时间'));
assert.equal(requests.filter(x=>x.options.method==='POST').length,1);
''')
