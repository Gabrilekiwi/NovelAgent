(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const project = params.get("project_id"), chapter = Number(params.get("chapter")), review = params.get("review_id");
  const path = `/api/projects/${encodeURIComponent(project || "")}/chapters/${chapter}`;
  const storageKey = `novelagent.publication:${project}:${chapter}:${review}`;
  const inputFields = ["review_id", "expected_draft_revision", "expected_review_revision", "expected_draft_sha256", "expected_facts_sha256"];
  let current = null, request = null, preview = null, pending = false, published = false, publication = null, terminal = false, busy = false;
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
    counter_id: "计数项", operation: "操作", increment: "增加", set: "设为", completed: "已发生", actor: "参与者" };
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
  const saveRequest = () => {
    // If durable browser storage is unavailable, do not send a publication that
    // the user could not identify after a lost response.
    localStorage.setItem(storageKey, JSON.stringify(request));
  };
  const controls = () => {
    $("preview").disabled = busy || !current || pending || published || terminal || current.review_state !== "confirmed";
    $("commit").disabled = busy || !preview || !$("confirmed").checked || pending || published || terminal;
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
      book_ownership_busy: "此作品正在由另一个进程处理，请稍后查询原提交结果。"
    };
    $("error").textContent = messages[error.code] || error.message;
    $("error").hidden = false;
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
  const presentResult = (result) => {
    preview = null; $("confirm-label").hidden = true;
    if (result.publication_verified === true) {
      published = true; pending = false; terminal = false;
      publication = result;
      $("title").textContent = `第 ${result.chapter} 章 · 已发布`;
      $("status").textContent = `第 ${result.chapter} 章已发布。正文、事实状态和提交凭证已核验。`;
      $("receipt").hidden = false; $("receipt").textContent = `提交凭证：${result.run_id}`;
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
  $("preview").addEventListener("click", () => run(async () => {
    await loadInput();
    const body = Object.fromEntries(inputFields.map((key) => [key, current[key]]));
    if (!request || JSON.stringify(request.body) !== JSON.stringify(body)) request = { body, key: crypto.randomUUID() };
    saveRequest();
    const result = await api(`${path}/publication-preview`, request.body, request.key);
    const quality = result.quality;
    const allowed = quality.local_quality_passed === true && quality.quality_coverage_complete === true && !quality.pending_gates.length;
    preview = allowed ? result : null;
    $("preview-panel").hidden = false;
    $("prose").textContent = result.preparation.final_prose;
    $("quality").textContent = allowed ? "本地标准质量检查通过。提交时将再次核验。" : "质量检查未通过，暂不能发布。请返回草稿处理问题。";
    $("checks").replaceChildren(...[
      ["正文完整性", quality.final_artifact_integrity.accepted], ["事实状态校验", quality.authoritative_state_validation.accepted],
      ["叙事审查", quality.review_gate.status === "pass"], ["检查项完整", quality.quality_coverage_complete]
    ].map(([name, ok]) => node("li", `${name}：${ok ? "通过" : "未通过"}`)));
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
    $("status").textContent = allowed ? "预览已生成，本章尚未发布。" : "请先修正质量检查发现的问题。";
  }));
  $("confirmed").addEventListener("change", controls);
  $("commit").addEventListener("click", () => run(async () => {
    if (!preview || !$("confirmed").checked || !request || pending || published) return;
    saveRequest(); pending = true; controls();
    $("status").textContent = "正在发布。若响应中断，请查询原提交结果。";
    const result = await api(`${path}/publication`, { ...request.body, author_confirmed: true }, request.key);
    presentResult(result.publication);
  }));
  $("resolve").addEventListener("click", () => run(resolve));
  $("recover").addEventListener("click", () => run(async () => {
    await api(`/api/projects/${encodeURIComponent(project)}/publication-recovery`, { author_confirmed: true });
    $("recover").hidden = true;
    if (request) await resolve();
    else $("status").textContent = "原事务恢复完成，请重新生成预览。";
  }));
  $("restart").addEventListener("click", () => run(async () => {
    localStorage.removeItem(storageKey); request = null; terminal = false; preview = null;
    $("preview-panel").hidden = true; await loadInput(); $("status").textContent = "请重新核对当前草稿并生成预览。";
  }));
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
    if (!request) $("status").textContent = current.review_state === "confirmed" ? "事实审核已确认，请生成提交预览。" : "事实审核尚未确认，暂不能发布。";
  });
})();
