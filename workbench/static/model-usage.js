(() => {
  'use strict';
  const doc = document, UI = window.NovelAgentUI;
  const labels = {
    generation:'正文生成',chapter_generation:'正文生成',chapter_draft:'正文生成',chapter_fact_extraction:'事实提取',
    outline_planning:'章纲规划',amendment_semantic_equivalence:'润色语义核对',
    succeeded:'成功',running:'执行中',awaiting_receipt:'等待回执',provider_uncertain:'结果不确定',
    partial_response_failed:'部分响应后失败',budget_rejected:'预算检查拒绝',request_not_sent:'请求未发送',provider_rejected:'服务商拒绝',
    official:'官方端点',openai_compatible:'兼容端点',unknown:'未知',
    provider_reported:'服务商返回',legacy_receipt:'旧回执记录',program_estimate:'程序估算',
    actual:'实际费用',estimated:'估算费用',provider_billing:'服务商费用凭据',pricing_snapshot:'价格快照',
    missing_usage:'未返回用量',synthetic_failure_usage:'失败记录中的程序填充值',unattributed_zero:'旧零值缺少来源',
    conflicting_usage_fields:'用量字段冲突',invalid_usage:'用量值无效',no_attributed_cost:'缺少费用与币种来源',
    invalid_cost_evidence:'费用依据不完整',no_receipt:'尚无回执',frozen_chapter_pricing:'本章冻结价格',
    reserved:'已预留，待结算',held_unknown:'用量不完整，保留占用',settled:'已结算',released:'已释放',
    input_tokens:'输入',uncached_input_tokens:'未缓存输入',output_tokens:'输出',
    cache_read_tokens:'缓存读取',cache_write_5m_tokens:'5 分钟缓存写入',cache_write_1h_tokens:'1 小时缓存写入'
  };
  const node = (tag,text,cls) => { const n=doc.createElement(tag); if(text!=null)n.textContent=String(text); if(cls)n.className=cls; return n; };
  const label = value => labels[value] || value || '未知';
  const tokenLabel = item => item?.value == null ? `未知（${label(item?.reason)}）` : `${item.value}（${label(item.source)}）`;
  const costLabel = cost => cost?.classification === 'unknown' || !cost ? `费用未知（${label(cost?.reason)}）`
    : `${label(cost.classification)} ${cost.amount} ${cost.currency}`;
  const summaryLines = summary => {
    const currencies = Object.entries(summary.costs_by_currency || {});
    const result = [`${summary.attempt_count} 次物理调用尝试 · ${summary.unknown_cost_count} 次费用未知`];
    for(const [currency,group] of currencies) {
      if(group.actual_count) result.push(`实际费用小计 ${group.actual} ${currency}（${group.actual_count} 次）`);
      if(group.estimated_count) result.push(`估算费用小计 ${group.estimated} ${currency}（${group.estimated_count} 次）`);
    }
    if(summary.unknown_cost_count) result.push('存在费用未知的调用，以上小计不能代表完整费用。');
    if(!summary.attempt_count) result.push('尚无持久模型调用记录；这不代表旧运行已证实免费。');
    return result;
  };
  const budgetLines = reports => (reports || []).map(report => {
    const pending=report.items.filter(item=>['reserved','held_unknown'].includes(item.accounting_state)).length;
    const amount=report.charged_amount==null ? '未设置金额上限' : `金额占用 ${report.charged_amount} ${report.currency}`;
    return `第 ${report.chapter} 章预算：${amount}；${pending} 次待结算。预算占用单独列示，不计入费用小计。`;
  });
  let route = null, requestNumber = 0;
  const panel = doc.querySelector('[data-model-usage-panel]');
  const heading = doc.querySelector('[data-model-usage-status]');
  const body = doc.querySelector('[data-model-usage-body]');
  const refresh = doc.querySelector('[data-model-usage-refresh]');
  const render = report => {
    body.replaceChildren();
    const summary = node('div',null,'model-usage-summary');
    for(const line of summaryLines(report.summary))summary.append(node('p',line));
    for(const line of budgetLines(report.chapter_budgets))summary.append(node('p',line,'muted-copy'));
    body.append(summary);
    for(const item of report.items) {
      const details=node('details',null,'model-usage-attempt');
      details.append(node('summary',`${label(item.stage)} · ${label(item.status)} · ${costLabel(item.cost)}`));
      const facts=node('dl',null,'model-usage-grid');
      const field=(name,value)=>{ const row=node('div');row.append(node('dt',name),node('dd',value));facts.append(row); };
      field('请求模型',`${item.provider} / ${item.requested_model}`);
      field('返回模型',item.actual_model || '未知');field('端点',label(item.endpoint_type));
      field('开始时间',item.created_at);field('回执时间',item.received_at || '尚无回执');
      field('输入 token',tokenLabel(item.usage.input_tokens));field('输出 token',tokenLabel(item.usage.output_tokens));
      field('总 token',tokenLabel(item.usage.total_tokens));
      field('缓存读取 token',tokenLabel(item.usage.cache_read_tokens));field('缓存写入 token',tokenLabel(item.usage.cache_creation_tokens));
      field('推理 token',tokenLabel(item.usage.reasoning_tokens));
      field('输入预算预留',item.reservation.reserved_input_tokens ?? '未知');
      field('输出预算预留',item.reservation.reserved_output_tokens ?? '未知');
      field('费用',costLabel(item.cost));
      if(item.budget_accounting) {
        const budget=item.budget_accounting;
        field('本章预算结算',label(budget.accounting_state));
        field('金额占用',budget.charged_amount==null ? '未设置金额上限' : `${budget.charged_amount} ${budget.currency}`);
      }
      if(item.cost.source)field('费用依据',item.cost.source==='frozen_chapter_pricing'
        ? `${label(item.cost.source)}（第 ${item.budget_accounting.chapter} 章）`
        : `${label(item.cost.source)} · ${item.cost.source_id}`);
      if(item.cost.calculation)for(const term of item.cost.calculation.terms)field(
        `${label(term.usage_field)}价格计算`,
        `${term.quantity} token × ${term.unit_price_per_million} ${item.cost.currency} / 百万 token`);
      details.append(facts,node('p','预算预留是调用前的额度保留，不是实际消耗；缓存与推理明细不会重复加到总 token。','muted-copy'));
      const evidence=node('details');evidence.append(node('summary','查看调用凭证标识'),
        node('p',`执行 ${item.execution_id}`),node('p',`调用 ${item.call_id} / ${item.attempt_id}`),
        node('p',`意图 ${item.intent_hash}`),node('p',`回执 ${item.receipt_hash || '尚无'}`));
      if(item.budget_accounting)evidence.append(node('p',`冻结预算 ${item.budget_accounting.budget_sha256}`));
      details.append(evidence);body.append(details);
    }
  };
  const load = async () => {
    if(!route || !panel.open)return;
    const current=route, number=++requestNumber;
    refresh.disabled=true;body.replaceChildren();heading.textContent='正在核验调用记录…';
    try {
      const response=await fetch(`/api/projects/${encodeURIComponent(current.projectId)}/model-calls`,{
        headers:UI.requestHeaders(),credentials:'same-origin'});
      const payload=await UI.parseResponse(response);
      if(number!==requestNumber || route!==current)return;
      if(!response.ok)throw {payload,status:response.status};
      if(payload.project_id!==current.projectId || payload.schema_version!=='model_usage_report_v1')throw new Error('模型调用记录不属于当前作品。');
      render(payload);heading.textContent='已核验本作品调用凭证。刷新仅查询记录，不会调用模型。';
    } catch(error) {
      if(number!==requestNumber || route!==current)return;
      body.replaceChildren();heading.textContent=error?.message || UI.errorMessage(error?.payload,error?.status);
    } finally { if(number===requestNumber)refresh.disabled=false; }
  };
  const setRoute = value => {
    route = value?.projectId && ['overview','chapter','history'].includes(value.area) ? {...value} : null;
    ++requestNumber;
    if(!panel)return;
    panel.hidden=!route;panel.open=false;body.replaceChildren();refresh.disabled=false;
    heading.textContent='按本作品的物理调用查看用量、来源和费用。';
  };
  panel?.addEventListener('toggle',()=>{if(panel.open)load();});
  refresh?.addEventListener('click',load);
  window.NovelAgentModelUsage=Object.freeze({setRoute,tokenLabel,costLabel,summaryLines,budgetLines});
})();
