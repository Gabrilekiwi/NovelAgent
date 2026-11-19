(() => {
  'use strict';
  const doc=document, UI=window.NovelAgentUI;
  const fields=['max_provider_calls','max_input_tokens','max_output_tokens','max_active_seconds'];
  const moneyFields=['budget_currency','budget_amount','budget_input_price','budget_cached_price','budget_output_price'];
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
      b.amount_limit==null ? '金额未受控：尚未配置可核验的价格与计费输入上限。'
        : `金额占用（估算及预留）：${quantity(u.charged_amount)} / ${b.amount_limit} ${b.currency}`,
      '占用包含已用和未释放的预留，不能当作实际账单；每次请求前还会检查所需额度。'
    ];
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
  const render=()=>{
    const view=presentation(report);status.textContent=view.message;
    body.replaceChildren(...view.lines.map(line=>node('p',line)));
    form.hidden=!context?.canFreeze || report?.state==='ready' || !report;
    const original=report?.state==='initializing',option=report?.monetary_option;
    if(original)form.elements.monetary_enabled.checked=report.budget.amount_limit!=null;
    else if(!option?.available && form.elements.monetary_enabled.checked){
      form.elements.monetary_enabled.checked=false;form.elements.author_confirmed.checked=false;
    }
    moneyBox.hidden=!option?.available && !(original && report.budget.amount_limit!=null);
    form.elements.monetary_enabled.disabled=writing || original || !option?.available;
    moneyNote.textContent=option?.available
      ? `${option.model}，官方标准端点，${option.service_tier}档位；每次最多按 ${option.input_reservation_per_call} 输入 token 保守预留。${option.message}`
      : option?.message || '当前配置未提供可核对的金额契约。';
    if(original && report.budget.amount_limit!=null)moneyNote.textContent=`继续完成原金额预算：${report.budget.amount_limit} ${report.budget.currency}。本次保留原价格和全部限额。`;
    for(const name of moneyFields){
      form.elements[name].disabled=writing || original || !form.elements.monetary_enabled.checked || !option?.available;
      form.elements[name].required=!form.elements[name].disabled;
    }
    if(report?.state==='initializing')for(const name of fields)form.elements[name].value=report.budget[name];
    for(const name of fields)form.elements[name].disabled=writing || report?.state==='initializing';
    submit.textContent=report?.state==='initializing'?'继续确认原预算':'确认本章限额';
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
    form.elements.author_confirmed.checked=false;
    refresh.disabled=true;clearError();report=null;render();
    try {
      const response=await fetch(path(captured),{credentials:'same-origin',headers:UI.requestHeaders()});
      const payload=await UI.parseResponse(response);
      if(!current(captured,number))return;
      if(!response.ok)throw {payload,status:response.status};
      report=verify(payload,captured);render();
    } catch(error) {
      if(!current(captured,number))return;
      report=null;body.replaceChildren();form.hidden=true;
      status.textContent='预算状态不可用，已暂停新的模型请求。';
      errorBox.textContent=errorMessage(error);errorBox.hidden=false;emit(true,status.textContent);
    } finally {if(current(captured,number))refresh.disabled=false;}
  };
  const setContext=(value,callback)=>{
    ++serial;context=value;report=null;writing=false;onChange=callback || (()=>{});
    if(!panel)return;
    panel.hidden=!value;body.replaceChildren();form.reset();clearError();refresh.disabled=false;
    if(!value){emit(false);return;}
    render();void load();
  };
  form?.addEventListener('submit',async event=>{
    event.preventDefault();
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
        if(!option?.available)throw Error('当前模型的金额契约不可用，请重新读取预算。');
        const value=name=>form.elements[name].value.trim();
        configuration.amount_limit=value('budget_amount');configuration.currency=value('budget_currency');
        configuration.pricing=[{...option.pricing_template,currency:configuration.currency,
          input_price_per_million:value('budget_input_price'),cache_read_price_per_million:value('budget_cached_price'),
          output_price_per_million:value('budget_output_price')}];
      }
      if(!form.elements.author_confirmed.checked)throw Error('请先勾选确认本章限额。');
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
  });
  refresh?.addEventListener('click',()=>void load());
  form?.addEventListener('change',event=>{
    if(event.target.name!=='author_confirmed'){form.elements.author_confirmed.checked=false;if(report)render();}
  });
  window.NovelAgentChapterBudget={presentation,setContext,refresh:load};
})();
