(() => {
  "use strict";

  const doc = document;
  if (!doc.body?.hasAttribute("data-spa")) return;
  const UI = window.NovelAgentUI;
  if (!UI) throw new Error("NovelAgentUI must load before the SPA controller.");

  const one = (selector, root = doc) => root.querySelector(selector);
  const all = (selector, root = doc) => Array.from(root.querySelectorAll(selector));
  const el = (tag, className, text) => {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = String(text);
    return node;
  };
  const state = {
    projects: [],
    projectId: null,
    project: null,
    detail: null,
    chapters: [],
    pending: [],
    pendingJobs: [],
    drafts: [],
    selectedPending: null,
    runs: [],
    batch: null,
    cohort: null,
    cohortLiveResult: null,
    scoreDimensions: [],
    evaluationChapter: null,
    route: null
  };
  const scoreDimensionLabels = {
    plot_progression: "情节推进",
    continuity: "连续性",
    character_consistency: "人物一致性",
    world_consistency: "世界观一致性",
    prose_quality: "文笔质量",
    pacing: "节奏",
    dialogue: "对话",
    ending_hook: "章末钩子",
    overall_usability: "整体可用性"
  };
  const projectCache = new Map();
  const views = new Map(all("[data-spa-view]").map((node) => [node.dataset.spaView, node]));
  const loading = one("[data-spa-loading]");
  const errorPanel = one("[data-spa-error]");

  const setText = (selector, value, root = doc, fallback = "—") => {
    all(selector, root).forEach((node) => { node.textContent = value == null || value === "" ? fallback : String(value); });
  };
  const clear = (node) => { while (node?.firstChild) node.removeChild(node.firstChild); };
  const numberLabel = (value) => Number(value || 0).toLocaleString("zh-CN");
  const projectIdOf = (project) => project?.id || project?.project_id || project?.book_id;
  const projectTitleOf = (project) => project?.title || project?.name || project?.metadata?.title || project?.metadata?.name || project?.book_id || "未命名作品";
  const projectGenreOf = (project) => project?.genre || project?.metadata?.genre || "未设定题材";
  const projectLanguageOf = (project) => project?.language || project?.metadata?.language || "中文";
  const projectSummaryOf = (project) => project?.summary || project?.metadata?.summary || "还没有作品简介。";
  const arrayFrom = (payload, ...keys) => {
    if (Array.isArray(payload)) return payload;
    for (const key of keys) if (Array.isArray(payload?.[key])) return payload[key];
    return [];
  };
  const zeroCallEvidenceVerified = (details) => Boolean(
    details && typeof details === "object"
    && details.provider_reached === false
    && details.provider_calls === 0
    && details.unsettled_attempt_count === 0
    && details.chapter_chars === 0
    && details.formal_prose_exists === false
    && details.accepted === false
    && details.committed === false
    && details.persistence_committed === false
    && details.writeback_applied === false
  );
  const errorCandidatesOf = (job) => [job?.error, job?.result?.error, job]
    .filter((value) => value && typeof value === "object");
  const diagnosticTextOf = (error) => [
    error?.message,
    error?.stderr,
    error?.stderr_tail
  ].filter((value) => typeof value === "string").join("\n");
  const contextBudgetCodes = new Set(["pre_provider_context_budget_exceeded", "context_budget_exceeded", "story_project_context_budget_exceeded"]);
  const contextBudgetDetailsOf = (job) => {
    if (!job || typeof job !== "object") return null;
    if (job.context_budget?.suspected === true) return job.context_budget;
    const candidates = errorCandidatesOf(job);
    const structuredError = candidates.find((value) => contextBudgetCodes.has(value.code));
    const legacyError = candidates.find((value) => (
      value.code === "engine_result_unreadable"
      && /(?:story_project_)?context_budget_exceeded:/i.test(diagnosticTextOf(value))
    ));
    if (!structuredError && !legacyError) return null;
    const details = structuredError?.details && typeof structuredError.details === "object"
      ? structuredError.details
      : {};
    const diagnosticText = diagnosticTextOf(legacyError || structuredError);
    const legacyMatch = diagnosticText.match(/(?:story_project_)?context_budget_exceeded:\s*(?:scene\s+)?input requires\s+(\d+)\s+tokens;\s*hard limit is\s+(\d+)/i);
    const numberFrom = (...names) => {
      for (const name of names) {
        const raw = details[name];
        if (raw == null || raw === "") continue;
        const value = Number(raw);
        if (Number.isFinite(value) && value >= 0) return Math.trunc(value);
      }
      return null;
    };
    const requiredTokens = numberFrom("input_tokens", "required_tokens", "required_input_tokens", "actual_input_tokens", "budgeted_input_tokens", "scene_input_tokens", "required") ?? (legacyMatch ? Number(legacyMatch[1]) : null);
    const hardLimit = numberFrom("hard", "hard_limit", "hard_limit_tokens", "hard_input_limit", "limit_tokens", "max_input_tokens", "limit") ?? (legacyMatch ? Number(legacyMatch[2]) : null);
    const overageTokens = numberFrom("excess", "overage_tokens", "overage", "excess_tokens", "exceeded_by", "over_by", "tokens_over_limit")
      ?? (requiredTokens != null && hardLimit != null ? Math.max(0, requiredTokens - hardLimit) : null);
    return {
      requiredTokens,
      hardLimit,
      overageTokens,
      suspected: true,
      verified: Boolean(structuredError && zeroCallEvidenceVerified(details)),
      legacy: !structuredError
    };
  };
  const contextBudgetSummary = (budget) => (
    budget?.requiredTokens != null && budget?.hardLimit != null && budget?.overageTokens != null
      ? `本地输入上下文为 ${budget.requiredTokens} tokens，上限 ${budget.hardLimit} tokens，超出 ${budget.overageTokens} tokens。`
      : "本地输入上下文超过了当前安全上限。"
  );
  const contextHeadroomDetailsOfRun = (run) => {
    if (run?.error_type !== "ContextBudgetError" || Number(run?.unsettled_attempt_count) !== 0) return null;
    const message = String(run?.error_message || "");
    const match = message.match(/story_project_context_headroom_exceeded:\s*(?:scene\s+)?input requires\s+(\d+)\s+tokens;\s*safe target is\s+(\d+);\s*hard limit is\s+(\d+)/i);
    if (!match) return null;
    const requiredTokens = Number(match[1]);
    const safeTarget = Number(match[2]);
    const hardLimit = Number(match[3]);
    return {
      requiredTokens,
      safeTarget,
      hardLimit,
      overageTokens: Math.max(0, requiredTokens - safeTarget)
    };
  };
  const repairAuthorityDetailsOfRun = (run) => {
    if (run?.error_type !== "StructuredContextError" || Number(run?.unsettled_attempt_count) !== 0 || run?.committed !== false) return null;
    const message = String(run?.error_message || "");
    const match = message.match(/required_authoritative_records_exceed_budget:\s*required records need\s+(\d+)\s+characters including audit metadata, exceeding the\s+(\d+)\s+character authority budget/i);
    if (!match) return null;
    const requiredChars = Number(match[1]);
    const budgetChars = Number(match[2]);
    const providerCalls = Number(run?.provider_calls);
    const chapterChars = Number(run?.chapter_chars);
    if (!Number.isFinite(providerCalls) || providerCalls < 0 || !Number.isFinite(chapterChars) || chapterChars <= 0) return null;
    return {
      requiredChars,
      budgetChars,
      overageChars: Math.max(0, requiredChars - budgetChars),
      providerCalls,
      chapterChars
    };
  };
  const qualityRejectionDetailsOfRun = (run) => {
    if (run?.status !== "rejected" || run?.committed !== false || Number(run?.unsettled_attempt_count) !== 0) return null;
    const validation = run?.validation;
    const problems = arrayFrom(validation?.problems, "items").filter((problem) => problem && typeof problem === "object" && problem.blocking === true);
    const providerCalls = Number(run?.provider_calls);
    const chapterChars = Number(run?.chapter_chars);
    if (validation?.ok !== false || !problems.length || !Number.isFinite(providerCalls) || providerCalls < 0 || !Number.isFinite(chapterChars) || chapterChars <= 0) return null;
    return {
      providerCalls,
      chapterChars,
      problems,
      primaryProblem: problems[0]
    };
  };
  const qualityProblemSummary = (quality) => quality?.primaryProblem?.message
    || quality?.primaryProblem?.repair_hint
    || "质量门发现阻断问题，需要修改候选草稿后重新验证。";
  const contextBudgetStopDetail = (budget) => budget?.verified
    ? "本次尝试在请求发出前停止：新增模型调用 0 次、新增费用 0；没有生成草稿正文，也没有提交任何内容。"
    : "系统已停止自动执行，正在核对本次模型调用、费用和产物状态；安全核对完成前不能重新执行。";
  const preProviderProvenanceCodes = new Set(["pre_provider_provenance_unavailable"]);
  const preProviderProvenanceDetailsOf = (job) => {
    if (!job || typeof job !== "object") return null;
    if (job.pre_provider_provenance?.suspected === true) return job.pre_provider_provenance;
    const candidates = errorCandidatesOf(job);
    const structuredError = candidates.find((value) => preProviderProvenanceCodes.has(value.code));
    const legacyError = candidates.find((value) => (
      value.code === "engine_result_unreadable"
      && /Git metadata capture is unavailable/i.test(diagnosticTextOf(value))
    ));
    if (!structuredError && !legacyError) return null;
    const details = structuredError?.details && typeof structuredError.details === "object"
      ? structuredError.details
      : {};
    return {
      dependency: typeof details.dependency === "string" ? details.dependency : "git",
      suspected: true,
      verified: Boolean(structuredError && zeroCallEvidenceVerified(details)),
      legacy: !structuredError
    };
  };
  const preProviderProvenanceSummary = (failure) => failure?.verified
    ? "Git 来源记录环境异常。"
    : "检测到疑似 Git 来源记录环境异常，正在进行安全核对。";
  const preProviderProvenanceStopDetail = (failure) => failure?.verified
    ? "本次尝试在模型请求前停止：新增模型调用 0 次、新增费用 0；无草稿正文，也没有提交任何内容。"
    : "模型调用、费用和产物状态尚未完全确认；系统不会自动重试，安全核对完成前按钮保持禁用。";
  const repairNotConvergingDetailsOf = (job) => {
    if (!job || typeof job !== "object") return null;
    const structuredError = errorCandidatesOf(job).find((value) => value.code === "chapter_repair_not_converging");
    if (!structuredError) return null;
    const details = structuredError.details && typeof structuredError.details === "object"
      ? structuredError.details
      : {};
    const integer = (name) => Number.isInteger(details[name]) && details[name] >= 0 ? details[name] : null;
    const providerCalls = integer("provider_calls");
    const settledProviderCalls = integer("settled_provider_calls");
    const candidateChars = integer("candidate_chars");
    const originalProblemCount = integer("original_problem_count");
    const finalProblemCount = integer("final_problem_count");
    const newProblemCodes = Array.isArray(details.new_problem_codes)
      && details.new_problem_codes.every((code) => typeof code === "string" && code.trim())
      ? details.new_problem_codes.map((code) => code.trim())
      : null;
    const verified = Boolean(
      details.failure_origin === "chapter_repair_convergence"
      && typeof details.run_id === "string"
      && details.run_id.trim()
      && providerCalls !== null
      && settledProviderCalls === providerCalls
      && details.unsettled_attempt_count === 0
      && candidateChars !== null
      && candidateChars > 0
      && details.formal_prose_exists === false
      && details.accepted === false
      && details.committed === false
      && details.writeback_attempted === false
      && details.writeback_applied === false
      && originalProblemCount !== null
      && finalProblemCount !== null
      && newProblemCodes !== null
      && details.automatic_retry === false
      && details.safe_to_retry === false
      && details.recovery_required === true
    );
    return {
      verified,
      providerCalls,
      settledProviderCalls,
      candidateChars,
      originalProblemCount,
      finalProblemCount,
      convergenceReason: typeof details.convergence_reason === "string" ? details.convergence_reason : "",
      newProblemCodes: newProblemCodes || []
    };
  };
  const repairProblemLabels = Object.freeze({
    missing_opening_bridge: "开篇衔接",
    missing_last_scene_continuity: "上一场景连续性"
  });
  const repairNewProblemLabel = (failure) => {
    const labels = failure?.newProblemCodes?.map((code) => repairProblemLabels[code] || code) || [];
    return labels.length ? labels.join("、") : "新的更严重问题";
  };
  const repairNotConvergingSummary = (failure) => failure?.verified
    ? `自动修订把阻断问题从 ${failure.originalProblemCount} 项降到 ${failure.finalProblemCount} 项，但新增了更严重的${repairNewProblemLabel(failure)}问题；系统已拒绝采用修订稿。`
    : "检测到修订未收敛，但模型调用、候选正文与提交状态的结构化证据尚未核对完成。";
  const reviewStatusOf = (job) => job?.result?.review_status || job?.review_status || job?.result?.review_pipeline?.status;
  const isQualityRevisionPause = (job) => job?.state === "awaiting_input" && reviewStatusOf(job) === "needs_revision" && !preProviderProvenanceDetailsOf(job) && !contextBudgetDetailsOf(job) && !repairNotConvergingDetailsOf(job);

  const api = async (path, options = {}) => {
    const headers = UI.requestHeaders(options.headers || {});
    if (options.json !== undefined) {
      headers.set("Content-Type", "application/json");
      options.body = JSON.stringify(options.json);
    }
    const response = await fetch(path, { credentials: "same-origin", ...options, headers });
    const payload = await UI.parseResponse(response);
    if (!response.ok) throw { status: response.status, payload };
    return { payload, response };
  };

  const showLoading = (visible) => { if (loading) loading.hidden = !visible; };
  const clearError = () => { if (errorPanel) errorPanel.hidden = true; };
  const showError = (error) => {
    const message = UI.errorMessage(error?.payload, error?.status);
    setText("[data-spa-error-message]", message, errorPanel, message);
    if (errorPanel) errorPanel.hidden = false;
  };

  const statusBadge = (status = "idle", label) => {
    const badge = el("span", "status-badge status-idle");
    badge.dataset.statusBadge = "idle";
    const dot = el("span", "status-dot");
    dot.setAttribute("aria-hidden", "true");
    const copy = el("span", "", "—");
    copy.dataset.statusLabel = "";
    badge.append(dot, copy);
    UI.updateStatusBadge(badge, status, label);
    return badge;
  };

  const parseRoute = () => {
    const raw = (window.location.hash || "#/ ").replace(/^#\/?/, "").trim();
    const parts = raw ? raw.split("/").filter(Boolean).map(decodeURIComponent) : [];
    let projectId = null;
    let area = "bookshelf";
    let chapter = null;
    if (parts[0] === "projects" && parts[1]) {
      projectId = parts[1];
      area = parts[2] || "overview";
      if (area === "chapters" && parts[3]) { area = "chapter"; chapter = Number(parts[3]); }
    } else if (parts[0]) {
      area = parts[0];
      projectId = state.projectId || window.localStorage.getItem("novelagent.projectId");
      if (area === "chapter" && parts[1]) chapter = Number(parts[1]);
    }
    const known = new Set(["bookshelf", "overview", "chapters", "chapter", "outlines", "production", "pending", "history", "health"]);
    if (!known.has(area)) area = "bookshelf";
    if (area !== "bookshelf" && !projectId) area = "bookshelf";
    return { area, projectId, chapter };
  };

  const projectHash = (area = "overview", suffix = "") => {
    if (!state.projectId) return "#/";
    return `#/projects/${encodeURIComponent(state.projectId)}/${area}${suffix}`;
  };

  const setView = (name) => {
    views.forEach((view, key) => { view.hidden = key !== name; });
    const activeArea = name === "chapter" || name === "outlines" ? "chapters" : name === "health" ? "overview" : name;
    all("[data-nav-area]").forEach((link) => {
      const active = link.dataset.navArea === activeArea;
      link.classList.toggle("is-active", active);
      if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
    });
    const labels = { bookshelf: "", overview: "", chapters: "/ 章节", chapter: "/ 章节详情", outlines: "/ 章纲提案", production: "/ 生产", pending: "/ 待处理", history: "/ 历史", health: "/ 项目健康" };
    setText("[data-breadcrumb-page]", labels[name] || "", doc, "");
    doc.title = `${name === "bookshelf" ? "书架" : projectTitleOf(state.project)} · NovelAgent`;
    one("#main-content")?.focus({ preventScroll: true });
  };

  const updateProjectChrome = () => {
    const hasProject = Boolean(state.projectId && state.project);
    const switcher = one("[data-spa-project-switcher]");
    if (switcher) switcher.hidden = !hasProject;
    all("[data-breadcrumb-project]").forEach((node) => { node.hidden = !hasProject; });
    if (!hasProject) return;
    setText("[data-project-title]", projectTitleOf(state.project));
    setText("[data-project-meta]", `${projectGenreOf(state.project)} · ${projectLanguageOf(state.project)}`);
    all("[data-nav-area]").forEach((link) => { link.href = projectHash(link.dataset.navArea); });
    all("[data-project-route]").forEach((link) => { link.href = projectHash(link.dataset.projectRoute); });
    const projectLink = one("[data-nav-project-overview]");
    if (projectLink) projectLink.href = projectHash("overview");
    const healthLink = one("[data-health-link]");
    if (healthLink) healthLink.href = projectHash("health");
    const exportLink = one("[data-project-export]");
    if (exportLink) exportLink.href = `/api/projects/${encodeURIComponent(state.projectId)}/export`;
    updatePendingCount(Number(state.project.pending_count || 0));
  };

  const renderBookshelf = () => {
    const grid = one("[data-project-grid]");
    const empty = one("[data-project-empty]");
    clear(grid);
    if (empty) empty.hidden = state.projects.length > 0;
    const template = one("#book-card-template");
    state.projects.forEach((raw, index) => {
      const project = raw.project || raw;
      const id = projectIdOf(project);
      const fragment = template.content.cloneNode(true);
      const card = fragment.querySelector(".book-card");
      const link = fragment.querySelector(".book-card-main");
      link.href = `#/projects/${encodeURIComponent(id)}/overview`;
      const cover = fragment.querySelector(".book-cover");
      cover.style.setProperty("--cover-hue", String(project.cover_hue ?? ((index + 1) * 47) % 360));
      fragment.querySelector(".cover-kicker").textContent = projectGenreOf(project);
      fragment.querySelector(".book-cover strong").textContent = projectTitleOf(project);
      fragment.querySelector("h3").textContent = projectTitleOf(project);
      fragment.querySelector(".book-title-row p").textContent = `${projectGenreOf(project)} · ${numberLabel(project.chapter_count)} 章`;
      fragment.querySelector(".book-summary").textContent = projectSummaryOf(project);
      fragment.querySelector(".book-progress-copy strong").textContent = project.progress_label || (project.chapter_count ? `已写至第 ${project.chapter_count} 章` : "尚未开始");
      const progress = Math.max(0, Math.min(100, Number(project.progress || 0)));
      fragment.querySelector(".progress").setAttribute("aria-valuenow", String(progress));
      fragment.querySelector(".progress span").style.setProperty("--progress", `${progress}%`);
      fragment.querySelector("footer > span").textContent = project.updated_at_label || "本地作品";
      UI.updateStatusBadge(fragment.querySelector(".status-badge"), project.status || "idle", project.status_label);
      if (project.pending_count > 0) card.classList.add("needs-attention");
      grid.append(fragment);
    });
  };

  const loadProjects = async () => {
    const { payload } = await api("/api/projects");
    state.projects = arrayFrom(payload, "items", "projects");
    renderBookshelf();
  };

  const loadProject = async (id, force = false) => {
    if (!force && projectCache.has(id)) {
      state.detail = projectCache.get(id);
    } else {
      const { payload } = await api(`/api/projects/${encodeURIComponent(id)}`);
      state.detail = payload;
      projectCache.set(id, payload);
    }
    state.projectId = id;
    state.project = state.detail.project || state.detail.record || state.projects.find((candidate) => String(projectIdOf(candidate.project || candidate)) === String(id)) || state.detail;
    window.localStorage.setItem("novelagent.projectId", id);
    updateProjectChrome();
    return state.detail;
  };

  const appendChapterRow = (container, chapter, compact = false) => {
    const number = Number(chapter.number ?? chapter.chapter ?? chapter.chapter_index ?? 0);
    const derivedStatus = chapter.status || (chapter.conflict ? "failed" : chapter.has_prose === false ? "draft" : "committed");
    const link = el("a", compact ? "chapter-row" : "chapter-index-row");
    link.href = projectHash("chapters", `/${number}`);
    link.dataset.searchValue = `${number} ${chapter.title || ""}`;
    link.dataset.status = derivedStatus;
    link.append(el("span", "chapter-number", String(number).padStart(2, "0")));
    const copy = el("span", "chapter-copy");
    copy.append(el("strong", "", chapter.title || "未命名章节"), el("small", "", `${chapter.word_count_label || (chapter.word_count ? `${numberLabel(chapter.word_count)} 字` : "—")} · ${chapter.updated_at_label || "—"}`));
    link.append(copy);
    if (!compact) link.append(el("span", "chapter-outline-note", chapter.outline_summary || chapter.core_event || "暂无章纲摘要"));
    link.append(statusBadge(derivedStatus, chapter.status_label), el("span", "row-arrow", "›"));
    container.append(link);
  };

  const renderOverview = (payload) => {
    const overview = payload.overview || payload;
    const project = payload.project || state.project;
    setText("[data-project-summary]", projectSummaryOf(project));
    setText("[data-overview-chapters]", `${numberLabel(overview.chapter_count ?? project.chapter_count)} 章`);
    setText("[data-overview-words]", overview.word_count_label || `${numberLabel(overview.word_count)} 字`);
    const nextChapter = overview.next_chapter ?? project.next_chapter ?? 1;
    setText("[data-overview-next]", `第 ${nextChapter} 章`);
    setText("[data-overview-next-title]", overview.next_outline_title || "待规划");
    const latestBatch = overview.latest_outline_batch || {};
    const approvedOutlineCount = overview.outline_ready_count ?? (latestBatch.status === "approved" ? arrayFrom(latestBatch.items || latestBatch.proposal, "items").length : 0);
    setText("[data-overview-outlines]", `${numberLabel(approvedOutlineCount)} 章`);
    const recoveryAttention = overview.chapter_20_recovery?.recoverable ? 1 : 0;
    const pendingTotal = Math.max(Number(overview.pending_count || 0), recoveryAttention);
    setText("[data-overview-pending]", numberLabel(pendingTotal));
    const health = payload.health || overview.health || {};
    const storyMode = project.mode || overview.mode || project.story_state_mode;
    const productionEligible = health.production_eligible === true;
    UI.updateStatusBadge(
      one("[data-overview-status]"),
      productionEligible ? "strict" : "experimental",
      health.mode_label || health.label || (productionEligible ? "Strict 生产" : storyMode === "strict" ? "Strict 需复核" : "实验运行")
    );
    const healthValidationOk = Boolean(health.validation?.ok ?? health.validation?.valid ?? (health.validation && !health.validation.errors?.length));
    const overviewHealthScore = health.score ?? (healthValidationOk ? 55 : 20) + (health.shadow_report ? 15 : 0) + (health.production_eligible ? 30 : 0);
    setText("[data-health-label]", health.label || health.mode_label || (health.production_eligible ? "Strict 生产" : "实验运行"));
    setText("[data-health-score]", overviewHealthScore);
    setText("[data-health-summary]", health.summary || "完成校准与激活后，才可作为 Strict 生产项目。");
    one("[data-health-ring]")?.style.setProperty("--score", String(Number(overviewHealthScore || 0)));
    const recentChapters = one("[data-recent-chapters]");
    clear(recentChapters);
    arrayFrom(overview.recent_chapters || payload.recent_chapters, "items").forEach((chapter) => appendChapterRow(recentChapters, chapter, true));
    if (!recentChapters.children.length) recentChapters.append(el("p", "muted-copy", "还没有已提交正文。"));
    const runs = one("[data-recent-runs]");
    clear(runs);
    arrayFrom(overview.recent_runs || overview.recent_history || payload.recent_runs, "items").slice(0, 6).forEach((run) => {
      const row = el("a", "activity-row");
      row.href = projectHash("history");
      row.append(el("span", `activity-mark activity-${run.status || "idle"}`));
      const copy = el("span"); copy.append(el("strong", "", run.title || (run.chapter ? `第 ${run.chapter} 章` : "运行记录")), el("small", "", `${run.time_label || ""}${run.detail ? ` · ${run.detail}` : ""}`));
      row.append(copy, statusBadge(run.status || "idle", run.status_label)); runs.append(row);
    });
    if (!runs.children.length) runs.append(el("p", "muted-copy", "暂无运行记录。"));
    const provider = payload.provider || overview.provider;
    if (provider) updateProvider(provider);
    updatePendingCount(pendingTotal);
  };

  const updateProvider = (provider) => {
    const ready = Boolean(provider.ready ?? provider.provider_ready);
    one("[data-provider-light]")?.classList.toggle("is-ready", ready);
    setText("[data-provider-name]", provider.name || provider.provider_name || "模型服务");
    setText("[data-provider-state]", ready ? "配置就绪" : "待配置");
    const providerButtons = [...all("[data-requires-provider]"), one('[data-session-form] button[type="submit"]')].filter(Boolean);
    providerButtons.forEach((button) => {
      button.disabled = !ready;
      button.title = ready ? "" : "请先在本地环境中配置 Provider 凭据";
    });
  };
  const updatePendingCount = (count) => {
    const badge = one("[data-pending-count]");
    if (badge) { badge.textContent = String(count || 0); badge.hidden = !count; }
  };

  const loadChapters = async () => {
    const { payload } = await api(`/api/projects/${encodeURIComponent(state.projectId)}/chapters`);
    state.chapters = arrayFrom(payload, "items", "chapters");
    const container = one("[data-chapter-index]"); clear(container);
    state.chapters.forEach((chapter) => appendChapterRow(container, chapter));
    if (!container.children.length) container.append(el("p", "muted-copy", "还没有章节，请先规划章纲。"));
  };

  const renderChapter = (chapter) => {
    const number = Number(chapter.number ?? chapter.chapter ?? chapter.chapter_index ?? 0);
    const title = chapter.title || "未命名章节";
    setText("[data-chapter-kicker]", `第 ${number} 章`);
    setText("[data-chapter-title]", title);
    setText("[data-chapter-english-number]", `CHAPTER ${String(number).padStart(2, "0")}`);
    setText("[data-chapter-meta]", `${chapter.word_count_label || (chapter.word_count ? `${numberLabel(chapter.word_count)} 字` : "—")} · ${chapter.committed_at_label || chapter.updated_at_label || "—"}`);
    const currentProse = chapter.content || chapter.prose || chapter.current_prose || chapter.formal_prose || "";
    setText("[data-chapter-content]", currentProse || "暂无正文。");
    setText("[data-chapter-revision]", `r${chapter.revision || 1}`);
    setText("[data-chapter-sha]", chapter.sha || chapter.sha256 || chapter.current_sha256 || "—");
    UI.updateStatusBadge(one("[data-chapter-status]"), chapter.status || "committed", chapter.status_label);
    const outline = chapter.outline || {};
    const structuredOutline = typeof outline === "object" ? outline : {};
    setText("[data-outline-title]", structuredOutline.title || title);
    setText("[data-outline-core]", structuredOutline.core_event || (typeof outline === "string" ? outline : "—"));
    setText("[data-outline-pressure]", structuredOutline.ending_pressure || "—");
    const beats = one("[data-outline-beats]"); clear(beats);
    (Array.isArray(structuredOutline.required_beats) ? structuredOutline.required_beats : []).forEach((beat) => beats.append(el("li", "", beat)));
    if (!beats.children.length) beats.append(el("li", "", "—"));
    const review = chapter.review || {};
    setText("[data-review-score]", review.score);
    setText("[data-review-summary]", review.summary || "尚未进行人工评分。");
    UI.updateStatusBadge(one("[data-review-status]"), review.status || "idle", review.status_label || (review.score ? "已评分" : "未评分"));
    const dimensions = one("[data-review-dimensions]"); clear(dimensions);
    arrayFrom(review.dimensions, "items").forEach((dimension) => {
      const row = el("div"); row.append(el("span", "", dimension.label));
      const bar = el("div", "mini-score"); const fill = el("i"); fill.style.setProperty("--score", `${Math.max(0, Math.min(100, Number(dimension.score || 0) * 20))}%`); bar.append(fill);
      row.append(bar, el("strong", "", dimension.score ?? "—")); dimensions.append(row);
    });
    if (!dimensions.children.length) dimensions.append(el("p", "muted-copy", "尚未进行人工评分。"));
    const versions = one("[data-version-timeline]"); clear(versions);
    const versionItems = arrayFrom(chapter.versions, "items");
    (versionItems.length ? versionItems : [{ label: "原始提交", revision: chapter.revision || 1, sha: chapter.sha || chapter.sha256, is_current: true }]).forEach((version) => {
      const item = el("li", version.is_current ? "is-current" : ""); item.append(el("span", "version-node"));
      const copy = el("div"); const heading = el("div", "version-title"); heading.append(el("strong", "", version.label || `r${version.revision}`)); if (version.is_current) heading.append(el("span", "", "当前"));
      copy.append(heading, el("p", "", version.summary || ""), el("small", "", `r${version.revision || 1} · ${version.created_at_label || ""} · ${(version.sha || "—").slice(0, 12)}`)); item.append(copy); versions.append(item);
    });
    const form = one("[data-amendment-form]");
    form.action = `/api/chapters/${number}/amendments`;
    form.dataset.casRevision = String(chapter.revision || 1);
    const currentSha = chapter.sha || chapter.sha256 || chapter.current_sha256 || "";
    form.dataset.casSha = currentSha;
    one('input[name="project_id"]', form).value = state.projectId;
    one('input[name="expected_base_sha256"]', form).value = currentSha;
    one('textarea[name="text"]', form).value = currentProse;
  };

  const loadChapter = async (number) => {
    let chapter;
    try {
      const { payload } = await api(`/api/projects/${encodeURIComponent(state.projectId)}/chapters/${number}`);
      chapter = payload && typeof payload.chapter === "object" ? payload.chapter : payload;
    } catch (error) {
      if (error.status !== 404) throw error;
      if (!state.chapters.length) await loadChapters();
      chapter = state.chapters.find((item) => Number(item.number ?? item.chapter ?? item.chapter_index) === Number(number));
      if (!chapter) throw error;
    }
    renderChapter(chapter);
  };

  const renderOutlineBatch = (batch) => {
    state.batch = batch;
    const createPanel = one("[data-outline-create-panel]");
    const editor = one("[data-outline-editor]");
    createPanel.hidden = Boolean(batch);
    editor.hidden = !batch;
    const createForm = one("[data-create-outline-form]");
    createForm.action = `/api/projects/${encodeURIComponent(state.projectId)}/outline-batches`;
    if (!batch) return;
    editor.action = `/api/outline-batches/${encodeURIComponent(batch.id)}`;
    editor.dataset.casRevision = String(batch.revision || 1);
    editor.dataset.casSha = batch.proposal_sha256 || batch.sha256 || "";
    one('input[name="expected_revision"]', editor).value = String(batch.revision || 1);
    one('input[name="expected_sha256"]', editor).value = batch.proposal_sha256 || batch.sha256 || "";
    setText("[data-batch-direction]", batch.direction || batch.metadata?.direction || "以总纲和当前权威状态为主。");
    setText("[data-batch-revision]", `r${batch.revision || 1}`);
    const items = arrayFrom(batch.items || batch.proposal, "items");
    const start = batch.start_chapter || items[0]?.chapter_index || state.project?.next_chapter || 1;
    setText("[data-batch-range]", `第 ${start}–${batch.end_chapter || start + 9} 章`);
    const stack = one("[data-outline-stack]"); clear(stack);
    const template = one("#outline-card-template");
    for (let index = 0; index < 10; index += 1) {
      const item = items[index] || {};
      const number = item.chapter_index || start + index;
      const fragment = template.content.cloneNode(true);
      const card = fragment.querySelector(".outline-card");
      fragment.querySelector(".outline-number strong").textContent = String(number);
      const titleInput = fragment.querySelector(".title-field input"); titleInput.name = `proposal.${index}.title`; titleInput.value = item.title || "";
      const hidden = fragment.querySelector('[data-field="chapter_index"]'); hidden.name = `proposal.${index}.chapter_index`; hidden.value = String(number);
      ["core_event", "required_beats", "ending_pressure"].forEach((field) => {
        const input = fragment.querySelector(`[data-field="${field}"]`); input.name = `proposal.${index}.${field}`;
        const value = item[field]; input.value = field === "required_beats" && Array.isArray(value) ? value.join("\n") : value || "";
      });
      const toggle = fragment.querySelector(".outline-toggle");
      toggle.addEventListener("click", () => { const collapsed = card.classList.toggle("is-collapsed"); toggle.setAttribute("aria-expanded", String(!collapsed)); });
      stack.append(fragment);
    }
  };

  const loadOutlines = async () => {
    const { payload } = await api(`/api/projects/${encodeURIComponent(state.projectId)}/outline-batches`);
    const batches = arrayFrom(payload, "items", "batches");
    const nextChapter = Number(state.project?.next_chapter || 0);
    const isCurrentRange = (item) => Number(item?.start_chapter || 0) === nextChapter;
    let batch = payload.batch && isCurrentRange(payload.batch)
      ? payload.batch
      : batches.find((item) => ["draft", "ready"].includes(item.status) && isCurrentRange(item)) || null;
    if (batch?.id && !batch.items && !batch.proposal) {
      const detail = await api(`/api/outline-batches/${encodeURIComponent(batch.id)}`);
      batch = detail.payload.batch || detail.payload;
    }
    renderOutlineBatch(batch);
  };

  const renderCohortEvaluation = (chapter) => {
    const cohort = state.cohort;
    if (!cohort) return;
    state.evaluationChapter = Number(chapter);
    const live = state.cohortLiveResult || {};
    const evaluations = arrayFrom(live.evaluations, "items");
    const evaluation = evaluations.find((item) => Number(item.chapter) === Number(chapter)) || null;
    all("[data-cohort-chapter-buttons] button").forEach((button) => button.classList.toggle("is-active", Number(button.dataset.chapter) === Number(chapter)));
    setText("[data-evaluation-title]", `第 ${chapter} 章真实评估`);
    const form = one("[data-cohort-evaluation-form]");
    form.action = `/api/cohorts/${encodeURIComponent(cohort.id)}/chapters/${chapter}/evaluation`;
    one('input[name="expected_revision"]', form).value = String(evaluation?.revision || 0);
    form.dataset.casRevision = String(evaluation?.revision || 0);
    const grid = one("[data-cohort-score-grid]"); clear(grid);
    state.scoreDimensions.forEach((dimension) => {
      const label = el("label", "score-field"); label.append(el("span", "", scoreDimensionLabels[dimension] || dimension));
      const select = el("select"); select.name = `scores.${dimension}`; select.required = true;
      const placeholder = el("option", "", "请评分"); placeholder.value = ""; select.append(placeholder);
      for (let score = 1; score <= 5; score += 1) { const option = el("option", "", `${score} 分`); option.value = String(score); select.append(option); }
      select.value = evaluation?.scores?.[dimension] ? String(evaluation.scores[dimension]) : "";
      label.append(select); grid.append(label);
    });
    one('input[name="edit_minutes"]', form).value = evaluation?.edit_minutes ?? "";
    one('input[name="actual_cost"]', form).value = evaluation?.actual_cost ?? "";
    one('textarea[name="severe_defects"]', form).value = Array.isArray(evaluation?.severe_defects) ? evaluation.severe_defects.join("\n") : "";
    one('textarea[name="notes"]', form).value = evaluation?.notes || "";
    one("[data-save-evaluation]", form).disabled = cohort.status !== "active";
    all("input, select, textarea", form).forEach((field) => { field.disabled = cohort.status !== "active"; });
  };

  const renderCohortDetail = (payload) => {
    const cohort = payload.cohort || payload;
    const live = payload.live_result || cohort.result || {};
    state.cohort = cohort;
    state.cohortLiveResult = live;
    state.scoreDimensions = payload.score_dimensions || state.scoreDimensions;
    one("[data-cohort-create-panel]").hidden = cohort.status === "active";
    one("[data-cohort-active]").hidden = false;
    setText("[data-cohort-range]", `第 ${cohort.start_chapter}–${cohort.end_chapter} 章`);
    const metrics = live.metrics || {};
    setText("[data-cohort-evaluated]", `${metrics.evaluated_chapters || 0} / 10`);
    setText("[data-cohort-score]", metrics.mean_human_score == null ? "—" : Number(metrics.mean_human_score).toFixed(2));
    setText("[data-cohort-cost]", `¥${Number(metrics.actual_cost || 0).toFixed(2)} / ¥${Number(cohort.weekly_budget || metrics.weekly_budget || 0).toFixed(2)}`);
    const criteria = live.criteria || {};
    const passedCriteria = Object.values(criteria).filter(Boolean).length;
    const totalCriteria = Object.keys(criteria).length;
    const resultText = cohort.status === "passed" ? "本轮已通过全部产品门槛。" : cohort.status === "invalidated" ? "本轮基线已漂移，结果作废。" : cohort.status === "failed" ? "本轮未通过，修复后需建立新 cohort。" : `当前已通过 ${passedCriteria} / ${totalCriteria || 13} 项门槛。`;
    setText("[data-cohort-live-result]", resultText);
    const closeButton = one("[data-close-cohort]");
    closeButton.disabled = cohort.status !== "active";
    closeButton.textContent = cohort.status === "active" ? "结束并验收本轮" : "本轮已关闭";
    const evaluations = arrayFrom(live.evaluations, "items");
    const evaluated = new Set(evaluations.map((item) => Number(item.chapter)));
    const buttons = one("[data-cohort-chapter-buttons]"); clear(buttons);
    for (let chapter = cohort.start_chapter; chapter <= cohort.end_chapter; chapter += 1) {
      const button = el("button", evaluated.has(chapter) ? "is-evaluated" : "", chapter); button.type = "button"; button.dataset.chapter = String(chapter);
      button.setAttribute("aria-label", `评分第 ${chapter} 章${evaluated.has(chapter) ? "，已评分" : ""}`);
      button.addEventListener("click", () => renderCohortEvaluation(chapter)); buttons.append(button);
    }
    const firstUnevaluated = Array.from({ length: cohort.end_chapter - cohort.start_chapter + 1 }, (_, index) => cohort.start_chapter + index).find((chapter) => !evaluated.has(chapter));
    renderCohortEvaluation(firstUnevaluated || state.evaluationChapter || cohort.start_chapter);
  };

  const loadCohort = async (cohortId) => {
    const { payload } = await api(`/api/cohorts/${encodeURIComponent(cohortId)}`);
    renderCohortDetail(payload);
  };

  const renderCohorts = async (payload) => {
    const cohorts = arrayFrom(payload.cohorts, "items");
    state.scoreDimensions = payload.score_dimensions || Object.keys(scoreDimensionLabels);
    UI.updateStatusBadge(one("[data-product-readiness]"), payload.self_use_production_ready ? "strict" : "experimental", payload.self_use_production_ready ? "自用生产版已达标" : `${cohorts.filter((item) => item.status === "passed").length} / 2 轮通过`);
    const active = cohorts.find((item) => item.status === "active");
    const latest = active || cohorts[0] || null;
    const createPanel = one("[data-cohort-create-panel]");
    const activePanel = one("[data-cohort-active]");
    const createForm = one("[data-cohort-create-form]");
    createForm.action = `/api/projects/${encodeURIComponent(state.projectId)}/cohorts`;
    one('input[name="start_chapter"]', createForm).value = payload.project?.next_chapter || state.project?.next_chapter || 1;
    createPanel.hidden = Boolean(active);
    activePanel.hidden = !latest;
    if (latest) await loadCohort(latest.id);
    else { state.cohort = null; state.cohortLiveResult = null; }
  };

  const renderProduction = async (payload) => {
    const jobs = arrayFrom(payload, "jobs");
    const finishedStates = new Set(["completed", "committed", "cancelled", "failed"]);
    const pausedStates = new Set(["awaiting_input", "needs_recovery"]);
    const runningStates = new Set(["held", "queued", "starting", "running", "pause_requested"]);
    const session = payload.session || payload.active_session || payload.job || (payload.job_id ? payload : jobs.find((item) => !finishedStates.has(item.state)));
    const launch = one("[data-production-launch]");
    const sessionPanel = one("[data-production-session]");
    const form = one("[data-session-form]");
    form.action = `/api/projects/${encodeURIComponent(state.projectId)}/sessions`;
    one('input[name="start_chapter"]', form).value = payload.project?.next_chapter || payload.next_chapter || state.project?.next_chapter || 1;
    await renderCohorts(payload);
    const jobId = session?.job_id || session?.id;
    const jobStatus = session?.status || session?.state;
    const visible = Boolean(session && jobId && !finishedStates.has(jobStatus));
    const paused = visible && pausedStates.has(jobStatus);
    const running = visible && runningStates.has(jobStatus);
    const provenanceFailure = paused ? preProviderProvenanceDetailsOf(session) : null;
    const provenanceReady = Boolean(provenanceFailure?.verified && session?.resume_ready === true);
    const contextBudget = paused && !provenanceFailure ? contextBudgetDetailsOf(session) : null;
    const contextBudgetReady = Boolean(contextBudget?.verified && session?.resume_ready === true);
    const repairConvergence = paused && !provenanceFailure && !contextBudget
      ? repairNotConvergingDetailsOf(session)
      : null;
    const repairAuthority = paused && !provenanceFailure && !contextBudget && !repairConvergence
      ? repairAuthorityDetailsOfRun(payload.chapter_recovery?.latest_run)
      : null;
    const qualityRejection = paused && !provenanceFailure && !contextBudget && !repairConvergence && !repairAuthority
      && payload.chapter_recovery?.formal_prose_exists === false
      ? qualityRejectionDetailsOfRun(payload.chapter_recovery?.latest_run)
      : null;
    const qualityRevision = paused && !provenanceFailure && !contextBudget && !repairConvergence && !repairAuthority && (Boolean(qualityRejection) || isQualityRevisionPause(session));
    const providerUncertain = paused && !provenanceFailure && !contextBudget && !repairConvergence && !repairAuthority && !qualityRejection && jobStatus === "needs_recovery";
    launch.hidden = visible;
    sessionPanel.hidden = !visible;
    const statusLabel = provenanceFailure ? (provenanceReady ? "等待作者确认" : provenanceFailure.verified ? "等待系统修复" : "安全核对中") : contextBudget ? (contextBudgetReady ? "等待作者确认" : contextBudget.verified ? "等待系统修复" : "安全核对中") : repairConvergence ? (repairConvergence.verified ? "等待无模型恢复" : "安全核对中") : repairAuthority ? "等待安全恢复" : qualityRevision ? "质量审核需修订" : paused ? (providerUncertain ? "需要安全恢复" : "等待作者处理") : session?.status_label;
    UI.updateStatusBadge(one("[data-production-status]"), visible ? jobStatus || "generating" : "idle", visible ? statusLabel : "空闲");
    const currentChapter = session?.current_chapter || session?.next_chapter;
    setText("[data-production-title]", provenanceFailure ? (provenanceFailure.verified ? `第 ${currentChapter || "—"} 章在模型请求前停止` : `第 ${currentChapter || "—"} 章正在安全核对`) : contextBudget ? `第 ${currentChapter || "—"} 章输入上下文超出安全上限` : repairConvergence ? (repairConvergence.verified ? `第 ${currentChapter || "—"} 章自动修订未收敛` : `第 ${currentChapter || "—"} 章修订故障正在安全核对`) : repairAuthority ? `第 ${currentChapter || "—"} 章修订上下文装配失败` : qualityRevision ? `第 ${currentChapter || "—"} 章质量审核需修订` : paused ? `第 ${currentChapter || "—"} 章已安全暂停` : visible ? `正在写第 ${currentChapter || "—"} 章` : "开始新的生产批次");
    const pill = one("[data-active-job-pill]");
    if (pill) {
      pill.hidden = !visible;
      pill.href = paused ? projectHash("pending") : projectHash("production");
      pill.classList.toggle("is-attention", paused);
    }
    setText("[data-active-job-label]", paused ? "有写作任务需要处理" : "写作任务运行中");
    if (!visible) {
      views.get("production")?._workbenchStopStream?.();
      return;
    }
    setText("[data-session-chapter]", provenanceFailure ? `第 ${currentChapter || "—"} 章 · ${provenanceReady ? "Git 环境已修复，等待确认" : provenanceFailure.verified ? "等待系统修复 Git 环境" : "疑似 Git 环境错误，安全核对中"}` : contextBudget ? `第 ${currentChapter || "—"} 章 · ${contextBudgetReady ? "修复已完成，等待确认" : contextBudget.verified ? "等待系统修复" : "安全核对中"}` : repairConvergence ? `第 ${currentChapter || "—"} 章 · ${repairConvergence.verified ? "候选正文已保留，等待无模型恢复" : "调用与产物仍在安全核对"}` : repairAuthority ? `第 ${currentChapter || "—"} 章 · 候选正文已保留，修订请求尚未发出` : qualityRevision ? `第 ${currentChapter || "—"} 章 · 质量审核需修订` : paused ? `第 ${currentChapter || "—"} 章 · 已停止自动执行` : `第 ${currentChapter || "—"} 章 · ${session.chapter_title || "正在按冻结章纲生成"}`);
    const queue = one("[data-chapter-queue]"); clear(queue);
    let queueChapters = arrayFrom(session.chapters, "items");
    if (!queueChapters.length && session.start_chapter && session.end_chapter) {
      queueChapters = Array.from({ length: session.end_chapter - session.start_chapter + 1 }, (_, index) => {
        const number = session.start_chapter + index;
        return { number, status: number < currentChapter ? "committed" : number === currentChapter ? jobStatus : "queued" };
      });
    }
    queueChapters.forEach((chapter) => {
      const item = el("li", `queue-${chapter.status || "queued"}`); item.dataset.queueChapter = String(chapter.number);
      const copy = el("span"); copy.append(el("strong", "", chapter.title || "未命名"), el("small", "", chapter.status_label || "等待中"));
      item.append(el("span", "queue-number", chapter.number), copy, el("span", "queue-result", chapter.status === "committed" ? "✓" : "•")); queue.append(item);
    });
    one("[data-pause-job-form]").action = `/api/jobs/${encodeURIComponent(jobId)}/pause`;
    one("[data-cancel-job-form]").action = `/api/jobs/${encodeURIComponent(jobId)}/cancel`;
    const safetyPanel = one("[data-production-controls]");
    const attentionPanel = one("[data-production-attention]");
    if (safetyPanel) safetyPanel.hidden = paused;
    if (attentionPanel) attentionPanel.hidden = !paused;
    setText("[data-production-attention-eyebrow]", provenanceFailure ? "本机运行环境错误" : contextBudget ? "本地上下文超限" : repairConvergence ? "自动修订未收敛" : repairAuthority ? "修订上下文装配错误" : qualityRevision ? "质量审核需修订" : "已安全暂停");
    setText("[data-production-attention-title]", provenanceFailure ? (provenanceReady ? "Git 环境已修复，等待确认" : provenanceFailure.verified ? "Git 来源记录环境异常" : "疑似 Git 环境错误，安全核对中") : contextBudget ? (contextBudgetReady ? "上下文修复已完成" : contextBudget.verified ? "模型请求尚未发出" : "上下文错误正在安全核对") : repairConvergence ? (repairConvergence.verified ? "修订稿引入了更严重的连续性问题" : "修订故障正在安全核对") : repairAuthority ? "质量修订请求在本地预算检查中停止" : qualityRejection ? "质量门发现阻断逻辑问题" : qualityRevision ? "Provider 已成功结束" : "不要重复启动或紧急停止");
    setText("[data-production-attention-detail]", provenanceFailure ? (provenanceReady ? `${preProviderProvenanceStopDetail(provenanceFailure)} 系统已修复 Git 环境；前往待处理由你确认后才会重新执行。` : `${preProviderProvenanceSummary(provenanceFailure)} ${preProviderProvenanceStopDetail(provenanceFailure)}`) : contextBudget ? (contextBudgetReady ? `${contextBudgetSummary(contextBudget)} ${contextBudgetStopDetail(contextBudget)} 系统已完成降载修复，前往待处理由你确认后才会重新执行。` : `${contextBudgetSummary(contextBudget)} ${contextBudgetStopDetail(contextBudget)}`) : repairConvergence ? (repairConvergence.verified ? `${repairConvergence.settledProviderCalls}/${repairConvergence.providerCalls} 次模型调用已结算，${repairConvergence.candidateChars} 字原候选已保留；${repairNotConvergingSummary(repairConvergence)} 正式正文未提交。前往待处理先做无模型恢复分类。` : `${repairNotConvergingSummary(repairConvergence)} 系统不会盲目重试，安全核对完成前不能继续。`) : repairAuthority ? `生成与审核的 ${repairAuthority.providerCalls} 次模型调用均已结算，${repairAuthority.chapterChars} 字候选正文保存在隔离运行区。修订器需要 ${repairAuthority.requiredChars} 字符的权威记录，旧预算只有 ${repairAuthority.budgetChars}；修订模型请求尚未发出，正式正文未提交。前往待处理先做无模型恢复分类。` : qualityRejection ? `${qualityRejection.providerCalls} 次模型调用均已结算，${qualityRejection.chapterChars} 字候选已保留，正式正文未提交。阻断原因：${qualityProblemSummary(qualityRejection)} 前往待处理编辑恢复草稿并重新验证。` : qualityRevision ? "完整候选草稿已保留，正式正文未提交。前往待处理可启动自动修复并重新校验。" : "正式正文尚未提交。先到待处理识别最新可信恢复点，再决定是否重新校验。");
    const attentionLink = one("[data-production-attention-link]");
    if (attentionLink) attentionLink.href = projectHash("pending");
    const progress = one(".scene-progress .progress", sessionPanel);
    progress?.classList.toggle("is-indeterminate", running);
    if (progress) {
      progress.setAttribute("aria-valuetext", paused ? "任务已安全暂停" : "任务正在运行，暂时没有精确百分比");
      if (paused) progress.setAttribute("aria-valuenow", "0"); else progress.removeAttribute("aria-valuenow");
    }
    setText("[data-progress-label]", provenanceFailure ? preProviderProvenanceSummary(provenanceFailure) : contextBudget ? contextBudgetSummary(contextBudget) : repairConvergence ? repairNotConvergingSummary(repairConvergence) : repairAuthority ? `修订权威记录需要 ${repairAuthority.requiredChars} 字符，旧预算为 ${repairAuthority.budgetChars}` : qualityRejection ? qualityProblemSummary(qualityRejection) : qualityRevision ? "Provider 已成功结束，质量审核要求修订" : providerUncertain ? "模型调用状态不确定，系统已停止自动重试" : paused ? "任务已按作者要求暂停" : "模型正在生成与校验，本步可能需要几分钟");
    setText("[data-progress-percent]", provenanceFailure || contextBudget || repairConvergence || repairAuthority ? "已停止" : paused ? "已暂停" : "进行中");
    setText("[data-live-title]", provenanceFailure ? (provenanceFailure.verified ? "本次新增调用 0 次 · 新增费用 0 · 无草稿正文" : "模型调用、费用与产物仍在安全核对") : contextBudget ? (contextBudget.verified ? "本次新增调用 0 次，新增费用 0" : "模型调用、费用与产物仍在安全核对") : repairConvergence ? (repairConvergence.verified ? `${repairConvergence.settledProviderCalls}/${repairConvergence.providerCalls} 次调用已结算 · ${repairConvergence.candidateChars} 字原候选已保留` : "模型调用、候选正文与提交状态仍在安全核对") : repairAuthority ? `${repairAuthority.providerCalls} 次调用已结算 · ${repairAuthority.chapterChars} 字候选已保留` : qualityRejection ? `${qualityRejection.providerCalls} 次调用已结算 · ${qualityRejection.chapterChars} 字候选待修订` : qualityRevision ? "完整草稿已保留，等待自动修复" : paused ? "本轮已经停止，不需要紧急终止" : "正在等待模型与引擎回执");
    setText("[data-live-detail]", provenanceFailure ? `${preProviderProvenanceStopDetail(provenanceFailure)} ${provenanceReady ? "等待作者确认重新执行。" : "当前不能重新执行。"}` : contextBudget ? contextBudgetStopDetail(contextBudget) : repairConvergence ? (repairConvergence.verified ? "修订稿已拒绝，原候选仍在隔离运行区，正式正文未提交。请前往待处理执行无模型恢复分类；不要直接重试。" : "证据核对完成前不会恢复、重试或调用模型。") : repairAuthority ? "修订模型请求尚未发出；正式正文未提交。请前往待处理先执行无模型恢复分类。" : qualityRejection ? "正式正文未提交。请前往待处理编辑恢复草稿，重新验证通过后再确认提交。" : qualityRevision ? "正式正文未提交。请前往待处理，确认调用真实模型自动修复并重新校验。" : providerUncertain ? "正式正文尚未提交。请前往待处理执行无模型恢复分类。" : paused ? "任务正在等待作者决定是否继续。" : "关闭或刷新页面不会重复调用模型，任务状态会从本地记录恢复。");
    const root = views.get("production");
    root.dataset.jobStream = "";
    root.dataset.jobId = jobId;
    root.dataset.jobKind = session.kind || "";
    root.dataset.eventsUrl = session.events_url || `/api/jobs/${encodeURIComponent(jobId)}/events`;
    root.dataset.statusUrl = session.status_url || `/api/jobs/${encodeURIComponent(jobId)}`;
    root.dataset.lastEventId = session.last_event_id || "";
    if (root._spaTerminalHandler) root.removeEventListener("workbench:job-terminal", root._spaTerminalHandler);
    root._spaTerminalHandler = (event) => {
      const terminalStatus = event.detail?.status;
      const recoveryFinished = root.dataset.jobKind === "locked_chapter_recovery" && terminalStatus === "completed";
      if (pausedStates.has(terminalStatus) || recoveryFinished) {
        window.setTimeout(() => {
          if (state.route?.area === "production") window.location.hash = projectHash("pending");
        }, 80);
      } else if (finishedStates.has(terminalStatus)) {
        window.setTimeout(async () => {
          if (state.route?.area !== "production") return;
          try {
            await loadProject(state.projectId, true);
            if (state.route?.area === "production") await loadProduction();
          } catch (error) {
            showError(error);
          }
        }, 80);
      }
    };
    root.addEventListener("workbench:job-terminal", root._spaTerminalHandler);
    if (paused) {
      root._workbenchStopStream?.();
      const connection = one("[data-connection-state]", root);
      if (connection) {
        connection.classList.remove("is-polling");
        connection.classList.add("is-offline");
        const pulse = one(".pulse", connection);
        if (pulse) pulse.hidden = true;
        connection.lastChild.textContent = "本轮已停止";
      }
    } else {
      UI.initJobStream(root);
    }
    updateProvider(payload.provider || {});
  };

  const loadProduction = async () => {
    const [productionResponse, pendingResponse] = await Promise.all([
      api(`/api/projects/${encodeURIComponent(state.projectId)}/production`),
      api(`/api/projects/${encodeURIComponent(state.projectId)}/pending`)
    ]);
    const payload = {
      ...productionResponse.payload,
      chapter_recovery: pendingResponse.payload?.chapter_recovery || pendingResponse.payload?.chapter_20_recovery || null
    };
    await renderProduction(payload);
  };

  const renderRecoveryDraft = (issue) => {
    const panel = one("[data-recovery-draft-panel]");
    const chapter = Number(issue.chapter || issue.next_chapter || 0);
    const draft = state.drafts.find((item) => Number(item.chapter) === chapter);
    panel.hidden = !draft;
    if (!draft) return;
    const parentJob = issue.job_id ? issue : state.pendingJobs.find((job) => Number(job.next_chapter || job.chapter) === chapter);
    setText("[data-draft-title]", `第 ${chapter} 章恢复草稿`);
    UI.updateStatusBadge(one("[data-draft-status]"), draft.state === "validated" ? "pass" : draft.state === "validation_failed" ? "needs_revision" : "draft", draft.state === "validated" ? "已验证" : draft.state === "validation_failed" ? "验证未通过" : "草稿");
    setText("[data-draft-revision]", `r${draft.revision || 1} · ${(draft.sha256 || "—").slice(0, 12)}`);
    const updateForm = one("[data-draft-update-form]");
    updateForm.action = `/api/drafts/${encodeURIComponent(draft.id)}`;
    updateForm.dataset.casRevision = String(draft.revision || 1);
    updateForm.dataset.casSha = draft.sha256 || "";
    one('input[name="expected_revision"]', updateForm).value = String(draft.revision || 1);
    one('textarea[name="text"]', updateForm).value = draft.text || "";
    const sourceConflict = issue.draft_source_conflict;
    const sourceConflictPanel = one("[data-draft-source-conflict]");
    sourceConflictPanel.hidden = !sourceConflict;
    if (sourceConflict) {
      one("[data-draft-source-conflict-text]").value = sourceConflict.incoming_text || "";
      setText(
        "[data-draft-source-conflict-hashes]",
        `当前草稿 ${(sourceConflict.current_sha256 || "—").slice(0, 12)} · 更新来源 ${(sourceConflict.incoming_sha256 || "—").slice(0, 12)}`
      );
    }
    const validateForm = one("[data-draft-validate-form]");
    validateForm.action = `/api/drafts/${encodeURIComponent(draft.id)}/validate`;
    one('input[name="job_id"]', validateForm).value = parentJob?.job_id || parentJob?.id || "";
    const confirmForm = one("[data-draft-confirm-form]");
    confirmForm.action = `/api/drafts/${encodeURIComponent(draft.id)}/confirm`;
    one('input[name="job_id"]', confirmForm).value = parentJob?.job_id || parentJob?.id || "";
    const confirmButton = one("[data-draft-confirm-button]", confirmForm);
    confirmButton.disabled = draft.state !== "validated" || !parentJob;
    confirmButton.title = !parentJob ? "这份草稿不属于当前暂停批次；请使用上方的单章恢复确认" : draft.state !== "validated" ? "必须先通过恢复验证" : "";
  };

  const renderPendingDetail = (issue) => {
    state.selectedPending = issue;
    const provenanceFailure = issue.pre_provider_provenance || preProviderProvenanceDetailsOf(issue);
    const provenanceReady = Boolean(provenanceFailure?.verified && issue.resume_ready === true);
    const contextBudget = !provenanceFailure ? (issue.context_budget || contextBudgetDetailsOf(issue)) : null;
    const contextBudgetReady = Boolean(contextBudget?.verified && issue.resume_ready === true);
    const draftSourceConflict = issue.draft_source_conflict;
    const repairConvergence = !provenanceFailure && !contextBudget
      ? (issue.repair_not_converging || repairNotConvergingDetailsOf(issue))
      : null;
    const qualityRejection = !provenanceFailure && !contextBudget && !repairConvergence
      ? (issue.quality_rejection || qualityRejectionDetailsOfRun(issue.latest_run))
      : null;
    const qualityRevision = !provenanceFailure && !contextBudget && !repairConvergence && (Boolean(qualityRejection) || issue.quality_revision === true || isQualityRevisionPause(issue));
    const qualityDraft = qualityRevision
      ? state.drafts.find((item) => Number(item.chapter) === Number(issue.chapter || issue.next_chapter))
      : null;
    const sceneSchemaFailure = !provenanceFailure && !contextBudget && !repairConvergence && !qualityRevision && issue.scene_schema_failure === true;
    const contextHeadroomFailure = !provenanceFailure && !contextBudget && !repairConvergence && !qualityRevision && issue.context_headroom_failure === true;
    const repairAuthorityFailure = !provenanceFailure && !contextBudget && !repairConvergence && !qualityRevision && issue.repair_authority_failure === true;
    const repairAuthority = repairAuthorityFailure ? issue.repair_authority : null;
    const settledGenerationFailure = sceneSchemaFailure || contextHeadroomFailure || repairAuthorityFailure || Boolean(repairConvergence?.verified);
    setText("[data-pending-detail-eyebrow]", provenanceFailure ? "本机运行环境错误" : contextBudget ? "本地上下文超限" : repairConvergence ? "自动修订未收敛" : qualityRevision ? "质量审核需修订" : sceneSchemaFailure ? "模型响应格式错误" : contextHeadroomFailure ? "本地安全余量不足" : repairAuthorityFailure ? "修订上下文装配错误" : "可恢复生成");
    UI.updateStatusBadge(one(".recovery-hero .status-badge"), provenanceFailure ? (provenanceReady ? "paused_recoverable" : "needs_revision") : contextBudget ? (contextBudgetReady ? "paused_recoverable" : "needs_revision") : repairConvergence ? (repairConvergence.verified ? "paused_recoverable" : "needs_revision") : qualityRevision ? "needs_revision" : "paused_recoverable", provenanceFailure ? (provenanceReady ? "可重新执行" : provenanceFailure.verified ? "等待系统修复" : "安全核对中") : contextBudget ? (contextBudgetReady ? "可重新执行" : contextBudget.verified ? "等待系统修复" : "安全核对中") : repairConvergence ? (repairConvergence.verified ? "等待恢复分类" : "安全核对中") : qualityRevision ? "待修订" : "可恢复");
    setText("[data-pending-title]", issue.title || (provenanceFailure ? (provenanceFailure.verified ? `第 ${issue.chapter || 20} 章在模型请求前停止` : `第 ${issue.chapter || 20} 章正在安全核对`) : contextBudget ? `第 ${issue.chapter || 20} 章输入上下文超出安全上限` : repairConvergence ? (repairConvergence.verified ? `第 ${issue.chapter || 20} 章自动修订未收敛` : `第 ${issue.chapter || 20} 章修订故障正在安全核对`) : repairAuthorityFailure ? `第 ${issue.chapter || 20} 章修订上下文装配失败` : qualityRevision ? `第 ${issue.chapter || 20} 章质量审核需修订` : `第 ${issue.chapter || 20} 章生成中断`));
    setText("[data-pending-summary]", provenanceFailure ? `${preProviderProvenanceSummary(provenanceFailure)} ${preProviderProvenanceStopDetail(provenanceFailure)}` : contextBudget ? `${contextBudgetSummary(contextBudget)} ${contextBudgetStopDetail(contextBudget)}` : repairConvergence ? repairNotConvergingSummary(repairConvergence) : issue.summary || issue.reason || (qualityRevision ? "Provider 已成功结束，完整候选草稿已保留，正式正文未提交。" : "可信场景可以安全复用，从中断处继续。"));
    setText("[data-pending-chapter]", issue.chapter || 20);
    setText("[data-pending-section-title]", provenanceFailure ? (provenanceFailure.verified ? "模型请求前停止" : "安全状态仍在核对") : contextBudget ? (contextBudget.verified ? "请求在发送给模型前已停止" : "安全状态仍在核对") : repairConvergence ? (repairConvergence.verified ? "原候选已保留，失败修订稿已拒绝" : "安全状态仍在核对") : qualityRevision ? "本章已经完成生成，但尚未通过质量门" : sceneSchemaFailure ? "调用已结算，格式错误没有进入正文" : contextHeadroomFailure ? "调用已结算，边界重生请求被本地预算门拦下" : repairAuthorityFailure ? "候选正文已保留，修订请求尚未发出" : "已完成内容不会重写");
    setText("[data-pending-safe-chip]", provenanceFailure ? (provenanceFailure.verified ? "✓ 本次新增调用 0 · 新增费用 0 · 无草稿正文" : "! 调用、费用与产物尚未完全确认") : contextBudget ? (contextBudget.verified ? "✓ 本次新增调用与新增费用均为 0" : "! 调用、费用与产物尚未完全确认") : repairConvergence ? (repairConvergence.verified ? `✓ ${repairConvergence.settledProviderCalls}/${repairConvergence.providerCalls} 次调用已结算 · ${repairConvergence.candidateChars} 字原候选已保留 · 正式正文未提交` : "! 调用、候选正文与提交状态尚未完全确认") : qualityRejection ? `✓ ${qualityRejection.providerCalls} 次调用已结算 · ${qualityRejection.chapterChars} 字候选已保留` : qualityRevision ? "✓ 草稿已保留" : repairAuthorityFailure ? `✓ ${repairAuthority.providerCalls} 次调用已结算 · ${repairAuthority.chapterChars} 字候选已保留` : settledGenerationFailure ? `✓ ${Number(issue.latest_run?.provider_calls) || 0} 次调用已结算 · ${Number(issue.reusable_scene_count) || 0} 个场景可复用` : "✓ 哈希绑定");
    const scenes = one("[data-recovery-scenes]"); clear(scenes);
    let sceneItems = arrayFrom(issue.scenes || issue.recoverable_scenes, "items");
    const reusableCount = Number(issue.reusable_scene_count || issue.checkpoint?.reusable_scene_count || 0);
    const expectedSceneCount = Number(issue.expected_scene_count || issue.checkpoint?.expected_scene_count || 0);
    const allScenesReusable = expectedSceneCount > 0 && reusableCount >= expectedSceneCount;
    const completeSequenceNeedsRevalidation = allScenesReusable
      && issue.checkpoint?.reason === "complete_scene_sequence_requires_revalidation";
    if (provenanceFailure) {
      sceneItems = provenanceFailure.verified ? [
        {
          title: "Git 来源记录环境",
          detail: provenanceReady ? "上次尝试未能建立 Git 来源记录；当前环境已通过预检。" : "Git 执行来源记录未能建立，任务已停止。",
          status: provenanceReady ? "trusted" : "partial",
          action: provenanceReady ? "环境已修复" : "等待系统修复"
        },
        { title: "本次模型调用与费用", detail: "本次尝试在模型请求前停止；新增调用 0 次，新增费用 0。", status: "trusted", action: "未新增" },
        { title: "本次草稿与正文", detail: "本次尝试无草稿正文，也没有提交任何内容。", status: "trusted", action: "未生成" }
      ] : [
        { title: "疑似 Git 环境错误", detail: "系统正在核对错误来源与完整运行证据。", status: "partial", action: "安全核对中" },
        { title: "模型调用与费用", detail: "本次新增调用与新增费用尚未完全确认。", status: "partial", action: "尚未确认" },
        { title: "草稿与正文", detail: "草稿、正文与提交状态尚未完全确认。", status: "partial", action: "尚未确认" }
      ];
    } else if (contextBudget) {
      sceneItems = contextBudget.verified ? [
        { title: "本地上下文检查", detail: contextBudgetSummary(contextBudget), status: "partial", action: `超出 ${contextBudget.overageTokens ?? "—"} tokens` },
        { title: "本次模型调用与费用", detail: "请求尚未发出；本次新增调用 0 次，新增费用 0。", status: "trusted", action: "未新增" },
        { title: "本次草稿与正文", detail: "本次没有生成草稿正文，也没有提交任何内容。", status: "trusted", action: "未生成" }
      ] : [
        { title: "本地上下文检查", detail: contextBudgetSummary(contextBudget), status: "partial", action: "已识别" },
        { title: "模型调用与费用", detail: "本次新增调用与新增费用尚未完全确认。", status: "partial", action: "尚未确认" },
        { title: "草稿与正文", detail: "草稿、正文与提交状态尚未完全确认。", status: "partial", action: "尚未确认" }
      ];
    } else if (repairConvergence) {
      sceneItems = repairConvergence.verified ? [
        { title: "模型调用", detail: `${repairConvergence.providerCalls} 次调用全部有已结算回执，没有未结算调用。`, status: "trusted", action: `${repairConvergence.settledProviderCalls}/${repairConvergence.providerCalls} 已结算` },
        { title: "原候选正文", detail: `${repairConvergence.candidateChars} 字原候选已保留在隔离运行区；正式正文未提交，也没有执行状态回写。`, status: "trusted", action: "已保留 · 未提交" },
        { title: "自动修订", detail: `阻断问题从 ${repairConvergence.originalProblemCount} 项降到 ${repairConvergence.finalProblemCount} 项，但新增了更严重的${repairNewProblemLabel(repairConvergence)}问题；系统已拒绝采用修订稿。`, status: "partial", action: "修订稿已拒绝" }
      ] : [
        { title: "修订未收敛", detail: "已收到稳定故障码，但完整结构化运行证据仍在核对。", status: "partial", action: "安全核对中" },
        { title: "模型调用与费用", detail: "调用数量与结算状态尚未完全确认。", status: "partial", action: "尚未确认" },
        { title: "候选正文与提交", detail: "候选正文、正式提交和状态回写尚未完全确认。", status: "partial", action: "尚未确认" }
      ];
    } else if (repairAuthorityFailure) {
      sceneItems = [
        { title: "生成与语义审核", detail: `${repairAuthority.providerCalls} 次模型调用都有完整回执且已经结算。`, status: "trusted", action: "已结算" },
        { title: "候选正文", detail: `${repairAuthority.chapterChars} 字候选正文保存在隔离运行区，正式正文尚未提交。`, status: "trusted", action: "已保留" },
        { title: "质量修订上下文", detail: `本次修订必须保留 ${repairAuthority.requiredChars} 字符的权威记录，旧预算只有 ${repairAuthority.budgetChars}，超出 ${repairAuthority.overageChars} 字符；修订模型请求尚未发出。`, status: "partial", action: "等待重新分类" }
      ];
    } else if (qualityRejection) {
      sceneItems = [
        { title: "模型调用与候选正文", detail: `${qualityRejection.providerCalls} 次模型调用都有完整回执且已经结算；${qualityRejection.chapterChars} 字候选保存在隔离运行区。`, status: "trusted", action: "已结算" },
        { title: "阻断质量问题", detail: qualityProblemSummary(qualityRejection), status: "partial", action: "需修订" },
        { title: "正式正文", detail: "候选尚未写入正式正文；必须修改草稿并重新通过校验后才能提交。", status: "trusted", action: "未提交" }
      ];
    } else if (qualityRevision) {
      sceneItems = [
        { title: "Provider 调用", detail: "模型响应与完整候选草稿已经安全落盘。", status: "trusted", action: "已成功结束" },
        { title: "质量审核", detail: "本章需要自动修复后再次通过审核与质量门。", status: "partial", action: "需修订" },
        { title: "正式正文", detail: "当前候选仍在隔离运行区，尚未写入正式正文。", status: "partial", action: "未提交" }
      ];
    } else if (!sceneItems.length && reusableCount > 0) {
      const displayedSceneCount = allScenesReusable ? expectedSceneCount : reusableCount + 1;
      sceneItems = Array.from({ length: displayedSceneCount }, (_, index) => ({ number: index + 1, status: index < reusableCount ? "trusted" : "partial" }));
    }
    if (!provenanceFailure && !qualityRevision && !sceneItems.length && Number(issue.next_scene) > 1) {
      sceneItems = Array.from({ length: Number(issue.next_scene) }, (_, index) => ({ number: index + 1, status: index + 1 < issue.next_scene ? "trusted" : "partial" }));
    }
    sceneItems.forEach((scene) => {
      const trusted = scene.status === "trusted" || scene.recoverable === true;
      const item = el("li", trusted ? "scene-trusted" : "scene-partial");
      item.append(el("span", "scene-node", scene.mark || (trusted ? "✓" : "!")));
      const copy = el("div"); copy.append(el("strong", "", scene.title || `场景 ${scene.number}`), el("p", "", scene.detail || (trusted ? "回执完整，内容哈希一致" : "Provider 部分响应，不作为可信正文")));
      item.append(copy, el("span", "scene-action", scene.action || (trusted ? "保留" : "从此重试"))); scenes.append(item);
    });
    const nextScene = issue.next_scene || sceneItems.find((scene) => scene.status !== "trusted")?.number || 1;
    if (!provenanceFailure && !repairConvergence && !qualityRevision && allScenesReusable) setText(
      "[data-pending-summary]",
      completeSequenceNeedsRevalidation
        ? `全部 ${reusableCount} 份场景候选都已通过回执、结构与哈希核对；恢复后会逐场重跑边界校验，只重生未通过的场景。`
        : `全部 ${reusableCount} 个场景都已通过回执与哈希核对；不会重新生成场景，但会调用真实模型重新运行导演决策与语义校验。`
    );
    setText(
      "[data-recovery-action-label]",
      provenanceFailure ? (provenanceReady ? "Git 环境已修复，等待你的确认" : provenanceFailure.verified ? "等待系统修复 Git 环境" : "安全核对完成前不能重新执行") : contextBudget ? (contextBudgetReady ? "系统修复已完成，等待你的确认" : contextBudget.verified ? `修复完成后重新执行第 ${issue.chapter || 20} 章` : "安全核对完成前不能重新执行") : repairConvergence ? (!repairConvergence.verified ? "安全核对完成前不能恢复或重试" : issue.checkpoint ? "恢复证据已建立，等待安全继续" : "先执行无模型恢复分类") : qualityRejection ? (!issue.checkpoint ? "先生成可编辑恢复草稿" : qualityDraft?.state === "validated" ? "草稿已通过恢复验证，等待确认提交" : "建议：修改下方草稿并重新验证") : qualityRevision ? "建议：自动修复并重新校验" : issue.checkpoint ? (allScenesReusable ? (completeSequenceNeedsRevalidation ? `建议：复核全部 ${reusableCount} 份场景候选，仅重生未通过场景` : `建议：复用全部 ${reusableCount} 个场景，重新执行导演决策与校验`) : `建议：从场景 ${nextScene} 继续`) : "先识别最新可信恢复边界"
    );
    setText("[data-recovery-action-detail]", provenanceFailure ? (provenanceReady ? "只有点击下方按钮并再次确认后，系统才会重新执行并调用真实模型；不点击则继续暂停，不新增费用。" : provenanceFailure.verified ? "当前不能重新执行；系统修复 Git 环境前，按钮保持禁用。" : "调用、费用与产物证据尚未完全确认；安全核对完成前按钮保持禁用。") : contextBudget ? (contextBudgetReady ? "只有点击下方按钮并再次确认后，系统才会调用真实模型；不点击则继续保持暂停，不新增费用。" : contextBudget.verified ? "系统修复并重新核对输入预算后才能再次执行；当前不会调用模型。" : "调用、费用与产物证据尚未完全确认；安全核对完成前按钮保持禁用。") : repairConvergence ? (!repairConvergence.verified ? "结构化安全证据尚未闭环；按钮保持禁用，不会盲目重试或调用模型。" : !issue.checkpoint ? "先执行无模型恢复分类，核对已结算回执、原候选与可信场景边界；这一步不会调用模型、提交正文或写回状态。" : issue.resume_ready === true ? "恢复证据已绑定原暂停任务；只有再次明确确认后才会调用模型重新校验，系统不会直接重试失败修订稿。" : "恢复检查点已经建立，但尚未绑定到原暂停任务；当前不能直接重试。") : qualityRejection ? (!issue.checkpoint ? "先做无模型分类，把完整候选转成带 revision/SHA 的可编辑草稿；不会调用模型或提交正文。" : draftSourceConflict ? "你的编辑已原样保留；请先对照下方更新来源稿完成合并，再保存并重新验证。" : qualityDraft?.state === "validated" ? "请使用下方“确认提交并继续”；原任务只会在再次确认后恢复，并且仍须通过质量门。" : "在下方只修改阻断问题，保存后点击“重新验证”；正式正文在验证通过前不会提交。") : qualityRevision ? "此操作会调用真实模型；只有修复内容重新通过校验与质量门后，才会提交正式正文。" : settledGenerationFailure ? (issue.checkpoint ? (completeSequenceNeedsRevalidation ? `将载入 ${reusableCount} 份候选并逐场重跑边界校验；只有未通过的场景会调用模型重生，全部通过后才会继续审核与提交。` : `将保留前 ${Number(issue.reusable_scene_count) || 0} 个可信场景，从场景 ${Number(issue.next_scene) || 1} 继续；恢复会调用真实模型完成剩余场景。`) : "先执行无模型恢复分类，核对已结算回执和可复用场景；这一步不会调用模型或提交正文。") : "恢复会创建追加式检查点，不改写正式正文或历史快照。");
    const form = one("[data-recover-form]");
    const chapter = issue.chapter || 20;
    const checkpointReady = Boolean(issue.checkpoint);
    const exactParentId = issue.job_id || issue.id;
    const pausedParent = state.pendingJobs.find((job) => String(job.job_id || job.id) === String(exactParentId)) || state.pendingJobs.find((job) => (
      job.kind === "generation_session"
      && ["needs_recovery", "awaiting_input"].includes(job.state)
      && Number(job.next_chapter || job.chapter) === Number(chapter)
    ));
    const classifyChapter = one("[data-recovery-classify-chapter]", form);
    const sessionFields = all("[data-recovery-session-field]", form);
    const resumeField = one("[data-recovery-resume-field]", form);
    const recoverButton = one("[data-recover-button]", form);
    classifyChapter.value = String(chapter);
    classifyChapter.disabled = false;
    sessionFields.forEach((field) => { field.disabled = true; });
    resumeField.disabled = true;
    recoverButton.disabled = false;
    recoverButton.title = "";
    one('input[name="start_chapter"]', form).value = String(chapter);
    if (provenanceFailure) {
      classifyChapter.disabled = true;
      form.dataset.recoveryMode = "pre-provider-provenance";
      form.action = `/api/jobs/${encodeURIComponent(pausedParent?.job_id || pausedParent?.id || exactParentId)}/resume`;
      if (provenanceFailure.verified && pausedParent?.resume_ready === true) {
        resumeField.disabled = false;
        form.dataset.confirm = `系统已修复本机 Git 环境。现在重新执行第 ${chapter} 章将调用真实模型，确认吗？`;
        form.dataset.successMessage = `第 ${chapter} 章已重新进入执行队列。`;
        setText("[data-recover-button]", `确认重新执行第 ${chapter} 章`);
      } else {
        form.dataset.confirm = "";
        form.dataset.successMessage = "";
        setText("[data-recover-button]", provenanceFailure.verified ? "等待系统修复" : "等待安全核对");
        recoverButton.disabled = true;
        recoverButton.title = provenanceFailure.verified ? "Git 来源记录环境异常，暂时不能重新执行" : "安全状态尚未完全确认，暂时不能重新执行";
      }
    } else if (contextBudget) {
      classifyChapter.disabled = true;
      form.dataset.recoveryMode = "context-budget";
      form.action = `/api/jobs/${encodeURIComponent(pausedParent?.job_id || pausedParent?.id || exactParentId)}/resume`;
      if (contextBudget.verified && pausedParent?.resume_ready === true) {
        resumeField.disabled = false;
        form.dataset.confirm = `系统已完成上下文修复。现在重新执行第 ${chapter} 章将调用真实模型，确认吗？`;
        form.dataset.successMessage = `第 ${chapter} 章已重新进入执行队列。`;
        setText("[data-recover-button]", `确认重新执行第 ${chapter} 章`);
      } else {
        form.dataset.confirm = "";
        form.dataset.successMessage = "";
        setText("[data-recover-button]", contextBudget.verified ? "等待系统修复" : "等待安全核对");
        recoverButton.disabled = true;
        recoverButton.title = contextBudget.verified ? "上下文预算尚未修复，暂时不能重新执行" : "安全状态尚未完全确认，暂时不能重新执行";
      }
    } else if (repairConvergence) {
      form.action = `/api/jobs/${encodeURIComponent(pausedParent?.job_id || pausedParent?.id || exactParentId)}/recover`;
      if (!repairConvergence.verified) {
        classifyChapter.disabled = true;
        form.dataset.recoveryMode = "repair-convergence-audit";
        form.dataset.confirm = "";
        form.dataset.successMessage = "";
        setText("[data-recover-button]", "等待安全核对");
        recoverButton.disabled = true;
        recoverButton.title = "模型调用、候选正文与提交状态尚未形成完整结构化证据";
      } else if (!checkpointReady) {
        form.dataset.recoveryMode = "repair-convergence-classify";
        form.dataset.confirm = "先执行无模型恢复分类，核对全部已结算回执、原候选与可信场景边界。这一步不会调用模型、提交正文或写回状态，确认吗？";
        form.dataset.successMessage = "无模型恢复分类任务已启动。";
        setText("[data-recover-button]", "先做无模型恢复分类");
      } else if (pausedParent?.resume_ready === true) {
        classifyChapter.disabled = true;
        resumeField.disabled = false;
        form.action = `/api/jobs/${encodeURIComponent(pausedParent.job_id || pausedParent.id)}/resume`;
        form.dataset.recoveryMode = "repair-convergence-resume";
        form.dataset.confirm = `已完成无模型恢复分类并绑定原任务。将复用 ${reusableCount} 个可信场景，调用真实模型重新校验第 ${chapter} 章；不会采用失败修订稿，只有通过质量门后才会提交。确认吗？`;
        form.dataset.successMessage = `第 ${chapter} 章已从可信恢复点继续。`;
        setText("[data-recover-button]", `确认复用 ${reusableCount} 个场景并重新校验`);
      } else if (pausedParent) {
        classifyChapter.disabled = true;
        form.dataset.recoveryMode = "repair-convergence-bind";
        form.dataset.confirm = "先把无模型恢复检查点绑定到原暂停任务。这一步不会调用模型或提交正文，确认吗？";
        form.dataset.successMessage = "恢复检查点正在绑定原任务。";
        setText("[data-recover-button]", "绑定恢复证据到原任务");
      } else {
        classifyChapter.disabled = true;
        form.dataset.recoveryMode = "repair-convergence-missing-parent";
        form.dataset.confirm = "";
        form.dataset.successMessage = "";
        setText("[data-recover-button]", "等待原任务恢复");
        recoverButton.disabled = true;
        recoverButton.title = "找不到原暂停任务，禁止新建任务或直接重试";
      }
    } else if (qualityRejection) {
      if (!checkpointReady) {
        form.action = `/api/jobs/${encodeURIComponent(pausedParent?.job_id || pausedParent?.id || exactParentId)}/recover`;
        form.dataset.recoveryMode = "quality-classify";
        form.dataset.confirm = "先把完整候选安全转换为可编辑恢复草稿。这一步不会调用模型或提交正文，确认吗？";
        form.dataset.successMessage = "可编辑恢复草稿正在建立。";
        setText("[data-recover-button]", "生成可编辑恢复草稿");
      } else {
        classifyChapter.disabled = true;
        form.dataset.recoveryMode = "quality-draft";
        form.dataset.confirm = "";
        form.dataset.successMessage = "";
        setText("[data-recover-button]", qualityDraft?.state === "validated" ? "请使用下方确认提交" : qualityDraft ? "请先编辑并重新验证下方草稿" : "正在准备可编辑草稿");
        recoverButton.disabled = true;
        recoverButton.title = "质量问题必须通过下方 revision/SHA 草稿流程处理";
      }
    } else if (qualityRevision) {
      classifyChapter.disabled = true;
      form.dataset.recoveryMode = "quality-repair";
      if (pausedParent?.resume_ready === true) {
        resumeField.disabled = false;
        form.action = `/api/jobs/${encodeURIComponent(pausedParent.job_id || pausedParent.id)}/resume`;
        form.dataset.confirm = `将恢复原任务并调用真实模型，自动修复第 ${chapter} 章的质量问题，然后重新执行确定性校验、语义审核与提交门。确认吗？`;
        form.dataset.successMessage = `第 ${chapter} 章已开始自动修复并重新校验。`;
        setText("[data-recover-button]", "自动修复并重新校验");
      } else {
        form.action = `/api/jobs/${encodeURIComponent(pausedParent?.job_id || pausedParent?.id || exactParentId)}/resume`;
        form.dataset.confirm = "当前任务尚未达到安全恢复条件。";
        form.dataset.successMessage = "";
        setText("[data-recover-button]", "等待自动修复就绪");
        recoverButton.disabled = true;
      }
    } else if (checkpointReady && pausedParent?.resume_ready === true) {
      classifyChapter.disabled = true;
      resumeField.disabled = false;
      form.action = `/api/jobs/${encodeURIComponent(pausedParent.job_id || pausedParent.id)}/resume`;
      form.dataset.recoveryMode = "resume";
      form.dataset.confirm = allScenesReusable
        ? completeSequenceNeedsRevalidation
          ? `已确认 ${reusableCount} 份有回执的场景候选。将恢复原任务并逐场重跑边界校验；只有未通过的场景会调用真实模型重生，全部通过后才会审核并提交第 ${chapter} 章。确认吗？`
          : `已确认全部 ${reusableCount} 个场景。不会重新生成场景；将恢复原任务，调用真实模型重新运行导演决策与语义校验，并仅在通过后提交第 ${chapter} 章。确认吗？`
        : `已确认 ${reusableCount} 个可信场景。将恢复原任务，显式复用它们，从场景 ${nextScene} 继续生成、重新校验并提交第 ${chapter} 章。确认吗？`;
      form.dataset.successMessage = `已恢复原任务，正在重新校验第 ${chapter} 章。`;
      setText("[data-recover-button]", allScenesReusable ? (completeSequenceNeedsRevalidation ? `确认复核 ${reusableCount} 份候选并继续` : `确认复用全部 ${reusableCount} 个场景并重新校验`) : `确认复用 ${reusableCount} 个场景并继续提交`);
    } else if (checkpointReady && pausedParent) {
      classifyChapter.disabled = true;
      form.action = `/api/jobs/${encodeURIComponent(pausedParent.job_id || pausedParent.id)}/recover`;
      form.dataset.recoveryMode = "bind";
      form.dataset.confirm = "先把已识别的恢复边界绑定到原暂停任务。这一步不会调用模型或提交正文，确认吗？";
      form.dataset.successMessage = "原任务的恢复证据正在建立。";
      setText("[data-recover-button]", "将恢复证据绑定到原任务");
    } else if (checkpointReady) {
      classifyChapter.disabled = true;
      sessionFields.forEach((field) => { field.disabled = false; });
      form.action = `/api/projects/${encodeURIComponent(state.projectId)}/sessions`;
      form.dataset.recoveryMode = "new-session";
      form.dataset.confirm = allScenesReusable
        ? `已确认全部 ${reusableCount} 个场景。不会重新生成场景；将调用真实模型重新运行导演决策与语义校验，并仅在通过后提交第 ${chapter} 章。确认吗？`
        : `已确认 ${reusableCount} 个可信场景。将显式复用它们，从场景 ${nextScene} 继续生成、重新校验并提交第 ${chapter} 章。确认吗？`;
      form.dataset.successMessage = `第 ${chapter} 章恢复提交任务已启动。`;
      setText("[data-recover-button]", allScenesReusable ? `确认复用全部 ${reusableCount} 个场景并重新校验` : `确认复用 ${reusableCount} 个场景并继续提交`);
    } else {
      form.action = issue.recover_url || (issue.project_recovery ? `/api/projects/${encodeURIComponent(state.projectId)}/recovery` : `/api/jobs/${encodeURIComponent(issue.job_id || issue.id)}/recover`);
      form.dataset.recoveryMode = "classify";
      form.dataset.confirm = "先执行无模型恢复分类，确认可信场景边界。这一步不会调用模型，确认吗？";
      form.dataset.successMessage = "恢复分类任务已启动。";
      setText("[data-recover-button]", "先做无模型恢复分类");
    }
    renderRecoveryDraft(issue);
    all(".pending-item", one("[data-pending-list]")).forEach((node) => node.classList.toggle("is-active", node.dataset.pendingId === String(issue.id)));
  };

  const renderPending = (payload) => {
    const pausedJobs = arrayFrom(payload, "items", "pending_items", "jobs").map((job) => {
      const normalized = {
        ...job,
        job_id: job.job_id || job.id,
        chapter: job.chapter || job.next_chapter,
        title: job.title || `第 ${job.next_chapter || "—"} 章需要作者决定`,
        reason: job.reason || job.error?.message || (job.state === "needs_recovery" ? "Provider 状态不确定，不会自动重试" : "生产已暂停")
      };
      const repairNotConverging = repairNotConvergingDetailsOf(normalized);
      return {
        ...normalized,
        title: repairNotConverging
          ? (repairNotConverging.verified ? `第 ${normalized.chapter || "—"} 章自动修订未收敛` : `第 ${normalized.chapter || "—"} 章修订故障正在安全核对`)
          : normalized.title,
        reason: repairNotConverging ? repairNotConvergingSummary(repairNotConverging) : normalized.reason,
        summary: repairNotConverging ? repairNotConvergingSummary(repairNotConverging) : normalized.summary,
        pre_provider_provenance: preProviderProvenanceDetailsOf(normalized),
        context_budget: contextBudgetDetailsOf(normalized),
        repair_not_converging: repairNotConverging
      };
    });
    state.pendingJobs = pausedJobs;
    state.drafts = arrayFrom(payload.drafts, "items");
    const recovery = payload.chapter_recovery || payload.chapter_20_recovery;
    const recoveryChapter = Number(recovery?.chapter || 20);
    const provenanceJobs = pausedJobs.filter((job) => Boolean(job.pre_provider_provenance));
    const provenanceIssues = provenanceJobs.map((job) => {
      const failure = job.pre_provider_provenance;
      return {
        ...job,
        id: job.id,
        job_id: job.job_id || job.id,
        chapter: job.chapter || job.next_chapter,
        checkpoint: null,
        project_recovery: false,
        title: failure.verified ? `第 ${job.chapter || job.next_chapter || "—"} 章在模型请求前停止` : `第 ${job.chapter || job.next_chapter || "—"} 章正在安全核对`,
        reason: preProviderProvenanceSummary(failure),
        summary: `${preProviderProvenanceSummary(failure)} ${preProviderProvenanceStopDetail(failure)}`
      };
    });
    const provenanceReady = provenanceIssues.some((job) => job.pre_provider_provenance?.verified && job.resume_ready === true);
    const provenanceUnverified = provenanceIssues.some((job) => !job.pre_provider_provenance?.verified);
    const contextBudgetJobs = pausedJobs.filter((job) => !job.pre_provider_provenance && Boolean(job.context_budget));
    const contextBudgetIssues = contextBudgetJobs.map((job) => ({
      ...job,
      id: job.id,
      job_id: job.job_id || job.id,
      chapter: job.chapter || job.next_chapter,
      checkpoint: null,
      project_recovery: false,
      title: `第 ${job.chapter || job.next_chapter || "—"} 章输入上下文超出安全上限`,
      reason: contextBudgetSummary(job.context_budget),
      summary: `${contextBudgetSummary(job.context_budget)} ${contextBudgetStopDetail(job.context_budget)}`
    }));
    const contextBudgetReady = contextBudgetIssues.some((job) => job.context_budget?.verified && job.resume_ready === true);
    const contextBudgetUnverified = contextBudgetIssues.some((job) => !job.context_budget?.verified);
    const qualityRevisionJobs = pausedJobs.filter((job) => !job.pre_provider_provenance && !job.context_budget && isQualityRevisionPause(job));
    const qualityRevisionAtRecoveryChapter = qualityRevisionJobs.some((job) => Number(job.chapter || job.next_chapter) === recoveryChapter);
    const contextBudgetAtRecoveryChapter = contextBudgetJobs.some((job) => Number(job.chapter || job.next_chapter) === recoveryChapter);
    const provenanceAtRecoveryChapter = provenanceJobs.some((job) => Number(job.chapter || job.next_chapter) === recoveryChapter);
    const qualityRevisionIssues = qualityRevisionJobs.map((job) => ({
      ...job,
      id: job.id,
      job_id: job.job_id || job.id,
      chapter: job.chapter || job.next_chapter,
      quality_revision: true,
      checkpoint: null,
      project_recovery: false,
      title: `第 ${job.chapter || job.next_chapter || "—"} 章质量审核需修订`,
      reason: "Provider 已成功结束，正式正文未提交",
      summary: "Provider 已成功结束，完整候选草稿已安全保留；质量审核要求修订，正式正文未提交。"
    }));
    const pausedRecoveryJob = pausedJobs.find((job) => (
      job.kind === "generation_session"
      && ["awaiting_input", "needs_recovery"].includes(job.state)
      && Number(job.chapter || job.next_chapter) === recoveryChapter
    ));
    const repairConvergenceFailure = pausedRecoveryJob?.repair_not_converging || null;
    const latestRecoveryRun = recovery?.latest_run || {};
    const sceneSchemaFailure = latestRecoveryRun.error_type === "ValueError"
      && String(latestRecoveryRun.error_message || "").includes("Scene response was not valid structured JSON")
      && Number(latestRecoveryRun.unsettled_attempt_count) === 0;
    const contextHeadroom = contextHeadroomDetailsOfRun(latestRecoveryRun);
    const contextHeadroomFailure = Boolean(contextHeadroom);
    const repairAuthority = repairAuthorityDetailsOfRun(latestRecoveryRun);
    const repairAuthorityFailure = Boolean(repairAuthority);
    const qualityRejection = qualityRejectionDetailsOfRun(latestRecoveryRun);
    const qualityRejectionFailure = Boolean(qualityRejection && recovery?.formal_prose_exists === false);
    let recoveryIssue = null;
    if (!provenanceAtRecoveryChapter && !contextBudgetAtRecoveryChapter && !qualityRevisionAtRecoveryChapter && recovery?.recoverable && recovery.needs_reclassification && pausedRecoveryJob) {
      recoveryIssue = {
        ...pausedRecoveryJob,
        id: pausedRecoveryJob.id,
        job_id: pausedRecoveryJob.job_id || pausedRecoveryJob.id,
        chapter: recoveryChapter,
        checkpoint: null,
        project_recovery: false,
        scene_schema_failure: sceneSchemaFailure,
        context_headroom_failure: contextHeadroomFailure,
        context_headroom: contextHeadroom,
        repair_authority_failure: repairAuthorityFailure,
        repair_authority: repairAuthority,
        repair_not_converging: repairConvergenceFailure,
        quality_revision: qualityRejectionFailure,
        quality_rejection: qualityRejection,
        latest_run: latestRecoveryRun,
        title: repairConvergenceFailure ? (repairConvergenceFailure.verified ? `第 ${recoveryChapter} 章自动修订未收敛，等待无模型恢复分类` : `第 ${recoveryChapter} 章修订故障正在安全核对`) : sceneSchemaFailure ? `第 ${recoveryChapter} 章场景响应格式错误，等待安全分类` : contextHeadroomFailure ? `第 ${recoveryChapter} 章边界重生输入超过安全余量，等待安全分类` : repairAuthorityFailure ? `第 ${recoveryChapter} 章修订上下文装配失败，等待安全分类` : qualityRejectionFailure ? `第 ${recoveryChapter} 章质量门发现阻断问题，等待生成编辑草稿` : `第 ${recoveryChapter} 章已安全暂停`,
        reason: repairConvergenceFailure
          ? repairNotConvergingSummary(repairConvergenceFailure)
          : sceneSchemaFailure
          ? `本次 ${Number(latestRecoveryRun.provider_calls) || 0} 次模型调用均已结算；第 2 个场景的 JSON 格式错误。`
          : contextHeadroomFailure
          ? `本次 ${Number(latestRecoveryRun.provider_calls) || 0} 次模型调用均已结算；边界重生输入为 ${contextHeadroom.requiredTokens} tokens，超过 ${contextHeadroom.safeTarget} 安全目标 ${contextHeadroom.overageTokens} tokens。`
          : repairAuthorityFailure
          ? `本次 ${repairAuthority.providerCalls} 次模型调用均已结算；${repairAuthority.chapterChars} 字候选已保留，修订所需权威记录 ${repairAuthority.requiredChars} 字符超过旧预算 ${repairAuthority.budgetChars} 字符。`
          : qualityRejectionFailure
          ? `本次 ${qualityRejection.providerCalls} 次模型调用均已结算；${qualityRejection.chapterChars} 字候选已保留。阻断原因：${qualityProblemSummary(qualityRejection)}`
          : "最新完整输出尚未重新识别恢复边界",
        summary: repairConvergenceFailure
          ? (repairConvergenceFailure.verified ? `${repairConvergenceFailure.providerCalls} 次模型调用全部结算，${repairConvergenceFailure.candidateChars} 字原候选已保留，正式正文未提交。先做无模型恢复分类，不直接重试失败修订稿。` : "调用、候选正文与提交状态正在安全核对；完成前不会恢复或重试。")
          : sceneSchemaFailure || contextHeadroomFailure || repairAuthorityFailure || qualityRejectionFailure
          ? "正式正文没有提交；先执行无模型恢复分类，核对候选正文、回执与最新运行后再继续。"
          : "场景和合并草稿已保存在隔离运行区。先执行无模型分类，系统不会重写正文、调用模型或提交章节。",
        reusable_scene_count: 0
      };
    } else if (!provenanceAtRecoveryChapter && !contextBudgetAtRecoveryChapter && !qualityRevisionAtRecoveryChapter && recovery?.recoverable) {
      recoveryIssue = {
        id: `chapter-${recoveryChapter}-recovery`,
        ...recovery,
        job_id: pausedRecoveryJob?.job_id || pausedRecoveryJob?.id || null,
        project_recovery: !pausedRecoveryJob,
        resume_ready: pausedRecoveryJob?.resume_ready === true,
        scene_schema_failure: sceneSchemaFailure,
        context_headroom_failure: contextHeadroomFailure,
        context_headroom: contextHeadroom,
        repair_authority_failure: repairAuthorityFailure,
        repair_authority: repairAuthority,
        repair_not_converging: repairConvergenceFailure,
        quality_revision: qualityRejectionFailure,
        quality_rejection: qualityRejection,
        latest_run: latestRecoveryRun,
        title: repairConvergenceFailure ? (repairConvergenceFailure.verified ? `第 ${recoveryChapter} 章自动修订未收敛，已安全分类` : `第 ${recoveryChapter} 章修订故障正在安全核对`) : sceneSchemaFailure ? `第 ${recoveryChapter} 章场景响应格式错误，已安全分类` : contextHeadroomFailure ? `第 ${recoveryChapter} 章边界重生输入超过安全余量，已安全分类` : repairAuthorityFailure ? `第 ${recoveryChapter} 章修订上下文装配失败，已安全分类` : qualityRejectionFailure ? `第 ${recoveryChapter} 章质量门发现阻断问题，候选草稿可编辑` : `第 ${recoveryChapter} 章可安全恢复`,
        reason: repairConvergenceFailure
          ? repairNotConvergingSummary(repairConvergenceFailure)
          : sceneSchemaFailure
          ? `本次 ${Number(latestRecoveryRun.provider_calls) || 0} 次模型调用均已结算；第 2 个场景的 JSON 格式错误。`
          : contextHeadroomFailure
          ? `本次 ${Number(latestRecoveryRun.provider_calls) || 0} 次模型调用均已结算；边界重生输入超过 ${contextHeadroom.safeTarget} 安全目标 ${contextHeadroom.overageTokens} tokens。`
          : repairAuthorityFailure
          ? `本次 ${repairAuthority.providerCalls} 次模型调用均已结算；${repairAuthority.chapterChars} 字候选已保留，修订所需权威记录超过旧预算 ${repairAuthority.overageChars} 字符。`
          : qualityRejectionFailure
          ? `本次 ${qualityRejection.providerCalls} 次模型调用均已结算；${qualityRejection.chapterChars} 字候选已保留。阻断原因：${qualityProblemSummary(qualityRejection)}`
          : recovery.checkpoint ? `已分类 ${recovery.checkpoint.reusable_scene_count || 0} 个可信场景` : "生成未提交，先执行无模型恢复分类",
        summary: repairConvergenceFailure
          ? (repairConvergenceFailure.verified ? `${repairConvergenceFailure.providerCalls} 次模型调用全部结算，${repairConvergenceFailure.candidateChars} 字原候选已保留，正式正文未提交；不会采用失败修订稿。` : "调用、候选正文与提交状态正在安全核对；完成前不会恢复或重试。")
          : qualityRejectionFailure
          ? "请在下方恢复草稿中修正阻断问题，保存并重新验证；正式正文在通过前不会提交。"
          : sceneSchemaFailure || contextHeadroomFailure || repairAuthorityFailure
          ? `正式正文没有提交；${Number(recovery.checkpoint?.reusable_scene_count || 0)} 个有回执的场景候选已完成恢复核对，继续时会重新验证后再进入修订与提交。`
          : recovery.checkpoint ? "恢复检查点已建立，可信场景会被复用，只从中断处继续。" : "工作台将先在不调用模型的情况下分类现有回执，不会改写正文。",
        reusable_scene_count: recovery.checkpoint?.reusable_scene_count || 0,
        next_scene: (recovery.checkpoint?.reusable_scene_count || 0) + 1
      };
    }
    const qualityRevisionIds = new Set(qualityRevisionJobs.map((job) => String(job.job_id || job.id)));
    const contextBudgetIds = new Set(contextBudgetJobs.map((job) => String(job.job_id || job.id)));
    const provenanceIds = new Set(provenanceJobs.map((job) => String(job.job_id || job.id)));
    const remainingPaused = pausedJobs.filter((job) => {
      if (provenanceIds.has(String(job.job_id || job.id))) return false;
      if (contextBudgetIds.has(String(job.job_id || job.id))) return false;
      if (qualityRevisionIds.has(String(job.job_id || job.id))) return false;
      if (!recoveryIssue) return true;
      return String(job.id) !== String(pausedRecoveryJob?.id || "") && Number(job.next_chapter) !== recoveryChapter;
    });
    state.pending = [...provenanceIssues, ...contextBudgetIssues, ...qualityRevisionIssues, ...(recoveryIssue ? [recoveryIssue] : []), ...remainingPaused];
    updatePendingCount(state.pending.length);
    setText("[data-pending-heading-count]", state.pending.length);
    setText("[data-pending-lede]", provenanceIssues.length ? (provenanceUnverified ? "检测到疑似 Git 来源记录环境异常；调用、费用与产物证据正在安全核对，完成前不能重新执行。" : provenanceReady ? "Git 环境已修复；重新执行需要你的明确确认，点击前不会新增模型调用或费用。" : "Git 来源记录环境异常；本次失败证据已核对，系统修复前不能重新执行。") : contextBudgetIssues.length ? (contextBudgetUnverified ? "本地输入超过安全上限；调用、费用与产物证据正在安全核对，完成前不能重新执行。" : contextBudgetReady ? "上下文修复已完成；重新执行需要你的明确确认，点击前不会新增模型调用或费用。" : "本地输入在调用模型前超过安全上限；系统修复前不能重新执行。") : recoveryIssue?.repair_not_converging ? (recoveryIssue.repair_not_converging.verified ? "自动修订引入了更严重问题，系统已保留原候选并停止；下一步先做无模型恢复分类，不会盲目重试。" : "修订故障的调用、候选正文与提交证据正在安全核对；完成前不能恢复或重试。") : qualityRevisionIssues.length || recoveryIssue?.quality_revision ? "质量门要求修订候选草稿；正式正文在重新验证通过前不会提交。" : "自动生产在不确定处停下，这里只展示必须由作者决定的内容。");
    const pill = one("[data-active-job-pill]");
    if (pill) {
      pill.hidden = pausedJobs.length === 0;
      pill.href = projectHash("pending");
      pill.classList.toggle("is-attention", pausedJobs.length > 0);
    }
    setText("[data-active-job-label]", pausedJobs.length ? "有写作任务需要处理" : "写作任务运行中");
    const list = one("[data-pending-list]"); clear(list);
    const layout = one("[data-spa-view=" + CSS.escape("pending") + "] .pending-layout");
    const empty = one("[data-pending-empty]");
    layout.hidden = state.pending.length === 0; empty.hidden = state.pending.length > 0;
    state.pending.forEach((issue) => {
      const button = el("button", "pending-item"); button.type = "button"; button.dataset.pendingId = String(issue.id);
      button.append(el("span", `pending-symbol pending-${issue.kind || "review"}`, "!"));
      const copy = el("span"); copy.append(el("strong", "", issue.title || `第 ${issue.chapter || "—"} 章需处理`), el("small", "", issue.reason || issue.summary || ""), el("time", "", issue.created_at_label || ""));
      button.append(copy, el("span", "", "›"));
      button.addEventListener("click", () => renderPendingDetail(issue)); list.append(button);
    });
    if (state.pending.length) renderPendingDetail(state.pending[0]);
    else one("[data-recovery-draft-panel]").hidden = true;
  };

  const loadPending = async () => {
    const { payload } = await api(`/api/projects/${encodeURIComponent(state.projectId)}/pending`);
    renderPending(payload);
  };

  const renderHistory = (payload) => {
    const runRecords = arrayFrom(payload, "items", "runs");
    const jobRecords = arrayFrom(payload.jobs, "items").map((job) => ({
      ...job,
      id: job.id,
      history_source: "job",
      chapter: job.next_chapter,
      title: job.kind === "locked_chapter_recovery" ? `第 ${job.next_chapter} 章恢复` : `第 ${job.start_chapter}–${job.end_chapter} 章生产批次`,
      status: job.state,
      kind_label: job.kind === "locked_chapter_recovery" ? "安全恢复" : "连续生产",
      created_at_label: job.created_at ? new Date(job.created_at).toLocaleString("zh-CN") : "—"
    }));
    state.runs = [...jobRecords, ...runRecords.map((run) => ({ ...run, history_source: "run" }))];
    const stats = payload.stats || {};
    setText("[data-history-total]", stats.total_runs ?? state.runs.length);
    const completed = jobRecords.filter((job) => ["completed", "committed"].includes(job.status)).length;
    setText("[data-history-pass-rate]", stats.first_pass_rate || (jobRecords.length ? `${Math.round(completed / jobRecords.length * 100)}%` : "—"));
    setText("[data-history-manual]", stats.manual_interventions ?? jobRecords.filter((job) => job.manual_confirmation_required).length);
    setText("[data-history-cost]", stats.cost_label || "—");
    const tbody = one("[data-history-rows]"); clear(tbody);
    state.runs.forEach((run) => {
      const row = el("tr"); row.dataset.searchValue = `${run.chapter || ""} ${run.id || ""} ${run.title || ""}`;
      row.append(el("td", "", run.created_at_label || run.time_label || "—"));
      const subject = el("td"); subject.append(el("strong", "", run.title || (run.chapter ? `第 ${run.chapter} 章` : "运行记录")), el("small", "", `${run.kind_label || "正文生成"} · ${(run.id || "—").slice(0, 12)}`)); row.append(subject);
      const result = el("td"); result.append(statusBadge(run.status || "idle", run.status_label));
      const detail = el("td");
      const detailButton = el("button", "text-link", "查看详情");
      detailButton.type = "button";
      detailButton.dataset.historyDetailId = String(run.id || "");
      detailButton.dataset.historyDetailSource = run.history_source || "run";
      detailButton.disabled = !run.id;
      detail.append(detailButton);
      row.append(result, el("td", "", run.duration_label || "—"), el("td", "", run.token_label || "—"), el("td", "", run.cost_label || "—"), detail); tbody.append(row);
    });
    if (!tbody.children.length) { const row = el("tr"); const cell = el("td", "empty-inline", "暂无运行记录。"); cell.colSpan = 7; row.append(cell); tbody.append(row); }
  };

  const loadHistory = async () => {
    const { payload } = await api(`/api/projects/${encodeURIComponent(state.projectId)}/history`);
    renderHistory(payload);
  };

  const renderHistoryDetail = (payload) => {
    const panel = one("[data-history-detail]");
    if (!panel) return;
    clear(panel);
    panel.hidden = false;
    const run = payload.run;
    const heading = el("div", "section-heading compact");
    const headingCopy = el("div");
    headingCopy.append(el("p", "eyebrow", "运行凭据与正文回读"));
    headingCopy.append(el("h2", "", run?.chapter_index ? `第 ${run.chapter_index} 章运行详情` : "生产任务详情"));
    heading.append(headingCopy);
    panel.append(heading);

    if (!run) {
      panel.append(el("p", "muted-copy", "该生产任务尚未写入可读取的运行记录。"));
      return;
    }

    const facts = el("dl", "history-detail-grid");
    const addFact = (label, value) => {
      const item = el("div", "history-detail-fact");
      item.append(el("dt", "", label), el("dd", "", value ?? "—"));
      facts.append(item);
    };
    addFact("运行 ID", run.id || "—");
    addFact("结果", run.committed ? "已提交" : run.status || "—");
    addFact("持久化", run.persistence_state || "—");
    addFact("审校", run.review_status || "—");
    addFact("自动修订", `${Number(run.repair_attempts || 0)} 次`);
    addFact("模型调用", run.provider_calls == null ? "—" : `${run.provider_calls} 次`);
    panel.append(facts);

    const verification = payload.verification;
    const evidence = el("div", "recovery-callout");
    if (verification) {
      const receipt = verification.receipt_verified ? `已验证（${verification.receipt_kind || "receipt"}）` : "未验证";
      evidence.append(el("strong", "", `提交凭据：${receipt}`));
      evidence.append(el("p", "", verification.prose_path ? `正式正文回读：${verification.prose_path} · SHA-256 ${String(verification.prose_sha256 || "—").slice(0, 16)}` : "正式正文回读：未找到"));
      const errors = Array.isArray(verification.errors) ? verification.errors : [];
      if (errors.length) evidence.append(el("p", "", `校验说明：${errors.join("；")}`));
    } else {
      evidence.append(el("strong", "", "提交凭据：该运行尚未提交正式正文"));
      if (run.error_message) evidence.append(el("p", "", `失败信息：${run.error_message}`));
    }
    panel.append(evidence);
  };

  const loadHistoryDetail = async (entryId, source) => {
    const path = `/api/projects/${encodeURIComponent(state.projectId)}/history/${encodeURIComponent(entryId)}?source=${encodeURIComponent(source || "run")}`;
    const { payload } = await api(path);
    renderHistoryDetail(payload);
  };

  const renderHealth = () => {
    const health = state.detail?.health || state.detail?.overview?.health || {};
    const validationOk = Boolean(health.validation?.ok ?? health.validation?.valid ?? (health.validation && !health.validation.errors?.length));
    const score = health.score ?? (validationOk ? 55 : 20) + (health.shadow_report ? 15 : 0) + (health.production_eligible ? 30 : 0);
    setText("[data-health-page-score]", score);
    setText("[data-health-page-mode]", health.mode_label || health.label || (health.production_eligible ? "Strict 生产" : "实验运行"));
    setText("[data-health-page-summary]", health.summary || (health.production_eligible ? "项目已显式激活 Strict，可进入受监督的连续生产。" : "Shadow 结果可用于观察，但不作为稳定生产证据。"));
    const activation = one("[data-health-activation]");
    if (activation) activation.hidden = Boolean(health.production_eligible);
    const activationForm = one("[data-health-activate-form]");
    if (activationForm) activationForm.action = `/api/projects/${encodeURIComponent(state.projectId)}/health/activate`;
    const requirements = one("[data-calibration-requirements]");
    if (requirements) {
      clear(requirements);
      arrayFrom(health.calibration_requirements, "items").forEach((requirement) => requirements.append(el("li", "", requirement)));
    }
    const checks = one("[data-health-checks]"); clear(checks);
    const healthChecks = arrayFrom(health.checks, "items");
    if (!healthChecks.length && Object.keys(health).length) {
      healthChecks.push(
        { label: "StoryProject 结构与权威状态", detail: validationOk ? "基础预检已通过。" : "预检发现需要处理的问题。", state: validationOk ? "pass" : "failed" },
        { label: "Shadow 报告", detail: health.shadow_report ? "已生成可观察报告。" : health.shadow_error?.message || "尚无可用 Shadow 报告。", state: health.shadow_report ? "pass" : "pending" },
        { label: "Strict 显式激活", detail: health.production_eligible ? "已激活生产模式。" : "尚未激活，当前运行会标记为实验。", state: health.production_eligible ? "pass" : "pending" }
      );
    }
    healthChecks.forEach((check, index) => {
      const checkState = check.state || "pending";
      const stateLabel = check.state_label || ({ pass: "通过", failed: "阻断", pending: "待完成" }[checkState] || "待检查");
      const item = el("li", `health-step-${checkState}`); item.append(el("span", "health-step-mark", checkState === "pass" ? "✓" : checkState === "failed" ? "!" : index + 1));
      const copy = el("div"); copy.append(el("strong", "", check.label), el("p", "", check.detail || "")); item.append(copy, statusBadge(checkState, stateLabel)); checks.append(item);
    });
    if (!checks.children.length) { const item = el("li", "health-step-pending"); item.append(el("span", "health-step-mark", "—")); const copy = el("div"); copy.append(el("strong", "", "尚未执行项目预检"), el("p", "", "生产预检后会显示项目健康证据。")); item.append(copy); checks.append(item); }

    const shadow = health.shadow_report || null;
    const shadowState = shadow?.state || {};
    const conflictCount = Number(shadow?.blocking_conflict_count || 0);
    setText("[data-shadow-conflicts]", shadow ? conflictCount : "—");
    setText("[data-shadow-warnings]", shadow ? Number(shadow?.warning_count || 0) : "—");
    setText("[data-shadow-unsupported]", shadow ? arrayFrom(shadowState.unsupported_excerpts, "items").length : "—");
    setText("[data-shadow-differences]", shadow ? arrayFrom(shadow.differences, "items").length : "—");
    const conflictCard = one("[data-shadow-conflict-card]");
    if (conflictCard) {
      conflictCard.classList.toggle("report-pass", Boolean(shadow) && conflictCount === 0);
      conflictCard.classList.toggle("report-warning", !shadow || conflictCount > 0);
    }
    const blockerLabels = {
      shadow_mode_not_activated: "尚未显式激活 Strict",
      target_book_calibration_not_proven: "目标书校准尚未证明",
      blocking_semantic_conflict: "存在阻断语义冲突"
    };
    const blockerText = shadow
      ? arrayFrom(shadow.strict_blockers, "items").map((item) => blockerLabels[item] || item).join("；")
      : health.shadow_error?.message || "当前无法生成 Shadow 报告。";
    setText(
      "[data-shadow-blockers]",
      health.production_eligible
        ? "当前 Strict 已有效；本摘要仅显示激活前的非权威 Shadow 观察，不影响生产资格。"
        : (shadow ? `当前仍需处理：${blockerText || "无"}` : blockerText)
    );
  };

  const loadHealth = async () => {
    const { payload } = await api(`/api/projects/${encodeURIComponent(state.projectId)}/health`);
    state.detail = { ...(state.detail || {}), health: payload.health || payload };
    projectCache.set(state.projectId, state.detail);
    renderHealth();
    return state.detail.health;
  };

  const route = async () => {
    clearError();
    const next = parseRoute();
    state.route = next;
    showLoading(true);
    try {
      if (next.area === "bookshelf") {
        setView("bookshelf");
        await loadProjects();
        return;
      }
      if (!state.projects.length) await loadProjects();
      await loadProject(next.projectId, next.projectId !== state.projectId);
      setView(next.area);
      if (next.area === "overview") {
        const detail = await loadProject(next.projectId, true);
        try { detail.health = await loadHealth(); } catch { /* Overview remains useful when health parsing fails. */ }
        try {
          const chapterPayload = (await api(`/api/projects/${encodeURIComponent(state.projectId)}/chapters`)).payload;
          detail.overview = detail.overview || {};
          detail.overview.recent_chapters = arrayFrom(chapterPayload, "items", "chapters").filter((chapter) => chapter.has_prose !== false).slice(-5).reverse();
        } catch { /* A missing chapter list must not hide the rest of the overview. */ }
        renderOverview(detail);
      }
      else if (next.area === "chapters") await loadChapters();
      else if (next.area === "chapter") await loadChapter(next.chapter);
      else if (next.area === "outlines") await loadOutlines();
      else if (next.area === "production") await loadProduction();
      else if (next.area === "pending") await loadPending();
      else if (next.area === "history") await loadHistory();
      else if (next.area === "health") await loadHealth();
    } catch (error) {
      showError(error);
    } finally {
      showLoading(false);
    }
  };

  one("[data-spa-retry]")?.addEventListener("click", route);
  window.addEventListener("hashchange", route);

  one("[data-spa-chapter-search]")?.addEventListener("input", (event) => {
    const query = event.target.value.trim().toLocaleLowerCase("zh-CN");
    all("[data-chapter-index] .chapter-index-row").forEach((row) => { row.hidden = Boolean(query) && !(row.dataset.searchValue || "").toLocaleLowerCase("zh-CN").includes(query); });
  });
  all("[data-spa-chapter-filters] button").forEach((button) => button.addEventListener("click", () => {
    all("[data-spa-chapter-filters] button").forEach((item) => item.classList.toggle("is-active", item === button));
    all("[data-chapter-index] .chapter-index-row").forEach((row) => { row.hidden = button.dataset.value !== "all" && row.dataset.status !== button.dataset.value; });
  }));
  one("[data-spa-history-search]")?.addEventListener("input", (event) => {
    const query = event.target.value.trim().toLocaleLowerCase("zh-CN");
    all("[data-history-rows] tr[data-search-value]").forEach((row) => { row.hidden = Boolean(query) && !(row.dataset.searchValue || "").toLocaleLowerCase("zh-CN").includes(query); });
  });
  one("[data-history-rows]")?.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-history-detail-id]");
    if (!button || button.disabled) return;
    try {
      clearError();
      await loadHistoryDetail(button.dataset.historyDetailId, button.dataset.historyDetailSource);
    } catch (error) {
      showError(error);
    }
  });

  one("[data-spa-approve-outline]")?.addEventListener("click", async () => {
    const batch = state.batch;
    if (!batch) return;
    if (!await UI.confirmAction("确认发布这 10 章章纲？发布后将冻结，用于连续生产。")) return;
    try {
      const expected = batch.proposal_sha256 || batch.sha256 || "";
      const { payload } = await api(`/api/outline-batches/${encodeURIComponent(batch.id)}/approve`, { method: "POST", headers: { "X-Expected-Revision": String(batch.revision || 1), "X-Expected-SHA256": expected, "If-Match": `"${expected}"` }, json: { expected_revision: batch.revision || 1, expected_sha256: expected, source_state_sha: batch.source_state_sha || "" } });
      UI.showToast(payload.message || "10 章章纲已确认并冻结。");
      await loadOutlines();
    } catch (error) { showError(error); UI.showToast(UI.errorMessage(error.payload, error.status), "error"); }
  });

  one("[data-close-cohort]")?.addEventListener("click", async () => {
    const cohort = state.cohort;
    if (!cohort || cohort.status !== "active") return;
    const confirmed = await UI.confirmAction("结束后本轮不能再修改。如果任一门槛未通过，本轮将记为失败，修复后需创建新 cohort。仍要继续吗？", "danger");
    if (!confirmed) return;
    try {
      const { payload } = await api(`/api/cohorts/${encodeURIComponent(cohort.id)}/close`, { method: "POST", json: { expected_revision: cohort.revision } });
      UI.showToast(payload.cohort?.status === "passed" ? "本轮 Cohort 已通过。" : "本轮 Cohort 已关闭，结果已固化。");
      await loadProduction();
    } catch (error) { showError(error); UI.showToast(UI.errorMessage(error.payload, error.status), "error"); }
  });

  doc.addEventListener("workbench:success", async (event) => {
    const form = event.target.closest("form");
    if (!form) return;
    form.closest("dialog")?.close();
    const payload = event.detail || {};
    if (form.closest("#create-project-dialog") || form.closest("#import-project-dialog")) {
      await loadProjects();
      const project = payload.project || payload.record;
      const id = projectIdOf(project);
      if (id) window.location.hash = `#/projects/${encodeURIComponent(id)}/overview`;
      return;
    }
    if (form.matches("[data-create-outline-form], [data-outline-editor]")) await loadOutlines();
    else if (form.matches("[data-session-form], [data-cohort-create-form]")) await loadProduction();
    else if (form.matches("[data-cohort-evaluation-form]")) await loadCohort(state.cohort.id);
    else if (form.matches("[data-recover-form]")) {
      await loadPending();
      if (payload.job || payload.job_id) {
        await loadProject(state.projectId, true);
        window.location.hash = projectHash("production");
      }
    }
    else if (form.matches("[data-draft-update-form]")) await loadPending();
    else if (form.matches("[data-draft-validate-form], [data-draft-confirm-form]")) { await loadPending(); if (payload.job) window.location.hash = projectHash("production"); }
    else if (form.matches("[data-amendment-form]")) await loadChapter(state.route?.chapter);
    else if (form.matches("[data-health-activate-form]")) await loadHealth();
  });

  api("/api/bootstrap").then(({ payload }) => updateProvider(payload.provider || {})).catch(() => updateProvider({ ready: false }));
  route();
})();
