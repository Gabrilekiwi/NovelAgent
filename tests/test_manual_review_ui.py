from pathlib import Path
import shutil
import subprocess
import pytest

ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which('node') is None,reason='Node.js unavailable')
def test_manual_review_does_not_send_from_dirty_or_replaced_draft():
    script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const helper=source.slice(source.indexOf('  const canStartManualFactReview ='),source.indexOf('  const renderChapterFacts ='));
const start=source.indexOf("  one('[data-fact-manual]')?.addEventListener"),end=source.indexOf('\n  });',start)+6;
assert(start>0 && end>start);
(async()=>{
 for(const scenario of ['cancel','dirty-before','dirty-after','route-changed','valid']) {
  let listener,accept,calls=[],busyButton;
  let dirty=scenario==='dirty-before',current=true;
  const button={disabled:false},action={draft_id:'draft-1',draft_revision:4,draft_sha256:'abc'};
  const sandbox={state:{chapterCandidate:{draft:{id:'draft-1'}}},
   one:s=>s==='[data-fact-manual]'?{addEventListener:(_,fn)=>listener=fn}:dirty?{}:null,
   UI:{showToast:()=>{},confirmAction:()=>new Promise(r=>accept=r)},beginFactAction:()=>action,
   factActionTargetsCurrentDraft:()=>current,setFactButtonBusy:b=>busyButton=b,
   api:async(path,options)=>{calls.push({path,options});return {payload:{fact_review:{}}};},
   reviewFromPayload:p=>p.fact_review,renderChapterFacts:()=>{},finishFactAction:()=>{},showFactError:()=>{}};
  vm.runInNewContext(helper+source.slice(start,end),sandbox);
  const event={currentTarget:button},done=listener(event);event.currentTarget=null;
  if(scenario!=='dirty-before'){
   if(scenario==='dirty-after')dirty=true;
   if(scenario==='route-changed')current=false;
   accept(scenario!=='cancel');
  }
  await done;
  assert.equal(calls.length,scenario==='valid'?1:0);
  if(calls.length){assert.strictEqual(busyButton,button);assert.equal(calls[0].path,'/api/candidate-drafts/draft-1/fact-reviews');
   assert.equal(calls[0].options.json.expected_draft_revision,4);assert.equal(calls[0].options.json.proposals.length,0);}
 }
 const box={};vm.runInNewContext(helper+'globalThis.allowed=canStartManualFactReview;',box);
 for(const status of ['running','needs_recovery','provider_uncertain'])assert(!box.allowed({draft:{},extraction:{status}}));
 assert(!box.allowed({draft:{},review:{state:'editing'}}));assert(!box.allowed({draft:{},error:'unreadable'}));
 assert(box.allowed({draft:{},review:{state:'stale'}}));
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result=subprocess.run([shutil.which('node'),'-e',script,str(ROOT/'workbench/static/workbench-spa.js')],cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
