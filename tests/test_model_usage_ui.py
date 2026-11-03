from pathlib import Path
import shutil
import subprocess

import pytest

ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which('node') is None,reason='Node.js unavailable')
def test_usage_ui_distinguishes_unknown_zero_sources_and_currency_subtotals():
    script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const box={document:{querySelector:()=>null},window:{NovelAgentUI:{}}};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),box);
const ui=box.window.NovelAgentModelUsage;
assert(ui.tokenLabel({value:null,reason:'unattributed_zero'}).includes('未知'));
assert.equal(ui.tokenLabel({value:0,source:'provider_reported'}),'0（服务商返回）');
assert(ui.tokenLabel({value:12,source:'program_estimate'}).includes('程序估算'));
assert(ui.costLabel({classification:'unknown',reason:'no_receipt'}).includes('未知'));
assert.equal(ui.costLabel({classification:'actual',amount:'0',currency:'USD'}),'实际费用 0 USD');
const lines=ui.summaryLines({attempt_count:4,unknown_cost_count:1,costs_by_currency:{
  USD:{actual:'0.02',actual_count:1,estimated:'0.1',estimated_count:1},
  CNY:{actual:'0.2',actual_count:1,estimated:'0',estimated_count:0}}});
assert(lines.some(l=>l.includes('实际费用小计 0.02 USD')));
assert(lines.some(l=>l.includes('估算费用小计 0.1 USD')));
assert(lines.some(l=>l.includes('实际费用小计 0.2 CNY')));
assert(lines.some(l=>l.includes('不能代表完整费用')));
const budget=ui.budgetLines([{chapter:2,charged_amount:'0.0006',currency:'CNY',items:[{accounting_state:'reserved'}]}]);
assert(budget[0].includes('金额占用 0.0006 CNY') && budget[0].includes('1 次待结算'));
assert(budget[0].includes('不计入费用小计'));
'''
    result=subprocess.run([shutil.which('node'),'-e',script,str(ROOT/'workbench/static/model-usage.js')],capture_output=True,text=True)
    assert result.returncode==0,result.stderr or result.stdout


@pytest.mark.skipif(shutil.which('node') is None,reason='Node.js unavailable')
def test_usage_response_from_previous_project_cannot_render_in_current_project():
    script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
let toggle,resolve;
const panel={open:false,hidden:true,addEventListener:(name,fn)=>{if(name==='toggle')toggle=fn;}},
  body={replaceChildren:()=>{}},heading={textContent:''},refresh={disabled:false,addEventListener:()=>{}};
let created=0;
const elements={'[data-model-usage-panel]':panel,'[data-model-usage-body]':body,
  '[data-model-usage-status]':heading,'[data-model-usage-refresh]':refresh};
const sandbox={document:{querySelector:s=>elements[s],createElement:()=>{created++;throw Error('stale response reached rendering');}},
  window:{NovelAgentUI:{requestHeaders:()=>({}),parseResponse:r=>r.json()}},
  fetch:()=>new Promise(r=>resolve=r)};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),sandbox);
const ui=sandbox.window.NovelAgentModelUsage;
ui.setRoute({projectId:'book-a',area:'chapter'});panel.open=true;toggle();
ui.setRoute({projectId:'book-b',area:'overview'});
const before=heading.textContent;
resolve({ok:true,json:async()=>({project_id:'book-a',schema_version:'model_usage_report_v1',summary:{},items:[]})});
setImmediate(()=>{assert.equal(created,0);assert.equal(heading.textContent,before);assert.equal(refresh.disabled,false);});
'''
    result=subprocess.run([shutil.which('node'),'-e',script,str(ROOT/'workbench/static/model-usage.js')],capture_output=True,text=True)
    assert result.returncode==0,result.stderr or result.stdout
