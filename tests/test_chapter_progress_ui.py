"""Exercise actual SPA progress transitions and delayed read guards with Node."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPA = ROOT / 'workbench/static/workbench-spa.js'
pytestmark = pytest.mark.skipif(shutil.which('node') is None, reason='Node.js unavailable')


def run_js(assertions):
    harness = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
let source = fs.readFileSync(process.argv[1], 'utf8');
const marker = '  const renderChapterCandidate =';
source = source.replace(marker, '  globalThis.progress = { present: chapterWorkflowPresentation, matches: chapterProgressMatches };\n  return;\n' + marker);
const sandbox = { document: { body: { hasAttribute: () => true }, querySelector: () => null,
  querySelectorAll: () => [], createElement: () => ({}) }, window: { NovelAgentUI: {} } };
vm.runInNewContext(source, sandbox);
const { present, matches } = sandbox.progress;
'''
    result = subprocess.run([shutil.which('node'), '-e', harness + assertions, str(SPA)],
        cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr or result.stdout


def test_saved_draft_and_inflight_execution_have_distinct_next_steps():
    run_js(r'''
assert.equal(present({}).phase, 'intent');
assert.equal(present({confirmed:true}).label, '可生成');
assert.equal(present({confirmed:true,budgetBlocked:true,budgetMessage:'预算已满'}).label,'预算待处理');
assert.equal(present({confirmed:true,budgetBlocked:true,budgetMessage:'预算已满'}).message,'预算已满');
assert.equal(present({candidate:{status:'completed',draft:{}}}).label, '可编辑草稿');
assert.equal(present({candidate:{status:'running'}}).label, '生成中');
assert.equal(present({candidate:{status:'completed',draft:{}},extraction:{status:'running'}}).label, '检查中');
assert.equal(present({pending:'candidate'}).label, '生成中');
assert.equal(present({pending:'facts'}).label, '检查中');
assert.equal(present({review:{state:'editing'}}).label, '待审阅');
assert.equal(present({review:{state:'confirmed'}}).phase, 'publication');
''')


def test_failures_stale_reviews_and_recovery_do_not_appear_publishable():
    run_js(r'''
assert.equal(present({review:{state:'stale'}}).label, '事实需重查');
for (const status of ['needs_recovery','provider_uncertain','failed']) {
  assert.equal(present({candidate:{status}}).phase, 'blocked');
  assert.equal(present({extraction:{status}}).phase, 'blocked');
}
assert.equal(present({formal:true}).phase, 'committed');
assert.equal(present({formal:true,recovery:true}).phase, 'blocked');
assert.equal(present({review:{state:'confirmed'},error:'无法读取'}).phase, 'blocked');
''')


def test_progress_response_cannot_replace_another_route_action_or_saved_draft():
    run_js(r'''
const draft = {id:'draft-a', revision:3, sha256:'abc'};
const state = {routeRevision:5, projectId:'book-a', route:{area:'chapter',projectId:'book-a',chapter:2},
  candidateActionNonce:4,factActionNonce:6,chapterCandidate:{candidate_id:'candidate-a',draft},
  chapterFactExtraction:{extraction_id:'facts-a'}};
const snapshot = {routeRevision:5,projectId:'book-a',chapter:2,candidateNonce:4,factNonce:6,
  candidateId:'candidate-a',draft,extractionId:'facts-a'};
assert(matches(snapshot,state));
for (const change of [{routeRevision:6},{projectId:'book-b'},{candidateActionNonce:5},{factActionNonce:7},
  {route:{area:'overview',projectId:'book-a',chapter:2}},
  {chapterCandidate:{candidate_id:'candidate-b',draft}},
  {chapterCandidate:{candidate_id:'candidate-a',draft:{...draft,revision:4}}},
  {chapterCandidate:{candidate_id:'candidate-a',draft:{...draft,sha256:'changed'}}},
  {chapterFactExtraction:{extraction_id:'facts-b'}}]) assert(!matches(snapshot,{...state,...change}));
''')


@pytest.mark.parametrize('control', ['candidate-generate', 'fact-extract'])
def test_confirmation_keeps_button_reference_after_browser_clears_current_target(control):
    script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(process.argv[1], 'utf8');
const control = process.argv[2];
const marker = `  one("[data-${control}]")?.addEventListener("click", async (event) => {`;
const start = source.indexOf(marker), end = source.indexOf('\n  });', start) + 6;
assert(start > 0 && end > start);
let listener, accept, observedButton;
const button = { disabled:false }, draft = {id:'draft-a'};
const sandbox = {
  one: selector => selector === `[data-${control}]` ? { addEventListener:(_, fn)=>listener=fn } : {classList:{contains:()=>false}},
  UI:{confirmAction:()=>new Promise(resolve=>accept=resolve)},
  state:{chapterCandidate:{draft},chapterConfirmation:{confirmation_sha256:'a'.repeat(64)}},
  currentConfirmationMatchesCard:()=>true,
  beginCandidateAction:()=>({project_id:'book-a',chapter:2}),
  beginFactAction:()=>({draft_id:'draft-a'}),
  candidateOperationKey:()=> 'candidate-key',factOperationKey:()=> 'fact-key',
  candidateCollectionPath:()=> '/candidates',factExtractionCollectionPath:()=> '/facts',
  setCandidateButtonBusy: b=>{ observedButton=b; b.disabled=true; },
  setFactButtonBusy: b=>{ observedButton=b; b.disabled=true; },
  api:()=>new Promise(()=>{})
};
vm.runInNewContext(source.slice(start,end),sandbox);
const event = {currentTarget:button};
listener(event);
event.currentTarget = null; // DOM dispatch ends while custom confirmation waits.
accept(true);
setImmediate(()=>{ assert.strictEqual(observedButton,button); assert(button.disabled); });
'''
    result = subprocess.run([shutil.which('node'), '-e', script, str(SPA), control],
        cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr or result.stdout
