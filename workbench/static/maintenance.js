(() => {
  "use strict";
  const root=document.querySelector('[data-maintenance-controls]');
  if (!root) return;
  const one=(key)=>root.querySelector(`[data-maintenance-${key}]`);
  let current=null, busy=false;
  let restoreScope=null;
  const messages={
    interrupted:'上次维护尚未结束。数据库初始化、编辑和新任务仍暂停；请继续原维护，核对后再结束维护。',
    draining:'正在等待已开始的操作完成，暂不接受编辑和新任务。',
    quiescent:'已暂停编辑和新任务。请核对当前维护操作，结束维护后将恢复编辑与任务调度。',
    capturing:'正在取得快照，暂不能结束维护。',
  };
  const render=()=>{
    const state=current?.state;
    one('begin').hidden=state!=='idle'||!!restoreScope?.controller;
    one('notice').hidden=state==='idle';
    one('message').textContent=restoreScope?.controller ?
      (state==='interrupted' ? '原恢复维护已中断；请明确继续原维护，再核对原恢复会话。' :
        '原副本已交出写资格。结束维护后，此实例仍只能核对原会话或打开目标副本。') :
      messages[state]||'维护状态暂不可读，请刷新核对。';
    one('finish').textContent=restoreScope?.controller ? '结束原维护' : '结束维护，恢复使用';
    one('resume').hidden=state!=='interrupted';
    one('finish').hidden=state!=='quiescent';
    root.querySelectorAll('button').forEach(button=>{button.disabled=busy;});
  };
  const request=async(action)=>{
    if (busy) return;
    busy=true;one('error').hidden=true;render();
    try {
      const response=await fetch('/api/maintenance'+(action?'/'+action:''),{
        method:action?'POST':'GET',credentials:'same-origin',cache:'no-store',
        ...(action?{headers:{'Content-Type':'application/json'},body:JSON.stringify(action==='begin'?{}:{operation_id:current?.operation_id})}:{}),
      });
      const payload=await response.json();
      if (!response.ok) throw new Error(window.NovelAgentUI.errorMessage(payload,response.status));
      current=payload;
      if (action==='finish') window.dispatchEvent(new HashChangeEvent('hashchange'));
    } catch (error) {
      one('error').textContent=error.message;one('error').hidden=false;
      // A rejected or lost transition response does not describe the current
      // durable gate. Read it again before enabling any dependent controls.
      try {
        const response=await fetch('/api/maintenance',{credentials:'same-origin',cache:'no-store'});
        if (!response.ok) throw new Error('maintenance status unavailable');
        const status=await response.json();
        if (!['idle','interrupted','draining','quiescent','capturing'].includes(status.state)) {
          throw new Error('maintenance status invalid');
        }
        current=status;
      } catch (_) { current=null; }
    } finally {
      busy=false;render();
      document.dispatchEvent(new CustomEvent('workbench:maintenance',{detail:current}));
    }
  };
  ['begin','resume','finish'].forEach(action=>one(action).addEventListener('click',()=>request(action)));
  one('refresh').addEventListener('click',()=>request());
  window.addEventListener('focus',()=>request());
  document.addEventListener('workbench:error',()=>request());
  document.addEventListener('workbench:maintenance-status',event=>{
    current=event.detail;render();
  });
  document.addEventListener('workbench:restore-scope',event=>{restoreScope=event.detail;render();});
  request();
})();
