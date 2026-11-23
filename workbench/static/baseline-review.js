(() => {
  'use strict';
  const withFeedback = globalThis.NovelAgentFeedback?.action || (handler => handler);
  const $ = id => document.getElementById(id);
  const project = new URLSearchParams(location.search).get('project_id');
  document.getElementById('open-upgrade').href = project ? '/upgrade?project_id=' + encodeURIComponent(project) : '/';
  const base = '/api/projects/' + encodeURIComponent(project || '') + '/onboarding/baselines';
  const clone = value => structuredClone(value);
  const groups = [
    ['character_state','characters','人物状态','character_id'],
    ['relationship','relationships','人物关系','relationship_id'],
    ['roster','roster','名册','roster_id'],
    ['location','locations','人物位置','entity_id'],
    ['inventory','inventory','物品与数量','inventory_id'],
    ['counter','numeric_counters','计数','counter_id'],
    ['event','events','事件','event_id'],
  ];
  let maintenance = null, busy = false, state = null, record = null, calibration = null;
  let refs = [], referenceDirty = false, dirty = false, current = false, head = true, source = null, cursor = null;
  let rows = {}, acknowledgements = {}, formsReady = false;
  const ready = () => maintenance?.state === 'quiescent' && maintenance?.purpose === 'backup';
  const el = (tag, text) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; return node; };
  const option = (select, value, title) => { const item = el('option', title); item.value = value; select.append(item); };
  const clearAcks = () => {
    Object.values(acknowledgements).forEach(input => { input.checked = false; });
    $('author-confirmed').checked = false;
  };
  const changed = () => { dirty = true; current = false; clearAcks(); render(); };
  const editable = () => Boolean(state && head && formsReady);
  const canConfirm = () => record?.status === 'draft' && head && formsReady && current && !dirty && !referenceDirty && ready() &&
    $('saved-review').value === record.id && Object.keys(acknowledgements).length === groups.length;
  function render() {
    $('proposal').disabled = busy || !ready() || !project;
    $('saved-review').disabled = busy;
    $('load-review').disabled = busy || !$('saved-review').value;
    $('refresh-reviews').disabled = busy;
    $('more-reviews').hidden = !cursor; $('more-reviews').disabled = busy;
    $('saved-revision').disabled = busy || !record;
    $('load-revision').disabled = busy || !record;
    $('inspect-review').disabled = busy || !record || !ready() || dirty || referenceDirty || !head || $('saved-review').value !== record.id;
    $('copy-review').disabled = busy || !record;
    $('fact-editors').disabled = busy || !editable();
    $('calibration-file').disabled = busy || Boolean(record);
    $('read-source').disabled = busy || !ready() || !$('source-path').value;
    $('add-reference').disabled = busy || !source || !head;
    $('source-excerpt').disabled = busy || !head;
    $('source-path').disabled = busy;
    $('save-review').disabled = busy || !ready() || !editable() || !calibration;
    $('save-review').textContent = referenceDirty && record ? '另存为新的审核草稿' : '保存审核草稿';
    Object.values(acknowledgements).forEach(input => { input.disabled = busy || !canConfirm(); });
    $('author-confirmed').disabled = busy || !canConfirm();
    $('confirm-review').disabled = busy || !canConfirm() || !$('author-confirmed').checked ||
      !Object.values(acknowledgements).every(input => input.checked);
    $('record-summary').textContent = record ?
      `截至第 ${record.chapter} 章 · 第 ${record.revision} 版 · ${record.status === 'confirmed' ? '已确认的历史记录' : '审核草稿'}${head ? '' : ' · 旧版本仅供查看'}${dirty ? ' · 有未保存修改' : ''}${current ? ' · 当前来源已核对' : ' · 尚未证明当前来源有效'}` : '尚未保存审核记录';
    document.querySelectorAll('[data-remove-reference]').forEach(button => { button.disabled = busy || !head; });
  }
  async function request(path = '', body) {
    const response = await fetch(base + path, {method: body === undefined ? 'GET' : 'POST',
      credentials:'same-origin',cache:'no-store', ...(body === undefined ? {} : {
        headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})});
    const value = await response.json();
    if (!response.ok) throw new Error(window.NovelAgentUI.errorMessage(value,response.status));
    return value;
  }
  async function run(action) {
    if (busy) return;
    busy = true; $('review-error').hidden = true; render();
    try { await action(); }
    catch (error) {
      current = false; clearAcks();
      $('review-error').textContent = error.message + ' 已清除确认，请核对当前来源；若保存响应丢失，请刷新审核列表找回原记录。';
      $('review-error').hidden = false;
    } finally { busy = false; render(); }
  }
  const operation = () => {
    if (!ready()) throw new Error('请先进入备份维护状态。');
    return {operation_id:maintenance.operation_id};
  };
  function sourcePaths(paths) {
    $('source-path').replaceChildren(); option($('source-path'),'','请选择原资料');
    paths.forEach(path => option($('source-path'),path,path));
    source = null; $('source-text').textContent = ''; $('source-excerpt').value = '';
  }
  function renderRefs() {
    $('references').replaceChildren();
    refs.forEach((ref,index) => {
      const item = el('li'); item.append(el('strong',ref.relative_path),el('p',ref.text));
      const remove = el('button','移除摘录'); remove.type = 'button'; remove.dataset.removeReference = '';
      remove.addEventListener('click',() => { refs.splice(index,1); referenceDirty = true; changed(); renderRefs(); });
      item.append(remove); $('references').append(item);
    });
  }
  const text = {type:'string',minLength:1};
  const number = {type:'number'};
  function characterNames() {
    const result = new Map();
    (rows.characters || []).forEach(row => {
      const name = row.form.read().canonical_name.trim();
      if (!name || result.has(name)) throw new Error('请先为每个人物填写不同的姓名或称呼。');
      result.set(name,row.id);
    });
    return result;
  }
  function fieldConfig(key,names) {
    const person = {type:'string',enum:[...names.keys()]};
    const configs = {
      characters:[{canonical_name:text,aliases:{type:'array',items:text},current_goal:text,status:text,condition:text,identity:text},['canonical_name']],
      relationships:[{source_entity_id:person,target_entity_id:person,relation_type:text,trust:{type:['string','number']}},['source_entity_id','target_entity_id','relation_type']],
      roster:[{name:text,member_ids:{type:'array',items:person},unresolved_count:{type:'integer',minimum:0}},['name','member_ids','unresolved_count']],
      locations:[{entity_id:person,location_id:text},['entity_id','location_id']],
      inventory:[{owner_id:person,item_id:text,quantity:{type:'number',minimum:0}},['owner_id','item_id','quantity']],
      numeric_counters:[{name:text,current_value:number,minimum:number,maximum:number},['name','current_value']],
      events:[{event_type:text,description:text,status:text},['event_type','description']],
    };
    return configs[key];
  }
  const displayValue = value => {
    if (Array.isArray(value)) return value.map(displayValue).join('、');
    if (value && typeof value === 'object') return Object.entries(value).map(([key,item]) =>
      `${window.NovelAgentFactForm.fieldLabel(key)}：${displayValue(item)}`).join('；');
    return value === null ? '未说明' : String(value);
  };
  function addRow(group,id,original = {}) {
    const [category,key,label,idField] = group;
    if (rows[key].length >= 200) throw new Error('单类事实超过网页支持的 200 条，请保留原记录。');
    const names = key === 'characters' ? new Map() : characterNames();
    const personName = value => [...names].find(([,identifier]) => identifier === value)?.[0] || value;
    const value = clone(original);
    if (key === 'relationships') Object.assign(value,{source_entity_id:personName(value.source_character_id),target_entity_id:personName(value.target_character_id),relation_type:value.type});
    if (key === 'roster') value.member_ids = (value.members || []).map(member => personName(member.member_id));
    if (key === 'locations') value.entity_id = personName(value.entity_id || id);
    if (key === 'inventory' && value.owner_id) value.owner_id = personName(value.owner_id);
    const [properties,required] = fieldConfig(key,names);
    const shown = Object.fromEntries(Object.entries(value).filter(([field,item]) => field in properties && item !== undefined));
    const box = el('article'); box.className = 'baseline-row';
    box.append(el('h3',label + '记录')); const formNode = el('div'); box.append(formNode);
    const form = window.NovelAgentFactForm.render(formNode,{type:'object',properties,required,additionalProperties:false},shown);
    const hiddenFields = new Set([idField,'id','source_tier','source_character_id','target_character_id','type','members','declared_count']);
    const extra = Object.fromEntries(Object.entries(original).filter(([field]) => !(field in properties) && !hiddenFields.has(field)));
    if (Object.keys(extra).length) { const details = el('details'); details.className = 'baseline-extra';
      details.append(el('summary','其他已有资料（保留原值）'),el('p',displayValue(extra)));box.append(details); }
    const row = {id,original:clone(original),form,names,box};
    const remove = el('button','移除此条记录'); remove.type = 'button';
    remove.addEventListener('click',() => { rows[key].splice(rows[key].indexOf(row),1);box.remove();changed(); });
    box.append(remove); rows[key].push(row); $('rows-' + category).append(box);
  }
  function showState(value) {
    formsReady = false;state = clone(value); rows = {}; acknowledgements = {}; $('fact-editors').replaceChildren();
    groups.forEach(group => {
      const [category,key,label] = group;rows[key] = [];
      const details = el('details');details.className = 'baseline-category';details.open = key === 'characters';
      details.append(el('summary',label));const list = el('div');list.id = 'rows-' + category;details.append(list);
      const add = el('button','添加' + label + '记录');add.type = 'button';
      add.addEventListener('click',withFeedback(() => run(async () => {addRow(group,'author_' + crypto.randomUUID().replaceAll('-',''));changed();})));
      details.append(add);const caption = el('label');const check = el('input');check.type = 'checkbox';
      check.addEventListener('change',render);caption.append(check,document.createTextNode('我已核对' + label + '，包括空白或缺失记录。'));
      details.append(caption);acknowledgements[category] = check;$('fact-editors').append(details);
      Object.entries(state[key] || {}).forEach(([id,item]) => addRow(group,id,item));
    });
    formsReady = true;
  }
  function readState() {
    const result = clone(state);const names = characterNames();
    groups.forEach(([,key,,idField]) => {
      const values = Object.create(null);
      rows[key].forEach(row => {
        const value = row.form.read();const person = name => {
          const id = row.names.get(name) || names.get(name);
          if (!id || !Object.values(result.characters || {}).some(item => item.character_id === id)) throw new Error('人物引用已变化，请移除并重新添加相关记录。');
          return id;
        };
        const item = clone(row.original);Object.keys(fieldConfig(key,row.names)[0]).forEach(field => { delete item[field]; });
        Object.assign(item,value);item[idField] = row.id;
        if (key === 'relationships') {item.source_character_id = person(value.source_entity_id);item.target_character_id = person(value.target_entity_id);item.type = value.relation_type;
          delete item.source_entity_id;delete item.target_entity_id;delete item.relation_type;}
        if (key === 'roster') {item.members = value.member_ids.map(name => {const id = person(name);return {...(row.original.members || []).find(member => member.member_id === id),member_id:id};});
          item.declared_count = item.members.length + value.unresolved_count;delete item.member_ids;}
        if (key === 'locations') item.entity_id = person(value.entity_id);
        if (key === 'inventory') item.owner_id = person(value.owner_id);
        const id = key === 'locations' ? item.entity_id : row.id;
        if (Object.hasOwn(values,id)) throw new Error('同一人物存在重复位置记录，请合并后保存。');values[id] = item;
      });
      result[key] = values;
    });return result;
  }
  async function list(append = false) {
    const selected = $('saved-review').value;
    const value = await request(append && cursor ? '?after=' + encodeURIComponent(cursor) : '');
    if (!append) { $('saved-review').replaceChildren();option($('saved-review'),'','请选择审核记录'); }
    value.items.forEach(item => option($('saved-review'),item.id,`截至第 ${item.chapter} 章 · 第 ${item.revision} 版 · ${item.status === 'confirmed' ? '已确认' : '草稿'} · ${item.created_at}`));
    if ([...$('saved-review').options].some(item => item.value === selected)) $('saved-review').value = selected;
    cursor = value.next_cursor;
  }
  function accept(value,{sourceCurrent = false} = {}) {
    if (typeof value.calibration_report_json !== 'string') throw new Error('缺少完整原校准报告，请重新加载审核。');
    record = value.record;calibration = value.calibration_report_json;refs = clone(record.static_references);
    head = value.head_revision === undefined || value.head_revision === record.revision;
    referenceDirty = false;dirty = false;current = sourceCurrent;showState(record.state);clearAcks();renderRefs();
    sourcePaths(Object.keys(record.sources.entries));
    $('calibration-status').textContent = '已读取此审核保留的完整原校准报告。';
    if (value.revisions) { $('saved-revision').replaceChildren();value.revisions.forEach(item =>
      option($('saved-revision'),String(item.revision),`第 ${item.revision} 版 · ${item.status === 'confirmed' ? '已确认' : '草稿'} · ${item.created_at}`)); }
    else { $('saved-revision').replaceChildren();option($('saved-revision'),String(record.revision),'第 ' + record.revision + ' 版（加载最新版本可查看完整历史）'); }
    $('saved-revision').value = String(record.revision);
  }
  $('refresh-reviews').addEventListener('click',withFeedback(() => run(async () => {current = false;clearAcks();await list();})));
  $('more-reviews').addEventListener('click',withFeedback(() => run(() => list(true))));
  $('saved-review').addEventListener('change',() => {current = false;clearAcks();render();});
  $('load-review').addEventListener('click',withFeedback(() => run(async () => {
    if (dirty || referenceDirty) throw new Error('请先保存未完成修改，或刷新页面后加载其他审核。');
    accept(await request('/' + encodeURIComponent($('saved-review').value)));$('review-status').textContent = '已加载保存的审核，请核对当前来源。';
  })));
  $('load-revision').addEventListener('click',withFeedback(() => run(async () => {
    if (dirty || referenceDirty) throw new Error('请先保存未完成修改。');
    accept(await request('/' + record.id + '?revision=' + encodeURIComponent($('saved-revision').value)));
  })));
  $('inspect-review').addEventListener('click',withFeedback(() => run(async () => {
    const value = await request('/' + record.id + '/inspect',operation());accept(value,{sourceCurrent:value.source_current});
    $('review-status').textContent = value.source_current ? '当前来源核对通过。草稿可逐类核对后确认。' : value.reason;
  })));
  $('copy-review').addEventListener('click',() => {
    record = null;head = true;current = false;dirty = true;referenceDirty = false;clearAcks();
    $('saved-revision').replaceChildren();$('review-status').textContent = '已以所选内容新建未保存审核，请重新核对全部类别和当前来源。';render();
  });
  $('proposal').addEventListener('click',withFeedback(() => run(async () => {
    if (dirty || referenceDirty) throw new Error('请先保存当前修改，或刷新页面后重新整理。');
    const value = await request('/proposal',operation());record = null;head = true;current = false;dirty = true;refs = [];referenceDirty = false;
    showState(value.state);sourcePaths(value.source_paths);renderRefs();clearAcks();
    $('review-status').textContent = `已整理截至第 ${value.chapter} 章的建议，七类事实均尚未确认。`;
  })));
  $('calibration-file').addEventListener('change',() => run(async () => {
    calibration = null;current = false;clearAcks();const file = $('calibration-file').files[0];
    if (!file || file.size > 2*1024*1024) throw new Error('请选择不超过 2 MiB 的完整原校准报告。');
    const raw = await file.text();const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('请选择完整原校准报告文件。');
    calibration = raw;$('calibration-status').textContent = '报告已读取；保存时将核对它是否匹配当前作品。';
  }));
  $('source-path').addEventListener('change',() => {source = null;$('source-text').textContent = '';$('source-excerpt').value = '';render();});
  $('read-source').addEventListener('click',withFeedback(() => run(async () => {
    source = await request('/source',{...operation(),relative_path:$('source-path').value});$('source-text').textContent = source.text;
  })));
  $('add-reference').addEventListener('click',withFeedback(() => run(async () => {
    const text = $('source-excerpt').value;
    if (!source || !text.trim() || !source.text.includes(text)) throw new Error('摘录须完整匹配当前原资料中的一段文字。');
    if (refs.some(ref => ref.relative_path === source.relative_path && ref.text === text)) throw new Error('这段摘录已保留。');
    refs.push({relative_path:source.relative_path,sha256:source.sha256,text});referenceDirty = true;changed();renderRefs();
  })));
  $('save-review').addEventListener('click',withFeedback(() => run(async () => {
    const state = readState();let value;
    clearAcks();
    if (record && !referenceDirty) value = await request('/' + record.id + '/edit',{...operation(),state,expected_revision:record.revision,expected_sha256:record.record_sha256});
    else {
      const body = {...operation(),state,calibration_report:calibration,static_references:refs};
      const preview = await request('/preview',body);
      value = await request('',{...body,expected_preview_sha256:preview.preview_sha256});
    }
    accept(value,{sourceCurrent:true});await list();$('saved-review').value = record.id;
    $('review-status').textContent = '审核草稿已保存。请逐类核对后明确确认。';
  })));
  $('author-confirmed').addEventListener('change',render);
  $('confirm-review').addEventListener('click',withFeedback(() => run(async () => {
    if (!canConfirm() || !$('author-confirmed').checked || !Object.values(acknowledgements).every(input => input.checked)) throw new Error('请逐类核对后明确确认。');
    const body = {...operation(),expected_revision:record.revision,expected_sha256:record.record_sha256,
      confirmed_categories:['event','character_state','relationship','roster','location','inventory','counter'],author_confirmed:true};
    clearAcks();current = false;
    accept(await request('/' + record.id + '/confirm',body),{sourceCurrent:true});await list();
    $('review-status').textContent = '作者基线已确认。请回工作台生成新备份和认证，再继续后续升级。';
  })));
  document.addEventListener('workbench:maintenance',event => {
    const value = event.detail;
    current = false;clearAcks();
    maintenance = value;render();
  });
  window.addEventListener('pagehide',() => {current = false;clearAcks();});
  $('fact-editors').addEventListener('input',event => {if (event.target.closest('.baseline-row')) changed();});
  if (!project) {$('review-error').textContent = '请从工作台选择归档作品后进入作者审核。';$('review-error').hidden = false;$('review-status').textContent = '尚未选择作品';render();}
  else run(async () => {await list();$('review-status').textContent = '可加载已有审核，或进入维护后整理当前资料。';});
})();
