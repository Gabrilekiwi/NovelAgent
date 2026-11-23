(() => {
  "use strict";
  const withFeedback = globalThis.NovelAgentFeedback?.action || (handler => handler);
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const project = params.get("project_id"), chapter = Number(params.get("chapter")), review = params.get("review_id");
  const path = `/api/projects/${encodeURIComponent(project || "")}/chapters/${chapter}`;
  const storageKey = `novelagent.publication:${project}:${chapter}:${review}`;
  const inputFields = ["review_id", "expected_draft_revision", "expected_review_revision", "expected_draft_sha256", "expected_facts_sha256"];
  let current = null, request = null, preview = null, pending = false, published = false, publication = null, terminal = false, busy = false;
  let budgetBlocked = true;
  let entityNames = {};
  const labels = { characters: "人物", locations: "地点", items: "物品", events: "事件", relations: "关系", counters: "计数",
    relationships: "人物关系", roster: "成员名单", numeric_counters: "数值记录", inventory: "物品持有",
    foreshadows: "伏笔", factions: "势力", resources: "资源", name: "名称", description: "说明", status: "状态",
    character_id: "人物", location_id: "地点", chapter: "章节", value: "数值", text: "内容", kind: "类别",
    payload: "事实内容", evidence: "正文依据", quote: "原文", accepted: "已接受", rejected: "已拒绝", decision: "决定",
    event: "事件", character_state: "人物状态", relationship: "人物关系", location: "位置", counter: "数值变化",
    character: "人物", item: "物品", aliases: "别名", reason: "作者说明", action: "操作", declare: "新实体", reuse: "复用已有实体",
    reference: "本章称呼", entity_id: "实体", owner_id: "持有人", item_id: "物品", quantity: "初始数量", field: "字段",
    summary: "说明", proposal: "事实变化", source_event_key: "关联事件", event_type: "事件类型", occurrence_location: "发生地点",
    onsite_participants: "现场参与者", remote_participants: "远程参与者", transported_items: "运输物品", related_entities: "关联实体",
    role: "作用", relation: "关联", delta: "变化数量", before: "此前", after: "此后", current_goal: "当前目标",
    counter_id: "计数项", operation: "操作", increment: "增加", set: "设为", completed: "已完成", ongoing: "进行中", started: "开始", actor: "行动者", witness: "见证者", recipient: "接受者", present_other: "其他在场人物" };
  const node = (tag, text) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; return n; };
  const explain = (value) => {
    if (value === null || typeof value !== "object") return node("span", value == null ? "—" : labels[value] || entityNames[value] || String(value));
    if (Array.isArray(value)) {
      const list = node("ul");
      if (!value.length) list.append(node("li", "暂无记录"));
      value.forEach((v) => { const li = node("li"); li.append(explain(v)); list.append(li); });
      return list;
    }
    const list = node("dl");
    Object.entries(value).forEach(([key, v]) => { const dd = node("dd"); dd.append(explain(v)); list.append(node("dt", labels[key] || key), dd); });
    if (!Object.keys(value).length) list.append(node("dt", "暂无记录"));
    return list;
  };
  const showDisposition = (value, allowed) => {
    if(!value || value.schema_version!=="reviewed_quality_disposition_v1"
      || value.publication_authorized!==false || value.repair_authorized!==false)
      throw new Error("本次问题与正文依据不完整，请重新生成预览。");
    const groups={pause:"需要作者核对",repair_once:"可进行一次修正评估",advice:"一般建议"};
    const names={missing_conflict_marker:"情节冲突提示",duplicate_scene_text:"正文重复",near_duplicate_scene_text:"正文近似重复",
      duplicate_opening:"重复开头",duplicate_ending_hook:"重复结尾",repetition_or_stalling:"重复或停滞",
      chapter_length_reasonable:"篇幅建议",missing_required_beat:"章纲节拍缺失",
      missing_ending_pressure:"结尾压力缺失",no_meta_output:"混入非正文内容",
      language_consistency:"语言要求",continues_previous_ending:"前后章衔接",
      preserves_last_scene_location:"场景位置衔接",continue_previous_ending:"前后章衔接规则",
      llm_validator_unavailable:"必要模型检查未完成"};
    names.short_near_prose_repetition_risk="短正文疑似重复";
    // This page is for manual publication. automatic_action controls unattended runs only.
    $("quality-action-summary").textContent = allowed ? ((value.findings || []).length || (value.pause_reasons || []).length
      ? "本章已通过发布检查。以下是写作提醒，不阻止发布；核对正文和事实后即可确认。" : "本章已通过发布检查，没有额外提醒。")
      : "本章尚未通过全部发布检查。请按上方检查结果和下方可选操作处理。";
    const children=[];
    for(const reason of value.pause_reasons || [])children.push(node("p",({
      quality_coverage_incomplete:"必要检查尚未完成。",authoritative_state_not_accepted:"事实状态校验尚未通过。",
      review_producer_failed:"叙事检查执行失败。",unexplained_local_gate_failure:"本地检查未通过，需要核对完整记录。",
      admission_review_not_executed:"本次记录缺少处置策略所需的风险检查。",
      short_prose_review_incomplete:"短正文风险检查超过当前支持范围，需要人工核对。"
    }[reason] || "本项条件需要核对。")));
    for(const finding of value.findings || []) {
      const lexical = finding.codes.length === 1 && finding.codes[0] === "missing_conflict_marker";
      const label = lexical || allowed ? "写作提醒" : (groups[finding.disposition] || "需核对");
      const item=node("article");item.append(node("h4",`${label}：${finding.codes.map(code=>names[code] || code).join("、")}`));
      item.append(node("p", lexical ? "检查只查找少数冲突词。即使没有命中，人物的行动、选择或后果也可能已经写出了冲突。这项提醒不阻止发布，不必为了消除它而加词。" : finding.message));
      if(finding.quotes.length)for(const span of finding.quotes) {
        item.append(node("p",`正文第 ${span.start_char+1}–${span.end_char} 字符（摘录）`),node("blockquote",span.quote));
      } else if(!lexical && !allowed && finding.disposition!=="advice")item.append(node("p","此项未提供可核对的正文范围，请人工定位；当前依据不足以自动修正。"));
      children.push(item);
    }
    $("quality-actions").replaceChildren(...children);
  };
  const saveRequest = () => {
    // If durable browser storage is unavailable, do not send a publication that
    // the user could not identify after a lost response.
    localStorage.setItem(storageKey, JSON.stringify(request));
  };
  let choiceInputs = [];
  const choicesReady = () => choiceInputs.every(input => input.checked);
  const renderChoices = (result) => {
    const offer = result.offered_author_quality_choice;
    choiceInputs = [];
    $("choice-list").replaceChildren();
    if (!offer) return;
    const rows = new Map(result.quality_disposition.findings.map(row => [row.finding_id, row]));
    for (const id of offer.finding_ids) {
      const row = rows.get(id);
      if (!row) throw new Error("未能加载拦截项，请重新生成预览。");
      const box = node("input"); box.type = "checkbox"; box.value = id;
      const title = row.codes.includes("missing_ending_pressure") ? "结尾压力" : "必写节拍";
      const label = node("label"); label.append(box, node("span", ` 忽略「${title}」：${row.message}`));
      const paragraph = node("p"); paragraph.append(label); $("choice-list").append(paragraph);
      choiceInputs.push(box);
      box.addEventListener("change", () => { $("confirmed").checked = false; controls(); });
    }
  };
  const controls = () => {
    const choosing = Boolean(preview?.offered_author_quality_choice) && !pending && !published;
    $("author-choices").hidden = !choosing;
    if (choosing) $("choice-progress").textContent = choicesReady() ? "所有拦截项均已选择忽略。请核对正文和事实后确认发布。"
      : `已选择忽略 ${choiceInputs.filter(input => input.checked).length} / ${choiceInputs.length} 项。未忽略的问题须先修改。`;
    for (const box of choiceInputs) box.disabled = busy || !choosing;
    $("confirm-label").hidden = !preview || !choicesReady() || pending || published;

    $("preview").disabled = busy || budgetBlocked || !current || pending || published || terminal || current.review_state !== "confirmed";
    $("commit").disabled = busy || budgetBlocked || !preview || !choicesReady() || !$("confirmed").checked || pending || published || terminal;
    $("reload").disabled = busy || pending;
    $("resolve").hidden = !request || published || terminal;
    $("resolve").disabled = busy;
    $("recover").disabled = busy;
    $("restart").hidden = !terminal;
    $("restart").disabled = busy;
  };
  const fail = (error) => {
    const messages = {
      chapter_fact_authority_changed: "章节来源或事实依据已变更。请重新核对来源并生成候选，再审核事实。当前草稿已保留。",
      story_project_source_drift: "章节来源已变更，当前预览失效。请重新核对来源与草稿。",
      chapter_commit_recovery_required: "存在尚未恢复的原事务，请先恢复。若证据损坏，作品会继续保持阻断。",
      chapter_commit_integrity_error: "提交证据未能通过完整性核验。请保留原记录并处理恢复问题，暂时不能发布。",
      chapter_commit_conflict: "草稿、审核或提交条件已变化。请查询原提交结果，或重新读取当前审核记录。",
      book_ownership_invalid: "此作品的本机登记尚未通过核验，暂时不能发布。请先完成作品接入或恢复核验。",
      book_ownership_busy: "此作品正在由另一个进程处理，请稍后查询原提交结果。",
      chapter_active_time_exceeded: "本章主动执行时间已用尽，已暂停新的检查和发布准备。原提交结果仍可查询。",
      chapter_active_time_unknown: "上次执行没有完整计时记录，新的检查已暂停。请保留原记录并核对恢复状态。",
      chapter_budget_evidence_invalid: "本章预算记录未能通过核验，请先核对本章原预算。",
      chapter_time_evidence_invalid: "本章计时记录未能通过核验，请保留原记录并核对恢复状态。"
    };
    $("error").textContent = messages[error.code] || error.message;
    $("error").hidden = false;
    if(["chapter_active_time_exceeded","chapter_active_time_unknown","chapter_time_evidence_invalid","chapter_budget_evidence_invalid"].includes(error.code))budgetBlocked=true;
    if (pending && !published) $("status").textContent = "发布结果待核对，请查询原提交结果。";
    if (error.code === "chapter_commit_recovery_required") $("recover").hidden = false;
    preview = null; $("confirmed").checked = false; $("confirm-label").hidden = true;
    $("preview-panel").hidden = true;
  };
  const api = async (url, body, key) => {
    const response = await fetch(url, body === undefined ? { cache: "no-store" } : {
      method: "POST", headers: { "Content-Type": "application/json", ...(key ? { "Idempotency-Key": key } : {}) }, body: JSON.stringify(body)
    });
    let payload;
    try { payload = await response.json(); }
    catch (_) { throw new Error("未能读取服务器结果，请保留原请求并查询原提交结果。"); }
    if (!response.ok) {
      const error = new Error(payload.error?.message || (typeof payload.detail === "string" ? payload.detail : "请求未完成，请核对记录后重试。"));
      error.code = payload.error?.code; error.status = response.status; throw error;
    }
    return payload;
  };
  const run = async (action) => {
    if (busy) return;
    busy = true; $("error").hidden = true; controls();
    try { await action(); } catch (error) { fail(error); }
    finally { busy = false; controls(); }
  };
  const loadInput = async () => {
    current = await api(`${path}/publication-input?review_id=${encodeURIComponent(review)}`);
    $("title").textContent = `第 ${chapter} 章 · ${published ? "已发布" : "确认发布"}`;
    $("binding").textContent = `草稿版本 ${current.expected_draft_revision} · 事实审核版本 ${current.expected_review_revision}`;
    $("prose").textContent = current.final_draft;
    if (published && publication.request.request.expected_draft_sha256 !== current.expected_draft_sha256) {
      $("binding").textContent = "草稿已有后续修改，请在章节详情查看已发布正文。";
      $("prose").textContent = "";
    }
  };
  const loadBudget = async () => {
    budgetBlocked = true;
    if (current?.execution_mode === 'local_author_review') {
      budgetBlocked = false;
      return true;
    }
    const value = await api(`${path}/budget`);
    if(value.project_id !== project || value.book_id !== current.book_id || value.chapter !== chapter)
      throw new Error("预算不属于当前章节，请保留原记录并核对作品。");
    if(value.state !== "ready") {
      $("status").textContent = "本章原预算尚未就绪，新的检查和发布准备已暂停。请返回章节页核对。";
      return false;
    }
    const t = value.time;
    if(!t || t.binding?.budget_sha256 !== value.budget?.budget_sha256)
      throw new Error("本章计时与预算绑定不完整，请保留原记录并核对。");
    if(t.active_status !== "settled" || t.active_limit_reached) {
      $("status").textContent = t.active_status === "running" ? "本章正在执行，请等待原操作完成后重新读取。"
        : t.active_status === "unknown" ? "上次执行没有完整计时记录，新的检查已暂停。原提交结果仍可查询。"
        : "本章主动执行时间已用尽，新的检查和发布准备已暂停。原提交结果仍可查询。";
      return false;
    }
    budgetBlocked = false;
    return true;
  };
  const presentResult = (result) => {
    preview = null; $("confirm-label").hidden = true;
    if (result.publication_verified === true) {
      published = true; pending = false; terminal = false;
      publication = result;
      $("title").textContent = `第 ${result.chapter} 章 · 已发布`;
      $("status").textContent = `第 ${result.chapter} 章已发布。正文、事实状态和提交凭证已核验。`;
      $("receipt").hidden = false; $("receipt").textContent = `提交凭证：${result.run_id}`;
      const ignored = result.request?.request?.author_quality_choice?.finding_ids;
      if (ignored?.length) $("receipt").textContent += ` · 已记录作者选择忽略的 ${ignored.length} 项章纲提示。`;
      $("next-chapter").hidden = false;
      $("next-chapter").href = `/#/projects/${encodeURIComponent(project)}/chapters/${result.chapter + 1}`;
      $("recover").hidden = true;
    } else if (result.retry_requires_new_key === true) {
      terminal = true; pending = false;
      $("status").textContent = result.state === "rolled_back" ? "原提交已回滚，章节尚未发布。重新准备后才能再次提交。" : "原提交在暂存前中断，章节尚未发布。重新准备后才能再次提交。";
    } else if (result.state === "not_started") {
      pending = false;
      $("status").textContent = "原请求尚未开始发布。请重新生成预览并确认。";
    } else {
      throw new Error("尚未取得可核验的发布结果，请保留原请求并查询结果。");
    }
  };
  const resolve = async () => {
    if (!request) return;
    presentResult((await api(`${path}/publication-status`, request.body, request.key)).publication);
  };
  $("preview").addEventListener("click", withFeedback(() => run(async () => {
    preview = null; $("confirmed").checked = false; $("confirm-label").hidden = true; $("preview-panel").hidden = true;
    await loadInput();
    if(!await loadBudget())return;
    const body = Object.fromEntries(inputFields.map((key) => [key, current[key]]));
    if (!request || JSON.stringify(request.body) !== JSON.stringify(body)) request = { body, key: crypto.randomUUID() };
    saveRequest();
    const result = await api(`${path}/publication-preview`, request.body, request.key);
    const quality = result.quality;
    const allowed = quality.local_quality_passed === true && quality.quality_coverage_complete === true && !quality.pending_gates.length;
    renderChoices(result);
    preview = allowed || choiceInputs.length ? result : null;
    $("preview-panel").hidden = false;
    $("prose").textContent = result.preparation.final_prose;
    $("quality").textContent = allowed ? "本地标准质量检查通过。提交时将再次核验。" : choiceInputs.length ? "章纲检查有分歧。你可以修改草稿，或在下方逐项选择忽略。" : "仍有不可忽略的检查未通过或尚未完成。请核对检查详情并处理后再发布。";
    if(result.execution_budget?.time)$("quality").textContent+=` 本章累计已测主动执行 ${result.execution_budget.time.active_seconds} 秒（上限 ${result.execution_budget.time.active_limit_seconds} 秒），作者等待单列。`;
    showDisposition(result.quality_disposition, allowed);
    $("checks").replaceChildren(...[
      ["正文完整性", quality.final_artifact_integrity.accepted], ["事实状态校验", quality.authoritative_state_validation.accepted],
      ["叙事审查", quality.review_gate.status === "pass"], ["检查项完整", quality.quality_coverage_complete]
    ].map(([name, ok]) => node("li", `${name}：${ok ? "通过" : "未通过"}`)));
    const coverage = quality.blueprint_coverage;
    if (coverage) {
      const missing = coverage.missing_beat_indexes || [];
      $("checks").append(node("li", !coverage.required_beat_count ? "必写节拍：章纲未指定额外要求"
        : missing.length ? `必写节拍：未覆盖第 ${missing.join("、")} 项` : "必写节拍：通过"),
        node("li", !coverage.ending_pressure_required ? "结尾压力：章纲未指定要求"
          : coverage.ending_pressure_covered ? "结尾压力：通过" : "结尾压力：未通过"));
    }
    const factPlan = result.preparation.fact_preparation;
    const supplement = factPlan.support_preparation;
    entityNames = Object.fromEntries((supplement?.identity_plan.directory_after || []).map((entry) => [entry.entity_id, entry.name]));
    Object.entries(supplement?.identity_plan.local_references || {}).forEach(([key, entry]) => { entityNames[`local:${key}`] = entityNames[entry.entity_id] || entry.entity_id; });
    $("facts").replaceChildren(explain((factPlan.resolved_facts || result.preparation.facts)
      .filter((f) => f.review?.decision === "accepted")
      .map((f) => ({ kind: f.kind, summary: f.summary, proposal: f.proposal, evidence: f.evidence_spans.map((span) => span.quote) }))));
    $("support-panel").hidden = !supplement;
    $("support").replaceChildren();
    if (supplement) {
      const decisions = [...supplement.identity_plan.decisions, ...supplement.resolved_baselines]
        .map(({ local_key, review_source, ...decision }) => decision);
      $("support").append(explain(decisions));
    }
    $("state").replaceChildren(...Object.entries(result.preparation.fact_preparation.state_after)
      .filter(([key]) => ["characters", "relationships", "roster", "numeric_counters", "inventory", "locations", "events"].includes(key)).map(([key, value]) => {
      const details = node("details"); details.append(node("summary", labels[key] || key), explain(value)); return details;
    }));
    $("confirm-label").hidden = !allowed; $("confirmed").checked = false;
    $("status").textContent = allowed ? "预览已生成，本章尚未发布。" : choiceInputs.length ? "预览已生成。请到下方选择如何处理章纲提示。" : "请先处理不可忽略的检查项。";
  })));
  $("confirmed").addEventListener("change", controls);
  $("commit").addEventListener("click", withFeedback(() => run(async () => {
    if (!preview || !choicesReady() || !$("confirmed").checked || !request || pending || published) return;
    if (choiceInputs.length) request.body.author_quality_choice = {
      review_sha256: preview.offered_author_quality_choice.review_sha256,
      finding_ids: choiceInputs.filter(box => box.checked).map(box => box.value).sort()
    };
    saveRequest(); pending = true; controls();
    $("status").textContent = "正在发布。若响应中断，请查询原提交结果。";
    const result = await api(`${path}/publication`, { ...request.body, author_confirmed: true }, request.key);
    presentResult(result.publication);
  })));
  $("resolve").addEventListener("click", withFeedback(() => run(resolve)));
  $("reload").addEventListener("click", withFeedback(() => run(async () => {
    preview = null; $("confirmed").checked = false; $("confirm-label").hidden = true; $("preview-panel").hidden = true;
    await loadInput();
    if(!published && await loadBudget())$("status").textContent="审核与预算已重新读取，请重新生成预览。";
  })));
  $("recover").addEventListener("click", withFeedback(() => run(async () => {
    await api(`/api/projects/${encodeURIComponent(project)}/publication-recovery`, { author_confirmed: true });
    $("recover").hidden = true;
    if (request) await resolve();
    else $("status").textContent = "原事务恢复完成，请重新生成预览。";
  })));
  $("restart").addEventListener("click", withFeedback(() => run(async () => {
    localStorage.removeItem(storageKey); request = null; terminal = false; preview = null;
    $("preview-panel").hidden = true; await loadInput(); $("status").textContent = "请重新核对当前草稿并生成预览。";
    await loadBudget();
  })));
  run(async () => {
    if (!project || !review || !Number.isInteger(chapter) || chapter < 1) throw new Error("请从本章已确认的事实审核记录进入发布页面。");
    $("back").href = `/#/projects/${encodeURIComponent(project)}/chapters/${chapter}`;
    const saved = localStorage.getItem(storageKey);
    if (saved) {
      request = JSON.parse(saved);
      if (!request || typeof request.key !== "string" || !request.body || request.body.review_id !== review) throw new Error("本地提交记录不完整，请保留记录并核对恢复状态。");
      pending = true;
      // Resolve before reading the mutable current draft: the original request
      // can have published even if that draft was edited after a lost response.
      await resolve();
    }
    await loadInput();
    if(!published && !terminal)await loadBudget();
    if (!request && !budgetBlocked) $("status").textContent = current.review_state === "confirmed" ? "事实审核已确认，请生成提交预览。" : "事实审核尚未确认，暂不能发布。";
  });
})();
