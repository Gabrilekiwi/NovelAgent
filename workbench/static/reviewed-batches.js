(() => {
  'use strict';
  const $=id=>document.getElementById(id),params=new URLSearchParams(location.search);
  const project=params.get('project_id'),base=`/api/projects/${encodeURIComponent(project || '')}/reviewed-batches`;
  const storage=`novelagent.batch-request:${project}`;
  let current=null,preview=null,option=null,busy=false,timer=null,pending=null,authorRequests=[],restoredPreview=null;
  const stageNames={generation:'生成正文',extraction:'提取完整事实',quality:'完整质量检查',repair_generation:'一次修复正文',
    repair_extraction:'重新提取完整事实',repair_quality:'修复后完整检查',publication:'正式提交',readback:'核对正式正文和事实',author_review:'等待作者处理'};
  const stateNames={paused:'已暂停',running:'执行中',completed:'已完成',stopped:'已停止'};
  const node=(tag,text)=>{const n=document.createElement(tag);n.textContent=text;return n;};
  const url=part=>`${base}/${encodeURIComponent($('batch').value)}${part || ''}`;
  const api=async(path,body,key)=>{
    const response=await fetch(path,body===undefined?{cache:'no-store'}:{method:'POST',headers:{'Content-Type':'application/json',
      ...(key?{'Idempotency-Key':key}:{})},body:JSON.stringify(body)});
    const payload=await response.json();
    if(!response.ok)throw Error(payload.error?.message || (typeof payload.detail==='string'?payload.detail:'本次操作未完成，请刷新核对原进度。'));
    return payload;
  };
  const fail=error=>{$('error').textContent=error.message || '本次操作未完成。';$('error').hidden=false;};
  const save=value=>{localStorage.setItem(storage,JSON.stringify(value));pending=value;};
  const clear=()=>{localStorage.removeItem(storage);pending=null;};
  const expected=()=>current?.recovery?.expected_control_sha256 || current?.control?.control?.record_sha256;
  const confirmed=()=>{if(!$('command-confirm').checked)throw Error('请先核对当前进度并勾选操作确认。');};
  const displayManifest=m=>{
    const box=$('manifest');box.replaceChildren();box.hidden=false;
    box.append(node('h3',`第 ${m.start_chapter}–${m.end_chapter} 章`),node('p',`模型：${m.model_selection.openai_model}；最多每章修复一次，失败暂停。`),
      node('p',`批次限额：${m.budget.amount_limit} ${m.budget.currency}，${m.budget.max_provider_calls} 次调用；输入 ${m.budget.max_input_tokens}、输出 ${m.budget.max_output_tokens} token。`));
    const b=m.budget.chapter_budgets[0];box.append(node('p',`每章限额：${b.amount_limit} ${b.currency}，${b.max_provider_calls} 次调用，主动执行 ${b.max_active_seconds} 秒。`));
    for(const row of b.pricing)box.append(node('p',`每百万 token：输入 ${row.input_price_per_million}，缓存命中 ${row.cache_read_price_per_million ?? '未知'}，输出 ${row.output_price_per_million} ${row.currency}。`));
    for(const item of m.outlines){const detail=document.createElement('details');detail.append(node('summary',`第 ${item.blueprint.chapter_index} 章章纲`));
      for(const beat of item.blueprint.required_beats || [])detail.append(node('p',beat.text));
      detail.append(node('p',item.blueprint.ending_pressure || ''));box.append(detail);}
  };
  const render=()=>{
    const progress=current?.control?.progress,live=current?.execution_live===true,recovery=current?.recovery;
    const terminal=progress && ['stopped','completed'].includes(progress.status),author=progress?.stage==='author_review';
    $('commands').hidden=!current && !pending;$('command-confirm-label').hidden=!current && !pending;
    $('progress').replaceChildren();$('usage').replaceChildren();
    if(progress){$('status').textContent=`${stateNames[progress.status] || '待核对'} · 第 ${progress.chapter} 章 · ${stageNames[progress.stage] || '核对原记录'}`;
      $('progress').append(node('p',current.history_only?'恢复副本保留了原批次记录。续接前需要重新核对当前写入位置；原预算和已有用量不会清零。':live?'原后台任务仍在执行，刷新只读取进度。':terminal?'本批次已结束。':'当前没有后台任务执行；开始或继续需要明确确认。'));
      if(current.task?.result?.error_code)$('progress').append(node('p','原执行遇到问题并已停止派发，请核对原稿、预算与恢复记录。'));
      const u=current.usage,b=current.grant.manifest.budget;
      if(u)$('usage').append(node('p',`金额占用（估算及预留）：${u.charged_amount} / ${b.amount_limit} ${u.currency}；调用 ${u.charged.calls} / ${b.max_provider_calls} 次；输入 ${u.charged.input_tokens}、输出 ${u.charged.output_tokens} token。`));
      if(current.chapter_time)$('usage').append(node('p',`本章已测主动执行 ${current.chapter_time.active_seconds} 秒${current.chapter_time.active_status==='unknown'?'；原计时不完整，不能据此开始新调用。':''}`));
    }
    else $('status').textContent=current?'原批次已保存，等待明确完成准备。':'请选择已保存批次，或核对并准备新批次。';
    $('prepare-panel').hidden=Boolean(current);
    $('restored-panel').hidden=!current?.history_only;
    $('restored-preview').disabled=busy || !current?.history_only;
    $('restored-prepare').disabled=busy || !current?.history_only || !restoredPreview?.can_prepare || !$('restored-confirm').checked;
    $('initialize').hidden=!current || current.initialized;
    $('initialize').disabled=busy || !$('command-confirm').checked;
    for(const action of ['start','resume','pause','stop']){
      const allowed=Boolean(current?.initialized && !current.history_only && !terminal && !recovery && (action==='start'?current.control.control.commands.length===0:
        action==='resume'?!live && !author:action==='pause'?progress.status==='running':true));
      $(action).disabled=busy || !$('command-confirm').checked || !allowed;
    }
    $('recover-stage').hidden=!recovery || !['claim_step','finish_step','begin_repair','author_takeover'].includes(recovery.action);
    $('recover-stage').disabled=busy || live || !$('command-confirm').checked;
    $('retry-command').hidden=!pending;$('retry-command').disabled=busy || live || !$('command-confirm').checked;
    $('recovery-note').hidden=!recovery;$('recovery-note').textContent=live?'原任务仍在结束，等待完成后再恢复。':
      recovery && ['start','resume','pause','stop'].includes(recovery.action)?'原控制命令保存中断。请使用本浏览器保留的原请求续办；恢复本身不会启动模型调用。':
      '原步骤记录保存中断。恢复只核验并补齐原记录，之后仍需明确继续。';
    const quality=current?.quality;$('quality-panel').hidden=!quality;
    $('quality').replaceChildren();
    for(const finding of quality?.findings || []){$('quality').append(node('p',finding.message));for(const span of finding.quotes || [])$('quality').append(node('blockquote',span.quote));}
    for(const reason of quality?.pause_reasons || [])$('quality').append(node('p',({quality_coverage_incomplete:'必要检查未完成。',authoritative_state_not_accepted:'事实状态需要核对。'}[reason] || '必要条件未通过，请核对本章。')));
    $('takeover-panel').hidden=!current || live || terminal || !(current.drafts?.length || current.takeover);
    const selected=$('draft').value;$('draft').replaceChildren();
    for(const draft of current?.drafts || []){const n=node('option',`${draft.stage==='repair_generation'?'修复稿':'初稿'} · 草稿版本 ${draft.revision}`);n.value=draft.draft_id;$('draft').append(n);}
    if([...$('draft').options].some(n=>n.value===selected))$('draft').value=selected;
    $('draft').disabled=busy || author;
    $('takeover').disabled=busy || live || !$('command-confirm').checked || (!recovery && progress?.status!=='paused') || author || !$('draft').value;
    $('edit').hidden=!current?.takeover;
    if(current?.takeover){const draft=current.takeover.takeover.draft_at_takeover;
      $('edit').href=`/?candidate_project=${encodeURIComponent(project)}&candidate_chapter=${draft.chapter}&candidate_id=${encodeURIComponent(draft.candidate_id)}#/projects/${encodeURIComponent(project)}/chapters/${draft.chapter}`;}
    authorRequests=[];$('author-request').replaceChildren();
    for(const review of current?.author_reviews || []){
      const saved=localStorage.getItem(`novelagent.publication:${project}:${current.takeover.takeover.chapter}:${review.id}`);
      if(!saved)continue;const request=JSON.parse(saved);
      if(request?.body?.review_id!==review.id || typeof request.key!=='string')continue;
      const n=node('option',`作者审核版本 ${review.revision} · ${review.state==='confirmed'?'已确认':'待确认'}`);n.value=String(authorRequests.length);
      authorRequests.push(request);$('author-request').append(n);
    }
    $('author-request-label').hidden=!author;$('complete-author').hidden=!author && recovery?.action!=='finish_author_chapter';
    $('complete-author').disabled=busy || !$('command-confirm').checked || !authorRequests.length;
    $('author-note').textContent=author && !authorRequests.length?'请先从接管稿件完成事实审核和确认发布；保留发布页面所在浏览器的原请求记录。':'';
    $('authorize').disabled=busy || !preview || !$('grant-confirm').checked;
    $('preview').disabled=busy || !option?.available;
    $('refresh').disabled=busy;$('batch').disabled=busy;
    if(timer)clearTimeout(timer);timer=null;
    if(live && !busy)timer=setTimeout(()=>run(loadStatus),2000);
  };
  const run=async operation=>{if(busy)return;busy=true;$('error').hidden=true;render();try{await operation();}catch(error){fail(error);}finally{busy=false;render();}};
  const loadStatus=async()=>{
    $('command-confirm').checked=false;
    restoredPreview=null;$('restored-confirm').checked=false;$('restored-summary').replaceChildren();
    const address=new URL(location.href);if($('batch').value)address.searchParams.set('batch_id',$('batch').value);else address.searchParams.delete('batch_id');history.replaceState(null,'',address);
    current=$('batch').value?await api(url()):null;render();
  };
  const loadList=async selected=>{
    const result=await api(base);option=result.monetary_option;
    $('money-note').textContent=option.available?`${option.model}；每次调用最多按 ${option.input_reservation_per_call} 输入 token 预留。${option.message}`:option.message;
    $('batch').replaceChildren(node('option','准备新批次'));$('batch').options[0].value='';
    for(const item of result.items){const n=node('option',`第 ${item.start_chapter}–${item.end_chapter} 章`);n.value=item.batch_id;$('batch').append(n);}
    $('batch').value=selected || '';await loadStatus();
  };
  const send=async operation=>{
    save(operation);
    const result=await api(operation.url,operation.body,operation.key);
    // Keep the server's accepted task state if the following read is busy.
    // Polls remain read-only; an accepted write is never automatically resent.
    if(result?.control && typeof result.execution_live==='boolean')current={...current,...result};
    clear();
    await loadList(operation.batch || result.grant?.manifest.batch_id);
  };
  $('prepare-form').addEventListener('input',()=>{preview=null;$('manifest').hidden=true;$('grant-confirm-label').hidden=true;$('grant-confirm').checked=false;render();});
  $('prepare-form').addEventListener('submit',event=>{event.preventDefault();void run(async()=>{
    const f=$('prepare-form').elements,number=name=>Number(f[name].value),text=name=>f[name].value.trim();
    const configuration={chapter_limits:{max_provider_calls:number('chapter_calls'),max_input_tokens:number('chapter_input'),
      max_output_tokens:number('chapter_output'),max_active_seconds:text('max_active_seconds'),amount_limit:text('chapter_amount'),currency:text('currency'),
      pricing:[{...option.pricing_template,currency:text('currency'),input_price_per_million:text('input_price'),
        cache_read_price_per_million:text('cached_price'),output_price_per_million:text('output_price')}]},
      batch_limits:{amount_limit:text('batch_amount'),max_provider_calls:number('batch_calls'),max_input_tokens:number('batch_input'),max_output_tokens:number('batch_output')}};
    const body={end_chapter:number('end_chapter'),configuration},key=crypto.randomUUID();
    preview={body,key,...await api(`${base}/preview`,body,key)};displayManifest(preview.manifest);
    $('grant-confirm-label').hidden=false;$('grant-confirm').checked=false;
  });});
  $('authorize').addEventListener('click',()=>run(async()=>{if(!preview || !$('grant-confirm').checked)return;
    await send({url:base,batch:preview.manifest.batch_id,key:preview.key,body:{...preview.body,expected_manifest_sha256:preview.manifest.manifest_sha256,author_confirmed:true}});preview=null;}));
  $('batch').addEventListener('change',()=>run(loadStatus));$('refresh').addEventListener('click',()=>run(()=>loadList($('batch').value)));
  for(const action of ['start','resume','pause','stop'])$(action).addEventListener('click',()=>run(async()=>{confirmed();
    await send({url:url('/commands'),batch:$('batch').value,key:crypto.randomUUID(),body:{action,expected_control_sha256:expected(),author_confirmed:true}});}));
  $('initialize').addEventListener('click',()=>run(async()=>{confirmed();await send({url:url('/initialize'),batch:$('batch').value,body:{author_confirmed:true}});}));
  $('recover-stage').addEventListener('click',()=>run(async()=>{confirmed();const r=current.recovery;
    await send({url:url('/recover-stage'),batch:$('batch').value,body:{action:r.action,chapter:r.chapter,stage:r.stage,author_confirmed:true}});}));
  $('retry-command').addEventListener('click',()=>run(async()=>{confirmed();if(!pending)return;
    const operation={...pending};if(current?.recovery && ['start','resume','pause','stop'].includes(current.recovery.action))operation.url=url('/recover-command');
    await send(operation);}));
  $('takeover').addEventListener('click',()=>run(async()=>{confirmed();await send({url:url('/takeover'),batch:$('batch').value,
    body:{draft_id:$('draft').value,expected_control_sha256:expected(),author_confirmed:true}});}));
  $('complete-author').addEventListener('click',()=>run(async()=>{confirmed();const request=authorRequests[Number($('author-request').value)];if(!request)throw Error('未找到原发布请求。');
    await send({url:url('/complete-author'),batch:$('batch').value,body:{chapter:current.recovery?.chapter || current.control.progress.chapter,
      request,expected_control_sha256:expected(),author_confirmed:true}});}));
  $('restored-preview').addEventListener('click',()=>run(async()=>{
    restoredPreview=await api(url('/restored-continuation/preview'),{});$('restored-confirm').checked=false;
    const r=restoredPreview;$('restored-summary').replaceChildren(
      node('p',`原位置：第 ${r.original_progress.chapter} 章 · ${stageNames[r.original_progress.stage] || '待核对'}。`),
      node('p',`保留金额占用 ${r.budget_usage.charged_amount} ${r.budget_usage.currency}，调用 ${r.budget_usage.charged.calls} 次。`),
      node('p',r.can_prepare?'原记录与当前恢复位置已核对；确认后仍需明确继续或接管。':'当前无法准备续接，请保留原记录并处理以下条件。'));
    const labels={original_control_advanced_or_torn:'原控制记录已前进或保存未完成。',original_step_requires_recovery:'原步骤需先恢复。',
      original_source_grant_changed:'原授权记录变化。',original_source_control_changed:'原来源控制变化。',formal_position_differs:'正式提交位置不同。',
      original_spending_not_available:'原用量未结算或已超限。',frozen_author_inputs_changed:'冻结章纲或作者来源变化。',
      frozen_model_or_policy_changed:'原模型或策略变化。',software_changed:'软件版本变化。',original_parent_readback_not_available:'前章原回读证明需核对。',restored_authoritative_context_differs:'当前事实状态与原回读不同。'};
    for(const reason of r.blockers || [])$('restored-summary').append(node('p',labels[reason] || '原章节执行时间不完整或已用尽。'));
  }));
  $('restored-prepare').addEventListener('click',()=>run(async()=>{
    if(!restoredPreview?.can_prepare || !$('restored-confirm').checked)return;
    await send({url:url('/restored-continuation'),batch:$('batch').value,
      body:{expected_preview_sha256:restoredPreview.record_sha256,author_confirmed:true}});
  }));
  for(const id of ['command-confirm','grant-confirm','draft','restored-confirm'])$(id).addEventListener('change',render);
  void run(async()=>{if(!project)throw Error('请从作品概览进入自动连续写作。');$('back').href=`/#/projects/${encodeURIComponent(project)}/overview`;
    const saved=localStorage.getItem(storage);if(saved){pending=JSON.parse(saved);if(!pending || !pending.url?.startsWith(base) || !pending.body)throw Error('原批次请求记录不完整，请保留记录。');}
    await loadList(params.get('batch_id') || pending?.batch || '');
  });
})();
