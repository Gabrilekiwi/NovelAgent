"""Exercise the helper used by the workbench against a deterministic UI clock."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(not shutil.which('node'), reason='Node.js unavailable')
def test_pending_success_failure_confirm_and_duplicate_clicks():
    helper = Path(__file__).resolve().parents[1] / 'workbench/static/action-feedback.js'
    script = r"""
const fs=require('fs'), vm=require('vm'), assert=require('assert/strict');
let now=0, scheduled, confirmOpen=false, onClose, invoked=0, resolve, reject;
function element(){return {setAttribute(){},append(...xs){xs.forEach(x=>x.parentElement=this);this.children=xs;}};}
const body=element(), doc={body,createElement:element,querySelector:()=>confirmOpen,querySelectorAll:()=>[],addEventListener:(_,fn)=>onClose=fn};
const box={window:{},document:doc,performance:{now:()=>now},setTimeout:fn=>(scheduled=fn),clearTimeout:()=>scheduled=null};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),box);
const wrap=box.window.NovelAgentFeedback.action;
const button={textContent:'Save',getAttribute:()=>null};
const event={target:{closest:()=>button},preventDefault(){this.prevented=true;}};
function advance(){now+=150;const fn=scheduled;if(fn)fn();}
(async()=>{
 await wrap(()=>Promise.resolve('done'))(event);advance();assert(!body.children);
 const run=wrap(()=>{invoked++;return new Promise((ok,fail)=>{resolve=ok;reject=fail;});});
 const first=run(event);run(event);assert.equal(invoked,1);assert(event.prevented);
 advance();const layer=body.children[0];assert.equal(layer.hidden,false);assert(layer.children[1].textContent.includes('Save'));
 resolve(1);await first;assert.equal(layer.hidden,true);
 confirmOpen=true;const second=run(event);advance();assert.equal(layer.hidden,true);
 confirmOpen=false;onClose();assert.equal(layer.hidden,false);
 reject(Error('read failed'));await assert.rejects(second,/read failed/);assert.equal(layer.hidden,true);
 const third=run(event);resolve(3);assert.equal(await third,3);assert.equal(invoked,3);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    completed = subprocess.run([shutil.which('node'), '-e', script, str(helper)], capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
