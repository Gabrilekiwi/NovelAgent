// Offline VM regression only; no browser automation, HTTP or model calls.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const path='workbench/static/reviewed-batches.js',original=fs.readFileSync(path,'utf8');
const source=original;
class Element{
 constructor(){this.children=[];this.handlers={};this.value='';this.checked=false;this.hidden=false;this.disabled=false;this.textContent='';}
 append(...items){this.children.push(...items);}
 replaceChildren(...items){this.children=items;}
 get options(){return this.children;}
 addEventListener(name,callback){this.handlers[name]=callback;}
}
const elements=new Map(),get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
const timers=new Map(),requests=[],storage=new Map();let sequence=0,accepted=false,busyReads=2;
const batch='batch_'+'a'.repeat(32),base='/api/projects/fixture/reviewed-batches';
const paused={initialized:true,history_only:false,execution_live:false,explicit_resume_required:true,
 grant:{manifest:{budget:{}}},drafts:[],control:{control:{record_sha256:'a'.repeat(64),commands:[{action:'pause'}]},
 progress:{chapter:4,stage:'generation',status:'paused'}},recovery:null};
const response=(status,payload)=>({ok:status===200,status,json:async()=>payload});
const fetch=async(url,options={})=>{
 const method=options.method||'GET';requests.push({method,url});
 if(method==='POST'){
  assert.equal(url,base+'/'+batch+'/commands');assert.equal(JSON.parse(options.body).action,'resume');
  accepted=true;return response(200,{execution_live:true,explicit_resume_required:false,task_state:'running',
   control:{control:{record_sha256:'b'.repeat(64),commands:[{action:'pause'},{action:'resume'}]},
    progress:{chapter:4,stage:'generation',status:'running'}}});
 }
 if(accepted){
  if(busyReads-->0)return response(409,{error:{code:'restore_scope_busy',message:'Fixture qualification temporarily busy'}});
  assert.equal(url,base+'/'+batch);
  return response(200,{...paused,control:{control:{record_sha256:'c'.repeat(64),commands:[{action:'resume'},{action:'finish_step'}]},
   progress:{chapter:4,stage:'readback',status:'completed'}}});
 }
 if(url===base)return response(200,{items:[{batch_id:batch,start_chapter:2,end_chapter:4}],monetary_option:{available:true,message:'fixture'}});
 assert.equal(url,base+'/'+batch);return response(200,paused);
};
const context={document:{getElementById:get,createElement:()=>new Element()},
 location:{href:'http://fixture/reviewed-batches?project_id=fixture&batch_id='+batch,search:'?project_id=fixture&batch_id='+batch},
 URL,URLSearchParams,history:{replaceState(){}},fetch,crypto:{randomUUID:()=> 'fixture-resume'},
 localStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},
 setTimeout:callback=>{const id=++sequence;timers.set(id,callback);return id;},clearTimeout:id=>timers.delete(id)};
async function main(){
 vm.runInNewContext(source,context,{filename:path});
 for(let i=0;i<5;i++)await new Promise(setImmediate);
 assert(get('status').textContent.includes('已暂停'));assert.equal(timers.size,0);
 get('command-confirm').checked=true;
 get('command-confirm').handlers.change();assert.equal(get('resume').disabled,false);
 await get('resume').handlers.click();
 for(let i=0;i<5;i++)await new Promise(setImmediate);
 const record={kind:'offline_VM_only_not_browser_evidence',production_asset:true,
  source_sha256:crypto.createHash('sha256').update(original).digest('hex'),accepted:true,
  busy_after_accept:true,visible_status:get('status').textContent,scheduled_readonly_polls:timers.size,
  simulated_requests:requests,real_http_requests:0,real_provider_calls:0,browser_verified:false};
 assert(get('status').textContent.includes('执行中'),'Accepted task must remain visibly running while a GET is busy');
 assert.equal(timers.size,1,'Accepted task must keep scheduling read-only status polls');
 for(let i=0;i<2;i++){
  const [id,callback]=timers.entries().next().value;timers.delete(id);await callback();
  for(let j=0;j<5;j++)await new Promise(setImmediate);
  assert.equal(timers.size,i===0?1:0,'Busy status retries,completed status stops polling');
 }
 assert(get('status').textContent.includes('已完成'),'Final successful read must show completion');
 assert.equal(requests.filter(r=>r.method==='POST').length,1,'No automatic write retry');
 record.final_visible_status=get('status').textContent;record.final_scheduled_polls=timers.size;
 record.readonly_busy_retry_and_completion_passed=true;console.log(JSON.stringify(record));
}
main().catch(error=>{console.error(error.message);process.exitCode=1;});
