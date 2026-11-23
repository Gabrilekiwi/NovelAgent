(() => {
  'use strict';
  const withFeedback = globalThis.NovelAgentFeedback?.action || (handler => handler);
  const doc=document, UI=window.NovelAgentUI;
  const fields=['max_provider_calls','max_input_tokens','max_output_tokens','max_active_seconds'];
  const moneyFields=['budget_input_price','budget_cached_price','budget_output_price'];
  const quantity=value => value == null ? '未知' : String(value);
  const priceLines=b=>(b.pricing || []).flatMap(row=>[
    `冻结价格：${row.provider} / ${row.model} / ${row.endpoint_type}；服务档位：${row.billing_policy?.service_tier || '旧价格记录未绑定档位'}`,
    `每百万 token：输入 ${row.input_price_per_million}，缓存命中 ${quantity(row.cache_read_price_per_million)}，输出 ${row.output_price_per_million} ${row.currency}`,
    ...['5m','1h'].filter(ttl=>row[`cache_write_${ttl}_price_per_million`]!=null)
      .map(ttl=>`缓存写入 ${ttl}：${row[`cache_write_${ttl}_price_per_million`]} ${row.currency} / 百万 token`)]);
  const errorMessage=error=>({
    chapter_budget_evidence_invalid:'本章预算记录未能通过核验。请保留原记录并检查恢复状态。',
    chapter_time_evidence_invalid:'本章计时记录未能通过核验。请保留原记录并检查恢复状态。',
    chapter_budget_exceeded:'本章额度不足，新的请求已暂停。',
    chapter_budget_uncertain:'本章有尚未结算的调用，请先处理原执行。'
  }[error?.payload?.error?.code] || error.message || UI.errorMessage(error.payload,error.status));
  const presentation=report => {
    if(!report)return {blocked:true,message:'正在读取本章预算…',lines:[]};
    if(report.state==='not_configured')return {blocked:true,message:'请在首次生成前确认本章限额。',lines:[]};
    if(report.state==='initializing')return {blocked:true,message:'上次预算确认中断。核对原限额后可继续完成确认，已有配置不会更改。',
      lines:[report.budget.amount_limit==null?'原预算未启用金额控制。':`原金额上限：${report.budget.amount_limit} ${report.budget.currency}`,
        ...priceLines(report.budget)]};
    if(report.state!=='ready' || !report.budget || !report.usage || !report.time)throw Error('预算记录不完整，请重新读取。');
    const b=report.budget,u=report.usage,t=report.time;
    const lines=[
      `调用占用：${quantity(u.charged.calls)} / ${b.max_provider_calls} 次`,
      `输入 token 占用：${quantity(u.charged.input_tokens)} / ${b.max_input_tokens}`,
      `输出 token 占用：${quantity(u.charged.output_tokens)} / ${b.max_output_tokens}`,
      `已测主动执行：${quantity(t.active_seconds)} / ${b.max_active_seconds} 秒${t.active_time_complete?'':'（尚有未结算区间）'}`,
      `作者等待：${quantity(t.author_wait_seconds)} 秒`,
      `端到端历时：${quantity(t.end_to_end_seconds)} 秒`,
      `其中本地恢复已测耗时：${quantity(t.local_recovery_active_seconds ?? '0')} 秒`,
      u.estimated_spend ? `按自填单价估算占用：${u.estimated_spend.amount} / ${u.estimated_spend.limit} ${u.estimated_spend.currency}，下一次请求预计超额时会停止。` : b.amount_limit==null ? '金额未受控：尚未配置可核验的价格与计费输入上限。'
        : `金额占用（估算及预留）：${quantity(u.charged_amount)} / ${b.amount_limit} ${b.currency}`,
      '占用包含已用和未释放的预留，不能当作实际账单；每次请求前还会检查所需额度。'
    ];
    if(b.spending_target)lines.push(`本章预算目标：${b.spending_target.amount} ${b.spending_target.currency}（不是账单硬上限）。`);
    if(b.amount_limit!=null)lines.push(...priceLines(b));
    let message='本章限额已确认；生成与事实提取共用，刷新不会重置。';
    let blocked=true;
    if(t.active_status==='running')message='本章正在执行，当前区间结束后更新已测耗时。';
    else if(t.active_status==='unknown')message='上次执行未留下完整计时，请先处理恢复。原调用和时间证据仍保留。';
    else if(u.unresolved_attempts.length)message='有调用尚未结算，服务商可能已计费。请先核对使用记录，原预留继续保留。';
    else if(t.active_limit_reached || u.exceeded.length || u.charged.calls>=b.max_provider_calls
      || u.charged.input_tokens>=b.max_input_tokens || u.charged.output_tokens>=b.max_output_tokens)message='本章限额已用尽，已暂停新的模型请求。已有草稿仍可阅读和编辑。';
    else { blocked=false; }
    return {blocked,message,lines};
  };
  const panel=doc.querySelector('[data-chapter-budget-panel]');
  const form=doc.querySelector('[data-chapter-budget-form]');
  const status=doc.querySelector('[data-chapter-budget-status]');
  const body=doc.querySelector('[data-chapter-budget-body]');
  const refresh=doc.querySelector('[data-chapter-budget-refresh]');
  const submit=doc.querySelector('[data-chapter-budget-submit]');
  const errorBox=doc.querySelector('[data-chapter-budget-error]');
  const moneyBox=doc.querySelector('[data-chapter-budget-money]');
  const moneyNote=doc.querySelector('[data-chapter-budget-money-note]');
  let context=null,report=null,serial=0,writing=false,onChange=()=>{};
  const node=(tag,text)=>{const value=doc.createElement(tag);value.textContent=text;return value;};
  const current=(captured,number)=>context===captured && serial===number;
  const emit=(blocked,message='')=>onChange(blocked,message);
  const clearError=()=>{errorBox.hidden=true;errorBox.textContent='';};
  const source=doc.querySelector('[data-chapter-budget-source]');
  const inherit=doc.querySelector('[data-budget-inherit]'), inheritText=doc.querySelector('[data-budget-inherit-text]');
  let edited=false, pendingRead=null;
  const prefill=r=>{
    const previous=r.state==='initializing'?r.budget:null, suggestion=r.suggestion;
    if(!previous && (!suggestion || edited))return;
    const limits=previous || suggestion.limits, target=previous ? previous.spending_target || {amount:previous.amount_limit,currency:previous.currency} : suggestion.target;
    const price=previous ? previous.pricing?.[0] || previous.spending_target?.estimate_rates : suggestion.pricing || suggestion.estimate_rates;
    for(const key of fields)form.elements[key].value=String(limits[key]);
    form.elements.budget_amount.value=target?.amount ?? '';form.elements.budget_currency.value=target?.currency ?? '';
    form.elements.monetary_enabled.checked=Boolean(price);
    for (const [name,key] of [['budget_input_price','input_price_per_million'],['budget_cached_price','cache_read_price_per_million'],['budget_output_price','output_price_per_million']])
      form.elements[name].value=price?.[key] ?? '';
  };
  const inheritanceSummary=()=>{
    const s=report?.suggestion;
    if(!context?.canFreeze || report?.state!=='not_configured' || !s?.source_chapter || s.tariff_changed || edited)return '';
    return `沿用第 ${s.source_chapter} 章预算：${s.target.amount} ${s.target.currency}（${s.pricing?'金额限额':s.estimate_rates?'按自填单价拦截预计超额':'预算目标，不是账单硬上限'}），${s.limits.max_provider_calls} 次调用、输入 ${s.limits.max_input_tokens} / 输出 ${s.limits.max_output_tokens} token、${s.limits.max_active_seconds} 秒。`;
  };
  const render=()=>{
    const view=presentation(report);
    if(report?.state==='not_configured' && context?.canFreeze && !context.contextSha)
      view.message='请先保存本章章纲，打开可确认的章节卡后再确认预算。';
    status.textContent=view.message;
    body.replaceChildren(...view.lines.map(line=>node('p',line)));
    form.hidden=!context?.canFreeze || report?.state==='ready' || !report;
    const original=report?.state==='initializing',option=report?.monetary_option;
    const canPrice=Boolean(option?.available || option?.estimate_identity), estimated=Boolean(original ? report.budget.spending_target?.estimate_rates : option?.estimate_identity);
    const summary=inheritanceSummary();
    if(inheritText){inheritText.textContent=summary ? `确认本章意图时${summary}如要调整，可取消勾选。` : '';inherit.closest('label').hidden=!summary;}
    if(source){const s=report?.suggestion;source.textContent=s?.source_chapter ? `已带入本书第 ${s.source_chapter} 章的设置，本章用量从零开始。`+(s.tariff_changed?'模型或计费条件已变化，旧单价未沿用。请单独确认本章采用的限额。':'')
      : s ? '首次设置建议从 1 USD / 章开始；可按作品和服务商费用修改。' : '';}
    if(original)form.elements.monetary_enabled.checked=report.budget.amount_limit!=null || estimated;
    else if(!canPrice && form.elements.monetary_enabled.checked){
      form.elements.monetary_enabled.checked=false;if(form.elements.author_confirmed)form.elements.author_confirmed.checked=false;
    }
    moneyBox.hidden=!canPrice && !(original && (report.budget.amount_limit!=null || estimated));
    form.elements.monetary_enabled.disabled=writing || original || !canPrice;
    moneyNote.textContent=option?.available
      ? `${option.model}，官方标准端点，${option.service_tier}档位；每次最多按 ${option.input_reservation_per_call} 输入 token 保守预留。${option.message}`
      : (estimated ? `${option?.estimate_identity?.model || report?.budget?.spending_target?.estimate_rates?.model}：请按自己的服务商填写输入、输出最高适用单价（含缓存及长上下文档）。` : '') + (option?.message || '当前配置未提供可核对的金额契约。');
    if(original && report.budget.amount_limit!=null)moneyNote.textContent=`继续完成原金额预算：${report.budget.amount_limit} ${report.budget.currency}。本次保留原价格和全部限额。`;
    moneyNote.textContent += form.elements.monetary_enabled.checked ? ' 当前启用按单价拦截。' : ' 当前金额仅作预算目标，不保证账单不超额；本章会按调用、token 和时间拦截。';
    for(const name of ['budget_amount','budget_currency'])form.elements[name].disabled=writing || original;
    const cacheLabel=form.elements.budget_cached_price.closest?.('label');
    if(cacheLabel)cacheLabel.hidden=estimated;
    for(const name of moneyFields){
      form.elements[name].disabled=writing || original || !form.elements.monetary_enabled.checked || !canPrice || (name==='budget_cached_price' && estimated);
      form.elements[name].required=!form.elements[name].disabled;
    }
    if(report?.state==='initializing')for(const name of fields)form.elements[name].value=report.budget[name];
    for(const name of fields)form.elements[name].disabled=writing || report?.state==='initializing';
    submit.textContent=report?.state==='initializing'?'继续确认原预算':form.elements.monetary_enabled.checked?(estimated?'确认并按估算金额拦截':'确认并启用金额上限'):'确认并使用调用、token 和时间限额';
    submit.disabled=writing || !context?.contextSha;
    emit(view.blocked,view.message);
  };
  const path=value=>`/api/projects/${encodeURIComponent(value.projectId)}/chapters/${value.chapter}/budget`;
  const verify=(payload,captured)=>{
    if(payload.project_id!==captured.projectId || payload.book_id!==captured.bookId || payload.chapter!==captured.chapter)
      throw Error('预算记录不属于当前章节。');
    return payload;
  };
  const load=async()=>{
    if(!context || writing)return;
    const captured=context,number=++serial;
    if(form.elements.author_confirmed)form.elements.author_confirmed.checked=false;
    refresh.disabled=true;clearError();report=null;render();
    try {
      const response=await fetch(path(captured),{credentials:'same-origin',headers:UI.requestHeaders()});
      const payload=await UI.parseResponse(response);
      if(!current(captured,number))return;
      if(!response.ok)throw {payload,status:response.status};
      report=verify(payload,captured);prefill(report);render();
    } catch(error) {
      if(!current(captured,number))return;
      report=null;body.replaceChildren();form.hidden=true;
      status.textContent='预算状态不可用，已暂停新的模型请求。';
      errorBox.textContent=errorMessage(error);errorBox.hidden=false;emit(true,status.textContent);
    } finally {if(current(captured,number))refresh.disabled=false;}
  };
  const setContext=(value,callback)=>{
    ++serial;edited=false;if(inherit){inherit.checked=true;inherit.closest('label').hidden=true;}context=value;report=null;writing=false;onChange=callback || (()=>{});
    if(!panel)return;
    panel.hidden=!value;body.replaceChildren();form.reset();clearError();refresh.disabled=false;
    if(!value){emit(false);return;}
    render();pendingRead=load();
  };
  const save=async()=>{
    if(!context || writing || !context.canFreeze || !context.contextSha || !form.reportValidity())return;
    const captured=context,number=++serial;
    try {
      const configuration=report?.state==='initializing'
        ? Object.fromEntries([...fields,'amount_limit','currency','pricing'].map(key=>[key,report.budget[key]]))
        : {amount_limit:null,currency:null,pricing:[]};
      if(report?.state!=='initializing')for(const name of fields) {
        const raw=form.elements[name].value.trim();
        if(name==='max_active_seconds')configuration[name]=raw;
        else {const value=Number(raw);if(!/^[1-9]\d*$/.test(raw) || !Number.isSafeInteger(value))throw Error('请填写有效的正整数限额。');configuration[name]=value;}
      }
      if(report?.state!=='initializing' && form.elements.monetary_enabled.checked) {
        const option=report?.monetary_option;
        const value=name=>form.elements[name].value.trim();
        if(!option?.available && option?.estimate_identity){
          configuration.spending_target={amount:value('budget_amount'),currency:value('budget_currency'),
            estimate_rates:{...option.estimate_identity,input_price_per_million:value('budget_input_price'),output_price_per_million:value('budget_output_price')}};
        }else if(option?.available){
        configuration.amount_limit=value('budget_amount');configuration.currency=value('budget_currency');
        configuration.pricing=[{...option.pricing_template,currency:configuration.currency,
          input_price_per_million:value('budget_input_price'),cache_read_price_per_million:value('budget_cached_price'),
          output_price_per_million:value('budget_output_price')}];
        }else throw Error('当前模型的单价条件已变化，请重新读取预算。');
      }
      if(report?.state==='initializing' && report.budget.spending_target)configuration.spending_target=report.budget.spending_target;
      else if(report?.suggestion && configuration.amount_limit==null && !configuration.spending_target)configuration.spending_target={amount:form.elements.budget_amount.value.trim(),currency:form.elements.budget_currency.value.trim()};
      writing=true;clearError();render();refresh.disabled=true;
      const response=await fetch(path(captured),{method:'POST',credentials:'same-origin',headers:UI.requestHeaders({'Content-Type':'application/json'}),
        body:JSON.stringify({configuration,expected_context_sha256:captured.contextSha,author_confirmed:true})});
      const payload=await UI.parseResponse(response);
      if(!current(captured,number))return;
      if(!response.ok)throw {payload,status:response.status};
      report=verify(payload,captured);
      UI.showToast('本章限额已保存。');
    } catch(error) {
      if(!current(captured,number))return;
      errorBox.textContent=errorMessage(error);errorBox.hidden=false;
      // Do not claim the write failed safely; a lost response requires a read.
      report=null;status.textContent='确认结果待核对，请点击“重新读取”核验保存状态。';body.replaceChildren();emit(true);
    } finally {
      if(current(captured,number)){writing=false;refresh.disabled=false;if(report)render();else {form.hidden=true;submit.disabled=true;}}
    }
  };
  form?.addEventListener('submit',withFeedback(event=>{event.preventDefault();return save();}));
  refresh?.addEventListener('click',withFeedback(load));
  form?.addEventListener('input',()=>{edited=true;if(inherit)inherit.closest('label').hidden=true;});
  form?.addEventListener('change',event=>{
    if(event.target.name!=='author_confirmed'){if(form.elements.author_confirmed)form.elements.author_confirmed.checked=false;if(report)render();}
  });
  function captureInheritance(){
    const summary=inheritanceSummary(), captured=context, selected=report;
    if(!inherit?.checked || !summary || writing)return null;
    return {summary,apply:()=>{if(context===captured && report===selected && !writing && inherit.checked && inheritanceSummary()===summary)return save();}};
  }
  window.NovelAgentChapterBudget={presentation,setContext,refresh:load,captureInheritance,
    pendingInitialization:()=>context && !report ? pendingRead : null};
})();
