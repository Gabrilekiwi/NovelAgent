(() => {
  "use strict";
  const withFeedback = globalThis.NovelAgentFeedback?.action || (handler => handler);
  const root = document.querySelector('[data-recovery-controls]');
  if (!root) return;
  const one = key => root.querySelector(`[data-recovery-${key}]`);
  const labels = {materialize:'复制归档资料',bind_roots:'核对副本目录绑定',prepare_database:'准备只读工作台',
    preview_handoff:'核对写资格交接',handoff:'确认转移写资格',capture_usage:'保留最新消费证据',
    prepare_budget:'核对并保留预算',prepare_jobs:'保留并隔离历史任务',
    prepare_executions:'准备新的执行目录',activate:'启用恢复副本',cancel:'取消本次恢复'};
  let maintenance = null, session = null, preview = null, busy = false, currentId = '';
  let restoreScope = null;
  const controllerOnly = () => !!restoreScope?.controller;
  const phaseLabels = {active:'已启用',rolled_back:'原作品资格已恢复',cancelled:'已取消并保留副本'};
  const sessionLabel = value => `${value.destination} · ${labels[value.pending_action || value.next_action] || phaseLabels[value.phase] || '核对资料'}`;
  const ready = () => maintenance?.state === 'quiescent' && maintenance.purpose === 'restore';
  const request = async (path, body) => {
    const response = await fetch(path,{method:body ? 'POST':'GET',credentials:'same-origin',cache:'no-store',
      ...(body ? {headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{})});
    const value = await response.json();
    if (!response.ok) throw new Error(window.NovelAgentUI.errorMessage(value,response.status));
    return value;
  };
  const action = () => session?.pending_action || session?.next_action;
  const render = () => {
    const selected = action();
    const confirm = selected === 'handoff' || selected === 'activate';
    const activationBlocked = selected === 'activate' && !session.pending_action &&
      (session.usage_settled !== true || session.usage_blockers.length || session.continuation_blockers.length);
    one('begin').hidden = controllerOnly();
    one('begin').disabled = busy || controllerOnly() || maintenance?.state !== 'idle';
    one('refresh').disabled = busy;
    one('session').disabled = busy;
    one('archive').disabled = busy || !!session;
    one('destination').disabled = busy || !!session;
    one('preview').disabled = busy || controllerOnly() || !ready() || !!session || !one('archive').value || !one('destination').value.trim();
    one('create').disabled = busy || controllerOnly() || !ready() || !!session || !preview;
    one('next').textContent = session?.pending_action ? `继续原步骤：${labels[selected] || selected}` : labels[selected] || '当前没有待执行步骤';
    one('next').disabled = busy || !ready() || !selected || activationBlocked || (confirm && !one('confirm').checked);
    one('confirm-label').hidden = !confirm;
    one('confirm-message').textContent = selected === 'handoff' ?
      `我已核对 ${session?.destination || ''}，同意停用原副本的写资格；启用前两个副本均不能写作。` :
      '我同意启用此恢复副本；原作品仍停用，重新启动不会自动调用模型。';
    one('readback').disabled = busy || !ready() || !session || !!session.pending_action ||
      ['planned','materialized','roots_bound','rolled_back','cancelled'].includes(session.phase);
    one('rollback').disabled = busy || !ready() || !session || session.pending_action === 'activate' ||
      ['planned','materialized','roots_bound','readonly_ready','active','rolled_back','cancelled'].includes(session.phase);
    one('cancel').disabled = busy || !ready() || !session ||
      !['planned','materialized','roots_bound','readonly_ready','handoff_ready'].includes(session.phase) ||
      ['handoff','activate','rollback'].includes(session.pending_action);
    one('open').disabled = busy || session?.phase !== 'active' ||
      !(maintenance?.state === 'idle' || ready());
    one('readback-open').disabled = busy || !ready();
    if (session) one('status').textContent = session.phase === 'active' ?
      `副本已启用：${session.destination}。可以打开此副本继续核对或写作；重启不会自动调用模型。` :
      session.phase === 'rolled_back' ? '原作品资格已恢复；此副本继续保持不可写。' :
      session.phase === 'cancelled' ? '本次恢复已取消，副本资料已保留，未交接原作品写资格。' :
      activationBlocked ? '消费证据、预算或继续条件尚未核实，副本不能启用；可以恢复原作品写资格。' :
      `恢复目标：${session.destination}。${session.pending_action ? '原步骤尚待确认完成，请明确继续。' : `下一步：${labels[selected] || '核对资料'}。`}`;
    else one('status').textContent = ready() ? '恢复维护已就绪，请核对归档与新的恢复目录。' :
      maintenance?.state === 'interrupted' ? '请先在上方继续原维护，再读取恢复会话。' : '请进入恢复维护；已有其他维护时先完成原操作。';
  };
  const selectSession = value => {
    session = value; currentId = value?.session_id || ''; one('confirm').checked = false;
    one('readback-summary').hidden = true; one('readback-text').value = '';
    if (value) { one('destination').value = value.destination; one('archive').value = value.archive_id; }
    const option = Array.from(one('session').options).find(item => item.value === currentId);
    if (value && option) option.textContent = sessionLabel(value);
    render();
  };
  const refresh = async () => {
    maintenance = await request('/api/maintenance');
    const list = await request('/api/recovery-sessions');
    const archives = controllerOnly() ? {archives:list.sessions.map(value => ({archive_id:value.archive_id}))} : await request('/api/backups');
    one('archive').replaceChildren(new Option('请选择归档',''));
    archives.archives.forEach(item => one('archive').add(new Option(item.archive_id,item.archive_id)));
    one('session').replaceChildren(...(controllerOnly() ? [] : [new Option('建立新会话','')]));
    list.sessions.forEach(item => one('session').add(new Option(sessionLabel(item),item.session_id)));
    if (currentId && list.sessions.some(item => item.session_id === currentId)) {
      one('session').value = currentId;
      selectSession(await request(`/api/recovery-sessions/${encodeURIComponent(currentId)}`));
    }
    render();
    document.dispatchEvent(new CustomEvent('workbench:maintenance-status',{detail:maintenance}));
  };
  const run = async operation => {
    if (busy) return;
    busy = true; one('error').hidden = true; render();
    try { await operation(); }
    catch (error) {
      one('confirm').checked = false;
      one('error').textContent = error.message; one('error').hidden = false;
    }
    finally {
      // An absent HTTP response is not proof that its owned worker stopped.
      try {
        maintenance = await request('/api/maintenance');
        if (currentId) {
          const latest = await request(`/api/recovery-sessions/${encodeURIComponent(currentId)}`);
          if (latest.state_sha256 !== session?.state_sha256) selectSession(latest);
          else session = latest;
        }
        if (restoreScope) {
          const bootstrap = await request('/api/bootstrap');
          document.dispatchEvent(new CustomEvent('workbench:restore-scope',{detail:bootstrap.restore_scope}));
        }
      } catch (error) {
        one('confirm').checked = false;
        one('error').textContent = error.message; one('error').hidden = false;
      }
      busy = false; render();
      document.dispatchEvent(new CustomEvent('workbench:maintenance-status',{detail:maintenance}));
    }
  };
  one('begin').addEventListener('click',withFeedback(() => run(async () => {
    maintenance = await request('/api/maintenance/begin',{purpose:'restore'}); await refresh();
  })));
  one('refresh').addEventListener('click',withFeedback(() => run(refresh)));
  root.addEventListener('toggle',() => { if (root.open) run(refresh); });
  one('session').addEventListener('change',() => run(async () => {
    currentId = one('session').value;
    selectSession(currentId ? await request(`/api/recovery-sessions/${encodeURIComponent(currentId)}`):null);
    preview = null; one('preview-summary').hidden = true;
  }));
  const invalidate = () => { preview = null; one('preview-summary').hidden = true; render(); };
  one('archive').addEventListener('change',invalidate);
  one('destination').addEventListener('input',invalidate);
  one('confirm').addEventListener('change',render);
  one('preview').addEventListener('click',withFeedback(() => run(async () => {
    preview = await request('/api/recovery-sessions/preview',{operation_id:maintenance.operation_id,
      archive_id:one('archive').value,destination:one('destination').value.trim()});
    one('preview-summary').textContent = `归档资料已核对。原作品：${preview.source_root}。恢复目标：${preview.destination}。${preview.continuation_blockers.length ? '继续写作仍有待处理条件；可以先恢复只读资料。':'可继续准备只读副本。'}`;
    one('preview-summary').hidden = false;
  })));
  one('create').addEventListener('click',withFeedback(() => run(async () => {
    if (!preview) throw new Error('请重新核对归档。');
    currentId = 'restore_'+crypto.randomUUID().replaceAll('-','');
    selectSession(await request('/api/recovery-sessions',{session_id:currentId,
      operation_id:maintenance.operation_id,archive_id:preview.archive_id,destination:preview.destination,
      expected_plan_sha256:preview.plan_sha256}));
    await refresh();
  })));
  const step = async selected => {
    selectSession(await request(`/api/recovery-sessions/${encodeURIComponent(currentId)}/steps/${selected}`,{
      operation_id:session.operation_id,expected_state_sha256:session.state_sha256,
      confirmed:selected === 'rollback' || one('confirm').checked,resume:session.pending_action === selected}));
  };
  one('next').addEventListener('click',withFeedback(() => run(() => step(action()))));
  one('rollback').addEventListener('click',withFeedback(() => run(async () => {
    if (!window.confirm('确认恢复原作品的写资格？恢复副本将继续不可写，后续不能直接重新启用此会话。')) return;
    await step('rollback');
  })));
  one('cancel').addEventListener('click',withFeedback(() => run(() => step('cancel'))));
  one('open').addEventListener('click',withFeedback(() => run(async () => {
    const value = await request(`/api/recovery-sessions/${encodeURIComponent(currentId)}/open`,{
      operation_id:session.operation_id,expected_state_sha256:session.state_sha256});
    const url = new URL(value.url);
    if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port ||
        url.username || url.password) throw new Error('恢复工作台地址无法核验，请重新核对。');
    window.location.assign(url.href);
  })));
  const readback = extra => request(`/api/recovery-sessions/${encodeURIComponent(currentId)}/readback`,{
    operation_id:session.operation_id,...extra});
  one('readback').addEventListener('click',withFeedback(() => run(async () => {
    const value = await readback({});
    const select = one('readback-item'); select.replaceChildren();
    value.chapters.forEach(item => select.add(new Option(`正式第 ${item.chapter} 章`,`chapter:${item.chapter}`)));
    value.author_drafts.forEach(item => select.add(new Option(`作者草稿第 ${item.chapter} 章 · r${item.revision}`,`draft:${item.id}`)));
    one('readback-summary').hidden = false;
  })));
  one('readback-open').addEventListener('click',withFeedback(() => run(async () => {
    const [kind,id] = one('readback-item').value.split(':');
    if (!id) throw new Error('请先选择要查看的章节或草稿。');
    const value = await readback(kind === 'chapter' ? {chapter:Number(id)}:{draft_id:id});
    one('readback-text').value = kind === 'chapter' ? value.chapter_text:value.author_draft.text;
    one('readback-summary').hidden = false;
  })));
  document.addEventListener('workbench:maintenance',event => { maintenance = event.detail; render(); });
  document.addEventListener('workbench:restore-scope',event => {
    const previous = restoreScope?.controller?.session_id;
    restoreScope = event.detail;
    const bound = restoreScope?.controller?.session_id;
    if (bound) {
      currentId = bound;
      root.open = true;
      if (previous !== bound && !busy) run(refresh);
    }
    render();
  });
})();
