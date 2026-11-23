(() => {
  'use strict';
  const withFeedback = globalThis.NovelAgentFeedback?.action || (handler => handler);
  const $ = id => document.getElementById(id), params = new URLSearchParams(location.search);
  const project = params.get('project_id'), base = '/api/projects/' + encodeURIComponent(project || '') + '/onboarding/upgrade';
  let maintenance = null, busy = false, preview = null, original = null, cursor = null;
  const reviews = new Map(), sha = value => /^[0-9a-f]{64}$/.test(value || '');
  const reference = () => new URLSearchParams(location.search).get('preview_sha256');
  const ready = () => maintenance?.state === 'quiescent' && maintenance?.purpose === 'backup';
  const clearAcks = () => { $('upgrade-policy-ack').checked = false; $('upgrade-author-ack').checked = false; };
  const element = (tag,text) => { const node = document.createElement(tag); node.textContent = text; return node; };
  const option = (select,value,title) => { const node = element('option',title); node.value = value; select.append(node); };
  const policyKnown = value => value?.recovery_policy?.host_platform === 'Windows' &&
    value.recovery_policy.compatibility === 'exact_issuer_and_verifier_source_build' &&
    value.recovery_policy.restore?.original_machine_control_files_required === true &&
    value.recovery_policy.restore.source_unavailable_write_activation_supported === false &&
    value.recovery_policy.restore.unknown_usage_write_activation_allowed === false;
  const chosen = () => preview || original?.preview;
  function render() {
    const pending = original?.can_continue === true, completed = original?.state === 'completed', value = chosen();
    const eligible = !busy && ready() && policyKnown(value) && maintenance.operation_id === value.operation_id &&
      (Boolean(preview) || pending);
    $('upgrade-baseline').disabled = busy || pending || completed; $('upgrade-archive').disabled = busy || pending || completed;
    $('upgrade-refresh').disabled = busy; $('upgrade-more').disabled = busy; $('upgrade-more').hidden = !cursor;
    $('upgrade-preview').disabled = busy || !ready() || pending || completed || !$('upgrade-baseline').value || !$('upgrade-archive').value;
    $('upgrade-query').disabled = busy || !project || !sha(reference());
    for (const id of ['upgrade-policy-ack','upgrade-author-ack']) $(id).disabled = !eligible;
    const confirmed = eligible && $('upgrade-policy-ack').checked && $('upgrade-author-ack').checked;
    $('upgrade-commit').disabled = !confirmed || !preview || pending;
    $('upgrade-continue').disabled = !confirmed || !pending;
    $('upgrade-summary').hidden = !value;
    if (!value) return;
    $('upgrade-binding').textContent = `截至第 ${value.committed_through} 章 · 作者基线第 ${value.expected_revision} 版 · 归档 ${value.archive_id}`;
    $('upgrade-changes').replaceChildren(...value.changes.map(text => element('li',text.replaceAll('book_id','作品编号'))));
    const labels = {characters:'人物状态',relationships:'人物关系',roster:'名册',locations:'人物位置',inventory:'物品与数量',numeric_counters:'计数',events:'事件'};
    $('upgrade-counts').replaceChildren(...Object.entries(labels).map(([key,label]) => element('li',`${label}：${value.fact_counts[key]} 条`)));
    const policies = policyKnown(value) ? ['仅适用于 Windows 同一台机器及本次认证对应的软件版本。',
      '恢复须保留原机器控制资料和作品登记见证，并在新的空目录中恢复。',
      '启用写入前须完成机器交接和最新用量核验；原来源不可访问或用量不明时，不能启用写入。',
      '恢复不会自动调用模型，后续章节须重新准备。', '认证不涵盖任意外部程序对作品的写入。'] : ['恢复前提无法识别，请保留原操作并重新核对。'];
    $('upgrade-policy').replaceChildren(...policies.map(text => element('li',text)));
  }
  async function request(url,body) {
    const response = await fetch(url,{method:body === undefined ? 'GET' : 'POST',credentials:'same-origin',cache:'no-store',
      ...(body === undefined ? {} : {headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})});
    const value = await response.json();
    if (!response.ok) throw new Error(window.NovelAgentUI.errorMessage(value,response.status));
    return value;
  }
  async function run(action) {
    if (busy) return;
    busy = true; $('upgrade-error').hidden = true; render();
    try { await action(); }
    catch (error) { preview = null; original = null; clearAcks(); $('upgrade-error').textContent = error.message +
      ' 已清除确认；若响应丢失，请刷新原操作状态找回记录。'; $('upgrade-error').hidden = false; }
    finally { busy = false; render(); }
  }
  function remember(value) {
    const url = new URL(location.href);url.searchParams.set('preview_sha256',value.preview_sha256);
    history.replaceState(null,'',url);
  }
  async function query() {
    if (!sha(reference())) throw new Error('原操作查询链接无效。');
    preview = null; clearAcks(); original = await request(base + '/operations/' + reference());
    const labels = {not_started:'原操作尚未开始，请重新核对预览。',intent_saved:'原升级意图已保存，等待明确续办。',
      preparation_interrupted:'原准备阶段已中断，等待明确续办。',prepared:'原事务已准备，等待明确续办。',
      publication_pending:'原事务已提交，等待完成发布。',rollback_pending:'原事务尚未提交，明确续办将回退原事务。',
      rolled_back:'原事务已回退；继续使用前请核对原作品。',completed:'原升级已完成。后续章节需要重新准备。'};
    $('upgrade-operation').textContent = labels[original.state] || '原状态暂不可识别，请保留现场。';
    $('upgrade-status').textContent = $('upgrade-operation').textContent;
  }
  async function list(more = false) {
    preview = null; clearAcks();
    const values = await request('/api/projects/' + encodeURIComponent(project) + '/onboarding/baselines' +
      (more && cursor ? '?after=' + encodeURIComponent(cursor) : ''));
    if (!more) { reviews.clear(); $('upgrade-baseline').replaceChildren(element('option','请选择已确认审核')); $('upgrade-baseline').firstChild.value = ''; }
    for (const review of values.items) if (review.status === 'confirmed') {
      reviews.set(review.id,review);option($('upgrade-baseline'),review.id,`截至第 ${review.chapter} 章 · 第 ${review.revision} 版 · ${review.created_at}`);
    }
    cursor = values.next_cursor;
    if (!more) {
      const archives = await request('/api/backups');$('upgrade-archive').replaceChildren(element('option','请选择归档'));$('upgrade-archive').firstChild.value = '';
      for (const archive of archives.archives) option($('upgrade-archive'),archive.archive_id,archive.archive_id);
    }
  }
  for (const id of ['upgrade-baseline','upgrade-archive']) $(id).addEventListener('change',() => {preview = null;clearAcks();render();});
  for (const id of ['upgrade-policy-ack','upgrade-author-ack']) $(id).addEventListener('change',render);
  $('upgrade-refresh').addEventListener('click',withFeedback(() => run(() => list())));
  $('upgrade-more').addEventListener('click',withFeedback(() => run(() => list(true))));
  $('upgrade-query').addEventListener('click',withFeedback(() => run(query)));
  $('upgrade-preview').addEventListener('click',withFeedback(() => run(async () => {
    clearAcks(); const review = reviews.get($('upgrade-baseline').value);
    if (!ready() || !review) throw new Error('请选择已确认审核并进入备份维护。');
    preview = await request(base + '/preview',{operation_id:maintenance.operation_id,archive_id:$('upgrade-archive').value,
      baseline_id:review.id,expected_revision:review.revision,expected_sha256:review.record_sha256});
    original = null;remember(preview);$('upgrade-status').textContent = '预览已核对，请阅读升级差异和恢复前提后分别确认。';
    $('upgrade-operation').textContent = '尚未提交；刷新不会自动升级。';
  })));
  $('upgrade-commit').addEventListener('click',withFeedback(() => run(async () => {
    if (!preview || !$('upgrade-policy-ack').checked || !$('upgrade-author-ack').checked || !ready()) throw new Error('请重新核对并明确确认。');
    const value = preview;remember(value);preview = null;clearAcks();
    await request(base + '/commit',{operation_id:value.operation_id,archive_id:value.archive_id,baseline_id:value.baseline_id,
      expected_revision:value.expected_revision,expected_sha256:value.expected_sha256,expected_preview_sha256:value.preview_sha256,
      acknowledged_policy_sha256:value.recovery_policy_sha256,confirmed:true});await query();
  })));
  $('upgrade-continue').addEventListener('click',withFeedback(() => run(async () => {
    if (!original?.can_continue || !$('upgrade-policy-ack').checked || !$('upgrade-author-ack').checked || !ready()) throw new Error('请先继续原维护，核对原操作后明确确认。');
    const value = original.preview;original = null;clearAcks();
    await request(base + '/continue',{operation_id:value.operation_id,preview_sha256:value.preview_sha256,
      acknowledged_policy_sha256:value.recovery_policy_sha256,confirmed:true});await query();
  })));
  document.addEventListener('workbench:maintenance',event => {maintenance = event.detail;preview = null;clearAcks();render();});
  window.addEventListener('pagehide',clearAcks);
  if (!project) {$('upgrade-status').textContent = '请从所选作品的作者审核页面进入升级。';render();return;}
  $('review-link').href = '/baseline-review?project_id=' + encodeURIComponent(project);
  run(async () => { await list();if (reference()) await query();else $('upgrade-status').textContent = '请选择已确认基线及新认证归档。'; });
})();
