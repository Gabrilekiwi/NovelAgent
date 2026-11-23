(() => {
  "use strict";
  const withFeedback = globalThis.NovelAgentFeedback?.action || (handler => handler);

  const doc = document;
  if (!doc.body?.hasAttribute("data-spa")) return;
  const UI = window.NovelAgentUI;
  if (!UI) throw new Error("NovelAgentUI must load before the SPA controller.");

  const one = (selector, root = doc) => root.querySelector(selector);
  const all = (selector, root = doc) => Array.from(root.querySelectorAll(selector));
  const incompatibleJobMessage = "旧任务无法在当前版本继续执行。原始记录已保留；请先核对运行与恢复证据，再决定后续处理。";
  const el = (tag, className, text) => {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = String(text);
    return node;
  };
  const state = {
    projects: [],
    providerReady: false,
    providerRevision: 0,
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
    chapterCard: null,
    chapterBudgetBlocked: false,
    chapterBudgetMessage: '',
    chapterConfirmation: null,
    chapterCandidate: null,
    chapterCandidateError: "",
    candidateActionNonce: 0,
    chapterFactExtraction: null,
    chapterFactReview: null,
    chapterFactEditorContext: null,
    factEditorLoadKey: null,
    chapterHasFormalProse: false,
    chapterRecoveryRequired: false,
    chapterFactError: "",
    factActionNonce: 0,
    route: null,
    routeRevision: 0
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
    if (Number(job.latest_run?.provider_calls) >= 1 && String(job.latest_run?.error_type || "") === "ContextBudgetError") return null;
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
    const unit = details.unit === "characters" ? "characters" : "tokens";
    const sourceCode = typeof details.code === "string" ? details.code : "";
    return {
      requiredTokens: unit === "tokens" ? requiredTokens : null,
      hardLimit,
      overageTokens: unit === "tokens" ? overageTokens : null,
      unit,
      sourceCode,
      suspected: true,
      verified: Boolean(structuredError && zeroCallEvidenceVerified(details)),
      legacy: !structuredError
    };
  };
  const contextBudgetSummary = (budget) => {
    if (budget?.unit === "characters" && budget?.hardLimit != null) {
      if (budget.sourceCode === "required_json_item_exceeds_budget") {
        return `装配本章必填 JSON 条目时超过了 ${budget.hardLimit} 字符的本地预算。`;
      }
      if (budget.sourceCode === "required_markdown_context_exceeds_budget") {
        return `装配本章必填 Markdown/JSON 上下文时超过了 ${budget.hardLimit} 字符的本地预算。`;
      }
      return `本章必填上下文超过了 ${budget.hardLimit} 字符的本地装配预算。`;
    }
    return budget?.requiredTokens != null && budget?.hardLimit != null && budget?.overageTokens != null
      ? `本地输入上下文为 ${budget.requiredTokens} tokens，上限 ${budget.hardLimit} tokens，超出 ${budget.overageTokens} tokens。`
      : "本地输入上下文超过了当前安全上限。";
  };
  const contextHeadroomDetailsOfRun = (run) => {
    if (run?.error_type !== "ContextBudgetError" || Number(run?.unsettled_attempt_count) !== 0) return null;
    const message = String(run?.error_message || "");
    const repairMatch = message.match(/story_project_repair_context_headroom_exceeded:\s*repair input requires\s+(\d+)\s+tokens;\s*safe target is\s+(\d+);\s*hard limit is\s+(\d+)/i);
    const sceneMatch = message.match(/story_project_context_headroom_exceeded:\s*(?:scene\s+)?input requires\s+(\d+)\s+tokens;\s*safe target is\s+(\d+);\s*hard limit is\s+(\d+)/i);
    const match = repairMatch || sceneMatch;
    if (!match) return null;
    const requiredTokens = Number(match[1]);
    const safeTarget = Number(match[2]);
    const hardLimit = Number(match[3]);
    if (![requiredTokens, safeTarget, hardLimit].every(Number.isFinite)) return null;
    const stage = repairMatch ? "repair" : "scene";
    if (stage === "repair") {
      if (run?.committed !== false) return null;
      const providerCalls = Number(run?.provider_calls);
      const chapterChars = Number(run?.chapter_chars);
      if (!Number.isFinite(providerCalls) || providerCalls < 0 || !Number.isFinite(chapterChars) || chapterChars <= 0) return null;
      return {
        stage,
        requiredTokens,
        safeTarget,
        hardLimit,
        overageTokens: Math.max(0, requiredTokens - safeTarget),
        providerCalls,
        chapterChars
      };
    }
    return {
      stage,
      requiredTokens,
      safeTarget,
      hardLimit,
      overageTokens: Math.max(0, requiredTokens - safeTarget)
    };
  };
  const contextHeadroomIsRepair = (failure) => failure?.stage === "repair";
  const contextHeadroomInputLabel = (failure) => (contextHeadroomIsRepair(failure) ? "修订" : "边界重生");
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

  const staleSceneEvidenceDetailsOfRun = (run) => {
    if (run?.error_type !== "SceneEvidenceAlignmentError" || Number(run?.unsettled_attempt_count) !== 0 || run?.committed !== false) return null;
    const message = String(run?.error_message || "");
    if (!message.startsWith("stale_scene_evidence:")) return null;
    const providerCalls = Number(run?.provider_calls);
    const chapterChars = Number(run?.chapter_chars);
    if (!Number.isFinite(providerCalls) || providerCalls < 1 || !Number.isFinite(chapterChars) || chapterChars <= 0) return null;
    return {
      providerCalls,
      chapterChars,
      reason: message.slice("stale_scene_evidence:".length).trim(),
      verified: true
    };
  };
  const staleSceneEvidenceDetailsOf = (job) => {
    if (!job || typeof job !== "object") return null;
    const structuredError = errorCandidatesOf(job).find((value) => value.code === "stale_scene_evidence");
    const details = structuredError?.details && typeof structuredError.details === "object"
      ? structuredError.details
      : null;
    const integer = (name) => Number.isInteger(details?.[name]) && details[name] >= 0 ? details[name] : null;
    const providerCalls = integer("provider_calls");
    const settledProviderCalls = integer("settled_provider_calls");
    const candidateChars = integer("candidate_chars");
    if (
      !structuredError
      || details?.failure_origin !== "stale_scene_evidence"
      || providerCalls == null
      || settledProviderCalls !== providerCalls
      || details?.unsettled_attempt_count !== 0
      || !candidateChars
      || details?.formal_prose_exists !== false
      || details?.accepted !== false
      || details?.committed !== false
      || details?.writeback_attempted !== false
      || details?.writeback_applied !== false
      || details?.automatic_retry !== false
      || details?.safe_to_retry !== false
      || details?.recovery_required !== true
      || details?.source_error_type !== "SceneEvidenceAlignmentError"
    ) {
      return staleSceneEvidenceDetailsOfRun(job.latest_run);
    }
    return {
      providerCalls,
      settledProviderCalls,
      chapterChars: candidateChars,
      reason: details.alignment_reason || String(details.source_error_message || "").replace(/^stale_scene_evidence:\s*/, ""),
      verified: true
    };
  };
  const staleSceneEvidenceReasonLabel = (failure) => {
    const reason = String(failure?.reason || "");
    if (/crosses a Scene boundary or separator/i.test(reason)) return "自动修订跨越了场景边界或分隔符";
    return reason ? `自动修订与已生成场景证据对不齐（${reason}）` : "自动修订与已生成场景证据对不齐";
  };
  const staleSceneEvidenceSummary = (failure) => failure
    ? `${staleSceneEvidenceReasonLabel(failure)}；${failure.providerCalls} 次模型调用均已结算，${failure.chapterChars} 字候选已保留，正式正文未提交。`
    : "自动修订与已生成场景证据对不齐。";

  const sceneBoundaryValidationDetailsOfRun = (run) => {
    if (run?.error_type !== "SceneBoundaryValidationError" || Number(run?.unsettled_attempt_count) !== 0 || run?.committed !== false) return null;
    const message = String(run?.error_message || "");
    if (!message.startsWith("scene boundary validation failed:")) return null;
    const providerCalls = Number(run?.provider_calls);
    const chapterChars = Number(run?.chapter_chars);
    if (!Number.isFinite(providerCalls) || providerCalls < 1 || !Number.isFinite(chapterChars) || chapterChars < 0) return null;
    const codeMatch = message.match(/^scene boundary validation failed: ([^;]+)/);
    const findingCodes = codeMatch
      ? codeMatch[1].split(",").map((item) => item.trim().replace(/ x[1-9][0-9]*$/, "")).filter(Boolean)
      : [];
    if (!findingCodes.length) return null;
    const sceneMatch = message.match(/Scene (\d+)/);
    return {
      providerCalls,
      chapterChars,
      findingCodes,
      failedScene: sceneMatch ? Number(sceneMatch[1]) : null,
      verified: true
    };
  };
  const sceneBoundaryValidationDetailsOf = (job) => {
    if (!job || typeof job !== "object") return null;
    const structuredError = errorCandidatesOf(job).find((value) => value.code === "scene_boundary_validation");
    const details = structuredError?.details && typeof structuredError.details === "object"
      ? structuredError.details
      : null;
    const integer = (name) => Number.isInteger(details?.[name]) && details[name] >= 0 ? details[name] : null;
    const providerCalls = integer("provider_calls");
    const settledProviderCalls = integer("settled_provider_calls");
    const candidateChars = integer("candidate_chars");
    const findingCodes = Array.isArray(details?.finding_codes)
      && details.finding_codes.length
      && details.finding_codes.every((code) => typeof code === "string" && code.trim())
      ? details.finding_codes
      : null;
    if (
      !structuredError
      || details?.failure_origin !== "scene_boundary_validation"
      || providerCalls == null
      || settledProviderCalls !== providerCalls
      || details?.unsettled_attempt_count !== 0
      || candidateChars == null
      || details?.formal_prose_exists !== false
      || details?.accepted !== false
      || details?.committed !== false
      || details?.writeback_attempted !== false
      || details?.writeback_applied !== false
      || details?.automatic_retry !== false
      || details?.safe_to_retry !== false
      || details?.recovery_required !== true
      || details?.source_error_type !== "SceneBoundaryValidationError"
      || !findingCodes
    ) {
      return sceneBoundaryValidationDetailsOfRun(job.latest_run);
    }
    return {
      providerCalls,
      settledProviderCalls,
      chapterChars: candidateChars,
      findingCodes,
      failedScene: integer("failed_scene"),
      verified: true
    };
  };
  const sceneBoundaryValidationSummary = (failure) => {
    if (!failure) return "场景边界校验未通过。";
    const codes = Array.isArray(failure.findingCodes) && failure.findingCodes.length
      ? `（${failure.findingCodes.join("、")}）`
      : "";
    const candidate = failure.chapterChars > 0
      ? `${failure.chapterChars} 字候选已保留，`
      : "本章尚无完整候选正文，";
    const scene = failure.failedScene ? `失败发生在第 ${failure.failedScene} 个场景，` : "";
    return `场景边界校验未通过${codes}；${failure.providerCalls} 次模型调用均已结算，${candidate}${scene}正式正文未提交。`;
  };



  const sceneSchemaValidationDetailsOfRun = (run) => {
    if (run?.error_type !== "ValueError" || Number(run?.unsettled_attempt_count) !== 0 || run?.committed !== false) return null;
    const message = String(run?.error_message || "");
    if (message !== "Scene response was not valid structured JSON") return null;
    const providerCalls = Number(run?.provider_calls);
    const chapterChars = Number(run?.chapter_chars);
    if (!Number.isFinite(providerCalls) || providerCalls < 1 || !Number.isFinite(chapterChars) || chapterChars < 0) return null;
    return {
      providerCalls,
      chapterChars,
      verified: true
    };
  };
  const sceneSchemaValidationDetailsOf = (job) => {
    if (!job || typeof job !== "object") return null;
    const structuredError = errorCandidatesOf(job).find((value) => value.code === "scene_schema_validation");
    const details = structuredError?.details && typeof structuredError.details === "object"
      ? structuredError.details
      : null;
    const integer = (name) => Number.isInteger(details?.[name]) && details[name] >= 0 ? details[name] : null;
    const providerCalls = integer("provider_calls");
    const settledProviderCalls = integer("settled_provider_calls");
    const candidateChars = integer("candidate_chars");
    if (
      !structuredError
      || details?.failure_origin !== "scene_schema_validation"
      || providerCalls == null
      || settledProviderCalls !== providerCalls
      || details?.unsettled_attempt_count !== 0
      || candidateChars == null
      || details?.formal_prose_exists !== false
      || details?.accepted !== false
      || details?.committed !== false
      || details?.writeback_attempted !== false
      || details?.writeback_applied !== false
      || details?.automatic_retry !== false
      || details?.safe_to_retry !== false
      || details?.recovery_required !== true
      || details?.source_error_type !== "ValueError"
      || details?.source_error_message !== "Scene response was not valid structured JSON"
    ) {
      return sceneSchemaValidationDetailsOfRun(job.latest_run);
    }
    return {
      providerCalls,
      settledProviderCalls,
      chapterChars: candidateChars,
      verified: true
    };
  };
  const sceneSchemaValidationSummary = (failure) => {
    if (!failure) return "场景响应不是合法 JSON。";
    const candidate = failure.chapterChars > 0
      ? `${failure.chapterChars} 字候选已保留，`
      : "本章尚无完整候选正文，";
    return `场景响应不是合法 JSON；${failure.providerCalls} 次模型调用均已结算，${candidate}正式正文未提交。`;
  };



  const bridgePrevalidationDetailsOfRun = (run) => {
    if (run?.error_type !== "ValueError" || Number(run?.unsettled_attempt_count) !== 0 || run?.committed !== false) return null;
    const message = String(run?.error_message || "");
    if (!message.startsWith("bridge pre-validation failed:")) return null;
    const providerCalls = Number(run?.provider_calls);
    const chapterChars = Number(run?.chapter_chars);
    if (!Number.isFinite(providerCalls) || providerCalls !== 0 || !Number.isFinite(chapterChars) || chapterChars !== 0) return null;
    const codes = message.slice("bridge pre-validation failed:".length).split(",").map((item) => item.trim()).filter(Boolean);
    if (!codes.length || !codes.every((code) => /^[A-Za-z][A-Za-z0-9_]*$/.test(code))) return null;
    return {
      providerCalls: 0,
      chapterChars: 0,
      problemCodes: codes,
      verified: true
    };
  };
  const bridgePrevalidationDetailsOf = (job) => {
    if (!job || typeof job !== "object") return null;
    const structuredError = errorCandidatesOf(job).find((value) => value.code === "bridge_prevalidation");
    const details = structuredError?.details && typeof structuredError.details === "object"
      ? structuredError.details
      : null;
    const integer = (name) => Number.isInteger(details?.[name]) && details[name] >= 0 ? details[name] : null;
    const problemCodes = Array.isArray(details?.problem_codes)
      && details.problem_codes.length
      && details.problem_codes.every((code) => typeof code === "string" && code.trim())
      ? details.problem_codes
      : null;
    if (
      !structuredError
      || details?.failure_origin !== "bridge_prevalidation"
      || integer("provider_calls") !== 0
      || integer("settled_provider_calls") !== 0
      || details?.unsettled_attempt_count !== 0
      || integer("candidate_chars") !== 0
      || details?.provider_reached !== false
      || details?.formal_prose_exists !== false
      || details?.accepted !== false
      || details?.committed !== false
      || details?.writeback_attempted !== false
      || details?.writeback_applied !== false
      || details?.automatic_retry !== false
      || details?.safe_to_retry !== false
      || details?.recovery_required !== true
      || details?.source_error_type !== "ValueError"
      || !problemCodes
    ) {
      return bridgePrevalidationDetailsOfRun(job.latest_run);
    }
    return {
      providerCalls: 0,
      settledProviderCalls: 0,
      chapterChars: 0,
      problemCodes,
      verified: true
    };
  };
  const bridgePrevalidationSummary = (failure) => {
    if (!failure) return "开写前衔接预检未通过。";
    const codes = Array.isArray(failure.problemCodes) && failure.problemCodes.length
      ? `（${failure.problemCodes.join("、")}）`
      : "";
    return `开写前衔接预检未通过${codes}；没有模型调用，没有候选正文，正式正文未提交。`;
  };

  const providerPartialResponseDetailsOfRun = (run) => {
    if (run?.committed !== false) return null;
    const message = String(run?.error_message || "");
    if (!message.includes("provider_partial_response")) return null;
    if (message.includes("provider_call_uncertain") || message.includes("ProviderCallUncertainError")) return null;
    if (!message.includes("ProviderPartialResponseError") && !message.startsWith("provider_partial_response:")) return null;
    const errorType = String(run?.error_type || "");
    if (errorType && errorType !== "ModelCallError" && errorType !== "ProviderPartialResponseError") return null;
    const providerCalls = Number(run?.provider_calls);
    const chapterChars = Number(run?.chapter_chars);
    const unsettled = Number(run?.unsettled_attempt_count);
    if (!Number.isFinite(providerCalls) || providerCalls < 1 || !Number.isFinite(chapterChars) || chapterChars < 0) return null;
    if (!Number.isInteger(unsettled) || (unsettled !== 0 && unsettled !== 1)) return null;
    return {
      providerCalls,
      chapterChars,
      unsettledAttemptCount: unsettled,
      verified: true
    };
  };
  const providerPartialResponseDetailsOf = (job) => {
    if (!job || typeof job !== "object") return null;
    const structuredError = errorCandidatesOf(job).find((value) => value.code === "provider_partial_response");
    const details = structuredError?.details && typeof structuredError.details === "object"
      ? structuredError.details
      : null;
    const integer = (name) => Number.isInteger(details?.[name]) && details[name] >= 0 ? details[name] : null;
    const providerCalls = integer("provider_calls");
    const settledProviderCalls = integer("settled_provider_calls");
    const candidateChars = integer("candidate_chars");
    const unsettled = details?.unsettled_attempt_count;
    if (
      !structuredError
      || details?.failure_origin !== "provider_partial_response"
      || providerCalls == null
      || providerCalls < 1
      || settledProviderCalls !== providerCalls
      || (unsettled !== 0 && unsettled !== 1)
      || candidateChars == null
      || details?.formal_prose_exists !== false
      || details?.accepted !== false
      || details?.committed !== false
      || details?.writeback_attempted !== false
      || details?.writeback_applied !== false
      || details?.provider_reached !== true
      || details?.partial_receipt_status !== "partial_response_failed"
      || details?.automatic_retry !== false
      || details?.safe_to_retry !== false
      || details?.recovery_required !== true
      || (details?.source_error_type !== "ModelCallError" && details?.source_error_type !== "ProviderPartialResponseError")
    ) {
      return providerPartialResponseDetailsOfRun(job.latest_run);
    }
    return {
      providerCalls,
      settledProviderCalls,
      chapterChars: candidateChars,
      unsettledAttemptCount: unsettled,
      partialStage: details.partial_stage,
      verified: true
    };
  };
  const providerPartialResponseSummary = (failure) => {
    if (!failure) return "模型调用在返回部分内容后中断，已留下失败回执。";
    const candidate = failure.chapterChars > 0
      ? `${failure.chapterChars} 字候选正文已保留，`
      : "本章尚无完整候选正文，但已保留部分响应回执，";
    return `模型调用在返回部分内容后中断，已留下失败回执，不是结果不确定；${failure.providerCalls} 次调用均有终态回执，${candidate}正式正文未提交。`;
  };

  const localBudgetAfterProviderDetailsOfRun = (run) => {
    if (run?.committed !== false) return null;
    const message = String(run?.error_message || "");
    if (!message.includes("(local_budget;")) return null;
    if (message.includes("provider_error") || message.includes("provider_partial_response") || message.includes("provider_call_uncertain") || message.includes("ProviderCallUncertainError")) return null;
    const errorType = String(run?.error_type || "");
    if (errorType && errorType !== "ModelCallError") return null;
    const providerCalls = Number(run?.provider_calls);
    const chapterChars = Number(run?.chapter_chars);
    const unsettled = Number(run?.unsettled_attempt_count);
    if (!Number.isFinite(providerCalls) || providerCalls < 2 || !Number.isFinite(chapterChars) || chapterChars < 0) return null;
    if (!Number.isInteger(unsettled) || (unsettled !== 0 && unsettled !== 1)) return null;
    return {
      providerCalls,
      chapterChars,
      unsettledAttemptCount: unsettled,
      succeededProviderCalls: providerCalls - 1,
      verified: true
    };
  };
  const localBudgetAfterProviderDetailsOf = (job) => {
    if (!job || typeof job !== "object") return null;
    const structuredError = errorCandidatesOf(job).find((value) => value.code === "local_budget_after_provider");
    const details = structuredError?.details && typeof structuredError.details === "object"
      ? structuredError.details
      : null;
    const integer = (name) => Number.isInteger(details?.[name]) && details[name] >= 0 ? details[name] : null;
    const providerCalls = integer("provider_calls");
    const settledProviderCalls = integer("settled_provider_calls");
    const succeededProviderCalls = integer("succeeded_provider_calls");
    const candidateChars = integer("candidate_chars");
    const unsettled = details?.unsettled_attempt_count;
    if (
      !structuredError
      || details?.failure_origin !== "local_budget_after_provider"
      || providerCalls == null
      || providerCalls < 2
      || settledProviderCalls !== providerCalls
      || succeededProviderCalls == null
      || succeededProviderCalls < 1
      || (unsettled !== 0 && unsettled !== 1)
      || candidateChars == null
      || details?.formal_prose_exists !== false
      || details?.accepted !== false
      || details?.committed !== false
      || details?.writeback_attempted !== false
      || details?.writeback_applied !== false
      || details?.provider_reached !== true
      || details?.rejected_receipt_status !== "budget_rejected"
      || details?.automatic_retry !== false
      || details?.safe_to_retry !== false
      || details?.recovery_required !== true
      || details?.source_error_type !== "ModelCallError"
    ) {
      return localBudgetAfterProviderDetailsOfRun(job.latest_run);
    }
    return {
      providerCalls,
      settledProviderCalls,
      succeededProviderCalls,
      chapterChars: candidateChars,
      unsettledAttemptCount: unsettled,
      rejectedStage: details.rejected_stage,
      verified: true
    };
  };

  const contextBudgetAfterProviderDetailsOfRun = (run) => {
    if (run?.committed !== false) return null;
    if (String(run?.error_type || "") !== "ContextBudgetError") return null;
    const message = String(run?.error_message || "");
    if (message.includes("provider_error") || message.includes("(local_budget;") || message.includes("ModelCallError") || message.includes("provider_partial_response") || message.includes("provider_call_uncertain") || message.includes("ProviderCallUncertainError")) return null;
    const hardMatch = message.match(/^story_project_context_budget_exceeded:\s*(scene|repair) input requires\s+(\d+)\s+tokens;\s*hard limit is\s+(\d+)$/i);
    const sceneHeadroom = message.match(/^story_project_context_headroom_exceeded:\s*(?:scene\s+)?input requires\s+(\d+)\s+tokens;\s*safe target is\s+(\d+);\s*hard limit is\s+(\d+)$/i);
    const repairHeadroom = message.match(/^story_project_repair_context_headroom_exceeded:\s*repair input requires\s+(\d+)\s+tokens;\s*safe target is\s+(\d+);\s*hard limit is\s+(\d+)$/i);
    let inputStage = null;
    let limitKind = null;
    let requiredTokens = null;
    let safeTarget = null;
    let hardLimit = null;
    if (hardMatch) {
      inputStage = hardMatch[1].toLowerCase();
      limitKind = "hard_limit";
      requiredTokens = Number(hardMatch[2]);
      hardLimit = Number(hardMatch[3]);
    } else if (sceneHeadroom) {
      inputStage = "scene";
      limitKind = "headroom";
      requiredTokens = Number(sceneHeadroom[1]);
      safeTarget = Number(sceneHeadroom[2]);
      hardLimit = Number(sceneHeadroom[3]);
    } else if (repairHeadroom) {
      inputStage = "repair";
      limitKind = "headroom";
      requiredTokens = Number(repairHeadroom[1]);
      safeTarget = Number(repairHeadroom[2]);
      hardLimit = Number(repairHeadroom[3]);
    } else {
      return null;
    }
    const providerCalls = Number(run?.provider_calls);
    const chapterChars = Number(run?.chapter_chars);
    const unsettled = Number(run?.unsettled_attempt_count);
    if (!Number.isFinite(providerCalls) || providerCalls < 1 || !Number.isFinite(chapterChars) || chapterChars < 0) return null;
    if (!Number.isInteger(unsettled) || unsettled !== 0) return null;
    if (![requiredTokens, hardLimit].every(Number.isFinite)) return null;
    if (limitKind === "hard_limit" && requiredTokens <= hardLimit) return null;
    if (limitKind === "headroom" && (!Number.isFinite(safeTarget) || requiredTokens <= safeTarget)) return null;
    return {
      inputStage,
      limitKind,
      stage: `${inputStage}_${limitKind === "headroom" ? "headroom" : "hard_limit"}`,
      requiredTokens,
      safeTarget,
      hardLimit,
      overageTokens: Math.max(0, requiredTokens - (limitKind === "headroom" ? safeTarget : hardLimit)),
      providerCalls,
      chapterChars,
      unsettledAttemptCount: unsettled,
      succeededProviderCalls: providerCalls,
      verified: true
    };
  };
  const contextBudgetAfterProviderDetailsOf = (job) => {
    if (!job || typeof job !== "object") return null;
    const structuredError = errorCandidatesOf(job).find((value) => value.code === "context_budget_after_provider");
    const details = structuredError?.details && typeof structuredError.details === "object"
      ? structuredError.details
      : null;
    const integer = (name) => Number.isInteger(details?.[name]) && details[name] >= 0 ? details[name] : null;
    const providerCalls = integer("provider_calls");
    const settledProviderCalls = integer("settled_provider_calls");
    const succeededProviderCalls = integer("succeeded_provider_calls");
    const candidateChars = integer("candidate_chars");
    const requiredTokens = integer("required_tokens");
    const hardLimit = integer("hard_limit_tokens");
    const safeTarget = details?.safe_target_tokens == null ? null : integer("safe_target_tokens");
    const overage = integer("overage_tokens");
    const unsettled = details?.unsettled_attempt_count;
    const inputStage = details?.input_stage;
    const limitKind = details?.limit_kind;
    if (
      !structuredError
      || details?.failure_origin !== "context_budget_after_provider"
      || (inputStage !== "scene" && inputStage !== "repair")
      || (limitKind !== "hard_limit" && limitKind !== "headroom")
      || providerCalls == null
      || providerCalls < 1
      || settledProviderCalls !== providerCalls
      || succeededProviderCalls !== providerCalls
      || unsettled !== 0
      || candidateChars == null
      || requiredTokens == null
      || hardLimit == null
      || overage == null
      || (limitKind === "headroom" && safeTarget == null)
      || details?.formal_prose_exists !== false
      || details?.accepted !== false
      || details?.committed !== false
      || details?.writeback_attempted !== false
      || details?.writeback_applied !== false
      || details?.provider_reached !== true
      || details?.automatic_retry !== false
      || details?.safe_to_retry !== false
      || details?.recovery_required !== true
      || details?.source_error_type !== "ContextBudgetError"
    ) {
      return contextBudgetAfterProviderDetailsOfRun(job.latest_run);
    }
    return {
      inputStage,
      limitKind,
      stage: details.stage || `${inputStage}_${limitKind === "headroom" ? "headroom" : "hard_limit"}`,
      requiredTokens,
      safeTarget,
      hardLimit,
      overageTokens: overage,
      providerCalls,
      settledProviderCalls,
      succeededProviderCalls,
      chapterChars: candidateChars,
      unsettledAttemptCount: unsettled,
      verified: true
    };
  };
  const contextBudgetAfterProviderSummary = (failure) => {
    if (!failure) return "已付费模型调用之后，本地上下文预算拒绝继续。";
    const candidate = failure.chapterChars > 0
      ? `${failure.chapterChars} 字候选正文已保留，`
      : "本章尚无完整候选正文，但已保留已付费调用回执，";
    const stageLabel = failure.inputStage === "repair" ? "修订输入" : "下一场景输入";
    const limitLabel = failure.limitKind === "headroom"
      ? (failure.requiredTokens != null && failure.safeTarget != null && failure.overageTokens != null
        ? `${stageLabel}为 ${failure.requiredTokens} tokens，超过安全目标 ${failure.safeTarget} tokens，超出 ${failure.overageTokens} tokens`
        : `${stageLabel}超过安全目标`)
      : (failure.requiredTokens != null && failure.hardLimit != null && failure.overageTokens != null
        ? `${stageLabel}为 ${failure.requiredTokens} tokens，超过硬上限 ${failure.hardLimit} tokens，超出 ${failure.overageTokens} tokens`
        : `${stageLabel}超过硬上限`);
    return `在已产生已付费模型调用后，${limitLabel}，不是结果不确定，也不是调用前的预算拒绝；${failure.providerCalls} 次调用均有终态回执且全部成功计费，${candidate}正式正文未提交。`;
  };

  const structuredContextAfterProviderDetailsOfRun = (run) => {
    if (run?.committed !== false) return null;
    if (String(run?.error_type || "") !== "StructuredContextError") return null;
    const message = String(run?.error_message || "");
    if (message.includes("provider_error") || message.includes("(local_budget;") || message.includes("ModelCallError") || message.includes("ContextBudgetError") || message.includes("provider_partial_response") || message.includes("provider_call_uncertain") || message.includes("ProviderCallUncertainError")) return null;
    const records = message.match(/^required_authoritative_records_exceed_budget:\s*required records need\s+(\d+)\s+characters including audit metadata, exceeding the\s+(\d+)\s+character authority budget(?:: .+)?$/);
    const record = message.match(/^required_authoritative_record_exceeds_budget:\s*required record .+ needs\s+(\d+)\s+characters including audit metadata, exceeding the\s+(\d+)\s+character authority budget$/);
    const metadata = message.match(/^required_authoritative_context_metadata_exceeds_budget:\s*authoritative metadata exceeds\s+(\d+)\s+characters$/);
    const character = message.match(/^(required_json_item_exceeds_budget|required_markdown_context_exceeds_budget|required_structured_entry_exceeds_budget|required_structured_manifest_exceeds_budget):\s*(?:required (?:complete Markdown\/JSON )?entries|selection and manifest) exceed(?:s)?\s+(\d+)\s+characters$/);
    let code = null;
    let inputStage = null;
    let stage = null;
    let requiredChars = null;
    let hardLimitChars = null;
    let overageChars = null;
    if (records) {
      code = "required_authoritative_records_exceed_budget";
      inputStage = "repair";
      stage = "repair_authority";
      requiredChars = Number(records[1]);
      hardLimitChars = Number(records[2]);
      overageChars = requiredChars - hardLimitChars;
    } else if (record) {
      code = "required_authoritative_record_exceeds_budget";
      inputStage = "repair";
      stage = "repair_authority";
      requiredChars = Number(record[1]);
      hardLimitChars = Number(record[2]);
      overageChars = requiredChars - hardLimitChars;
    } else if (metadata) {
      code = "required_authoritative_context_metadata_exceeds_budget";
      inputStage = "repair";
      stage = "repair_authority_metadata";
      hardLimitChars = Number(metadata[1]);
    } else if (character) {
      code = character[1];
      inputStage = "generate";
      stage = "generate_structured_context";
      hardLimitChars = Number(character[2]);
    } else {
      return null;
    }
    const providerCalls = Number(run?.provider_calls);
    const chapterChars = Number(run?.chapter_chars);
    const unsettled = Number(run?.unsettled_attempt_count);
    if (!Number.isFinite(providerCalls) || providerCalls < 1 || !Number.isFinite(chapterChars) || chapterChars < 0) return null;
    if (!Number.isInteger(unsettled) || unsettled !== 0) return null;
    if (!Number.isFinite(hardLimitChars) || hardLimitChars < 1) return null;
    if (requiredChars != null && requiredChars <= hardLimitChars) return null;
    return {
      code,
      inputStage,
      limitKind: "character_budget",
      stage,
      requiredChars,
      hardLimitChars,
      overageChars,
      providerCalls,
      chapterChars,
      unsettledAttemptCount: unsettled,
      succeededProviderCalls: providerCalls,
      verified: true
    };
  };
  const structuredContextAfterProviderDetailsOf = (job) => {
    if (!job || typeof job !== "object") return null;
    const structuredError = errorCandidatesOf(job).find((value) => value.code === "structured_context_after_provider");
    const details = structuredError?.details && typeof structuredError.details === "object"
      ? structuredError.details
      : null;
    const integer = (name) => Number.isInteger(details?.[name]) && details[name] >= 0 ? details[name] : null;
    const providerCalls = integer("provider_calls");
    const settledProviderCalls = integer("settled_provider_calls");
    const succeededProviderCalls = integer("succeeded_provider_calls");
    const candidateChars = integer("candidate_chars");
    const requiredChars = details?.required_chars == null ? null : integer("required_chars");
    const hardLimitChars = integer("hard_limit_chars");
    const overageChars = details?.overage_chars == null ? null : integer("overage_chars");
    const unsettled = details?.unsettled_attempt_count;
    const inputStage = details?.input_stage;
    const code = details?.code;
    if (
      !structuredError
      || details?.failure_origin !== "structured_context_after_provider"
      || (inputStage !== "generate" && inputStage !== "repair")
      || details?.limit_kind !== "character_budget"
      || !code
      || providerCalls == null
      || providerCalls < 1
      || settledProviderCalls !== providerCalls
      || succeededProviderCalls !== providerCalls
      || unsettled !== 0
      || candidateChars == null
      || hardLimitChars == null
      || details?.formal_prose_exists !== false
      || details?.accepted !== false
      || details?.committed !== false
      || details?.writeback_attempted !== false
      || details?.writeback_applied !== false
      || details?.provider_reached !== true
      || details?.automatic_retry !== false
      || details?.safe_to_retry !== false
      || details?.recovery_required !== true
      || details?.source_error_type !== "StructuredContextError"
    ) {
      return structuredContextAfterProviderDetailsOfRun(job.latest_run);
    }
    return {
      code,
      inputStage,
      limitKind: "character_budget",
      stage: details.stage || (inputStage === "repair" ? "repair_authority" : "generate_structured_context"),
      requiredChars,
      hardLimitChars,
      overageChars,
      providerCalls,
      settledProviderCalls,
      succeededProviderCalls,
      chapterChars: candidateChars,
      unsettledAttemptCount: unsettled,
      verified: true
    };
  };
  const structuredContextAfterProviderSummary = (failure) => {
    if (!failure) return "已付费模型调用之后，结构化上下文装配超过本地字符预算。";
    const candidate = failure.chapterChars > 0
      ? `${failure.chapterChars} 字候选正文已保留，`
      : "本章尚无完整候选正文，但已保留已付费调用回执，";
    const authority = String(failure.code || "").startsWith("required_authoritative_");
    const limitLabel = authority
      ? (failure.requiredChars != null && failure.hardLimitChars != null && failure.overageChars != null
        ? `修订所需权威记录需要 ${failure.requiredChars} 字，上限 ${failure.hardLimitChars} 字，超出 ${failure.overageChars} 字`
        : `修订所需权威记录超过 ${failure.hardLimitChars} 字上限`)
      : `装配本章必填上下文时超过 ${failure.hardLimitChars} 字上限`;
    return `在已产生已付费模型调用后，${limitLabel}，不是结果不确定，也不是调用前的预算拒绝；${failure.providerCalls} 次调用均有终态回执且全部成功计费，${candidate}正式正文未提交。`;
  };

  const localBudgetAfterProviderSummary = (failure) => {
    if (!failure) return "本地预算在已有已付费模型调用后拒绝继续。";
    const candidate = failure.chapterChars > 0
      ? `${failure.chapterChars} 字候选正文已保留，`
      : "本章尚无完整候选正文，但已保留已付费调用回执，";
    return `本地预算在已产生已付费模型调用后拒绝继续，不是结果不确定，也不是调用前的预算拒绝；${failure.providerCalls} 次调用均有终态回执，${candidate}正式正文未提交。`;
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
  const isQualityRevisionPause = (job) => job?.state === "awaiting_input" && ["needs_revision", "blocked", "error"].includes(reviewStatusOf(job)) && !preProviderProvenanceDetailsOf(job) && !contextBudgetDetailsOf(job) && !repairNotConvergingDetailsOf(job);
  const durablePolishedDraftAvailable = (issue) => {
    const recovery = issue?.chapter_recovery || issue;
    const checkpoint = issue?.checkpoint || recovery?.checkpoint;
    if (recovery?.formal_prose_exists === true || issue?.formal_prose_exists === true) return false;
    if (recovery?.uncertain_without_run === true || issue?.uncertain_without_run === true) return false;
    if (!checkpoint || checkpoint.action === "reset") return false;
    return recovery?.durable_polished_draft_available === true
      || issue?.durable_polished_draft_available === true
      || (checkpoint.reason === "durable_polished_draft_available" && checkpoint.has_complete_draft === true);
  };
  const polishedDraftGuidance = (issue) => {
    const draftState = issue?.draft?.state || issue?.quality_draft?.state;
    if (draftState === "validated" || issue?.resume_ready === true) {
      return "已有完整润色稿并通过验证。请确认提交已有草稿继续本章，不要结束失败尝试或新开一章。";
    }
    return "已有完整润色稿，不确定调用已写入恢复记录。请先验证或确认已有草稿，不要结束失败尝试或新开一章。";
  };
  const retainedCompleteDraftAvailable = (issue) => {
    const recovery = issue?.chapter_recovery || issue;
    const checkpoint = issue?.checkpoint || recovery?.checkpoint;
    if (recovery?.formal_prose_exists === true || issue?.formal_prose_exists === true) return false;
    if (recovery?.uncertain_without_run === true || issue?.uncertain_without_run === true) return false;
    if (!checkpoint || checkpoint.action === "reset") return false;
    if (recovery?.needs_reclassification === true || issue?.needs_reclassification === true) return false;
    const reasons = new Set([
      "complete_failed_draft_available",
      "complete_draft_available",
      "durable_repaired_draft_available",
      "durable_polished_draft_available"
    ]);
    return recovery?.retained_complete_draft_available === true
      || issue?.retained_complete_draft_available === true
      || durablePolishedDraftAvailable(issue)
      || (checkpoint.has_complete_draft === true && reasons.has(checkpoint.reason));
  };
  const retainedDraftGuidance = (issue) => {
    if (durablePolishedDraftAvailable(issue)) return polishedDraftGuidance(issue);
    const draftState = issue?.draft?.state || issue?.quality_draft?.state || (state.drafts || []).find((item) => Number(item.chapter) === Number(issue?.chapter || issue?.next_chapter))?.state;
    if (draftState === "validated" || issue?.resume_ready === true) {
      return "已保留完整候选稿并通过验证。请确认提交已有草稿继续本章；不要盲目重试这次失败的修订或上下文装配，也不要结束失败尝试来隐藏已付费正文。";
    }
    return "已保留完整候选稿。请先验证或编辑后再验证；不要盲目重试这次失败的修订或上下文装配，也不要结束失败尝试来隐藏已付费正文。";
  };
  const uncertainFailedStageLabel = (action) => ({
    validate: "语义审核",
    polish: "润色",
    repair_if_needed: "修订",
    repair: "修订",
    generate_chapter: "生成",
  }[String(action || "")] || "");

  const retainedUnsettledPaidDraft = (issue) => {
    const run = issue?.latest_run && typeof issue.latest_run === "object" ? issue.latest_run : null;
    const chars = Number(run?.chapter_chars);
    if (!Number.isFinite(chars) || chars < 500) return null;
    if (Number(run?.unsettled_attempt_count) !== 1) return null;
    if (run?.committed === true) return null;
    const details = issue?.error?.details && typeof issue.error.details === "object" ? issue.error.details : {};
    const message = [
      run?.error_message,
      issue?.error?.user_message,
      issue?.error?.message,
      issue?.error?.reason,
      issue?.reason,
    ].filter(Boolean).join("\n");
    const looksUncertain = issue?.uncertain_without_run !== true
      && (
        /ProviderCallUncertainError|provider_call_uncertain|without a durable receipt|没有耐久回执/.test(message)
        || run?.error_type === "ProviderCallUncertainError"
        || issue?.error?.error_type === "ProviderCallUncertainError"
        || issue?.error?.code === "provider_call_uncertain"
      );
    if (!looksUncertain) return null;
    if (/provider_partial_response|ProviderPartialResponseError/.test(message)) return null;
    return {
      chapterChars: chars,
      failedAction: run?.failed_action || details.failed_action || "",
    };
  };

  const providerUncertainPaidDraftCopy = (issue) => {
    const paidDraft = retainedUnsettledPaidDraft(issue);
    if (!paidDraft) return null;
    const stage = uncertainFailedStageLabel(paidDraft.failedAction);
    const stageClause = stage ? `（${stage}阶段中断）` : "";
    return `上次模型调用结果不确定，最后一次调用尚未结算。已付费的 ${paidDraft.chapterChars} 字候选正文仍保留在隔离运行区${stageClause}，但不能当作已验证候选。不要盲目重试，也不要先结束失败尝试来隐藏已付费正文。请先做无模型恢复分类。`;
  };

  const providerUncertainCopy = (issue) => {
    const paidDraftCopy = providerUncertainPaidDraftCopy(issue);
    if (paidDraftCopy) return paidDraftCopy;
    if (durablePolishedDraftAvailable(issue)) return polishedDraftGuidance(issue);
    const error = issue?.error || {};
    const pieces = [error.user_message, error.message, error.reason, error.stderr_tail, issue?.reason, issue?.summary].filter(Boolean).join("\n");
    if (issue?.uncertain_without_run === true || /ProviderCallUncertainError|provider_call_uncertain|without a durable receipt|没有耐久回执|没有完整回执/.test(pieces)) {
      return "上次模型调用没有完整回执，服务商可能已计费。请先核对使用记录和本地保留的正文，不要直接重试。";
    }
    return null;
  };


  const retainedPaidDraftPreview = (issue) => {
    const preview = issue?.retained_paid_draft_preview;
    if (!preview || typeof preview !== "object") return null;
    if (typeof preview.text !== "string" || preview.text.length < 500) return null;
    if (preview.committed === true || preview.verified_candidate === true || preview.confirm_submit_allowed === true) return null;
    return preview;
  };

  const renderRetainedPaidDraftInspect = (issue, mount) => {
    if (!mount) return;
    let panel = one("[data-retained-paid-draft-panel]", mount);
    const preview = retainedPaidDraftPreview(issue);
    const chapter = Number(issue?.chapter || issue?.next_chapter || issue?.latest_run?.chapter_index || 0);
    const actionableDraft = Array.isArray(state.drafts)
      ? state.drafts.find((item) => Number(item.chapter) === chapter)
      : null;
    const show = Boolean(preview && !actionableDraft);
    if (!panel && show) {
      panel = el("section", "panel recovery-draft-panel");
      panel.dataset.retainedPaidDraftPanel = "";
      const heading = el("div", "section-heading compact");
      const headingCopy = el("div");
      headingCopy.append(el("p", "eyebrow", "已付费正文（只读）"));
      headingCopy.append(el("h2", "", ""));
      heading.append(headingCopy);
      panel.append(heading);
      panel.append(el("p", "muted-copy", ""));
      const meta = el("p", "");
      meta.dataset.retainedPaidDraftMeta = "";
      panel.append(meta);
      const label = el("label", "field");
      label.append(el("span", "", "保留的已付费正文"));
      const textarea = el("textarea");
      textarea.className = "prose-editor";
      textarea.rows = 18;
      textarea.readOnly = true;
      textarea.dataset.retainedPaidDraftText = "";
      label.append(textarea);
      panel.append(label);
      const draftPanel = one("[data-recovery-draft-panel]", mount);
      const retainedAuthor = one("[data-retained-author-draft-panel]", mount);
      const tabShell = one("[data-tabs]", mount);
      if (draftPanel) mount.insertBefore(panel, draftPanel);
      else if (retainedAuthor) retainedAuthor.insertAdjacentElement("afterend", panel);
      else if (tabShell) mount.insertBefore(panel, tabShell);
      else mount.append(panel);
    }
    if (!panel) return;
    panel.hidden = !show;
    if (!show) return;
    const title = one("h2", panel);
    if (title) title.textContent = chapter ? `第 ${chapter} 章保留的已付费候选` : "保留的已付费候选";
    const help = one(".muted-copy", panel);
    if (help) help.textContent = "这是隔离运行区里保留的已付费正文，只供阅读核对，不能当作已验证候选，也不能确认提交。请先做无模型恢复分类；不要盲目重试，也不要把强制结束失败尝试当作查看正文的方式。";
    const stage = uncertainFailedStageLabel(preview.failedAction || preview.failed_action);
    const chars = Number(preview.chars || preview.text.length) || preview.text.length;
    const unsettled = Number(preview.unsettled_attempt_count);
    const parts = [`${chars} 字`];
    if (stage) parts.push(`${stage}阶段中断`);
    if (preview.source_run_id) parts.push(`运行 ${(String(preview.source_run_id).slice(0, 28))}`);
    if (unsettled === 1) parts.push("最后一次调用尚未结算");
    const meta = one("[data-retained-paid-draft-meta]", panel);
    if (meta) meta.textContent = parts.join(" · ");
    const textarea = one("[data-retained-paid-draft-text]", panel);
    if (textarea) textarea.value = preview.text;
  };

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
    const reviewedNotice = one("[data-reviewed-project-notice]");
    if (reviewedNotice) reviewedNotice.hidden = !hasProject || !state.project?.reviewed_format;
    const batchLink = one("[data-reviewed-batch-link]");
    if (batchLink) {
      batchLink.hidden = !hasProject || state.project?.reviewed_format !== true;
      batchLink.href = !batchLink.hidden ? `/reviewed-batches?project_id=${encodeURIComponent(state.projectId)}` : "#";
    }
    if (!hasProject) return;
    if (state.project.reviewed_format) {
      setText("[data-reviewed-project-message]", state.project.reviewed_status?.message || "请核对当前事实与提交历史。");
      const recovery = one("[data-reviewed-project-recover]");
      if (recovery) recovery.hidden = state.project.reviewed_status?.state !== "needs_recovery";
    }
    setText("[data-project-title]", projectTitleOf(state.project));
    setText("[data-project-meta]", `${projectGenreOf(state.project)} · ${projectLanguageOf(state.project)}`);
    all("[data-nav-area]").forEach((link) => {
      link.href = projectHash(link.dataset.navArea);
      const unsupported = link.dataset.navArea === "production" ? state.project.capabilities?.automatic_generation !== true
        : ["pending", "history"].includes(link.dataset.navArea) && state.project.capabilities?.job_history !== true;
      link.hidden = Boolean(state.project.reviewed_format && unsupported);
    });
    all("[data-project-route]").forEach((link) => { link.href = projectHash(link.dataset.projectRoute); });
    all("[data-project-route]").forEach((link) => {
      link.hidden = Boolean(state.project.reviewed_format && link.dataset.projectRoute === "outlines" && state.project.capabilities?.outline_planning !== true);
      if (link.dataset.projectRoute === "production") {
        const manual = state.project.reviewed_format && state.project.capabilities?.automatic_generation !== true;
        link.href = manual ? projectHash("chapters", state.project.next_chapter ? `/${state.project.next_chapter}` : "") : projectHash("production");
        setText("strong", manual ? "继续逐章写作" : "继续生成正文", link);
        setText("p", manual ? "确认本章意图，编辑草稿并审核事实。" : "按已确认章纲连续创作，异常时自动停下。", link);
      }
    });
    all('a[href="#/history"]').forEach((link) => { link.hidden = Boolean(state.project.reviewed_format && state.project.capabilities?.job_history !== true); });
    const projectLink = one("[data-nav-project-overview]");
    if (projectLink) projectLink.href = projectHash("overview");
    const healthLink = one("[data-health-link]");
    if (healthLink) healthLink.href = projectHash("health");
    const exportLink = one("[data-project-export]");
    if (exportLink) exportLink.href = `/api/projects/${encodeURIComponent(state.projectId)}/export`;
    updatePendingCount(Number(state.project.pending_count || 0));

    const editionNotice = one("[data-previous-edition-notice]");
    if (editionNotice) {
      const isPrevious = Boolean(hasProject && state.project?.edition_role === "previous");
      editionNotice.hidden = !isPrevious;
      if (isPrevious) {
        const currentTitle = state.project.current_edition_title || "当前作品";
        setText("[data-previous-edition-message]", `这是上一版，不是正在续写的那一本。当前作品是「${currentTitle}」。`);
        const editionLink = one("[data-previous-edition-link]");
        if (editionLink) {
          if (state.project.current_edition_id) {
            editionLink.hidden = false;
            editionLink.href = `#/projects/${encodeURIComponent(state.project.current_edition_id)}/overview`;
          } else {
            editionLink.hidden = true;
          }
        }
      }
    }
  };

  const fillBookCard = (project, index, container) => {
    if (!container) return;
    const template = one("#book-card-template");
    const fragment = template.content.cloneNode(true);
    const card = fragment.querySelector(".book-card");
    const link = fragment.querySelector(".book-card-main");
    const id = projectIdOf(project);
    link.href = `#/projects/${encodeURIComponent(id)}/overview`;
    const cover = fragment.querySelector(".book-cover");
    cover.style.setProperty("--cover-hue", String(project.cover_hue ?? ((index + 1) * 47) % 360));
    fragment.querySelector(".cover-kicker").textContent = projectGenreOf(project);
    fragment.querySelector(".book-cover strong").textContent = projectTitleOf(project);
    fragment.querySelector("h3").textContent = projectTitleOf(project);
    const countLabel = `${projectGenreOf(project)} · ${numberLabel(project.chapter_count)} 章`;
    if (project.edition_role === "previous") {
      fragment.querySelector(".book-title-row p").textContent = `上一版 · ${countLabel}`;
      card.classList.add("book-card-previous");
      const fallback = project.current_edition_title
        ? `当前作品是「${project.current_edition_title}」。这是上一版，不是正在续写的那一本。`
        : "这是上一版，不是正在续写的那一本。";
      fragment.querySelector(".book-summary").textContent = fallback;
    } else if (project.edition_role === "current") {
      fragment.querySelector(".book-title-row p").textContent = `当前作品 · ${countLabel}`;
      fragment.querySelector(".book-summary").textContent = projectSummaryOf(project);
    } else {
      fragment.querySelector(".book-title-row p").textContent = countLabel;
      fragment.querySelector(".book-summary").textContent = projectSummaryOf(project);
    }
    const editionBadge = fragment.querySelector("[data-edition-badge]");
    if (editionBadge && project.edition_label) {
      editionBadge.hidden = false;
      editionBadge.textContent = project.edition_label;
      editionBadge.classList.toggle("edition-badge-current", project.edition_role === "current");
      editionBadge.classList.toggle("edition-badge-previous", project.edition_role === "previous");
    }
    fragment.querySelector(".book-progress-copy strong").textContent = project.progress_label || (project.chapter_count ? `已写至第 ${project.chapter_count} 章` : "尚未开写");
    const progress = Math.max(0, Math.min(100, Number(project.progress || 0)));
    fragment.querySelector(".progress").setAttribute("aria-valuenow", String(progress));
    fragment.querySelector(".progress span").style.setProperty("--progress", `${progress}%`);
    fragment.querySelector("footer > span").textContent = project.updated_at_label || "本地作品";
    UI.updateStatusBadge(fragment.querySelector(".status-badge"), project.status || "idle", project.active_job?.execution_compatible === false ? "旧任务版本不兼容" : project.status_label);
    if (project.pending_count > 0) card.classList.add("needs-attention");
    container.append(fragment);
  };

  const renderBookshelf = () => {
    const grid = one("[data-project-grid]");
    const previousGrid = one("[data-previous-project-grid]");
    const previousHeading = one("[data-previous-editions-heading]");
    const previousLede = one("[data-previous-editions-lede]");
    const empty = one("[data-project-empty]");
    clear(grid);
    if (previousGrid) clear(previousGrid);
    if (empty) empty.hidden = state.projects.length > 0;
    const current = [];
    const previous = [];
    state.projects.forEach((raw) => {
      const project = raw.project || raw;
      if (project.edition_role === "previous") previous.push(project);
      else current.push(project);
    });
    current.forEach((project, index) => fillBookCard(project, index, grid));
    const previousTarget = previousGrid || grid;
    previous.forEach((project, index) => fillBookCard(project, index, previousTarget));
    const showPrevious = previous.length > 0 && previousGrid;
    if (previousHeading) previousHeading.hidden = !showPrevious;
    if (previousLede) previousLede.hidden = !showPrevious;
    if (previousGrid) previousGrid.hidden = !showPrevious;
  };

  const loadProjects = async () => {
    const { payload } = await api("/api/projects");
    state.projects = arrayFrom(payload, "items", "projects");
    renderBookshelf();
  };

  const loadProject = async (id, force = false, expectedRouteRevision = null) => {
    let detail;
    if (!force && projectCache.has(id)) {
      detail = projectCache.get(id);
    } else {
      const { payload } = await api(`/api/projects/${encodeURIComponent(id)}`);
      detail = payload;
      projectCache.set(id, payload);
    }
    if (expectedRouteRevision != null && state.routeRevision !== expectedRouteRevision) return null;
    state.detail = detail;
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


  function updateOutlinePlanningCopy(remaining) {
    const covered = Boolean(remaining?.covered) && Number(remaining?.chapter_count || 0) > 0;
    const start = Number(remaining?.start_chapter || 0);
    const end = Number(remaining?.end_chapter || 0);
    const count = Number(remaining?.chapter_count || 0);
    setText("[data-outlines-heading]", covered ? "剩余章纲" : "准备章纲");
    setText("[data-outlines-lede]", covered ? "这些章纲已经批准并冻结，可直接用于生产。不必再生成重叠的新提案。" : "先调整整体节奏，再一次性确认。确认后章纲冻结，成为连续生产的唯一依据。");
    setText("[data-outline-action-title]", covered ? "查看剩余章纲" : "准备下一章章纲");
    setText("[data-outline-action-detail]", covered ? (count === 1 ? `已批准章纲还可写第 ${start} 章，确认后即可去生产页续写。` : `已批准章纲还可写第 ${start}–${end} 章，确认后即可去生产页续写。`) : "自己写一章，或让模型起草提案。");
    const planButton = one("[data-outline-plan-button]");
    if (planButton) planButton.textContent = covered ? "查看剩余章纲" : "准备章纲";
  }

  const renderOverview = (payload) => {
    const overview = payload.overview || payload;
    const project = payload.project || state.project;
    setText("[data-project-summary]", projectSummaryOf(project));
    setText("[data-overview-chapters]", `${numberLabel(overview.chapter_count ?? project.chapter_count)} 章`);
    setText("[data-overview-words]", overview.word_count_label || `${numberLabel(overview.word_count)} 字`);
    const nextChapter = overview.next_chapter ?? project.next_chapter ?? 1;
    setText("[data-overview-next]", project.reviewed_format && project.next_chapter == null ? "待核验" : `第 ${nextChapter} 章`);
    const continueLink = one('[data-spa-view="overview"] .page-heading-actions .button-primary');
    if (continueLink) continueLink.href = project.reviewed_format ? projectHash("chapters", project.next_chapter == null ? "" : `/${project.next_chapter}`) : projectHash("production");
    setText("[data-overview-next-title]", overview.next_outline_title || "待规划");
    const latestBatch = overview.latest_outline_batch || {};
    const approvedOutlineCount = overview.outline_ready_count ?? (latestBatch.status === "approved" ? arrayFrom(latestBatch.items || latestBatch.proposal, "items").length : 0);
    setText("[data-overview-outlines]", `${numberLabel(approvedOutlineCount)} 章`);
    const needsOutline = !project.reviewed_format && overview.outline_remaining?.covered === false && !Number(overview.pending_count ?? project.pending_count ?? 0) && !overview.current_tasks?.length;
    if (continueLink) {
      continueLink.textContent = needsOutline ? `准备第 ${nextChapter} 章章纲` : "继续写作";
      if (needsOutline) continueLink.href = projectHash("outlines");
    }
    if (needsOutline) {
      one('[data-spa-view="overview"] .action-card[data-project-route="outlines"]').hidden = true;
      const action = one('[data-spa-view="overview"] .primary-action[data-project-route="production"]');
      if (action) {
        action.href = projectHash("outlines");
        setText("strong", `准备第 ${nextChapter} 章章纲`, action);
        setText("p", "这章还没有可用章纲。先写下将发生的事，再开始写正文。", action);
      }
    }
    updateOutlinePlanningCopy(overview.outline_remaining);
    const pendingTotal = Number(overview.pending_count ?? project.pending_count ?? 0);
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
    setText("[data-health-score]", project.reviewed_format ? (healthValidationOk ? "已核验" : "待处理") : overviewHealthScore);
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
    state.providerReady = ready;
    syncChapterModelButtons();
    one("[data-production-model-help]").hidden = ready;
    updateSessionLaunchButton(one("[data-session-form]"));
    const providerButtons = all("[data-requires-provider]");
    providerButtons.forEach((button) => {
      button.disabled = !ready;
      button.title = ready ? "" : "请先打开「模型设置」";
    });
  };
  const pendingChapterNumber = (issue) => {
    const chapter = Number(issue?.chapter ?? issue?.next_chapter);
    return Number.isInteger(chapter) && chapter > 0 ? chapter : null;
  };
  const pendingChapterLabel = (issue) => pendingChapterNumber(issue) ?? "—";
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

  const chapterSubmissionTargetsCurrentCard = ({ submission, confirmation = null, form, currentState = state }) => {
    const projectId = submission?.project_id;
    const chapter = Number(submission?.chapter);
    const cardSha = submission?.card_sha256;
    if (!projectId || !Number.isInteger(chapter) || chapter < 1 || !cardSha) return false;
    return form?.dataset?.projectId === projectId
      && Number(form?.dataset?.chapter) === chapter
      && form?.dataset?.cardSha === cardSha
      && form?.dataset?.submissionNonce === submission?.submission_nonce
      && currentState.projectId === projectId
      && currentState.route?.area === "chapter"
      && currentState.route?.projectId === projectId
      && Number(currentState.route?.chapter) === chapter
      && currentState.chapterCard?.confirmation?.card_sha256 === cardSha
      && (!confirmation || (
        Number(confirmation.chapter_index) === chapter
        && confirmation.card_sha256 === cardSha
      ));
  };
  const candidateActionTargetsCurrentChapter = ({ action, candidate = null, draft = null, currentState = state }) => {
    const projectId = action?.project_id;
    const chapter = Number(action?.chapter);
    if (!projectId || !Number.isInteger(chapter) || chapter < 1 || !action?.nonce) return false;
    const currentCandidate = currentState.chapterCandidate;
    const returnedCandidateId = candidate?.candidate_id || draft?.candidate_id || null;
    return String(currentState.candidateActionNonce) === String(action.nonce)
      && currentState.routeRevision === action.route_revision
      && currentState.projectId === projectId
      && currentState.route?.area === "chapter"
      && currentState.route?.projectId === projectId
      && Number(currentState.route?.chapter) === chapter
      && (!action.candidate_id || currentCandidate?.candidate_id === action.candidate_id)
      && (!action.candidate_id || !returnedCandidateId || returnedCandidateId === action.candidate_id)
      && (!action.draft_id || currentCandidate?.draft?.id === action.draft_id)
      && (!action.draft_id || !draft || draft.id === action.draft_id)
      && (!action.require_card_binding || currentState.chapterCard?.confirmation?.card_sha256 === action.card_sha256)
      && (!action.require_confirmation || currentState.chapterConfirmation?.confirmation_sha256 === action.confirmation_sha256)
      && (!candidate || (
        candidate.project_id === projectId
        && Number(candidate.chapter_index) === chapter
        && (!action.require_confirmation || candidate.confirmation?.confirmation_sha256 === action.confirmation_sha256)
      ));
  };
  const invalidateChapterCardSubmission = () => {
    const form = one("[data-chapter-card-confirm-form]");
    if (!form) return;
    form.dataset.submissionNonce = String(Number(form.dataset.submissionNonce || 0) + 1);
    form.removeAttribute("aria-busy");
    all(".is-busy", form).forEach((element) => element.classList.remove("is-busy"));
  };
  const invalidateCandidateAction = () => {
    state.candidateActionNonce += 1;
    all("[data-chapter-candidate-panel] .is-busy, [data-candidate-editor] .is-busy").forEach((element) => element.classList.remove("is-busy"));
  };
  const invalidateFactAction = () => {
    state.factActionNonce += 1;
    all("[data-chapter-fact-panel] .is-busy").forEach((element) => element.classList.remove("is-busy"));
  };
  const chapterConfirmationKey = (projectId, chapter) => `novelagent.chapter-card-confirmation.${projectId}.${chapter}`;
  const readChapterConfirmation = (projectId, chapter, card) => {
    try {
      const raw = window.sessionStorage.getItem(chapterConfirmationKey(projectId, chapter));
      const value = raw ? JSON.parse(raw) : null;
      const intent = value?.chapter_intent;
      const focus = intent?.validation_focus;
      const notes = intent?.notes;
      const sha256 = /^[0-9a-f]{64}$/;
      const allowedFocus = new Set(["continuity", "spatial", "logic"]);
      const valid = value?.schema_version === "1.0"
        && value?.record_type === "chapter_intent_confirmation"
        && value?.book_id === card?.book_id
        && value?.confirmed === true
        && value?.card_sha256 === card?.confirmation?.card_sha256
        && value?.context_sha256 === card?.binding?.context_sha256
        && sha256.test(value.card_sha256)
        && sha256.test(value.context_sha256)
        && sha256.test(value?.confirmation_sha256 || "")
        && Number(value?.chapter_index) === Number(chapter)
        && typeof intent?.goal === "string"
        && intent.goal.trim().length > 0
        && Array.isArray(focus)
        && focus.length > 0
        && focus.every((item) => allowedFocus.has(item))
        && new Set(focus).size === focus.length
        && Array.isArray(notes)
        && notes.every((item) => typeof item === "string");
      return valid ? value : null;
    } catch { return null; }
  };
  const writeChapterConfirmation = (projectId, chapter, confirmation) => {
    try { window.sessionStorage.setItem(chapterConfirmationKey(projectId, chapter), JSON.stringify(confirmation)); }
    catch { /* A blocked session store must not turn confirmation into a formal write. */ }
  };
  const clearChapterConfirmation = (projectId, chapter) => {
    try { window.sessionStorage.removeItem(chapterConfirmationKey(projectId, chapter)); }
    catch { /* Keep the page usable when browser storage is unavailable. */ }
  };
  const renderChapterConfirmationState = (confirmation) => {
    const panel = one("[data-card-confirmed]");
    if (panel) panel.hidden = !confirmation;
    setText("[data-card-confirmation-sha]", confirmation ? `确认凭据 ${confirmation.confirmation_sha256}` : "", doc, "");
  };
  const candidateCollectionPath = (projectId, chapter) => `/api/projects/${encodeURIComponent(projectId)}/chapters/${chapter}/candidates`;
  const candidateDetailPath = (projectId, chapter, candidateId) => `${candidateCollectionPath(projectId, chapter)}/${encodeURIComponent(candidateId)}`;
  const candidateOperationStorageKey = (projectId, chapter, confirmationSha) => `novelagent.chapter-candidate-operation.${projectId}.${chapter}.${confirmationSha}`;
  const currentConfirmationMatchesCard = () => Boolean(
    state.chapterConfirmation
      && state.chapterCard
      && state.chapterConfirmation.book_id === state.chapterCard.book_id
      && Number(state.chapterConfirmation.chapter_index) === Number(state.chapterCard.chapter_index)
      && state.chapterConfirmation.card_sha256 === state.chapterCard.confirmation?.card_sha256
      && state.chapterConfirmation.context_sha256 === state.chapterCard.binding?.context_sha256
  );
  const candidateBindingMatchesCard = (candidate) => Boolean(
    candidate
      && state.chapterCard
      && Number(candidate.chapter_index) === Number(state.chapterCard.chapter_index)
      && candidate.confirmation?.card_sha256 === state.chapterCard.confirmation?.card_sha256
      && candidate.confirmation?.context_sha256 === state.chapterCard.binding?.context_sha256
  );
  const candidateBindingMatchesConfirmation = (candidate) => Boolean(
    candidateBindingMatchesCard(candidate)
      && currentConfirmationMatchesCard()
      && candidate.confirmation?.confirmation_sha256 === state.chapterConfirmation.confirmation_sha256
  );
  const readCandidateOperationKey = (projectId, chapter, confirmationSha) => {
    try {
      const key = window.sessionStorage.getItem(candidateOperationStorageKey(projectId, chapter, confirmationSha));
      return /^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$/.test(key || "") ? key : null;
    } catch { return null; }
  };
  const candidateOperationKey = (projectId, chapter, confirmationSha) => {
    const existing = readCandidateOperationKey(projectId, chapter, confirmationSha);
    if (existing) return existing;
    const random = window.crypto?.randomUUID?.().replaceAll("-", "")
      || `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
    const key = `workbench-candidate:${confirmationSha.slice(0, 24)}:${random}`;
    try { window.sessionStorage.setItem(candidateOperationStorageKey(projectId, chapter, confirmationSha), key); }
    catch { /* Server-side discovery and idempotency remain authoritative. */ }
    return key;
  };
  const clearCandidateOperationKey = (projectId, chapter, confirmationSha) => {
    try { window.sessionStorage.removeItem(candidateOperationStorageKey(projectId, chapter, confirmationSha)); }
    catch { /* A blocked session store must not change Candidate safety. */ }
  };
  let displayedQuote = null, pendingQuote = null;
  const quoteKey = () => state.route?.area === "chapter" && currentConfirmationMatchesCard()
    ? JSON.stringify([state.routeRevision, state.projectId, state.route.chapter, state.chapterConfirmation.confirmation_sha256, state.providerRevision]) : null;
  const quoteText = (quote) => {
    const n = v => Number(v).toLocaleString("zh-CN");
    const p = quote.price;
    const basis = `${quote.model}：预计输入 ${n(quote.input_tokens_estimate)} token。`;
    if (!p) return basis + "当前模型、渠道或输入档位没有可用报价，金额未知。请核对服务商单价并按作者指南登记。";
    const example = p.example_output_tokens === quote.output_token_limit ? "" : `若输出 ${n(p.example_output_tokens)} token，约 $${p.example_usd}；`;
    return basis + `${example}若输出达到设置的 ${n(quote.output_token_limit)} token，约 $${p.at_output_limit_usd}。` +
      `按 ${p.as_of} 的普通输入/输出单价（美元）估算，仅含一次正文生成；未计缓存差价、续试或事实提取。实际以账单为准，这不是扣费上限。`;
  };
  const renderQuote = () => {
    const key = quoteKey();
    if (displayedQuote?.key !== key) displayedQuote = null;
    const button = one("[data-chapter-estimate]");
    if (button) button.disabled = !key || pendingQuote?.key === key;
    setText("[data-chapter-estimate-result]", !key ? "请先确认本章意图。估算本身不调用模型。"
      : pendingQuote?.key === key ? "正在本地核对本章输入…"
      : displayedQuote?.text || "估算本身不调用模型，也不占用预算。");
  };
  const estimateForCurrentChapter = async () => {
    const key = quoteKey();
    if (!key) return null;
    if (pendingQuote?.key === key) return pendingQuote.promise;
    const url = `/api/projects/${encodeURIComponent(state.projectId)}/chapters/${state.route.chapter}/generation-estimate`;
    const confirmation = state.chapterConfirmation;
    const promise = api(url, {method: "POST", json: {confirmation}}).then(({payload}) => {
      if (quoteKey() !== key) return null;
      const text = quoteText(payload.estimate);
      displayedQuote = {key, text};
      return text;
    }).catch(error => {
      if (quoteKey() === key) displayedQuote = {key, text: `未能估算：${UI.errorMessage(error?.payload, error?.status)} 没有发送模型请求。`};
      return null;
    }).finally(() => {
      if (pendingQuote?.promise === promise) pendingQuote = null;
      renderQuote();
    });
    pendingQuote = {key, promise};
    renderQuote();
    return promise;
  };
  // Both the chapter response and the later budget response affect these controls.
  const syncChapterModelButtons = () => {
    const generate = one('[data-candidate-generate]');
    const openExecution = ['running', 'needs_recovery', 'provider_uncertain'].includes(state.chapterCandidate?.status);
    if (generate) {
      generate.disabled = !state.providerReady || !currentConfirmationMatchesCard() || openExecution || Boolean(state.chapterCandidateError)
        || state.chapterBudgetBlocked || state.chapterRecoveryRequired || state.chapterHasFormalProse || generate.classList.contains('is-busy');
      generate.title = !state.providerReady ? '请先配置写作模型，或导入已有稿件' : state.chapterCandidateError || openExecution ? '请先处理已有候选记录' : !currentConfirmationMatchesCard() ? '请先确认本章输入' : '';
    }
    const extract = one('[data-fact-extract]');
    if (extract) {
      extract.disabled = !state.providerReady || state.chapterBudgetBlocked || Boolean(state.chapterFactError) || extract.classList.contains('is-busy');
      extract.title = state.providerReady ? '' : '请先配置写作模型，或自行审阅事实';
    }
    const help = one('[data-chapter-model-help]');
    if (help) help.hidden = state.providerReady || state.chapterHasFormalProse || state.chapterRecoveryRequired;
  };
  const candidateStatusPresentation = (candidate, errorMessage = "") => {
    if (!candidate) {
      if (errorMessage) return { status: "failed", label: "状态不可用", title: "无法恢复正文候选状态", message: `${errorMessage} 为避免重复调用模型，状态恢复前不能生成新的候选。` };
      if (currentConfirmationMatchesCard() && !state.providerReady) return { status: "warning", label: "待配置模型", title: "尚未配置写作模型", message: "本章输入已确认。点击下方“配置写作模型”，保存后再生成正文。" };
      if (currentConfirmationMatchesCard() && state.chapterBudgetBlocked) return { status: "warning", label: "预算待处理", title: "章节意图已确认", message: "现在可以估算正文费用；预算允许后才能调用模型。" };
      if (currentConfirmationMatchesCard()) return { status: "ready", label: "可生成", title: "章节意图已确认", message: "可以调用模型生成一份隔离正文候选；结果不会自动提交正式正文，也不会写入权威状态。" };
      return { status: "idle", label: "等待确认", title: "确认本章意图后生成", message: "模型只生成可隔离、可恢复的正文候选；不会自动提交正式正文，也不会写入权威状态。" };
    }
    const presentations = {
      running: { status: "generating", label: "生成中", title: "正文候选正在生成", message: "可以离开本页；刷新后会从本地记录恢复状态。正式正文和权威状态仍未修改。" },
      needs_recovery: { status: "needs_recovery", label: "等待作者恢复", title: "已有持久 Intent，等待恢复", message: "上次执行没有留下终态记录。系统不会自动调用 Provider；确认后才会读取本地 Intent/Receipt 并继续。" },
      completed: { status: "pass", label: "候选已生成", title: candidate.draft ? "可编辑草稿已准备" : "正文候选已生成", message: candidate.draft ? "候选已复制为独立工作台草稿。请编辑并保存；正式正文和权威状态仍未修改。" : "不可变候选已安全保留。建立可编辑副本后再修改；正式正文和权威状态仍未修改。" },
      stale: { status: "warning", label: "输入已变化", title: "候选已隔离保留", message: "生成期间可信输入发生变化；该候选不能作为当前草稿采用。请核对最新章节卡并重新确认。" },
      failed: { status: "failed", label: "生成失败", title: "正文候选生成失败", message: candidate.error?.message || "失败记录已保留；正式正文和权威状态均未修改。" },
      provider_uncertain: { status: "blocked", label: "结果不确定", title: "Provider 结果需要作者处理", message: "系统没有自动重试。没有收到完整回执，模型服务可能已计费。请按时间、模型和请求方式核对服务商使用记录；不要直接重试。原记录和预算占用保留。" },
      cancelled: { status: "cancelled", label: "已取消", title: "正文候选已取消", message: "取消记录已保留；正式正文和权威状态均未修改。" },
      abandoned: { status: "warning", label: "已放弃采用", title: "不确定结果已放弃采用", message: "原请求的证据和预算占用仍保留。请按下方续试状态继续；放弃采用不代表请求免费，也不会自动重新生成。" }
    };
    return presentations[candidate.status] || { status: "warning", label: "需要检查", title: "正文候选状态需要检查", message: "工作台不会自动重试或提交任何内容。" };
  };
  const chapterWorkflowPresentation = ({ formal, recovery, candidate, extraction, review, error, pending, confirmed, budgetBlocked, budgetMessage, providerReady }) => {
    if (recovery) return { phase: "blocked", label: "提交待恢复", message: "本章有未完成的提交，请先在项目概览恢复原事务。" };
    if (formal) return { phase: "committed", label: "正式稿", message: "本章已正式保存，可以开始下一章。" };
    if (pending === "candidate") return { phase: "candidate", label: "生成中", message: "正在处理本次正文生成。可以离开本页，回来后从保存的记录继续。" };
    if (pending === "facts") return { phase: "facts", label: "检查中", message: "正在处理本次事实提取。请等待当前检查结束。" };
    if (error) return { phase: "blocked", label: "需要处理", message: error };
    if (candidate?.status === "running") return { phase: "candidate", label: "生成中", message: "正文生成仍在执行。页面只查询进度，刷新不会重复调用。" };
    if (extraction?.status === "running") return { phase: "facts", label: "检查中", message: "事实提取仍在执行。请等待当前提取结束。" };
    if (candidate?.continuation?.state === "ready") return { phase: "candidate", label: "可续试一次", message: "一次续试许可已保存。点击下方“开始这一次续试”才会发送请求；旧请求占用继续保留。" };
    if (candidate?.continuation?.state === "available") return { phase: "candidate", label: "续试待确认", message: "上次请求结果未知。可在下方核对占用，明确允许同一章再试一次。" };
    if (candidate?.status === 'failed' && candidate.error?.code === 'model_key_missing') return { phase: 'candidate', label: providerReady ? '上次未发送' : '待配置模型', message: providerReady ? '上次生成时缺少密钥，没有发送请求。现在已读到配置，请核对本章输入和限额后再生成。' : '上次生成时未配置写作模型，没有发送请求。请点击正文候选区的“配置写作模型”，或导入已有稿件。' };
    if ([candidate?.status, extraction?.status].some(s => ["needs_recovery", "provider_uncertain", "failed"].includes(s))) return { phase: "blocked", label: "需要处理", message: "当前操作已停止，请查看下方原因和恢复操作；已有草稿仍保留。" };
    if (review?.state === "stale") return { phase: "facts", label: "事实需重查", message: "正文或依据发生变化，请核对草稿并重新检查事实。旧决定保留为只读记录。" };
    if (review?.state === "confirmed") return { phase: "publication", label: "待发布", message: "事实决定已确认。下一步预览最终正文并发布。" };
    if (review) return { phase: "facts", label: "待审阅", message: "请对事实明确接受、修改、拒绝或补充，再确认整组决定。" };
    if (extraction?.status === "completed" && extraction.source_state === "current") return { phase: "facts", label: "事实已提取", message: "提取结果已经保存。点击“继续事实审核”采用本次结果，无需再次调用模型。" };
    if (budgetBlocked && candidate?.draft) return { phase: "candidate", label: "可编辑草稿", message: "草稿可以继续编辑和保存，也可选择“自行审阅事实”。新增模型调用仍受下方预算限制。" };
    if (budgetBlocked) return { phase: "blocked", label: "预算待处理", message: budgetMessage || "请先读取并确认下方本章预算，已有草稿仍可编辑。" };
    if (candidate?.draft) return { phase: "candidate", label: "可编辑草稿", message: "正文草稿已保存。编辑完成后提取事实，继续本章审核。" };
    if (candidate?.status === "completed") return { phase: "candidate", label: "候选已生成", message: "正文候选已保留，请建立可编辑草稿。" };
    if (confirmed && providerReady === false) return { phase: "candidate", label: "待配置模型", message: "点击正文候选区的“配置写作模型”。保存后可继续本章。" };
    if (confirmed) return { phase: "candidate", label: "可生成", message: "本章意图已确认，可以生成正文候选。" };
    return { phase: "intent", label: "待确认意图", message: "从本章目标开始：确认意图后生成正文、审阅事实并发布。" };
  };
  let chapterProgressTimer = null;
  let chapterProgressReading = false;
  const chapterActionBusy = () => Boolean(one('[data-chapter-candidate-panel] .is-busy, [data-candidate-editor] .is-busy, [data-chapter-fact-panel] .is-busy'));
  const scheduleChapterProgress = () => {
    if (chapterProgressTimer != null || chapterProgressReading || state.route?.area !== "chapter" || state.chapterHasFormalProse || state.chapterRecoveryRequired) return;
    if (state.chapterCandidateError || state.chapterFactError || ![state.chapterCandidate?.status, state.chapterFactExtraction?.status].includes("running")) return;
    chapterProgressTimer = window.setTimeout(refreshRunningChapterProgress, 2000);
  };
  const chapterProgressMatches = (snapshot, current = state) => current.routeRevision === snapshot.routeRevision
    && current.projectId === snapshot.projectId && current.route?.area === "chapter"
    && current.route?.projectId === snapshot.projectId && Number(current.route?.chapter) === snapshot.chapter
    && current.candidateActionNonce === snapshot.candidateNonce && current.factActionNonce === snapshot.factNonce
    && current.chapterCandidate?.candidate_id === snapshot.candidateId
    && current.chapterCandidate?.draft?.id === snapshot.draft?.id
    && current.chapterCandidate?.draft?.revision === snapshot.draft?.revision
    && current.chapterCandidate?.draft?.sha256 === snapshot.draft?.sha256
    && current.chapterFactExtraction?.extraction_id === snapshot.extractionId;
  const refreshRunningChapterProgress = async () => {
    chapterProgressTimer = null;
    if (state.route?.area !== "chapter" || chapterActionBusy()) { scheduleChapterProgress(); return; }
    const snapshot = { routeRevision: state.routeRevision, projectId: state.projectId, chapter: Number(state.route.chapter),
      candidateNonce: state.candidateActionNonce, factNonce: state.factActionNonce,
      candidateId: state.chapterCandidate?.candidate_id, draft: state.chapterCandidate?.draft,
      extractionId: state.chapterFactExtraction?.extraction_id };
    const candidateRunning = state.chapterCandidate?.status === "running";
    const factsRunning = state.chapterFactExtraction?.status === "running";
    if (!candidateRunning && !factsRunning) return;
    chapterProgressReading = true;
    const stillCurrent = () => chapterProgressMatches(snapshot) && !chapterActionBusy();
    try {
      // Observe only the exact execution already displayed. Never start/resume it from a timer.
      if (candidateRunning) {
        const { payload } = await api(candidateDetailPath(snapshot.projectId, snapshot.chapter, snapshot.candidateId));
        if (!stillCurrent()) return;
        const candidate = payload?.candidate || payload;
        if (candidate.candidate_id !== snapshot.candidateId) return;
        renderChapterCandidate(candidate, "");
        if (candidate.draft) await refreshChapterFactsForCurrentDraft();
      } else {
        const { payload } = await api(`${factExtractionCollectionPath(snapshot.draft.id)}/${encodeURIComponent(snapshot.extractionId)}`);
        if (!stillCurrent() || one('[data-fact-review-form].is-dirty') || one('[data-fact-review-form][data-support-dirty="true"]')) return;
        const extraction = payload?.fact_extraction || payload;
        if (extraction.extraction_id !== snapshot.extractionId) return;
        renderChapterFacts(extraction, extraction.fact_review || null, "");
      }
    } catch (error) {
      if (!stillCurrent()) return;
      const message = `${UI.errorMessage(error?.payload, error?.status)} 已停止查询；请刷新页面重新读取保存的进度。`;
      if (candidateRunning) state.chapterCandidateError = message;
      else state.chapterFactError = message;
      renderChapterWorkflow();
    } finally {
      chapterProgressReading = false;
      if (state.routeRevision === snapshot.routeRevision) void window.NovelAgentChapterBudget?.refresh();
      scheduleChapterProgress();
    }
  };
  const renderChapterWorkflow = () => {
    if (state.route?.area !== "chapter") return;
    const presentation = chapterWorkflowPresentation({ formal: state.chapterHasFormalProse, recovery: state.chapterRecoveryRequired,
      candidate: state.chapterCandidate, extraction: state.chapterFactExtraction, review: state.chapterFactReview,
      error: state.chapterCandidateError || state.chapterFactError, confirmed: currentConfirmationMatchesCard(),
      providerReady: state.providerReady, budgetBlocked: state.chapterBudgetBlocked, budgetMessage: state.chapterBudgetMessage,
      pending: one('[data-candidate-generate].is-busy, [data-candidate-resume].is-busy, [data-continuation-run].is-busy') ? "candidate"
        : one('[data-fact-extract].is-busy, [data-fact-resume].is-busy') ? "facts" : null });
    setText("[data-chapter-flow-message]", presentation.message);
    UI.updateStatusBadge(one("[data-chapter-status]"), presentation.phase === "committed" ? "committed" : presentation.phase === "blocked" ? "warning" : "draft", presentation.label);
    all("[data-chapter-step]").forEach(control => {
      const current = control.dataset.chapterStep === presentation.phase;
      control.classList.toggle("is-current", current);
      if (current) control.setAttribute("aria-current", "step"); else control.removeAttribute("aria-current");
    });
    all('button[data-chapter-step]').forEach(control => { control.disabled = state.chapterHasFormalProse || state.chapterRecoveryRequired; });
    const factStep = one('[data-chapter-step="facts"]'); if (factStep) factStep.disabled ||= !state.chapterCandidate?.draft;
    const publication = one('[data-chapter-step="publication"]');
    if (publication) { publication.hidden = state.chapterFactReview?.state !== "confirmed" || !state.chapterFactReview?.publication_url; if (!publication.hidden) publication.href = state.chapterFactReview.publication_url; }
    const next = one('[data-chapter-step="next"]');
    if (next) { next.hidden = !state.chapterHasFormalProse || state.chapterRecoveryRequired; next.href = `#/projects/${encodeURIComponent(state.projectId)}/chapters/${Number(state.route.chapter) + 1}`; }
    scheduleChapterProgress();
  };
  const fragmentIndex = candidate => (candidate?.partial_responses || []).map(fragment => ({
    candidateId: candidate.candidate_id, attempt: fragment.attempt_id, sha: fragment.sha256,
  }));
  let fragmentView = null;
  const renderChapterFragments = (candidate, previous) => {
    const panel = one('[data-candidate-fragments]');
    if (!panel) return;
    const sameChapter = candidate && previous && previous.project_id === candidate.project_id && previous.chapter_index === candidate.chapter_index;
    const items = candidate?.fragmentHistory ?? (sameChapter ? previous.fragmentHistory : null) ?? [];
    const refs = [...fragmentIndex(candidate), ...items].filter((v,i,all) => all.findIndex(x => x.candidateId === v.candidateId && x.attempt === v.attempt) === i);
    if (candidate) candidate.fragmentHistory = refs;
    const choices = one('[data-fragment-choices]',panel), preview = one('[data-fragment-preview]',panel);
    const output = one('[data-fragment-text]',panel), prepare = one('[data-fragment-prepare]',panel);
    const message = one('[data-fragment-message]',panel);
    const view = {}; fragmentView = view;
    choices.replaceChildren(); output.value = ''; message.textContent = ''; preview.hidden = true; panel.hidden = !refs.length;
    const current = () => fragmentView === view && state.route?.projectId === candidate.project_id && Number(state.route?.chapter) === candidate.chapter_index;
    refs.forEach((ref,index) => {
      const button = document.createElement('button'); button.type='button'; button.className='button button-secondary';
      button.textContent = refs.length === 1 ? '查看中断原文' : `查看中断原文 ${index+1}`;
      button.onclick = withFeedback(async () => {
        const request = {}; view.request = request;
        preview.hidden = true; output.value = ''; message.textContent = '正在核对本地原文…';
        try {
          const {payload} = await api(candidateDetailPath(candidate.project_id, candidate.chapter_index, ref.candidateId));
          if (!current() || view.request !== request) return;
          const fragment = (payload.candidate || payload).partial_responses?.find(x => x.attempt_id === ref.attempt && x.sha256 === ref.sha);
          if (!fragment?.text) throw new Error();
          output.value = fragment.text; preview.hidden = false;
          message.textContent = `已核对原文，共 ${Array.from(fragment.text).length} 个字符。`;
          prepare.hidden = Boolean(candidate.draft) || !state.project?.reviewed_format || state.chapterHasFormalProse || !currentConfirmationMatchesCard() || state.chapterRecoveryRequired || state.chapterCandidateError || ['running','provider_uncertain','needs_recovery'].includes(candidate.status);
          prepare.onclick = () => {
            if (!current() || prepare.hidden || one('[data-author-draft-open]')?.disabled) return;
            const input = one('[data-author-draft-text]');
            if (input.value && input.value !== fragment.text) { message.textContent='导入框已有文字；请先保留原稿，或直接复制上方原文。'; return; }
            input.value = fragment.text; input.dataset.fragmentChapter = `${candidate.project_id}/${candidate.chapter_index}`; one('[data-author-draft-origin]').value = `工作台中断原文 ${ref.candidateId} / ${ref.sha}`;
            one('[data-author-draft-panel]').hidden = false; input.focus();
          };
        } catch { if (current() && view.request === request) message.textContent = '无法核对这份原文，未导入任何文字。'; }
      });
      choices.append(button);
    });
  };
  const renderChapterCandidate = (candidate, errorMessage = state.chapterCandidateError, options = {}) => {
    const previous = state.chapterCandidate;
    state.chapterCandidate = candidate || null;
    state.chapterCandidateError = errorMessage || "";
    renderChapterWorkflow();
    const panel = one("[data-chapter-candidate-panel]");
    if (!panel) return;
    renderChapterFragments(candidate, previous);
    const presentation = candidateStatusPresentation(candidate, state.chapterCandidateError);
    UI.updateStatusBadge(one("[data-candidate-status]", panel), presentation.status, presentation.label);
    setText("[data-candidate-title]", presentation.title, panel);
    setText("[data-candidate-message]", presentation.message, panel);

    const binding = one("[data-candidate-binding]", panel);
    if (binding) binding.hidden = !candidate;
    setText("[data-candidate-id]", candidate?.candidate_id || "", panel, "");
    setText("[data-candidate-prose-sha]", candidate?.prose?.sha256 || "", panel, "");
    const bindingCurrent = candidateBindingMatchesCard(candidate);
    const confirmationCurrent = candidateBindingMatchesConfirmation(candidate);
    const oldBinding = one("[data-candidate-old-binding]", panel);
    if (oldBinding) oldBinding.hidden = !candidate || (bindingCurrent && (!currentConfirmationMatchesCard() || confirmationCurrent));

    const confirmed = currentConfirmationMatchesCard();
    renderQuote();
    const blocksNewCandidate = ["running", "needs_recovery", "provider_uncertain"].includes(candidate?.status);
    const currentCompleted = candidate?.status === "completed" && confirmationCurrent;
    const generate = one("[data-candidate-generate]", panel);
    const importButton = one('[data-author-draft-open]', panel);
    if (importButton) {
      importButton.hidden = !state.project?.reviewed_format || state.chapterHasFormalProse;
      importButton.disabled = !confirmed || blocksNewCandidate || Boolean(state.chapterCandidateError) || state.chapterRecoveryRequired;
    }
    if (generate) {
      generate.hidden = currentCompleted;
      generate.textContent = candidate ? "生成新的正文候选" : "生成正文候选";
      syncChapterModelButtons();
    }
    const resume = one("[data-candidate-resume]", panel);
    if (resume) resume.hidden = candidate?.status !== "needs_recovery" || !bindingCurrent;
    const createDraft = one("[data-candidate-create-draft]", panel);
    if (createDraft) createDraft.hidden = candidate?.status !== "completed" || Boolean(candidate?.draft);
    const abandon = one("[data-candidate-abandon-provider]", panel);
    if (abandon) abandon.hidden = candidate?.status !== "provider_uncertain" || !candidate?.uncertainty || candidate.uncertainty.abandoned === true;
    const continuation = candidate?.continuation;
    const continuationPanel = one('[data-candidate-continuation]', panel);
    if (continuationPanel) continuationPanel.hidden = !continuation;
    setText('[data-continuation-message]', continuation?.message || '', panel, '');
    const charged = continuation?.charged, limits = continuation?.limits;
    setText('[data-continuation-budget]', charged && limits
      ? `本章已占用 ${charged.calls}/${limits.max_provider_calls} 次调用（包含未知请求预留），输入 ${charged.input_tokens}/${limits.max_input_tokens}、输出 ${charged.output_tokens}/${limits.max_output_tokens} token。续试不增加总限额，实际费用仍可能未知。` : '', panel, '');
    const allowOnce = one('[data-continuation-authorize]', panel);
    const runOnce = one('[data-continuation-run]', panel);
    const resultOnce = one('[data-continuation-result]', panel);
    if (allowOnce) allowOnce.hidden = continuation?.state !== 'available';
    if (runOnce) runOnce.hidden = continuation?.state !== 'ready';
    if (resultOnce) resultOnce.hidden = continuation?.state !== 'used' || !continuation?.target_candidate_id;

    const actionError = one("[data-candidate-action-error]", panel);
    if (actionError && !options.keepActionError) { actionError.hidden = true; actionError.textContent = ""; }
    const editor = one("[data-candidate-editor]");
    const draft = candidate?.draft;
    if (editor) editor.hidden = !draft;
    if (!editor || !draft) return;
    const form = one("[data-candidate-draft-form]", editor);
    const sameDraft = form.dataset.draftId === draft.id;
    const wasDirty = sameDraft && form.classList.contains("is-dirty");
    form.action = `/api/candidate-drafts/${encodeURIComponent(draft.id)}`;
    form.dataset.projectId = candidate.project_id;
    form.dataset.chapter = String(candidate.chapter_index);
    form.dataset.candidateId = candidate.candidate_id;
    form.dataset.draftId = draft.id;
    form.dataset.draftRevision = String(draft.revision);
    one('input[name="expected_revision"]', form).value = String(draft.revision);
    const textarea = one('textarea[name="text"]', form);
    if (options.preserveDraftText != null) textarea.value = String(options.preserveDraftText);
    else if (!wasDirty) textarea.value = draft.text || "";
    if (!wasDirty && options.preserveDraftText == null) form.classList.remove("is-dirty");
    setText("[data-candidate-draft-revision]", `r${draft.revision} · ${(draft.sha256 || "—").slice(0, 12)}`, editor);
    setText("[data-candidate-draft-save-state]", options.preserveDraftText != null || wasDirty ? "有尚未保存的修改" : "所有修改已保存", editor);
    UI.updateStatusBadge(one("[data-candidate-draft-status]", editor), "draft", bindingCurrent ? "可编辑草稿" : "旧输入草稿");
  };
  const factKindLabels = Object.freeze({
    event: "事件",
    character_state: "角色状态",
    relationship: "关系",
    roster: "成员变更",
    location: "位置变化",
    inventory: "库存变化",
    counter: "数值变化"
  });
  const factDecisionLabels = Object.freeze({ pending: "待决定", accepted: "接受", rejected: "拒绝" });
  const factExtractionCollectionPath = (draftId) => `/api/candidate-drafts/${encodeURIComponent(draftId)}/fact-extractions`;
  const factReviewDetailPath = (reviewId) => `/api/fact-reviews/${encodeURIComponent(reviewId)}`;
  const factOperationStorageKey = (draft) => `novelagent.chapter-fact-extraction.${draft.id}.${draft.revision}.${draft.sha256}`;
  const readFactOperationKey = (draft) => {
    try {
      const key = window.sessionStorage.getItem(factOperationStorageKey(draft));
      return /^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$/.test(key || "") ? key : null;
    } catch { return null; }
  };
  const factOperationKey = (draft) => {
    const existing = readFactOperationKey(draft);
    if (existing) return existing;
    const random = window.crypto?.randomUUID?.().replaceAll("-", "")
      || `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
    const key = `workbench-facts:${String(draft.sha256 || "").slice(0, 24)}:${random}`;
    try { window.sessionStorage.setItem(factOperationStorageKey(draft), key); }
    catch { /* Server evidence remains the source of truth when session storage is blocked. */ }
    return key;
  };
  const clearFactOperationKey = (draft) => {
    try { window.sessionStorage.removeItem(factOperationStorageKey(draft)); }
    catch { /* The next explicit action can safely use a fresh key. */ }
  };
  const factActionTargetsCurrentDraft = ({ action, extraction = null, review = null, currentState = state }) => {
    const draft = currentState.chapterCandidate?.draft;
    if (!action?.nonce || !draft) return false;
    const extractionSource = extraction?.source;
    const reviewSource = review?.source;
    return String(currentState.factActionNonce) === String(action.nonce)
      && currentState.routeRevision === action.route_revision
      && currentState.projectId === action.project_id
      && currentState.route?.area === "chapter"
      && currentState.route?.projectId === action.project_id
      && Number(currentState.route?.chapter) === Number(action.chapter)
      && draft.id === action.draft_id
      && Number(draft.revision) === Number(action.draft_revision)
      && draft.sha256 === action.draft_sha256
      && (!extractionSource || (
        extractionSource.draft_id === action.draft_id
        && Number(extractionSource.draft_revision) === Number(action.draft_revision)
        && extractionSource.draft_sha256 === action.draft_sha256
      ))
      && (!review || (
        review.draft_id === action.draft_id
        && Number(reviewSource?.draft_revision) === Number(action.draft_revision)
        && reviewSource?.draft_sha256 === action.draft_sha256
      ));
  };
  const beginFactAction = () => {
    const candidate = state.chapterCandidate;
    const draft = candidate?.draft;
    if (!candidate || !draft) return null;
    state.factActionNonce += 1;
    return Object.freeze({
      nonce: String(state.factActionNonce),
      route_revision: state.routeRevision,
      project_id: state.projectId,
      chapter: Number(state.route?.chapter),
      candidate_id: candidate.candidate_id,
      draft_id: draft.id,
      draft_revision: Number(draft.revision),
      draft_sha256: draft.sha256
    });
  };
  const factStatusPresentation = (extraction, review, errorMessage = "") => {
    if (errorMessage) return { status: "failed", label: "状态不可用", title: "无法安全恢复事实审核", message: `${errorMessage} 状态恢复前不会调用模型或写入权威状态。` };
    if (review?.state === "stale") return { status: "warning", label: "来源已变化", title: "旧事实审核只读保留", message: "正文草稿或权威状态已经变化；旧卡片不会被覆盖，也不能用于后续写入。" };
    if (review?.state === "confirmed") return { status: "pass", label: "审核已确认", title: "关键事实已由作者锁定", message: "审核结果已锁定，但正式正文和权威状态仍未写入。" };
    if (review) return { status: "reviewing", label: "等待作者", title: "逐项确认关键事实", message: "对照正文补充事实，并对每项明确接受、拒绝或删除；保存和确认都不会写入权威状态。" };
    if (!extraction) return { status: "ready", label: "可审阅", title: "核对当前草稿的关键事实", message: "可调用模型提取待审建议，也可自行添加和审阅事实。两种方式都需要作者确认。" };
    if (extraction.status === "completed" && extraction.source_state === "current") return { status: "ready", label: "事实已提取", title: "继续审阅已保存的事实", message: "提取结果已保存。继续事实审核会核验并采用本次结果，不会再次调用模型。" };
    const presentations = {
      running: { status: "reviewing", label: "检查中", title: "事实提取正在执行", message: "系统仍在读取当前正文并提取事实。刷新后可继续查询进度，无需重新发起。" },
      needs_recovery: { status: "needs_recovery", label: "等待作者恢复", title: "事实提取等待安全恢复", message: "已有持久 Intent，但没有终态记录；系统不会自动调用 Provider。" },
      provider_uncertain: { status: "blocked", label: "结果不确定", title: "Provider 结果需要作者处理", message: "系统已停止所有新调用。只能按这次调用的精确证据明确放弃，不能直接重试。" },
      failed: { status: "failed", label: "提取失败", title: "事实候选提取失败", message: extraction.error?.message || "失败证据已保留，系统没有自动修复或重试。" },
      cancelled: { status: "cancelled", label: "已取消", title: "事实提取已取消", message: "取消记录已保留；正文、正式正文和权威状态均未修改。" },
      abandoned: { status: "warning", label: "已放弃采用", title: "不确定结果已放弃", message: "调用证据仍被保留。可以从当前草稿发起一次新的事实提取。" },
      completed: { status: "warning", label: "候选只读", title: "事实候选已保留但不能审核", message: "提取完成后来源发生变化；请从当前草稿重新提取。" }
    };
    return presentations[extraction.status] || { status: "warning", label: "需要检查", title: "事实提取状态需要检查", message: "系统不会自动重试或写入权威状态。" };
  };
  const participantText = (items, idKey = "entity_id") => arrayFrom(items, "items")
    .map((item) => `${item?.[idKey] || "—"}${item?.role ? `（${item.role}）` : ""}${item?.channel ? ` · ${item.channel}` : ""}`)
    .join("、") || "无";
  const transportedItemText = (items) => arrayFrom(items, "items")
    .map((item) => `${item?.item_id || "—"}：${item?.from_location || "未注明"} → ${item?.to_location || "未注明"}${item?.carrier_id ? `（承运：${item.carrier_id}）` : ""}`)
    .join("；") || "无";
  const appendFactRole = (container, label, value) => {
    const wrapper = el("div");
    wrapper.append(el("span", "", label), el("p", "", value || "无"));
    container.append(wrapper);
  };
  const renderFactDecision = (card) => {
    const decision = card.dataset.decision || "pending";
    const badge = one("[data-fact-decision-label]", card);
    if (badge) badge.textContent = factDecisionLabels[decision] || decision;
    all("[data-fact-decision]", card).forEach((button) => {
      button.classList.toggle("is-selected", button.dataset.factDecision === decision);
    });
  };
  const factEditorContext = () => {
    const context = state.chapterFactEditorContext;
    const review = state.chapterFactReview;
    return context?.review_id === review?.id && context?.revision === review?.revision ? context : null;
  };
  const factEntities = () => {
    const context = factEditorContext();
    const entries = [...(context?.entity_directory || [])];
    for (const decision of state.chapterFactReview?.support?.identities || []) {
      entries.push({ entity_id: `local:${decision.local_key}`, kind: decision.kind, name: decision.name || decision.reference, aliases: decision.aliases || [] });
    }
    return entries;
  };
  const factEvidenceSpans = (quotes, source) => quotes.map((quote) => {
    const offset = source.indexOf(quote);
    if (!quote || offset < 0) throw new Error("引用必须逐字来自本次审核正文。");
    if (source.indexOf(quote, offset + 1) >= 0) throw new Error("引用出现了多次，请扩充为唯一片段。");
    const start = Array.from(source.slice(0, offset)).length;
    return { start, end: start + Array.from(quote).length, quote };
  });
  const highlightFactQuote = (quote) => {
    const pane = one("[data-fact-source-text]");
    const source = factEditorContext()?.source_text || "";
    const start = source.indexOf(quote);
    if (!pane || !quote || start < 0) return;
    pane.replaceChildren(doc.createTextNode(source.slice(0, start)), el("mark", "", quote), doc.createTextNode(source.slice(start + quote.length)));
    one("mark", pane)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  };
  const factBaselineText = (fact) => {
    const context = factEditorContext();
    const base = context?.support_preview?.state_after_support || context?.baseline_state;
    if (!base || fact.kind === "event") return "";
    const p = fact.proposal || {};
    const refs = context?.support_preview?.identity_plan?.local_references || {};
    const resolve = value => typeof value === "string" && value.startsWith("local:") ? refs[value.slice(6)]?.entity_id || value : value;
    let value;
    if (fact.kind === "character_state") value = base.characters?.[resolve(p.entity_id)]?.[p.field];
    if (fact.kind === "relationship") value = base.relationships?.[p.relationship_id]?.[p.field];
    if (fact.kind === "location") value = base.locations?.[resolve(p.entity_id)]?.location_id;
    if (fact.kind === "counter") value = base.numeric_counters?.[p.counter_id]?.current_value;
    if (fact.kind === "roster") value = base.roster?.[p.roster_id]?.members?.map(m => m.member_id);
    if (fact.kind === "inventory") value = Object.values(base.inventory || {}).find(row => row.owner_id === resolve(p.owner_id) && row.item_id === resolve(p.item_id))?.quantity;
    const names = new Map(factEntities().map(e => [e.entity_id, e.name]));
    const describe = v => Array.isArray(v) ? v.map(describe).join("、") || "空名单" : v && typeof v === "object" ? Object.entries(v).map(([k,x]) => `${k}：${describe(x)}`).join("；") : v === null ? "明确为空" : typeof v === "boolean" ? (v ? "是" : "否") : names.get(v) || String(v);
    return value === undefined ? "初始情况：尚无已知值。请在下方补充或拒绝此项。" : `变化前：${describe(value)}${context?.support_preview ? "（包含已保存的作者补充）" : ""}`;
  };
  const appendFactCard = (container, fact, readOnly) => {
    const card = el("article", "fact-card");
    card.dataset.factCard = "";
    card.dataset.factId = fact.fact_id;
    card.dataset.decision = fact.review?.decision || "pending";
    card.dataset.originalSummary = fact.summary || "";
    card.dataset.originalProposal = JSON.stringify(fact.proposal || {});
    card._baseFact = structuredClone(fact);
    card.dataset.authorEdited = String(Boolean(fact.review?.author_edited));
    const header = el("div", "fact-card-header");
    const heading = el("div");
    heading.append(
      el("span", "fact-kind", `${factKindLabels[fact.kind] || fact.kind} · ${fact.review?.origin === "author" ? "作者补充" : fact.review?.author_edited ? "模型建议 · 作者已修改" : "模型建议"}`),
      el("h3", "", fact.summary || "未命名事实")
    );
    const decision = el("span", "fact-decision", factDecisionLabels[card.dataset.decision]);
    decision.dataset.factDecisionLabel = "";
    header.append(heading, decision);
    const selection = el("label", "fact-select-label", "选择此项");
    const checkbox = el("input"); checkbox.type = "checkbox"; checkbox.name = `fact_select_${fact.fact_id}`; checkbox.dataset.factSelect = ""; checkbox.disabled = readOnly;
    selection.prepend(checkbox); header.prepend(selection);
    card.append(header);

    const evidence = el("blockquote", "fact-evidence");
    evidence.textContent = arrayFrom(fact.evidence_spans, "items").map((item) => `“${item.quote || ""}”`).join("；") || "没有可显示的正文证据";
    card.append(evidence);
    const locate = el("button", "text-link", "在正文中定位引用"); locate.type = "button";
    locate.addEventListener("click", () => highlightFactQuote(fact.evidence_spans?.[0]?.quote)); card.append(locate);
    const baseline = factBaselineText(fact);
    if (fact.kind !== "event") card.append(el("p", "fact-baseline", baseline));
    if (fact.kind === "event") {
      const proposal = fact.proposal || {};
      const roles = el("div", "fact-role-grid");
      appendFactRole(roles, "发生地点", proposal.occurrence_location || "未注明");
      appendFactRole(roles, "现场参与者", participantText(proposal.onsite_participants));
      appendFactRole(roles, "远程参与者", participantText(proposal.remote_participants));
      appendFactRole(roles, "运输中物品", transportedItemText(proposal.transported_items));
      appendFactRole(roles, "逻辑关联对象", participantText(proposal.related_entities));
      card.append(roles);
    }

    const fields = el("div", "fact-card-fields");
    const summaryField = el("label", "field");
    summaryField.append(el("span", "", "作者表述"));
    const summary = el("textarea");
    summary.dataset.factSummary = "";
    summary.name = `summary_${fact.fact_id}`;
    summary.rows = 2;
    summary.maxLength = 500;
    summary.required = true;
    summary.value = fact.summary || "";
    summary.disabled = readOnly;
    summaryField.append(summary);
    const proposalField = el("div", "fact-change-fields");
    const context = factEditorContext();
    if (context) {
      card._proposalEditor = window.NovelAgentFactForm.render(proposalField, context.payload_contracts[fact.kind], fact.proposal,
        { readOnly, bindNames: context.support_supported, entities: factEntities(), events: [...new Map([...state.chapterFactReview.facts, ...all("[data-fact-card]", container).map(c => ({ ...c._baseFact, summary: one("[data-fact-summary]", c)?.value || c._baseFact.summary }))].filter(f => f?.kind === "event").map(f => [f.local_key, { local_key: f.local_key, summary: f.summary }])).values()] });
      const quotes = el("div", "fact-quote-fields"); quotes.append(el("h4", "", "支持这项事实的正文原句"));
      const quoteFields = el("div"); quotes.append(quoteFields);
      card._evidenceEditor = window.NovelAgentFactForm.render(quoteFields, { type: "array", minItems: 1, items: { type: "string", minLength: 1, maxLength: 1000 } },
        fact.evidence_spans.map(span => span.quote), { readOnly });
      proposalField.append(quotes);
      const original = context.original_model_facts?.find(f => f.fact_id === fact.fact_id);
      if (original) {
        const details = el("details", "fact-original"); details.append(el("summary", "", "查看原始模型建议"), el("p", "", original.summary));
        details.append(el("blockquote", "fact-evidence", original.evidence_spans.map(s => s.quote).join("；")));
        const originalFields = el("div"); details.append(originalFields);
        window.NovelAgentFactForm.render(originalFields, context.payload_contracts[original.kind], original.proposal, { readOnly: true, entities: factEntities() });
        proposalField.append(details);
      }
    }
    fields.append(summaryField, proposalField);
    card.append(fields);

    const actions = el("div", "fact-decision-actions");
    [["accepted", "接受"], ["rejected", "拒绝"]].forEach(([value, label]) => {
      const button = el("button", "button button-secondary", label);
      button.type = "button";
      button.dataset.factDecision = value;
      button.disabled = readOnly;
      actions.append(button);
    });
    const remove = el("button", "button button-danger-ghost", "删除建议");
    remove.type = "button";
    remove.dataset.factDelete = "";
    remove.disabled = readOnly;
    actions.append(remove);
    if (!readOnly && !state.chapterFactReview.facts.some(f => f.fact_id === fact.fact_id)) {
      const discard = el("button", "button button-danger-ghost", "撤回未保存的补充"); discard.type = "button";
      discard.addEventListener("click", () => { card.remove(); one("[data-fact-review-form]").classList.add("is-dirty"); updateFactReviewLocalState(); });
      actions.append(discard);
    }
    card.append(actions);

    const reasonField = el("label", "field fact-reason");
    reasonField.append(el("span", "", "决定备注（可选）"));
    const reason = el("input");
    reason.type = "text";
    reason.name = `reason_${fact.fact_id}`;
    reason.maxLength = 500;
    reason.dataset.factReason = "";
    reason.value = fact.review?.reason || "";
    reason.disabled = readOnly;
    reasonField.append(reason);
    card.append(reasonField);
    renderFactDecision(card);
    container.append(card);
  };
  const collectFactReview = (review) => {
    const byId = new Map(arrayFrom(review?.facts, "items").map((item) => [item.fact_id, item]));
    return all("[data-fact-card]", one("[data-fact-card-list]")).map((card) => {
      const original = byId.get(card.dataset.factId) || card._baseFact;
      if (!original) throw new Error("事实卡与当前审核记录不一致，请刷新后重试。");
      const summary = one("[data-fact-summary]", card)?.value.trim() || "";
      if (!summary) throw new Error("每张事实卡都需要作者表述。");
      if (!card._proposalEditor || !card._evidenceEditor) throw new Error("原始建议尚未核对完成，请稍后再保存。");
      const proposal = card._proposalEditor.read();
      const quotes = card._evidenceEditor.read();
      const evidence_spans = JSON.stringify(quotes) === JSON.stringify(original.evidence_spans.map(span => span.quote))
        ? structuredClone(original.evidence_spans) : factEvidenceSpans(quotes, factEditorContext().source_text);
      const changed = summary !== original.summary || JSON.stringify(proposal) !== JSON.stringify(original.proposal) || JSON.stringify(evidence_spans) !== JSON.stringify(original.evidence_spans);
      const reason = one("[data-fact-reason]", card)?.value.trim() || null;
      return {
        ...JSON.parse(JSON.stringify(original)),
        summary,
        proposal,
        evidence_spans,
        review: {
          origin: original.review?.origin || "model",
          decision: card.dataset.decision || "pending",
          author_edited: Boolean(original.review?.author_edited || changed),
          reason
        }
      };
    });
  };
  const updateFactReviewLocalState = () => {
    const form = one("[data-fact-review-form]");
    if (!form || form.hidden) return;
    const cards = all("[data-fact-card]", form);
    const counts = { pending: 0, accepted: 0, rejected: 0 };
    cards.forEach((card) => {
      counts[card.dataset.decision || "pending"] += 1; renderFactDecision(card);
      const baseline = one(".fact-baseline", card);
      if (baseline && card._proposalEditor) {
        try { baseline.textContent = factBaselineText({ ...card._baseFact, proposal: card._proposalEditor.read() }); }
        catch { baseline.textContent = "填写变化对象后显示它的初始情况。"; }
      }
    });
    const filter = one("[data-fact-filter]")?.value || "all";
    cards.forEach(card => { card.hidden = filter === "edited" ? card.dataset.authorEdited !== "true" : filter !== "all" && card.dataset.decision !== filter; });
    const selected = cards.filter(card => !card.hidden && one("[data-fact-select]", card)?.checked).length;
    setText("[data-fact-selected-count]", `已选 ${selected} 项`);
    const ready = state.chapterFactReview?.state === "editing" && Boolean(factEditorContext()) && form.getAttribute("aria-busy") !== "true";
    all("[data-fact-batch]").forEach(button => { button.disabled = !ready || !selected; });
    all("[data-fact-add], [data-fact-save], [data-fact-support-save]").forEach(button => { button.disabled = !ready; });
    setText("[data-fact-counts]", `${cards.length} 项 · 待决定 ${counts.pending} · 接受 ${counts.accepted} · 拒绝 ${counts.rejected}`, form);
    setText("[data-fact-save-state]", form.classList.contains("is-dirty") ? "有尚未保存的修改" : "所有修改已保存", form);
    const confirm = one("[data-fact-confirm]", form);
    if (confirm) {
      confirm.disabled = !ready || counts.pending > 0 || form.dataset.supportDirty === "true";
      confirm.title = counts.pending > 0 ? "每项都必须先明确接受、拒绝或删除" : "";
    }
  };
  const canStartManualFactReview = ({ draft, review, extraction, error }) => Boolean(draft) && !error
    && (!review || review.state === 'stale')
    && !['running', 'needs_recovery', 'provider_uncertain'].includes(extraction?.status);
  const renderChapterFacts = (extraction, review, errorMessage = "", options = {}) => {
    state.chapterFactExtraction = extraction || null;
    state.chapterFactReview = review || extraction?.fact_review || null;
    state.chapterFactError = errorMessage || "";
    renderChapterWorkflow();
    const panel = one("[data-chapter-fact-panel]");
    const draft = state.chapterCandidate?.draft;
    if (!panel) return;
    panel.hidden = !draft;
    if (!draft) return;
    const activeReview = state.chapterFactReview;
    const publicationLink = one("[data-fact-publication-link]", panel);
    if (publicationLink) {
      publicationLink.hidden = activeReview?.state !== "confirmed" || !activeReview?.publication_url;
      if (!publicationLink.hidden) publicationLink.href = activeReview.publication_url;
    }
    const presentation = factStatusPresentation(state.chapterFactExtraction, activeReview, state.chapterFactError);
    if (state.chapterBudgetBlocked && !state.chapterFactExtraction && !activeReview) {
      presentation.status = 'warning'; presentation.label = '可自行审阅';
      presentation.message = '模型提取受预算限制。你仍可自行审阅事实，不新增模型调用，旧请求的占用和未知费用继续保留。';
    }
    if (!state.providerReady && !state.chapterFactExtraction && !activeReview && !state.chapterFactError) {
      presentation.status = 'warning'; presentation.label = '可自行审阅'; presentation.message = '写作模型尚未配置。你可以自行审阅事实，不需要模型。';
    }
    UI.updateStatusBadge(one("[data-fact-status]", panel), presentation.status, presentation.label);
    setText("[data-fact-title]", presentation.title, panel);
    setText("[data-fact-message]", presentation.message, panel);
    const binding = one("[data-fact-binding]", panel);
    if (binding) binding.hidden = !state.chapterFactExtraction;
    setText("[data-fact-extraction-id]", state.chapterFactExtraction?.extraction_id || "", panel, "");
    setText("[data-fact-source-draft]", state.chapterFactExtraction ? `r${state.chapterFactExtraction.source?.draft_revision || "—"} · ${(state.chapterFactExtraction.source?.draft_sha256 || "—").slice(0, 12)}` : "", panel, "");
    const stale = state.chapterFactExtraction?.source_state === "stale" || activeReview?.state === "stale";
    const staleNotice = one("[data-fact-stale]", panel);
    if (staleNotice) staleNotice.hidden = !stale;

    const reviewBlocksStart = activeReview && activeReview.state !== "stale";
    const canStart = !reviewBlocksStart && (!state.chapterFactExtraction
      || ["failed", "cancelled", "abandoned"].includes(state.chapterFactExtraction.status)
      || stale);
    const extract = one("[data-fact-extract]", panel);
    if (extract) {
      extract.hidden = !canStart;
      syncChapterModelButtons();
      extract.textContent = state.chapterFactExtraction ? "从当前草稿重新提取" : "提取事实候选";
    }
    const manual = one('[data-fact-manual]', panel);
    const manualAllowed = canStartManualFactReview({ draft, review: activeReview, extraction: state.chapterFactExtraction, error: state.chapterFactError });
    if (manual) { manual.hidden = !manualAllowed; manual.disabled = !manualAllowed || manual.classList.contains('is-busy'); }
    const manualHelp = one('[data-fact-manual-help]', panel);
    if (manualHelp) manualHelp.hidden = !manualAllowed;
    const resume = one("[data-fact-resume]", panel);
    if (resume) resume.hidden = state.chapterFactExtraction?.status !== "needs_recovery" || stale;
    const openReview = one("[data-fact-open-review]", panel);
    if (openReview) openReview.hidden = state.chapterFactExtraction?.status !== "completed" || stale || Boolean(activeReview);
    const cancel = one("[data-fact-cancel]", panel);
    if (cancel) cancel.hidden = state.chapterFactExtraction?.status !== "needs_recovery" || stale;
    const abandon = one("[data-fact-abandon-provider]", panel);
    if (abandon) abandon.hidden = state.chapterFactExtraction?.status !== "provider_uncertain" || !state.chapterFactExtraction?.uncertainty?.sha256;
    const actionError = one("[data-fact-action-error]", panel);
    if (actionError && !options.keepActionError) { actionError.hidden = true; actionError.textContent = ""; }

    const form = one("[data-fact-review-form]", panel);
    form.hidden = !activeReview;
    panel.classList.toggle("is-readonly", Boolean(activeReview && activeReview.state !== "editing"));
    if (!activeReview) return;
    if (!options.keepActionError) {
      const reviewError = one("[data-fact-review-error]");
      reviewError.hidden = true; reviewError.textContent = "";
    }
    const context = factEditorContext();
    const loadKey = `${state.routeRevision}:${activeReview.id}:${activeReview.revision}`;
    const loadingEditor = one("[data-fact-editor-loading]");
    if (loadingEditor) { loadingEditor.hidden = Boolean(context); loadingEditor.textContent = "正在核对原始建议和当前状态…"; }
    if (!context && state.factEditorLoadKey !== loadKey) {
      state.factEditorLoadKey = loadKey;
      const routeRevision = state.routeRevision;
      api(`${factReviewDetailPath(activeReview.id)}/editor-context`).then(({ payload }) => {
        if (state.routeRevision !== routeRevision || state.chapterFactReview?.id !== activeReview.id || state.chapterFactReview?.revision !== activeReview.revision) return;
        state.chapterFactEditorContext = payload;
        if (payload.review_id !== activeReview.id || payload.revision !== activeReview.revision) throw new Error("审核修订已变化，请重新加载本章。");
        renderChapterFacts(state.chapterFactExtraction, state.chapterFactReview, errorMessage, options);
      }).catch(error => {
        if (state.routeRevision !== routeRevision || state.chapterFactReview?.id !== activeReview.id) return;
        if (loadingEditor) loadingEditor.textContent = "原始建议或当前状态暂时无法核对，请重新加载本章。";
        showFactError(error?.message || "无法核对审核输入。", "[data-fact-review-error]");
      });
    }
    form.dataset.reviewId = activeReview.id;
    form.dataset.reviewRevision = String(activeReview.revision);
    const facts = options.preserveFacts || activeReview.facts || [];
    const list = one("[data-fact-card-list]", form);
    clear(list);
    facts.forEach((fact) => appendFactCard(list, fact, activeReview.state !== "editing" || !context));
    if (!facts.length) list.append(el("p", "fact-empty", "事实清单目前为空。请对照正文补充需要记录的变化；清单为空不代表本章没有变化。"));
    setText("[data-fact-source-text]", context?.source_text || "正在读取本次审核正文…");
    const supportPanel = one("[data-fact-support-panel]");
    supportPanel.hidden = !context?.support_supported && !activeReview.support;
    form.dataset.supportDirty = "false";
    if (context && !supportPanel.hidden) {
      const supportEditor = one("[data-fact-support-editor]");
      supportEditor._editor = window.NovelAgentFactSupportForm.render(supportEditor, activeReview.support,
        { entities: context.entity_directory, readOnly: activeReview.state !== "editing" || !context.support_supported });
      setText("[data-fact-support-state]", activeReview.support ? "作者补充已保存" : "尚未补充");
    }
    form.classList.toggle("is-dirty", options.dirty === true);
    const save = one("[data-fact-save]", form);
    if (save) save.disabled = activeReview.state !== "editing";
    updateFactReviewLocalState();
  };
  const loadChapterFactState = async (draft) => {
    if (!draft) return { extraction: null, review: null };
    const collection = factExtractionCollectionPath(draft.id);
    const { payload } = await api(collection);
    const extractionItems = arrayFrom(payload, "items", "fact_extractions");
    const latest = extractionItems.length ? extractionItems[extractionItems.length - 1] : null;
    let extraction = null;
    if (latest?.extraction_id) {
      const detail = await api(`${collection}/${encodeURIComponent(latest.extraction_id)}`);
      extraction = detail.payload?.fact_extraction || detail.payload;
    }
    let review = extraction?.fact_review || null;
    if (!review) {
      const reviews = await api(`/api/candidate-drafts/${encodeURIComponent(draft.id)}/fact-reviews`);
      const latestReview = arrayFrom(reviews.payload, "items", "fact_reviews")[0];
      if (latestReview?.id) {
        const detail = await api(factReviewDetailPath(latestReview.id));
        review = detail.payload?.fact_review || detail.payload;
      }
    }
    return { extraction, review };
  };
  const refreshChapterFactsForCurrentDraft = async () => {
    const draft = state.chapterCandidate?.draft;
    const projectId = state.projectId;
    const chapter = Number(state.route?.chapter);
    const routeRevision = state.routeRevision;
    if (!draft) { renderChapterFacts(null, null); return; }
    try {
      const result = await loadChapterFactState(draft);
      const current = state.chapterCandidate?.draft;
      if (state.routeRevision !== routeRevision || state.projectId !== projectId || Number(state.route?.chapter) !== chapter || current?.id !== draft.id || current?.revision !== draft.revision || current?.sha256 !== draft.sha256) return;
      renderChapterFacts(result.extraction, result.review, "");
    } catch (error) {
      const current = state.chapterCandidate?.draft;
      if (state.routeRevision !== routeRevision || current?.id !== draft.id || current?.revision !== draft.revision || current?.sha256 !== draft.sha256) return;
      renderChapterFacts(null, null, UI.errorMessage(error?.payload, error?.status));
    }
  };
  const renderChapterCard = (card, errorMessage = "", errorCode = "") => {
    state.chapterCard = card || null;
    const root = one("[data-chapter-card]");
    const unavailable = one("[data-card-unavailable]");
    if (!card) {
      if (root) root.hidden = true;
      if (unavailable) {
        const alreadyWritten = errorCode === "chapter_card_already_written";
        const previousMissing = errorCode === "chapter_card_previous_chapter_missing";
        const firstChapter = errorCode === "chapter_card_first_chapter_shadow";
        const nextStep = one("[data-card-first-chapter-next]", unavailable);
        nextStep.hidden = !firstChapter;
        if (firstChapter) {
          const covered = state.detail?.overview?.outline_remaining?.covered === true;
          nextStep.href = projectHash(covered ? "production" : "outlines");
          nextStep.textContent = covered ? "去生产页试写" : "准备第一章章纲";
        }
        unavailable.hidden = errorCode === "new_book_outline_needed";
        unavailable.classList.toggle("notice-danger", !alreadyWritten && !previousMissing && !firstChapter);
        unavailable.classList.toggle("notice-warning", previousMissing || firstChapter);
        const heading = unavailable.querySelector("strong");
        if (heading) {
          heading.textContent = alreadyWritten
            ? "本章已提交正文"
            : firstChapter ? "第一章可先按章纲试写" : previousMissing
              ? "还不能开始这一章"
              : "章节卡暂不可用";
        }
      }
      setText("[data-card-unavailable-message]", errorMessage || "无法读取可信章节输入。请先检查项目健康状态与本章章纲。", doc);
      state.chapterConfirmation = null;
      renderChapterConfirmationState(null);
      return;
    }
    if (root) root.hidden = false;
    if (unavailable) unavailable.hidden = true;
    const blueprint = card.blueprint || {};
    setText("[data-outline-title]", blueprint.title || `第 ${card.chapter_index} 章`);
    setText("[data-outline-core]", blueprint.core_event || "—");
    setText("[data-outline-pressure]", blueprint.ending_pressure || "—");
    const beats = one("[data-outline-beats]"); clear(beats);
    arrayFrom(blueprint.required_beats, "items").forEach((beat) => beats.append(el("li", "", beat?.text || "—")));
    if (!beats.children.length) beats.append(el("li", "", "—"));

    const readiness = card.readiness || {};
    UI.updateStatusBadge(one("[data-card-readiness]"), readiness.can_generate ? "ready" : "warning", readiness.can_generate ? "可确认" : "需要补充章纲");
    const issues = one("[data-card-issues]"); clear(issues);
    arrayFrom(readiness.issues, "items").forEach((issue) => {
      const notice = el("div", `notice ${issue.blocking ? "notice-danger" : "notice-warning"}`);
      notice.append(el("strong", "", issue.blocking ? "需要处理" : "提示"), el("p", "", issue.message || issue.code));
      issues.append(notice);
    });

    const language = card.language || {};
    setText("[data-card-language]", language.effective || "—");
    const languageSource = language.source === "event_authority" ? "来自事件权威状态" : "来自当前 StoryProject 生成配置";
    const preference = language.workbench_preference && !language.preference_matches_effective ? `；工作台偏好为 ${language.workbench_preference}` : "";
    setText("[data-card-language-source]", `${languageSource}${preference}`, doc, "");
    const summary = card.context_summary || {};
    setText("[data-card-context-scope]", summary.scope === "explicit" ? "显式精简读取集" : "完整可信上下文（尚未裁剪）");
    const previous = summary.previous_chapter || {};
    setText("[data-card-previous]", previous.index == null ? "本书首章" : previous.present ? `第 ${previous.index} 章已纳入` : `未找到第 ${previous.index} 章正文`);
    setText("[data-card-reference-counts]", `${Number(summary.required_state_item_count || 0)} 项状态 / ${Number(summary.required_event_item_count || 0)} 项事件`);
    setText("[data-card-file-counts]", `${Number(summary.tracking_file_count || 0)} 个追踪文件 / ${Number(summary.setting_file_count || 0)} 个设定文件`);
    const continuity = summary.continuity;
    setText("[data-card-continuity]", continuity ? [continuity.last_scene_location && `地点：${continuity.last_scene_location}`, continuity.last_scene_character_ids?.length && `角色：${continuity.last_scene_character_ids.join("、")}`, continuity.required_opening_bridge && `开篇衔接：${continuity.required_opening_bridge}`].filter(Boolean).join("；") || "已声明，但没有额外要求" : "本章未声明显式连续性读取集");
    const constraints = one("[data-card-constraints]"); clear(constraints);
    arrayFrom(summary.narrative_constraints, "items").forEach((item) => constraints.append(el("li", "", item.instruction || item.constraint_id)));
    if (!constraints.children.length) constraints.append(el("li", "", "无显式条目"));
    const entities = one("[data-card-entities]"); clear(entities);
    arrayFrom(summary.expected_new_entities, "items").forEach((item) => entities.append(el("li", "", `${item.display_name || item.entity_id}${item.kind ? `（${item.kind}）` : ""}`)));
    if (!entities.children.length) entities.append(el("li", "", "无显式条目"));
    const cardSha = card.confirmation?.card_sha256 || "";
    setText("code[data-card-sha]", cardSha ? `sha256:${cardSha}` : "—");

    const form = one("[data-chapter-card-confirm-form]");
    const projectId = state.projectId;
    form.action = `/api/projects/${encodeURIComponent(projectId)}/chapters/${card.chapter_index}/card/confirm`;
    form.dataset.projectId = projectId;
    form.dataset.chapter = String(card.chapter_index);
    form.dataset.cardSha = cardSha;
    form.dataset.inputRevision = String(Number(form.dataset.inputRevision || 0) + 1);
    form.dataset.submissionNonce = String(Number(form.dataset.submissionNonce || 0) + 1);
    form.removeAttribute("aria-busy");
    all(".is-busy", form).forEach((element) => element.classList.remove("is-busy"));
    const errorBox = one("[data-form-error]", form);
    if (errorBox) { errorBox.hidden = true; errorBox.textContent = ""; }
    one('input[name="expected_card_sha256"]', form).value = cardSha;
    const stored = readChapterConfirmation(projectId, card.chapter_index, card);
    const intent = stored?.chapter_intent || card.intent_defaults || {};
    one('textarea[name="goal"]', form).value = intent.goal || "";
    const selectedFocus = new Set(arrayFrom(intent.validation_focus, "items"));
    all('input[name="validation_focus"]', form).forEach((input) => { input.checked = selectedFocus.has(input.value); });
    one('textarea[name="notes_text"]', form).value = arrayFrom(intent.notes, "items").join("\n");
    all("input, textarea, button", form).forEach((field) => { field.disabled = !readiness.can_generate; });
    state.chapterConfirmation = stored;
    renderChapterConfirmationState(stored);
  };

  const renderChapter = (chapter) => {
    const number = Number(chapter.number ?? chapter.chapter ?? chapter.chapter_index ?? 0);
    const title = chapter.chapter_card?.blueprint?.title || chapter.title || "未命名章节";
    const hasProse = chapter.has_prose ?? Boolean(chapter.content || chapter.prose || chapter.current_prose || chapter.formal_prose);
    const memory = chapter.chapter_memory, panel = one("[data-next-memory]");
    if (panel) {
      panel.hidden = !memory;
      for (const [selector, lines, empty] of [
        ["[data-next-settings]", memory?.settings, "没有填写开篇设定。"],
        ["[data-next-facts]", memory?.previous_facts?.map(f => `${f.summary}　正文：${f.quotes.map(q => `“${q}”`).join("；")}`),
          number === 1 ? "这是第一章，没有前章事实。" : "上一章没有接受的事实。"]]) {
        const list = one(selector, panel); clear(list);
        for (const line of lines?.length ? lines : [empty]) list.append(el("li", "", line));
      }
    }
    const outlineForm = one("[data-next-author-outline]");
    if (outlineForm) {
      const scope = `${state.projectId}:${number}`;
      if (outlineForm.dataset.scope !== scope) { outlineForm.reset(); one("[data-outline-start-error]").textContent = ""; }
      outlineForm.dataset.scope = scope;
      outlineForm.hidden = !(state.project?.capabilities?.new_book_outline && number === state.project.next_chapter && !chapter.has_outline && !hasProse);
    }
    state.chapterHasFormalProse = Boolean(hasProse);
    state.chapterRecoveryRequired = chapter.reviewed_status?.state === "needs_recovery";
    setText("[data-chapter-kicker]", `第 ${number} 章`);
    setText("[data-chapter-title]", title);
    setText("[data-chapter-english-number]", `CHAPTER ${String(number).padStart(2, "0")}`);
    setText("[data-chapter-meta]", `${chapter.word_count_label || (chapter.word_count ? `${numberLabel(chapter.word_count)} 字` : "—")} · ${chapter.committed_at_label || chapter.updated_at_label || "—"}`);
    const currentProse = chapter.content || chapter.prose || chapter.current_prose || chapter.formal_prose || "";
    const emptyProseCopy = state.project?.reviewed_format
      ? "尚未生成正文。请先在“章节卡”中核对并确认本章意图。"
      : (Number(state.project?.next_chapter) > 0 && Number(state.project.next_chapter) < number
        ? `尚未生成正文。请先到生产页续写第 ${state.project.next_chapter} 章。`
        : "尚未生成正文。请到生产页使用「预检并开始」续写。");
    setText("[data-chapter-content]", currentProse || (chapter.outline ? `${chapter.outline}\n\n——\n${emptyProseCopy}` : emptyProseCopy));
    const retainedDraft = chapter.retained_author_draft && chapter.retained_author_draft.state !== "archived"
      ? chapter.retained_author_draft
      : null;
    const retainedPanel = one('[data-retained-author-draft-panel]');
    if (retainedPanel) retainedPanel.hidden = !retainedDraft;
    const retainedText = one('[data-retained-author-draft-text]');
    if (retainedText) retainedText.value = retainedDraft?.text || '';
    setText('[data-retained-author-draft-meta]', retainedDraft ?
      `作者草稿第 ${number} 章 · r${retainedDraft.revision} · SHA ${retainedDraft.sha256}` : '');
    const chapterInspectHost = one("[data-spa-view='chapter']") || one('[data-spa-view="chapter"]');
    renderRetainedPaidDraftInspect({
      chapter: number,
      retained_paid_draft_preview: hasProse ? null : chapter.retained_paid_draft_preview,
    }, chapterInspectHost);
    setText("[data-chapter-revision]", `r${chapter.revision || 1}`);
    setText("[data-chapter-sha]", chapter.sha || chapter.sha256 || chapter.current_sha256 || "—");
    UI.updateStatusBadge(one("[data-chapter-status]"), chapter.status || (hasProse ? "committed" : "draft"), chapter.status_label || (hasProse ? "已提交" : "待生成"));
    const proseActions = one("[data-prose-actions]"); if (proseActions) proseActions.hidden = !hasProse;
    const amendmentPanel = one("[data-amendment-panel]"); if (amendmentPanel) amendmentPanel.hidden = !hasProse || chapter.reviewed_format === true;
    renderChapterCard(chapter.chapter_card, chapter.chapter_card_error, chapter.chapter_card_error_code);
    window.NovelAgentChapterBudget?.setContext(chapter.reviewed_format ? {
      projectId: state.projectId, bookId: state.project.book_id, chapter: number,
      contextSha: chapter.chapter_card?.binding?.context_sha256,
      canFreeze: !hasProse && !state.chapterRecoveryRequired
    } : null, (blocked, message) => {
      state.chapterBudgetBlocked = blocked;
      state.chapterBudgetMessage = message || '';
      if (!state.chapterCandidate && !state.chapterCandidateError) renderChapterCandidate(null, '');
      syncChapterModelButtons();
      if (state.providerReady && !state.chapterFactError && !state.chapterFactExtraction && !state.chapterFactReview) {
        UI.updateStatusBadge(one('[data-fact-status]'), blocked ? 'warning' : 'ready', blocked ? '可自行审阅' : '可审阅');
        setText('[data-fact-message]', blocked ? '模型提取受预算限制；仍可自行审阅事实，旧占用和未知费用继续保留。' : '可从已保存草稿提取事实建议，也可自行添加并审阅。');
      }
      renderChapterWorkflow();
    });
    renderChapterCandidate(chapter.chapter_candidate, chapter.chapter_candidate_error || "");
    renderChapterFacts(chapter.chapter_fact_extraction, chapter.chapter_fact_review, chapter.chapter_fact_error || "");
    if (!hasProse) one('[data-tab="outline"]')?.click();
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
    const displayedVersions = versionItems.length ? versionItems : hasProse ? [{ label: "原始提交", revision: chapter.revision || 1, sha: chapter.sha || chapter.sha256, is_current: true }] : [];
    displayedVersions.forEach((version) => {
      const item = el("li", version.is_current ? "is-current" : ""); item.append(el("span", "version-node"));
      const copy = el("div"); const heading = el("div", "version-title"); heading.append(el("strong", "", version.label || `r${version.revision}`)); if (version.is_current) heading.append(el("span", "", "当前"));
      copy.append(heading, el("p", "", version.summary || ""), el("small", "", `r${version.revision || 1} · ${version.created_at_label || ""} · ${(version.sha || "—").slice(0, 12)}`)); item.append(copy); versions.append(item);
    });
    if (!versions.children.length) versions.append(el("li", "version-empty", "尚无正式正文版本。"));
    const form = one("[data-amendment-form]");
    form.action = `/api/chapters/${number}/amendments`;
    form.dataset.casRevision = String(chapter.revision || 1);
    const currentSha = chapter.sha || chapter.sha256 || chapter.current_sha256 || "";
    form.dataset.casSha = currentSha;
    one('input[name="project_id"]', form).value = state.projectId;
    one('input[name="expected_base_sha256"]', form).value = currentSha;
    one('textarea[name="text"]', form).value = currentProse;
  };

  const loadChapterCandidate = async (projectId, chapterNumber) => {
    const collection = candidateCollectionPath(projectId, chapterNumber);
    const { payload } = await api(collection);
    const items = arrayFrom(payload, "items", "candidates");
    const latest = items[0];
    if (!latest) return null;
    const candidateId = latest.candidate_id || latest.id;
    if (typeof candidateId !== "string" || !/^candidate_[0-9a-f]{40}$/.test(candidateId)) {
      throw { status: 500, payload: { error: { message: "Candidate 列表返回了无效标识。" } } };
    }
    const detail = await api(candidateDetailPath(projectId, chapterNumber, candidateId));
    return {...(detail.payload?.candidate || detail.payload), fragmentHistory: items.flatMap(fragmentIndex)};
  };

  one("[data-next-author-outline]")?.addEventListener("submit", withFeedback(async event => {
    event.preventDefault();
    const form = event.currentTarget, project = state.projectId, number = Number(state.route.chapter);
    const button = form.querySelector("button"), error = one("[data-outline-start-error]");
    button.disabled = true; error.textContent = "";
    const data = Object.fromEntries(new FormData(form));
    data.required_beats = data.required_beats.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
    try {
      await api(`/api/projects/${encodeURIComponent(project)}/chapters/${number}/author-outline`, {method:"POST", json:data});
      if (state.projectId === project && Number(state.route?.chapter) === number) await loadChapter(number);
    } catch (e) { error.textContent = UI.errorMessage(e.payload, e.status); }
    finally { button.disabled = false; }
  }));
  const loadChapter = async (number, expectedRouteRevision = null) => {
    const projectId = state.projectId;
    const chapterNumber = Number(number);
    let chapter;
    try {
      const { payload } = await api(`/api/projects/${encodeURIComponent(projectId)}/chapters/${chapterNumber}`);
      chapter = payload && typeof payload.chapter === "object" ? payload.chapter : payload;
    } catch (error) {
      if (error.status !== 404) throw error;
      const { payload } = await api(`/api/projects/${encodeURIComponent(projectId)}/chapters`);
      const chapters = arrayFrom(payload, "items", "chapters");
      chapter = chapters.find((item) => Number(item.number ?? item.chapter ?? item.chapter_index) === chapterNumber);
      if (!chapter) throw error;
    }
    if (state.project?.capabilities?.new_book_outline && !chapter.has_outline && !chapter.has_prose && chapterNumber === state.project.next_chapter) {
      if (state.projectId === projectId && state.route?.area === "chapter" && Number(state.route.chapter) === chapterNumber)
        renderChapter({...chapter, chapter_card_error_code:"new_book_outline_needed", chapter_card_error:"请先填写上方的本章章纲。"});
      return;
    }
    if (chapter.reviewed_format && (chapter.reviewed_status?.state === "needs_recovery" || chapter.has_prose)) {
      if (state.projectId === projectId && state.route?.area === "chapter" && Number(state.route.chapter) === chapterNumber) {
        renderChapter({ ...chapter, chapter_card: null, chapter_card_error: chapter.has_prose
          ? "本章已发布；请在下一章确认新的写作意图。" : chapter.reviewed_status.message });
      }
      return;
    }
    const hasFormalProse = chapter.has_prose ?? Boolean(chapter.content || chapter.prose || chapter.current_prose || chapter.formal_prose);
    if (hasFormalProse) {
      // Generation cards are only for unwritten chapters. Asking the builder
      // after formal prose exists always fails, and must not be shown as a
      // project-health or outline problem.
      chapter.chapter_card = null;
      chapter.chapter_card_error = "本章已有正式正文，无需再确认生成输入。";
      chapter.chapter_card_error_code = "chapter_card_already_written";
    } else {
      try {
        const { payload } = await api(`/api/projects/${encodeURIComponent(projectId)}/chapters/${chapterNumber}/card`);
        chapter.chapter_card = payload.chapter_card || payload;
        chapter.chapter_card_error = "";
        chapter.chapter_card_error_code = "";
      } catch (error) {
        chapter.chapter_card = null;
        chapter.chapter_card_error = UI.errorMessage(error?.payload, error?.status);
        chapter.chapter_card_error_code = error?.payload?.error?.code || "";
      }
    }
    try {
      chapter.chapter_candidate = await loadChapterCandidate(projectId, chapterNumber);
      chapter.chapter_candidate_error = "";
    } catch (error) {
      chapter.chapter_candidate = null;
      chapter.chapter_candidate_error = UI.errorMessage(error?.payload, error?.status);
    }
    if (chapter.chapter_candidate?.draft) {
      try {
        const facts = await loadChapterFactState(chapter.chapter_candidate.draft);
        chapter.chapter_fact_extraction = facts.extraction;
        chapter.chapter_fact_review = facts.review;
        chapter.chapter_fact_error = "";
      } catch (error) {
        chapter.chapter_fact_extraction = null;
        chapter.chapter_fact_review = null;
        chapter.chapter_fact_error = UI.errorMessage(error?.payload, error?.status);
      }
    } else {
      chapter.chapter_fact_extraction = null;
      chapter.chapter_fact_review = null;
      chapter.chapter_fact_error = "";
    }
    const stillCurrent = state.projectId === projectId
      && state.route?.area === "chapter"
      && state.route?.projectId === projectId
      && Number(state.route?.chapter) === chapterNumber
      && (expectedRouteRevision == null || state.routeRevision === expectedRouteRevision);
    if (!stillCurrent) return;
    renderChapter(chapter);
  };

  const renderOutlineBatch = (batch) => {
    const editor = one("[data-outline-editor]");
    if (batch && state.batch?.id === batch.id && editor.classList.contains("is-dirty")) return;
    editor.classList.remove("is-dirty");
    setText("[data-save-state]", "所有修改已保存", editor);
    state.batch = batch;
    const createPanel = one("[data-outline-create-panel]");
    const nextChapter = Number(state.project?.next_chapter || 0);
    const approvedRemaining = Boolean(batch) && batch.status === "approved" && Number(batch.start_chapter || 0) <= nextChapter && nextChapter <= Number(batch.end_chapter || 0);
    const remainingCount = approvedRemaining ? Number(batch.end_chapter) - nextChapter + 1 : 0;
    updateOutlinePlanningCopy(approvedRemaining ? { covered: true, start_chapter: nextChapter, end_chapter: Number(batch.end_chapter), chapter_count: remainingCount } : null);
    createPanel.hidden = Boolean(batch);
    editor.hidden = !batch;
    const createForm = one("[data-create-outline-form]");
    createForm.action = `/api/projects/${encodeURIComponent(state.projectId)}/outline-batches`;
    const manual = one("[data-manual-outline]");
    if (manual.dataset.project !== state.projectId || manual.dataset.chapter !== String(nextChapter)) manual.reset();
    manual.dataset.project = state.projectId;
    manual.dataset.chapter = String(nextChapter);
    manual.action = createForm.action;
    manual.elements.namedItem("chapters.0.chapter_index").value = nextChapter;
    const savebar = one(".sticky-savebar", editor);
    if (savebar) savebar.hidden = approvedRemaining;
    const remainingNote = one("[data-outline-remaining-note]", editor);
    const continueLink = one("[data-outline-continue-production]", editor);
    if (remainingNote) {
      remainingNote.hidden = !approvedRemaining;
      remainingNote.textContent = approvedRemaining
        ? (remainingCount === 1 ? `当前已批准章纲还可写第 ${nextChapter} 章。以下内容已冻结，可直接去生产页续写。` : `当前已批准章纲还可写第 ${nextChapter}–${batch.end_chapter} 章，共 ${remainingCount} 章。以下内容已冻结，可直接去生产页续写。`)
        : "";
    }
    if (continueLink) {
      continueLink.hidden = !approvedRemaining;
      continueLink.href = projectHash("production");
    }
    if (!batch) return;
    editor.action = `/api/outline-batches/${encodeURIComponent(batch.id)}`;
    editor.dataset.casRevision = String(batch.revision || 1);
    editor.dataset.casSha = batch.proposal_sha256 || batch.sha256 || "";
    one('input[name="expected_revision"]', editor).value = String(batch.revision || 1);
    one('input[name="expected_sha256"]', editor).value = batch.proposal_sha256 || batch.sha256 || "";
    setText("[data-batch-direction]", batch.direction || batch.metadata?.direction || "以总纲和当前权威状态为主。");
    setText("[data-batch-direction-title]", approvedRemaining ? "已批准并冻结" : "推进当前主线");
    setText("[data-batch-revision]", `r${batch.revision || 1}`);
    const items = arrayFrom(batch.items || batch.proposal, "items");
    const start = batch.start_chapter || items[0]?.chapter_index || state.project?.next_chapter || 1;
    setText("[data-batch-range]", approvedRemaining
      ? (Number(start) === Number(batch.end_chapter) ? `已批准第 ${start} 章` : `已批准第 ${start}–${batch.end_chapter || start} 章，还可写第 ${nextChapter}–${batch.end_chapter} 章`)
      : items.length === 1 ? `第 ${start} 章` : `第 ${start}–${batch.end_chapter || start + items.length - 1} 章`);
    const stack = one("[data-outline-stack]"); clear(stack);
    const template = one("#outline-card-template");
    setText("[data-spa-approve-outline]", `确认并发布 ${items.length} 章章纲`, editor);
    for (let index = 0; index < items.length; index += 1) {
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
      if (approvedRemaining) {
        card.classList.add("is-frozen");
        card.querySelectorAll("input, textarea").forEach((field) => { field.readOnly = true; });
        const status = document.createElement("small");
        status.className = "outline-card-status";
        status.textContent = number < nextChapter ? "已提交正文" : "待生产";
        card.querySelector(".outline-card-header")?.append(status);
        if (number < nextChapter) card.classList.add("is-written");
      }
      stack.append(fragment);
    }
  };

  const loadOutlines = async () => {
    const projectId = state.projectId;
    const routeRevision = state.routeRevision;
    const { payload } = await api(`/api/projects/${encodeURIComponent(state.projectId)}/outline-batches`);
    const batches = arrayFrom(payload, "items", "batches");
    const nextChapter = Number(state.project?.next_chapter || 0);
    const isCurrentDraftRange = (item) => Number(item?.start_chapter || 0) === nextChapter;
    const coversNextChapter = (item) => (
      (item.status || "approved") === "approved"
      && Number(item.start_chapter) <= nextChapter
      && Number(item.end_chapter) >= nextChapter
      && item.approval_manifest_path
      && item.approval_receipt_sha256
    );
    const coveringApproved = (payload.batch && coversNextChapter(payload.batch) ? payload.batch : null)
      || batches.find((item) => coversNextChapter(item))
      || null;
    let batch = coveringApproved
      || (payload.batch && ["draft", "ready"].includes(payload.batch.status) && isCurrentDraftRange(payload.batch) ? payload.batch : null)
      || batches.find((item) => ["draft", "ready"].includes(item.status) && isCurrentDraftRange(item))
      || null;
    if (batch?.id && !batch.items && !batch.proposal) {
      const detail = await api(`/api/outline-batches/${encodeURIComponent(batch.id)}`);
      batch = detail.payload.batch || detail.payload;
    }
    if (projectId === state.projectId && routeRevision === state.routeRevision && state.route?.area === "outlines") renderOutlineBatch(batch);
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

  const isActionableCohort = (item) => Boolean(item) && item.status === "active";

  const productionCohortView = (cohorts) => {
    const items = Array.isArray(cohorts) ? cohorts : [];
    const actionable = items.find(isActionableCohort) || null;
    return {
      actionable,
      historical: items.filter((item) => !isActionableCohort(item)),
      expandScoring: Boolean(actionable)
    };
  };

  const cohortHistoryStatusLabel = (status) => ({
    active: "进行中",
    passed: "已通过",
    failed: "未通过",
    invalidated: "已作废"
  }[status] || "已结束");

  const formatCohortHistoryDate = (value) => {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit"
    }).format(date);
  };

  const renderCohortHistory = (historical) => {
    const historyPanel = one("[data-cohort-history]");
    const historyList = one("[data-cohort-history-list]");
    const historySummary = one("[data-cohort-history-summary]");
    if (!historyPanel || !historyList) return;
    clear(historyList);
    if (!historical.length) {
      historyPanel.hidden = true;
      if (historySummary) historySummary.textContent = "历史试验记录";
      return;
    }
    historyPanel.hidden = false;
    if (historySummary) historySummary.textContent = `历史试验记录（${historical.length} 轮，均已结束）`;
    historical.forEach((item) => {
      const li = el("li", "cohort-history-item");
      const range = el("strong", "", `第 ${item.start_chapter}–${item.end_chapter} 章`);
      const badge = el("span", `cohort-history-status is-${item.status || "closed"}`, cohortHistoryStatusLabel(item.status));
      const closed = formatCohortHistoryDate(item.closed_at || item.updated_at || item.created_at);
      li.append(range, badge);
      if (closed) li.append(el("span", "muted-copy", closed));
      historyList.append(li);
    });
  };

  const renderCohorts = async (payload) => {
    const cohorts = arrayFrom(payload.cohorts, "items");
    state.scoreDimensions = payload.score_dimensions || Object.keys(scoreDimensionLabels);
    const officialLaunch = payload.official_launch || {};
    UI.updateStatusBadge(one("[data-product-readiness]"), "idle", officialLaunch.badge_label || "上线不以十章验收轮次为准");
    const view = productionCohortView(cohorts);
    const createPanel = one("[data-cohort-create-panel]");
    const activePanel = one("[data-cohort-active]");
    const createForm = one("[data-cohort-create-form]");
    createForm.action = `/api/projects/${encodeURIComponent(state.projectId)}/cohorts`;
    // Optional historical experiment only: never prefill the official next chapter,
    // so this form cannot be mistaken for continuing the book.
    createPanel.hidden = Boolean(view.actionable);
    // Auto-expand scoring only for an in-progress trial the author must handle now.
    // Historical / failed / invalidated / closed trials stay in collapsed history.
    activePanel.hidden = !view.expandScoring;
    if (view.expandScoring) await loadCohort(view.actionable.id);
    else { state.cohort = null; state.cohortLiveResult = null; }
    renderCohortHistory(view.historical);
  };


  const approvedBatchWindow = (batches, start) => {
    const chapter = Number(start);
    const batch = arrayFrom(batches, "items").find((item) => (
      (item.status || "approved") === "approved"
      && Number(item.start_chapter) <= chapter
      && Number(item.end_chapter) >= chapter
      && item.approval_manifest_path
      && item.approval_receipt_sha256
    ));
    if (!batch) {
      return { start_chapter: chapter, chapter_count: 0, end_chapter: null, max_chapter_count: 0, covered: false };
    }
    const end = Number(batch.end_chapter);
    const remaining = end - chapter + 1;
    return {
      start_chapter: chapter,
      chapter_count: remaining,
      end_chapter: end,
      max_chapter_count: remaining,
      outline_batch_id: batch.id,
      outline_batch_start: Number(batch.start_chapter),
      outline_batch_end: end,
      covered: true
    };
  };

  function updateSessionLaunchButton(form) {
    const submit = one('button[type="submit"]', form);
    submit.disabled = !state.providerReady || form.dataset.outlineAvailable !== "true";
    submit.title = !state.providerReady ? "请先配置模型" : form.dataset.outlineAvailable !== "true" ? "请先确认本章章纲" : "";
  }

  const settingsDialog = one('#writing-model-dialog');
  const settingsForm = one('#writing-model-form');
  let settingsView = 0;
  const settingsError = msg => { const box = one('[data-writing-settings-error]'); box.textContent = msg; box.hidden = !msg; };
  all('[data-open-model-help]').forEach(button => button.addEventListener('click', withFeedback(async () => {
    const view = ++settingsView;
    settingsForm.reset(); settingsError(''); const submit = settingsForm.querySelector('[type=submit]'); submit.disabled = true;
    const fields = settingsForm.elements;
    settingsDialog.showModal();
    try {
      const {payload} = await api('/api/settings/writing-model');
      if (view !== settingsView) return;
      fields.model.value = payload.model || ''; fields.base_url.value = payload.base_url || '';
      const originalUrl = fields.base_url.value.trim().replace(/\/+$/, '');
      const updateKey = () => {
        const keep = payload.has_key && originalUrl === fields.base_url.value.trim().replace(/\/+$/, '');
        fields.api_key.required = !keep;
        setText('[data-writing-key-hint]', keep ? '已配置密钥。留空保留；更换服务地址时须填写新密钥。' : '请填写此服务的密钥；保存后不会回显。');
      };
      fields.base_url.oninput = updateKey; updateKey();
      if (payload.source === 'unreadable') settingsError('旧设置无法读取，请重新填写后保存。');
      submit.disabled = false;
    } catch (e) { if (view === settingsView) settingsError(UI.errorMessage(e.payload, e.status)); }
  })));
  settingsDialog?.addEventListener('close', () => { ++settingsView; settingsForm.reset(); settingsForm.elements.api_key.value = ''; });
  settingsForm?.addEventListener('submit', withFeedback(async event => {
    event.preventDefault(); const view = settingsView; const fields = settingsForm.elements;
    const submit = settingsForm.querySelector('[type=submit]'); if (submit.disabled) return;
    submit.disabled = true; settingsError('');
    try {
      const {payload} = await api('/api/settings/writing-model', {method:'PUT', json:{model:fields.model.value, base_url:fields.base_url.value, api_key:fields.api_key.value}});
      state.providerRevision++; updateProvider({ready:payload.has_key});
      if (state.route?.area === 'chapter') {
        renderQuote(); renderChapterWorkflow();
        const c = candidateStatusPresentation(state.chapterCandidate, state.chapterCandidateError);
        setText('[data-candidate-title]', c.title); setText('[data-candidate-message]', c.message); UI.updateStatusBadge(one('[data-candidate-status]'), c.status, c.label);
        if (!state.chapterFactReview && !state.chapterFactExtraction && state.chapterCandidate?.draft) renderChapterFacts(null, null, state.chapterFactError);
      }
      if (view === settingsView) settingsDialog.close();
      UI.showToast('模型设置已保存。下次调用使用新配置；尚未验证连通性。');
    } catch (e) { if (view === settingsView) settingsError(UI.errorMessage(e.payload, e.status)); }
    finally { if (view === settingsView) submit.disabled = false; }
  }));

  const applySessionOutlineWindow = (form, payload, startOverride) => {
    const startInput = one('input[name="start_chapter"]', form);
    const start = Number(
      startOverride
      || payload.session_defaults?.start_chapter
      || payload.project?.next_chapter
      || payload.next_chapter
      || state.project?.next_chapter
      || 1
    );
    if (startInput && startOverride == null) {
      startInput.value = String(start);
      startInput.oninput = () => applySessionOutlineWindow(form, payload, Number(startInput.value) || start);
    }
    const defaults = approvedBatchWindow(payload.approved_batches, start);
    const select = one('select[name="chapter_count"]', form);
    const note = one("[data-session-window-note]", form);
    const maxCount = Number(defaults.max_chapter_count || 0);
    if (select) {
      const options = [];
      if (maxCount >= 1) options.push(1);
      if (maxCount >= 4) options.push(4);
      if (maxCount >= 10) options.push(10);
      if (maxCount > 0 && !options.includes(maxCount)) options.push(maxCount);
      options.sort((a, b) => a - b);
      select.innerHTML = options.map((count) => `<option value="${count}">${count} 章</option>`).join("");
      if (maxCount > 0) select.value = String(maxCount);
      select.disabled = maxCount < 1;
    }
    form.dataset.outlineAvailable = String(maxCount >= 1);
    updateSessionLaunchButton(form);
    if (note) {
      note.textContent = maxCount < 1
        ? "下一章还没有已批准且冻结的章纲，不能开始生产。"
        : maxCount === 1
          ? `当前已批准章纲还可写第 ${start} 章，共 1 章。`
          : `当前已批准章纲还可写第 ${start}–${defaults.end_chapter} 章，共 ${maxCount} 章。`;
    }
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
    updateProvider(payload.provider || {});
    applySessionOutlineWindow(form, payload);
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
    const staleSceneEvidence = paused && !provenanceFailure && !contextBudget && !repairConvergence && !repairAuthority
      ? (staleSceneEvidenceDetailsOf(session) || staleSceneEvidenceDetailsOfRun(payload.chapter_recovery?.latest_run))
      : null;
    const sceneBoundaryValidation = paused && !provenanceFailure && !contextBudget && !repairConvergence && !repairAuthority && !staleSceneEvidence
      ? (sceneBoundaryValidationDetailsOf(session) || sceneBoundaryValidationDetailsOfRun(payload.chapter_recovery?.latest_run))
      : null;
    const qualityRejection = paused && !provenanceFailure && !contextBudget && !repairConvergence && !repairAuthority && !staleSceneEvidence && !sceneBoundaryValidation
      && payload.chapter_recovery?.formal_prose_exists === false
      ? qualityRejectionDetailsOfRun(payload.chapter_recovery?.latest_run)
      : null;
    const qualityRevision = paused && !provenanceFailure && !contextBudget && !repairConvergence && !repairAuthority && !staleSceneEvidence && !sceneBoundaryValidation && (Boolean(qualityRejection) || isQualityRevisionPause(session));
    const sceneSchemaValidation = paused && !provenanceFailure && !contextBudget && !repairConvergence && !repairAuthority && !staleSceneEvidence && !sceneBoundaryValidation
      ? (sceneSchemaValidationDetailsOf(session) || sceneSchemaValidationDetailsOfRun(payload.chapter_recovery?.latest_run))
      : null;
    const bridgePrevalidation = paused && !provenanceFailure && !contextBudget && !repairConvergence && !repairAuthority && !staleSceneEvidence && !sceneBoundaryValidation && !sceneSchemaValidation
      ? (bridgePrevalidationDetailsOf(session) || bridgePrevalidationDetailsOfRun(payload.chapter_recovery?.latest_run))
      : null;
    const providerPartialResponse = paused && !provenanceFailure && !contextBudget && !repairConvergence && !repairAuthority && !staleSceneEvidence && !sceneBoundaryValidation && !sceneSchemaValidation && !bridgePrevalidation
      ? (providerPartialResponseDetailsOf(session) || providerPartialResponseDetailsOfRun(payload.chapter_recovery?.latest_run))
      : null;
    const localBudgetAfterProvider = paused && !provenanceFailure && !contextBudget && !repairConvergence && !repairAuthority && !staleSceneEvidence && !sceneBoundaryValidation && !sceneSchemaValidation && !bridgePrevalidation && !providerPartialResponse
      ? (localBudgetAfterProviderDetailsOf(session) || localBudgetAfterProviderDetailsOfRun(payload.chapter_recovery?.latest_run))
      : null;
    const contextBudgetAfterProvider = paused && !provenanceFailure && !contextBudget && !repairConvergence && !repairAuthority && !staleSceneEvidence && !sceneBoundaryValidation && !sceneSchemaValidation && !bridgePrevalidation && !providerPartialResponse && !localBudgetAfterProvider
      ? (contextBudgetAfterProviderDetailsOf(session) || contextBudgetAfterProviderDetailsOfRun(payload.chapter_recovery?.latest_run))
      : null;
    const structuredContextAfterProvider = paused && !provenanceFailure && !contextBudget && !repairConvergence && !repairAuthority && !staleSceneEvidence && !sceneBoundaryValidation && !sceneSchemaValidation && !bridgePrevalidation && !providerPartialResponse && !localBudgetAfterProvider && !contextBudgetAfterProvider
      ? (structuredContextAfterProviderDetailsOf(session) || structuredContextAfterProviderDetailsOfRun(payload.chapter_recovery?.latest_run))
      : null;
    const providerUncertain = paused && !provenanceFailure && !contextBudget && !repairConvergence && !repairAuthority && !staleSceneEvidence && !sceneBoundaryValidation && !sceneSchemaValidation && !bridgePrevalidation && !providerPartialResponse && !localBudgetAfterProvider && !contextBudgetAfterProvider && !structuredContextAfterProvider && !qualityRejection && jobStatus === "needs_recovery";
    const polishedDraftReady = durablePolishedDraftAvailable(payload.chapter_recovery || session);
    const retainedDraftReady = retainedCompleteDraftAvailable(payload.chapter_recovery || session);
    const resetCleared = (payload.chapter_recovery?.reset_cleared === true || payload.chapter_recovery?.checkpoint?.action === "reset")
      && payload.chapter_recovery?.formal_prose_exists !== true
      && !polishedDraftReady
      && !retainedDraftReady;
    const stalledAfterReset = paused && resetCleared;
    launch.hidden = visible;
    sessionPanel.hidden = !visible;
    const statusLabel = provenanceFailure ? (provenanceReady ? "等待作者确认" : provenanceFailure.verified ? "等待系统修复" : "安全核对中") : contextBudget ? (contextBudgetReady ? "等待作者确认" : contextBudget.verified ? "等待系统修复" : "安全核对中") : contextBudgetAfterProvider ? "等待无模型恢复" : repairConvergence ? (repairConvergence.verified ? "等待无模型恢复" : "安全核对中") : repairAuthority ? "等待安全恢复" : staleSceneEvidence ? "等待无模型恢复" : sceneBoundaryValidation ? "等待无模型恢复" : qualityRevision ? "质量审核需修订" : paused ? (providerUncertain ? "需要安全恢复" : "等待作者处理") : session?.status_label;
    UI.updateStatusBadge(one("[data-production-status]"), visible ? jobStatus || "generating" : "idle", visible ? statusLabel : "空闲");
    const currentChapter = session?.current_chapter || session?.next_chapter;
    setText("[data-production-title]", provenanceFailure ? (provenanceFailure.verified ? `第 ${currentChapter || "—"} 章在模型请求前停止` : `第 ${currentChapter || "—"} 章正在安全核对`) : contextBudget ? `第 ${currentChapter || "—"} 章输入上下文超出安全上限` : contextBudgetAfterProvider ? `第 ${currentChapter || "—"} 章在已付费调用后因上下文预算中断` : structuredContextAfterProvider ? `第 ${currentChapter || "—"} 章在已付费调用后因结构化上下文装配中断` : repairConvergence ? (repairConvergence.verified ? `第 ${currentChapter || "—"} 章自动修订未收敛` : `第 ${currentChapter || "—"} 章修订故障正在安全核对`) : repairAuthority ? `第 ${currentChapter || "—"} 章修订上下文装配失败` : staleSceneEvidence ? `第 ${currentChapter || "—"} 章自动修订因场景证据对不齐而停止` : sceneBoundaryValidation ? `第 ${currentChapter || "—"} 章因场景边界校验失败而停止` : qualityRevision ? `第 ${currentChapter || "—"} 章质量审核需修订` : paused ? `第 ${currentChapter || "—"} 章已安全暂停` : visible ? `正在写第 ${currentChapter || "—"} 章` : "开始新的生产批次");
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
    setText("[data-session-chapter]", provenanceFailure ? `第 ${currentChapter || "—"} 章 · ${provenanceReady ? "Git 环境已修复，等待确认" : provenanceFailure.verified ? "等待系统修复 Git 环境" : "疑似 Git 环境错误，安全核对中"}` : contextBudget ? `第 ${currentChapter || "—"} 章 · ${contextBudgetReady ? "修复已完成，等待确认" : contextBudget.verified ? "等待系统修复" : "安全核对中"}` : contextBudgetAfterProvider ? `第 ${currentChapter || "—"} 章 · 已付费调用已结算，上下文预算中断，正式正文未提交` : structuredContextAfterProvider ? `第 ${currentChapter || "—"} 章 · 已付费调用已结算，结构化上下文装配中断，正式正文未提交` : repairConvergence ? `第 ${currentChapter || "—"} 章 · ${repairConvergence.verified ? "候选正文已保留，等待无模型恢复" : "调用与产物仍在安全核对"}` : repairAuthority ? `第 ${currentChapter || "—"} 章 · 候选正文已保留，修订请求尚未发出` : staleSceneEvidence ? `第 ${currentChapter || "—"} 章 · 候选正文已保留，越界修订已被拒绝` : sceneBoundaryValidation ? `第 ${currentChapter || "—"} 章 · 场景边界校验未通过，正式正文未提交` : qualityRevision ? `第 ${currentChapter || "—"} 章 · 质量审核需修订` : paused ? `第 ${currentChapter || "—"} 章 · 已停止自动执行` : `第 ${currentChapter || "—"} 章 · ${session.chapter_title || "正在按冻结章纲生成"}`);
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
    const closePausedForm = one("[data-close-paused-job-form]");
    closePausedForm.action = `/api/jobs/${encodeURIComponent(jobId)}/cancel`;
    closePausedForm.hidden = !stalledAfterReset;
    const closePausedButton = one("button[type='submit']", closePausedForm);
    if (closePausedButton) {
      closePausedButton.classList.toggle("button-primary", stalledAfterReset);
      closePausedButton.classList.toggle("button-secondary", !stalledAfterReset);
      closePausedButton.textContent = "关闭这次停住的生产任务";
    }
    if (stalledAfterReset) {
      closePausedForm.dataset.confirm = `第 ${currentChapter || "—"} 章失败尝试的锁已经解除。确认关闭这次已停住的生产任务，以便重新开写？不会删除失败证据，也不会改写正式正文。`;
      closePausedForm.dataset.successMessage = "已关闭这次停住的生产任务；运行证据已保留，可以重新开写。";
    }
    const safetyPanel = one("[data-production-controls]");
    const attentionPanel = one("[data-production-attention]");
    if (safetyPanel) safetyPanel.hidden = paused;
    if (attentionPanel) attentionPanel.hidden = !paused;
    setText("[data-production-attention-eyebrow]", stalledAfterReset ? "失败锁已解除" : polishedDraftReady ? "已有完整润色稿" : retainedDraftReady ? "已保留完整候选稿" : provenanceFailure ? "本机运行环境错误" : contextBudget ? "本地上下文超限" : contextBudgetAfterProvider ? "已付费后上下文预算中断" : structuredContextAfterProvider ? "已付费后结构化上下文中断" : repairConvergence ? "自动修订未收敛" : repairAuthority ? "修订上下文装配错误" : staleSceneEvidence ? "修订跨越场景边界" : sceneBoundaryValidation ? "场景边界校验未通过" : qualityRevision ? "质量审核需修订" : "已安全暂停");
    setText("[data-production-attention-title]", stalledAfterReset ? "关闭这次停住的生产任务后即可重新开写" : polishedDraftReady ? (session?.resume_ready === true ? "润色稿已验证，请确认提交" : "请验证已有润色稿") : retainedDraftReady ? "请验证已保留的完整候选稿" : provenanceFailure ? (provenanceReady ? "Git 环境已修复，等待确认" : provenanceFailure.verified ? "Git 来源记录环境异常" : "疑似 Git 环境错误，安全核对中") : contextBudget ? (contextBudgetReady ? "上下文修复已完成" : contextBudget.verified ? "模型请求尚未发出" : "上下文错误正在安全核对") : contextBudgetAfterProvider ? "已付费调用后上下文预算中断" : structuredContextAfterProvider ? "已付费调用后结构化上下文装配中断" : repairConvergence ? (repairConvergence.verified ? "修订稿引入了更严重的连续性问题" : "修订故障正在安全核对") : repairAuthority ? "质量修订请求在本地预算检查中停止" : staleSceneEvidence ? "自动修订与场景证据对不齐" : sceneBoundaryValidation ? "场景边界校验未通过" : qualityRejection ? "质量门发现阻断逻辑问题" : qualityRevision ? "Provider 已成功结束" : "不要重复启动或紧急停止");
    setText("[data-production-attention-detail]", stalledAfterReset ? `第 ${currentChapter || "—"} 章失败尝试已经结束，没有可复用的完整章节记录。关闭这次停住的生产任务后，「预检并开始」会重新出现；证据保留可追溯，正式正文与快照不会被改写。不要盲目重试上次不确定的模型调用。` : polishedDraftReady ? polishedDraftGuidance(Object.assign({}, session, payload.chapter_recovery || {})) : retainedDraftReady ? retainedDraftGuidance(Object.assign({}, session, payload.chapter_recovery || {})) : provenanceFailure ? (provenanceReady ? `${preProviderProvenanceStopDetail(provenanceFailure)} 系统已修复 Git 环境；前往待处理由你确认后才会重新执行。` : `${preProviderProvenanceSummary(provenanceFailure)} ${preProviderProvenanceStopDetail(provenanceFailure)}`) : contextBudget ? (contextBudgetReady ? `${contextBudgetSummary(contextBudget)} ${contextBudgetStopDetail(contextBudget)} 系统已完成降载修复，前往待处理由你确认后才会重新执行。` : `${contextBudgetSummary(contextBudget)} ${contextBudgetStopDetail(contextBudget)}`) : contextBudgetAfterProvider ? `${contextBudgetAfterProviderSummary(contextBudgetAfterProvider)} 前往待处理先做无模型恢复分类，不要盲目重试这次超限请求，已付费内容不会被隐藏。` : structuredContextAfterProvider ? `${structuredContextAfterProviderSummary(structuredContextAfterProvider)} 前往待处理先做无模型恢复分类，不要盲目重试这次装配失败，已付费内容不会被隐藏。` : repairConvergence ? (repairConvergence.verified ? `${repairConvergence.settledProviderCalls}/${repairConvergence.providerCalls} 次模型调用已结算，${repairConvergence.candidateChars} 字原候选已保留；${repairNotConvergingSummary(repairConvergence)} 正式正文未提交。前往待处理先做无模型恢复分类。` : `${repairNotConvergingSummary(repairConvergence)} 系统不会盲目重试，安全核对完成前不能继续。`) : repairAuthority ? `生成与审核的 ${repairAuthority.providerCalls} 次模型调用均已结算，${repairAuthority.chapterChars} 字候选正文保存在隔离运行区。修订器需要 ${repairAuthority.requiredChars} 字符的权威记录，旧预算只有 ${repairAuthority.budgetChars}；修订模型请求尚未发出，正式正文未提交。前往待处理先做无模型恢复分类。` : staleSceneEvidence ? `${staleSceneEvidence.providerCalls} 次模型调用已结算，${staleSceneEvidence.chapterChars} 字候选已保留；${staleSceneEvidenceReasonLabel(staleSceneEvidence)}，系统已拒绝采用这次修订，正式正文未提交。前往待处理先做无模型恢复分类，不要盲目重试。` : sceneBoundaryValidation ? `${sceneBoundaryValidationSummary(sceneBoundaryValidation)} 前往待处理先做无模型恢复分类，不要盲目重试这次失败的场景生成。` : qualityRejection ? `${qualityRejection.providerCalls} 次模型调用均已结算，${qualityRejection.chapterChars} 字候选已保留，正式正文未提交。阻断原因：${qualityProblemSummary(qualityRejection)} 前往待处理编辑恢复草稿并重新验证。` : qualityRevision ? "完整候选草稿已保留，正式正文未提交。前往待处理可启动自动修复并重新校验。" : "正式正文尚未提交。先到待处理识别最新可信恢复点，再决定是否重新校验。");
    const attentionLink = one("[data-production-attention-link]");
    if (attentionLink) {
      attentionLink.href = projectHash("pending");
      attentionLink.hidden = stalledAfterReset;
    }
    const progress = one(".scene-progress .progress", sessionPanel);
    progress?.classList.toggle("is-indeterminate", running);
    if (progress) {
      progress.setAttribute("aria-valuetext", paused ? "任务已安全暂停" : "任务正在运行，暂时没有精确百分比");
      if (paused) progress.setAttribute("aria-valuenow", "0"); else progress.removeAttribute("aria-valuenow");
    }
    setText("[data-progress-label]", provenanceFailure ? preProviderProvenanceSummary(provenanceFailure) : contextBudget ? contextBudgetSummary(contextBudget) : repairConvergence ? repairNotConvergingSummary(repairConvergence) : repairAuthority ? `修订权威记录需要 ${repairAuthority.requiredChars} 字符，旧预算为 ${repairAuthority.budgetChars}` : staleSceneEvidence ? staleSceneEvidenceSummary(staleSceneEvidence) : sceneBoundaryValidation ? sceneBoundaryValidationSummary(sceneBoundaryValidation) : qualityRejection ? qualityProblemSummary(qualityRejection) : qualityRevision ? "Provider 已成功结束，质量审核要求修订" : providerUncertain ? "模型调用状态不确定，系统已停止自动重试" : paused ? "任务已按作者要求暂停" : "模型正在生成与校验，本步可能需要几分钟");
    setText("[data-progress-percent]", provenanceFailure || contextBudget || repairConvergence || repairAuthority || staleSceneEvidence || sceneBoundaryValidation ? "已停止" : paused ? "已暂停" : "进行中");
    setText("[data-live-title]", provenanceFailure ? (provenanceFailure.verified ? "本次新增调用 0 次 · 新增费用 0 · 无草稿正文" : "模型调用、费用与产物仍在安全核对") : contextBudget ? (contextBudget.verified ? "本次新增调用 0 次，新增费用 0" : "模型调用、费用与产物仍在安全核对") : repairConvergence ? (repairConvergence.verified ? `${repairConvergence.settledProviderCalls}/${repairConvergence.providerCalls} 次调用已结算 · ${repairConvergence.candidateChars} 字原候选已保留` : "模型调用、候选正文与提交状态仍在安全核对") : repairAuthority ? `${repairAuthority.providerCalls} 次调用已结算 · ${repairAuthority.chapterChars} 字候选已保留` : staleSceneEvidence ? `${staleSceneEvidence.providerCalls} 次调用已结算 · ${staleSceneEvidence.chapterChars} 字候选已保留` : sceneBoundaryValidation ? `${sceneBoundaryValidation.providerCalls} 次调用已结算 · 正式正文未提交` : qualityRejection ? `${qualityRejection.providerCalls} 次调用已结算 · ${qualityRejection.chapterChars} 字候选待修订` : qualityRevision ? "完整草稿已保留，等待自动修复" : paused ? "本轮已经停止，不需要紧急终止" : "正在等待模型与引擎回执");
    setText("[data-live-detail]", provenanceFailure ? `${preProviderProvenanceStopDetail(provenanceFailure)} ${provenanceReady ? "等待作者确认重新执行。" : "当前不能重新执行。"}` : contextBudget ? contextBudgetStopDetail(contextBudget) : repairConvergence ? (repairConvergence.verified ? "修订稿已拒绝，原候选仍在隔离运行区，正式正文未提交。请前往待处理执行无模型恢复分类；不要直接重试。" : "证据核对完成前不会恢复、重试或调用模型。") : repairAuthority ? "修订模型请求尚未发出；正式正文未提交。请前往待处理先执行无模型恢复分类。" : staleSceneEvidence ? "候选正文仍在隔离运行区，正式正文未提交。请前往待处理执行无模型恢复分类；不要直接重试这次失败修订。" : sceneBoundaryValidation ? "场景边界校验未通过，正式正文未提交。请前往待处理执行无模型恢复分类；不要盲目重试这次失败的场景生成。" : qualityRejection ? "正式正文未提交。请前往待处理编辑恢复草稿，重新验证通过后再确认提交。" : qualityRevision ? "正式正文未提交。请前往待处理，确认调用真实模型自动修复并重新校验。" : providerUncertain ? "正式正文尚未提交。请前往待处理执行无模型恢复分类。" : paused ? "任务正在等待作者决定是否继续。" : "关闭或刷新页面不会重复调用模型，任务状态会从本地记录恢复。");
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
    if (session?.execution_compatible === false) {
      UI.updateStatusBadge(one("[data-production-status]"), "blocked", "旧任务版本不兼容");
      setText("[data-production-title]", `第 ${currentChapter || "—"} 章旧任务已隔离`);
      setText("[data-production-attention-eyebrow]", "版本兼容检查");
      setText("[data-production-attention-title]", "旧任务不能直接恢复执行");
      setText("[data-production-attention-detail]", incompatibleJobMessage);
      setText("[data-progress-label]", "旧任务已隔离，未启动新的执行");
      setText("[data-live-title]", "原始运行记录已保留");
      setText("[data-live-detail]", incompatibleJobMessage);
    }
  };

  const loadProduction = async () => {
    const [productionResponse, pendingResponse] = await Promise.all([
      api(`/api/projects/${encodeURIComponent(state.projectId)}/production`),
      api(`/api/projects/${encodeURIComponent(state.projectId)}/pending`)
    ]);
    const payload = {
      ...productionResponse.payload,
      chapter_recovery: pendingResponse.payload?.chapter_recovery || null
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
    one("button", validateForm).disabled = parentJob?.execution_compatible === false;
    one('input[name="job_id"]', validateForm).value = parentJob?.job_id || parentJob?.id || "";
    const confirmForm = one("[data-draft-confirm-form]");
    confirmForm.action = `/api/drafts/${encodeURIComponent(draft.id)}/confirm`;
    one('input[name="job_id"]', confirmForm).value = parentJob?.job_id || parentJob?.id || "";
    const confirmButton = one("[data-draft-confirm-button]", confirmForm);
    confirmButton.disabled = draft.state !== "validated" || !parentJob || parentJob.execution_compatible === false;
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
    const staleSceneEvidence = !provenanceFailure && !contextBudget && !repairConvergence
      ? (issue.stale_scene_evidence || staleSceneEvidenceDetailsOf(issue) || staleSceneEvidenceDetailsOfRun(issue.latest_run))
      : null;
    const staleSceneEvidenceFailure = Boolean(staleSceneEvidence);
    const sceneBoundaryValidation = !provenanceFailure && !contextBudget && !repairConvergence && !staleSceneEvidenceFailure
      ? (issue.scene_boundary_validation || sceneBoundaryValidationDetailsOf(issue) || sceneBoundaryValidationDetailsOfRun(issue.latest_run))
      : null;
    const sceneBoundaryValidationFailure = Boolean(sceneBoundaryValidation);
    const qualityRejection = !provenanceFailure && !contextBudget && !repairConvergence && !staleSceneEvidenceFailure && !sceneBoundaryValidationFailure
      ? (issue.quality_rejection || qualityRejectionDetailsOfRun(issue.latest_run))
      : null;
    const qualityRevision = !provenanceFailure && !contextBudget && !repairConvergence && !staleSceneEvidenceFailure && !sceneBoundaryValidationFailure && (Boolean(qualityRejection) || issue.quality_revision === true || isQualityRevisionPause(issue));
    const qualityDraft = qualityRevision
      ? state.drafts.find((item) => Number(item.chapter) === Number(issue.chapter || issue.next_chapter))
      : null;
    const sceneSchemaValidation = !provenanceFailure && !contextBudget && !repairConvergence && !qualityRevision
      ? (issue.scene_schema_validation || sceneSchemaValidationDetailsOf(issue) || sceneSchemaValidationDetailsOfRun(issue.latest_run))
      : null;
    const sceneSchemaFailure = Boolean(sceneSchemaValidation) || (!provenanceFailure && !contextBudget && !repairConvergence && !qualityRevision && issue.scene_schema_failure === true);
    const bridgePrevalidation = !provenanceFailure && !contextBudget && !repairConvergence && !qualityRevision && !sceneSchemaFailure
      ? (issue.bridge_prevalidation || bridgePrevalidationDetailsOf(issue) || bridgePrevalidationDetailsOfRun(issue.latest_run))
      : null;
    const bridgePrevalidationFailure = Boolean(bridgePrevalidation) || (!provenanceFailure && !contextBudget && !repairConvergence && !qualityRevision && !sceneSchemaFailure && issue.bridge_prevalidation_failure === true);
    const providerPartialResponse = !provenanceFailure && !contextBudget && !repairConvergence && !qualityRevision && !sceneSchemaFailure && !bridgePrevalidationFailure
      ? (issue.provider_partial_response || providerPartialResponseDetailsOf(issue) || providerPartialResponseDetailsOfRun(issue.latest_run))
      : null;
    const providerPartialResponseFailure = Boolean(providerPartialResponse) || (!provenanceFailure && !contextBudget && !repairConvergence && !qualityRevision && !sceneSchemaFailure && !bridgePrevalidationFailure && issue.provider_partial_response_failure === true);
    const localBudgetAfterProvider = !provenanceFailure && !contextBudget && !repairConvergence && !qualityRevision && !sceneSchemaFailure && !bridgePrevalidationFailure && !providerPartialResponseFailure
      ? (issue.local_budget_after_provider || localBudgetAfterProviderDetailsOf(issue) || localBudgetAfterProviderDetailsOfRun(issue.latest_run))
      : null;
    const localBudgetAfterProviderFailure = Boolean(localBudgetAfterProvider) || (!provenanceFailure && !contextBudget && !repairConvergence && !qualityRevision && !sceneSchemaFailure && !bridgePrevalidationFailure && !providerPartialResponseFailure && issue.local_budget_after_provider_failure === true);
    const contextBudgetAfterProvider = !provenanceFailure && !contextBudget && !repairConvergence && !qualityRevision && !sceneSchemaFailure && !bridgePrevalidationFailure && !providerPartialResponseFailure && !localBudgetAfterProviderFailure
      ? (issue.context_budget_after_provider || contextBudgetAfterProviderDetailsOf(issue) || contextBudgetAfterProviderDetailsOfRun(issue.latest_run))
      : null;
    const contextBudgetAfterProviderFailure = Boolean(contextBudgetAfterProvider) || (!provenanceFailure && !contextBudget && !repairConvergence && !qualityRevision && !sceneSchemaFailure && !bridgePrevalidationFailure && !providerPartialResponseFailure && !localBudgetAfterProviderFailure && issue.context_budget_after_provider_failure === true);
    const structuredContextAfterProvider = !provenanceFailure && !contextBudget && !contextBudgetAfterProviderFailure
      ? (issue.structured_context_after_provider || structuredContextAfterProviderDetailsOf(issue) || structuredContextAfterProviderDetailsOfRun(issue.latest_run))
      : null;
    const structuredContextAfterProviderFailure = Boolean(structuredContextAfterProvider) || (!provenanceFailure && !contextBudget && !repairConvergence && !qualityRevision && !sceneSchemaFailure && !bridgePrevalidationFailure && !providerPartialResponseFailure && !localBudgetAfterProviderFailure && !contextBudgetAfterProviderFailure && issue.structured_context_after_provider_failure === true);
    const contextHeadroom = !provenanceFailure && !contextBudget && !contextBudgetAfterProviderFailure && !repairConvergence && !qualityRevision
      ? (issue.context_headroom || contextHeadroomDetailsOfRun(issue.latest_run))
      : null;
    const contextHeadroomFailure = Boolean(contextHeadroom);
    const repairContextHeadroomFailure = contextHeadroomFailure && contextHeadroomIsRepair(contextHeadroom);
    const repairAuthorityFailure = !provenanceFailure && !contextBudget && !repairConvergence && !qualityRevision && issue.repair_authority_failure === true;
    const repairAuthority = repairAuthorityFailure ? issue.repair_authority : null;
    const settledGenerationFailure = sceneSchemaFailure || (contextHeadroomFailure && !repairContextHeadroomFailure) || repairAuthorityFailure || staleSceneEvidenceFailure || sceneBoundaryValidationFailure || Boolean(repairConvergence?.verified);
    setText("[data-pending-detail-eyebrow]", provenanceFailure ? "本机运行环境错误" : contextBudget ? "本地上下文超限" : repairConvergence ? "自动修订未收敛" : qualityRevision ? "质量审核需修订" : sceneSchemaFailure ? "模型响应格式错误" : bridgePrevalidationFailure ? "开写前衔接预检未通过" : providerPartialResponseFailure ? "模型部分响应中断" : localBudgetAfterProviderFailure ? "已付费后本地预算中断" : contextBudgetAfterProviderFailure ? "已付费后上下文预算中断" : structuredContextAfterProviderFailure ? "已付费后结构化上下文中断" : contextHeadroomFailure ? "本地安全余量不足" : repairAuthorityFailure ? "修订上下文装配错误" : staleSceneEvidenceFailure ? "修订跨越场景边界" : sceneBoundaryValidationFailure ? "场景边界校验未通过" : issue.evidence_not_classified ? "写作中断" : "可恢复生成");
    UI.updateStatusBadge(one(".recovery-hero .status-badge"), provenanceFailure ? (provenanceReady ? "paused_recoverable" : "needs_revision") : contextBudget ? (contextBudgetReady ? "paused_recoverable" : "needs_revision") : repairConvergence ? (repairConvergence.verified ? "paused_recoverable" : "needs_revision") : qualityRevision ? "needs_revision" : "paused_recoverable", provenanceFailure ? (provenanceReady ? "可重新执行" : provenanceFailure.verified ? "等待系统修复" : "安全核对中") : contextBudget ? (contextBudgetReady ? "可重新执行" : contextBudget.verified ? "等待系统修复" : "安全核对中") : repairConvergence ? (repairConvergence.verified ? "等待恢复分类" : "安全核对中") : qualityRevision ? "待修订" : issue.evidence_not_classified ? "待核对" : "可恢复");
    setText("[data-pending-title]", issue.title || (provenanceFailure ? (provenanceFailure.verified ? `第 ${pendingChapterLabel(issue)} 章在模型请求前停止` : `第 ${pendingChapterLabel(issue)} 章正在安全核对`) : contextBudget ? `第 ${pendingChapterLabel(issue)} 章输入上下文超出安全上限` : contextBudgetAfterProviderFailure ? `第 ${pendingChapterLabel(issue)} 章在已付费调用后因上下文预算中断` : structuredContextAfterProviderFailure ? `第 ${pendingChapterLabel(issue)} 章在已付费调用后因结构化上下文装配中断` : repairConvergence ? (repairConvergence.verified ? `第 ${pendingChapterLabel(issue)} 章自动修订未收敛` : `第 ${pendingChapterLabel(issue)} 章修订故障正在安全核对`) : repairContextHeadroomFailure ? `第 ${pendingChapterLabel(issue)} 章修订输入超过安全余量而停止` : contextHeadroomFailure ? `第 ${pendingChapterLabel(issue)} 章边界重生输入超过安全余量而停止` : repairAuthorityFailure ? `第 ${pendingChapterLabel(issue)} 章修订上下文装配失败` : staleSceneEvidenceFailure ? `第 ${pendingChapterLabel(issue)} 章自动修订因场景证据对不齐而停止` : sceneBoundaryValidationFailure ? `第 ${pendingChapterLabel(issue)} 章因场景边界校验失败而停止` : qualityRevision ? `第 ${pendingChapterLabel(issue)} 章质量审核需修订` : `第 ${pendingChapterLabel(issue)} 章生成中断`));
    setText("[data-pending-summary]", provenanceFailure ? `${preProviderProvenanceSummary(provenanceFailure)} ${preProviderProvenanceStopDetail(provenanceFailure)}` : contextBudget ? `${contextBudgetSummary(contextBudget)} ${contextBudgetStopDetail(contextBudget)}` : contextBudgetAfterProviderFailure ? contextBudgetAfterProviderSummary(contextBudgetAfterProvider) : structuredContextAfterProviderFailure ? structuredContextAfterProviderSummary(structuredContextAfterProvider) : repairConvergence ? repairNotConvergingSummary(repairConvergence) : staleSceneEvidenceFailure ? staleSceneEvidenceSummary(staleSceneEvidence) : sceneBoundaryValidationFailure ? sceneBoundaryValidationSummary(sceneBoundaryValidation) : issue.summary || issue.reason || (qualityRevision ? "Provider 已成功结束，完整候选草稿已保留，正式正文未提交。" : "回执完整的场景候选会先重跑当前状态边界；通过后才会复用。"));
    setText("[data-pending-chapter]", pendingChapterLabel(issue));
    setText("[data-pending-section-title]", provenanceFailure ? (provenanceFailure.verified ? "模型请求前停止" : "安全状态仍在核对") : contextBudget ? (contextBudget.verified ? "请求在发送给模型前已停止" : "安全状态仍在核对") : repairConvergence ? (repairConvergence.verified ? "原候选已保留，失败修订稿已拒绝" : "安全状态仍在核对") : qualityRevision ? "本章已经完成生成，但尚未通过质量门" : sceneSchemaFailure ? "调用已结算，格式错误没有进入正文" : bridgePrevalidationFailure ? "模型请求尚未发出，衔接预检未通过" : providerPartialResponseFailure ? "调用已有终态回执，部分响应已保留" : localBudgetAfterProviderFailure ? "调用已有终态回执，已付费内容已保留" : contextBudgetAfterProviderFailure ? "调用已结算，已付费内容已保留" : structuredContextAfterProviderFailure ? "调用已结算，已付费内容已保留" : repairContextHeadroomFailure ? "候选正文已保留，修订请求尚未发出" : contextHeadroomFailure ? "调用已结算，边界重生请求被本地预算门拦下" : repairAuthorityFailure ? "候选正文已保留，修订请求尚未发出" : staleSceneEvidenceFailure ? "候选正文已保留，越界修订已被拒绝" : sceneBoundaryValidationFailure ? "调用已结算，场景边界未通过，正文未提交" : issue.evidence_not_classified ? "先核对本次写作记录" : "已完成内容不会重写");
    setText("[data-pending-safe-chip]", provenanceFailure ? (provenanceFailure.verified ? "✓ 本次新增调用 0 · 新增费用 0 · 无草稿正文" : "! 调用、费用与产物尚未完全确认") : contextBudget ? (contextBudget.verified ? "✓ 本次新增调用与新增费用均为 0" : "! 调用、费用与产物尚未完全确认") : repairConvergence ? (repairConvergence.verified ? `✓ ${repairConvergence.settledProviderCalls}/${repairConvergence.providerCalls} 次调用已结算 · ${repairConvergence.candidateChars} 字原候选已保留 · 正式正文未提交` : "! 调用、候选正文与提交状态尚未完全确认") : qualityRejection ? `✓ ${qualityRejection.providerCalls} 次调用已结算 · ${qualityRejection.chapterChars} 字候选已保留` : qualityRevision ? "✓ 草稿已保留" : contextBudgetAfterProviderFailure ? `✓ ${contextBudgetAfterProvider.providerCalls} 次调用已结算且全部成功计费 · ${contextBudgetAfterProvider.chapterChars > 0 ? contextBudgetAfterProvider.chapterChars + " 字候选已保留" : "尚无完整候选，已付费回执已保留"}` : structuredContextAfterProviderFailure ? `✓ ${structuredContextAfterProvider.providerCalls} 次调用已结算且全部成功计费 · ${structuredContextAfterProvider.chapterChars > 0 ? structuredContextAfterProvider.chapterChars + " 字候选已保留" : "尚无完整候选，已付费回执已保留"}` : repairContextHeadroomFailure ? `✓ ${contextHeadroom.providerCalls} 次调用已结算 · ${contextHeadroom.chapterChars} 字候选已保留` : repairAuthorityFailure ? `✓ ${repairAuthority.providerCalls} 次调用已结算 · ${repairAuthority.chapterChars} 字候选已保留` : staleSceneEvidenceFailure ? `✓ ${staleSceneEvidence.providerCalls} 次调用已结算 · ${staleSceneEvidence.chapterChars} 字候选已保留 · 正式正文未提交` : sceneBoundaryValidationFailure ? `✓ ${sceneBoundaryValidation.providerCalls} 次调用已结算 · 正式正文未提交` : settledGenerationFailure ? `✓ ${Number(issue.latest_run?.provider_calls) || 0} 次调用已结算 · ${Number(issue.reusable_scene_count) || 0} 个场景可复用` : issue.evidence_not_classified ? "尚未核对" : "✓ 哈希绑定");
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
    } else if (repairContextHeadroomFailure) {
      sceneItems = [
        { title: "生成与语义审核", detail: `${contextHeadroom.providerCalls} 次模型调用都有完整回执且已经结算。`, status: "trusted", action: "已结算" },
        { title: "候选正文", detail: `${contextHeadroom.chapterChars} 字候选正文保存在隔离运行区，正式正文尚未提交。`, status: "trusted", action: "已保留" },
        { title: "修订输入预算", detail: `这次自动修订输入需要 ${contextHeadroom.requiredTokens} tokens，安全目标是 ${contextHeadroom.safeTarget}，超出 ${contextHeadroom.overageTokens} tokens；修订模型请求尚未发出。`, status: "partial", action: "等待重新分类" }
      ];
    } else if (repairAuthorityFailure) {
      sceneItems = [
        { title: "生成与语义审核", detail: `${repairAuthority.providerCalls} 次模型调用都有完整回执且已经结算。`, status: "trusted", action: "已结算" },
        { title: "候选正文", detail: `${repairAuthority.chapterChars} 字候选正文保存在隔离运行区，正式正文尚未提交。`, status: "trusted", action: "已保留" },
        { title: "质量修订上下文", detail: `本次修订必须保留 ${repairAuthority.requiredChars} 字符的权威记录，旧预算只有 ${repairAuthority.budgetChars}，超出 ${repairAuthority.overageChars} 字符；修订模型请求尚未发出。`, status: "partial", action: "等待重新分类" }
      ];
    } else if (staleSceneEvidenceFailure) {
      sceneItems = [
        { title: "模型调用", detail: `${staleSceneEvidence.providerCalls} 次模型调用都有完整回执且已经结算。`, status: "trusted", action: "已结算" },
        { title: "候选正文", detail: `${staleSceneEvidence.chapterChars} 字候选正文保存在隔离运行区，正式正文尚未提交。`, status: "trusted", action: "已保留" },
        { title: "自动修订", detail: `${staleSceneEvidenceReasonLabel(staleSceneEvidence)}，系统已拒绝采用这次修订，不会改写已保留候选。`, status: "partial", action: "修订已拒绝" }
      ];
    } else if (sceneBoundaryValidationFailure) {
      const candidateDetail = sceneBoundaryValidation.chapterChars > 0
        ? `${sceneBoundaryValidation.chapterChars} 字候选正文仍在隔离运行区，正式正文尚未提交。`
        : "本章尚无完整候选正文，正式正文尚未提交。";
      const sceneDetail = sceneBoundaryValidation.failedScene
        ? `失败发生在第 ${sceneBoundaryValidation.failedScene} 个场景，本地重试后仍未通过。`
        : "本地重试后仍未通过场景边界校验。";
      cards = [
        { title: "模型调用", detail: `${sceneBoundaryValidation.providerCalls} 次模型调用都有完整回执且已经结算。`, status: "trusted", action: "已结算" },
        { title: "正文状态", detail: candidateDetail, status: "trusted", action: "未提交" },
        { title: "场景边界", detail: `${sceneBoundaryValidationSummary(sceneBoundaryValidation)} ${sceneDetail}`, status: "partial", action: "校验未通过" }
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
      retainedCompleteDraftAvailable(issue) && issue.checkpoint ? (durablePolishedDraftAvailable(issue) ? "请验证或确认提交已有润色稿" : "请验证或确认提交已保留的完整候选稿") : provenanceFailure ? (provenanceReady ? "Git 环境已修复，等待你的确认" : provenanceFailure.verified ? "等待系统修复 Git 环境" : "安全核对完成前不能重新执行") : contextBudget ? (contextBudgetReady ? "系统修复已完成，等待你的确认" : contextBudget.verified ? `修复完成后重新执行第 ${pendingChapterLabel(issue)} 章` : "安全核对完成前不能重新执行") : contextBudgetAfterProviderFailure ? (issue.checkpoint ? "恢复证据已建立，等待确认后重新校验" : "先执行无模型恢复分类") : structuredContextAfterProviderFailure ? (issue.checkpoint ? "恢复证据已建立，等待确认后重新校验" : "先执行无模型恢复分类") : repairConvergence ? (!repairConvergence.verified ? "安全核对完成前不能恢复或重试" : issue.checkpoint ? "恢复证据已建立，等待安全继续" : "先执行无模型恢复分类") : repairContextHeadroomFailure ? (issue.checkpoint ? "恢复证据已建立，等待确认后重新校验" : "先执行无模型恢复分类") : staleSceneEvidenceFailure ? (issue.checkpoint ? "恢复证据已建立，等待确认后重新校验" : "先执行无模型恢复分类") : sceneBoundaryValidationFailure ? (issue.checkpoint ? "恢复证据已建立，等待确认后重新校验" : "先执行无模型恢复分类") : sceneSchemaFailure ? (issue.checkpoint ? "恢复证据已建立，等待确认后重新校验" : "先执行无模型恢复分类") : bridgePrevalidationFailure ? (issue.checkpoint ? "恢复证据已建立，等待确认后重新开写" : "先执行无模型恢复分类") : qualityRejection ? (!issue.checkpoint ? "先生成可编辑恢复草稿" : qualityDraft?.state === "validated" ? "草稿已通过恢复验证，等待确认提交" : "建议：修改下方草稿并重新验证") : qualityRevision ? "建议：自动修复并重新校验" : issue.checkpoint ? (allScenesReusable ? (completeSequenceNeedsRevalidation ? `建议：复核全部 ${reusableCount} 份场景候选，仅重生未通过场景` : `建议：复核全部 ${reusableCount} 个场景候选，再重新执行导演决策与校验`) : `建议：复核前 ${reusableCount} 个场景候选后从场景 ${nextScene} 继续`) : "先识别最新可信恢复边界"
    );
    setText("[data-recovery-action-detail]", retainedCompleteDraftAvailable(issue) && issue.checkpoint ? retainedDraftGuidance(issue) : provenanceFailure ? (provenanceReady ? "只有点击下方按钮并再次确认后，系统才会重新执行并调用真实模型；不点击则继续暂停，不新增费用。" : provenanceFailure.verified ? "当前不能重新执行；系统修复 Git 环境前，按钮保持禁用。" : "调用、费用与产物证据尚未完全确认；安全核对完成前按钮保持禁用。") : contextBudget ? (contextBudgetReady ? "只有点击下方按钮并再次确认后，系统才会调用真实模型；不点击则继续保持暂停，不新增费用。" : contextBudget.verified ? "系统修复并重新核对输入预算后才能再次执行；当前不会调用模型。" : "调用、费用与产物证据尚未完全确认；安全核对完成前按钮保持禁用。") : contextBudgetAfterProviderFailure ? (!issue.checkpoint ? "先执行无模型恢复分类，核对已结算回执与已保留的已付费内容；这一步不会调用模型、提交正文或写回状态，也不会把这次失败当成调用前的预算拒绝或盲目重试。已付费内容不会被隐藏。" : issue.resume_ready === true ? "恢复证据已绑定原暂停任务；只有再次明确确认后才会调用模型重新校验，系统不会盲目重试这次超限请求，已付费内容不会被隐藏。" : "恢复检查点已经建立，但尚未绑定到原暂停任务；当前不能直接重试这次超限请求。") : structuredContextAfterProviderFailure ? (!issue.checkpoint ? "先执行无模型恢复分类，核对已结算回执与已保留的已付费内容；这一步不会调用模型、提交正文或写回状态，也不会把这次失败当成调用前的预算拒绝或盲目重试。已付费内容不会被隐藏。" : issue.resume_ready === true ? "恢复证据已绑定原暂停任务；只有再次明确确认后才会调用模型重新校验，系统不会盲目重试这次装配失败，已付费内容不会被隐藏。" : "恢复检查点已经建立，但尚未绑定到原暂停任务；当前不能直接重试这次装配失败。") : repairConvergence ? (!repairConvergence.verified ? "结构化安全证据尚未闭环；按钮保持禁用，不会盲目重试或调用模型。" : !issue.checkpoint ? "先执行无模型恢复分类，核对已结算回执、原候选与可信场景边界；这一步不会调用模型、提交正文或写回状态。" : issue.resume_ready === true ? "恢复证据已绑定原暂停任务；只有再次明确确认后才会调用模型重新校验，系统不会直接重试失败修订稿。" : "恢复检查点已经建立，但尚未绑定到原暂停任务；当前不能直接重试。") : repairContextHeadroomFailure ? (!issue.checkpoint ? "先执行无模型恢复分类，核对已结算回执与已保留候选正文；这一步不会调用模型、提交正文或写回状态，也不会直接重试这次超限的修订。" : issue.resume_ready === true ? "恢复证据已绑定原暂停任务；只有再次明确确认后才会调用模型重新校验，系统不会直接重试这次超限的修订，已保留候选不会被隐藏。" : "恢复检查点已经建立，但尚未绑定到原暂停任务；当前不能直接重试这次超限修订。") : staleSceneEvidenceFailure ? (!issue.checkpoint ? "先执行无模型恢复分类，核对已结算回执与已保留候选正文；这一步不会调用模型、提交正文或写回状态，也不会直接重试这次失败的修订。" : issue.resume_ready === true ? "恢复证据已绑定原暂停任务；只有再次明确确认后才会调用模型重新校验，系统不会直接重试这次失败的越界修订，已保留候选不会被隐藏。" : "恢复检查点已经建立，但尚未绑定到原暂停任务；当前不能直接重试这次失败修订。") : sceneBoundaryValidationFailure ? (!issue.checkpoint ? "先执行无模型恢复分类，核对已结算回执与场景边界问题；这一步不会调用模型、提交正文或写回状态，也不会盲目重试这次失败的场景生成。" : issue.resume_ready === true ? "恢复证据已绑定原暂停任务；只有再次明确确认后才会调用模型重新校验，系统不会盲目重试这次失败的场景生成。" : "恢复检查点已经建立，但尚未绑定到原暂停任务；当前不能直接重试这次失败的场景生成。") : sceneSchemaFailure ? (!issue.checkpoint ? "先执行无模型恢复分类，核对已结算回执与可复用场景；这一步不会调用模型、提交正文或写回状态，也不会盲目重试这次格式错误的场景生成。" : issue.resume_ready === true ? "恢复证据已绑定原暂停任务；只有再次明确确认后才会调用模型重新校验，系统不会盲目重试这次格式错误的场景生成。" : "恢复检查点已经建立，但尚未绑定到原暂停任务；当前不能直接重试这次格式错误的场景生成。") : bridgePrevalidationFailure ? (!issue.checkpoint ? "先执行无模型恢复分类，核对接续问题；这一步不会调用模型、提交正文或写回状态，也不会盲目重试这次失败的衔接预检。" : "衔接预检仍未通过，不能直接重新开写；已确认没有模型调用和候选正文。") : qualityRejection ? (!issue.checkpoint ? "先做无模型分类，把完整候选转成带 revision/SHA 的可编辑草稿；不会调用模型或提交正文。" : draftSourceConflict ? "你的编辑已原样保留；请先对照下方更新来源稿完成合并，再保存并重新验证。" : qualityDraft?.state === "validated" ? "请使用下方“确认提交并继续”；原任务只会在再次确认后恢复，并且仍须通过质量门。" : "在下方只修改阻断问题，保存后点击“重新验证”；正式正文在验证通过前不会提交。") : qualityRevision ? "此操作会调用真实模型；只有修复内容重新通过校验与质量门后，才会提交正式正文。" : settledGenerationFailure ? (issue.checkpoint ? (completeSequenceNeedsRevalidation ? `将载入 ${reusableCount} 份候选并逐场重跑边界校验；只有未通过的场景会调用模型重生，全部通过后才会继续审核与提交。` : `将保留前 ${Number(issue.reusable_scene_count) || 0} 个可信场景，从场景 ${Number(issue.next_scene) || 1} 继续；恢复会调用真实模型完成剩余场景。`) : "先执行无模型恢复分类，核对已结算回执和可复用场景；这一步不会调用模型或提交正文。") : "恢复会创建追加式检查点，不改写正式正文或历史快照。");
    const form = one("[data-recover-form]");
    const chapter = pendingChapterNumber(issue) ?? "—";
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
    } else if (repairContextHeadroomFailure && !checkpointReady) {
      form.action = `/api/jobs/${encodeURIComponent(pausedParent?.job_id || pausedParent?.id || exactParentId)}/recover`;
      form.dataset.recoveryMode = "repair-context-headroom-classify";
      form.dataset.confirm = "先执行无模型恢复分类，核对全部已结算回执与已保留候选正文。这一步不会调用模型、提交正文或写回状态，也不会直接重试这次超限的修订，确认吗？";
      form.dataset.successMessage = "无模型恢复分类任务已启动。";
      setText("[data-recover-button]", "先做无模型恢复分类");
    } else if (staleSceneEvidenceFailure && !checkpointReady) {
      form.action = `/api/jobs/${encodeURIComponent(pausedParent?.job_id || pausedParent?.id || exactParentId)}/recover`;
      form.dataset.recoveryMode = "stale-scene-evidence-classify";
      form.dataset.confirm = "先执行无模型恢复分类，核对全部已结算回执与已保留候选正文。这一步不会调用模型、提交正文或写回状态，也不会直接重试这次失败修订，确认吗？";
      form.dataset.successMessage = "无模型恢复分类任务已启动。";
      setText("[data-recover-button]", "先做无模型恢复分类");
    } else if (sceneBoundaryValidationFailure && !checkpointReady) {
      form.action = `/api/jobs/${encodeURIComponent(pausedParent?.job_id || pausedParent?.id || exactParentId)}/recover`;
      form.dataset.recoveryMode = "scene-boundary-validation-classify";
      form.dataset.confirm = "先执行无模型恢复分类，核对已结算回执与场景边界问题。这一步不会调用模型、提交正文或写回状态，也不会盲目重试这次失败的场景生成，确认吗？";
      form.dataset.successMessage = "无模型恢复分类任务已启动。";
      setText("[data-recover-button]", "先做无模型恢复分类");
    } else if (sceneSchemaFailure && !checkpointReady) {
      form.action = `/api/jobs/${encodeURIComponent(pausedParent?.job_id || pausedParent?.id || exactParentId)}/recover`;
      form.dataset.recoveryMode = "scene-schema-validation-classify";
      form.dataset.confirm = "先执行无模型恢复分类，核对已结算回执与可复用场景。这一步不会调用模型、提交正文或写回状态，也不会盲目重试这次格式错误的场景生成，确认吗？";
      form.dataset.successMessage = "无模型恢复分类任务已启动。";
      setText("[data-recover-button]", "先做无模型恢复分类");
    } else if (bridgePrevalidationFailure && !checkpointReady) {
      form.action = `/api/jobs/${encodeURIComponent(pausedParent?.job_id || pausedParent?.id || exactParentId)}/recover`;
      form.dataset.recoveryMode = "bridge-prevalidation-classify";
      form.dataset.confirm = "先执行无模型恢复分类，核对接续问题。这一步不会调用模型、提交正文或写回状态，也不会盲目重试这次失败的衔接预检，确认吗？";
      form.dataset.successMessage = "无模型恢复分类任务已启动。";
      setText("[data-recover-button]", "先做无模型恢复分类");
    } else if (localBudgetAfterProviderFailure && !checkpointReady) {
      form.action = `/api/jobs/${encodeURIComponent(pausedParent?.job_id || pausedParent?.id || exactParentId)}/recover`;
      form.dataset.recoveryMode = "local-budget-after-provider-classify";
      form.dataset.confirm = "先执行无模型恢复分类，核对应保留的已付费内容。这一步不会调用模型、提交正文或写回状态，也不会把这次失败当成调用前的预算拒绝或盲目重试，确认吗？";
      form.dataset.successMessage = "无模型恢复分类任务已启动。";
      setText("[data-recover-button]", "先做无模型恢复分类");
    } else if (contextBudgetAfterProviderFailure && !checkpointReady) {
      form.action = `/api/jobs/${encodeURIComponent(pausedParent?.job_id || pausedParent?.id || exactParentId)}/recover`;
      form.dataset.recoveryMode = "context-budget-after-provider-classify";
      form.dataset.confirm = "先执行无模型恢复分类，核对已结算回执与已保留的已付费内容。这一步不会调用模型、提交正文或写回状态，也不会把这次失败当成调用前的预算拒绝或盲目重试，确认吗？";
      form.dataset.successMessage = "无模型恢复分类任务已启动。";
      setText("[data-recover-button]", "先做无模型恢复分类");
    } else if (structuredContextAfterProviderFailure && !checkpointReady) {
      form.action = `/api/jobs/${encodeURIComponent(pausedParent?.job_id || pausedParent?.id || exactParentId)}/recover`;
      form.dataset.recoveryMode = "structured-context-after-provider-classify";
      form.dataset.confirm = "先执行无模型恢复分类，核对已结算回执与已保留的已付费内容。这一步不会调用模型、提交正文或写回状态，也不会把这次失败当成调用前的预算拒绝或盲目重试，确认吗？";
      form.dataset.successMessage = "无模型恢复分类任务已启动。";
      setText("[data-recover-button]", "先做无模型恢复分类");
    } else if (providerPartialResponseFailure && !checkpointReady) {

      form.action = `/api/jobs/${encodeURIComponent(pausedParent?.job_id || pausedParent?.id || exactParentId)}/recover`;
      form.dataset.recoveryMode = "provider-partial-response-classify";
      form.dataset.confirm = "先执行无模型恢复分类，核对已付费的部分响应和已保留候选。这一步不会调用模型、提交正文或写回状态，也不会把这次失败当成无回执的不确定调用或盲目重试，确认吗？";
      form.dataset.successMessage = "无模型恢复分类任务已启动。";
      setText("[data-recover-button]", "先做无模型恢复分类");
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
        } else if (retainedCompleteDraftAvailable(issue) && checkpointReady) {
      const retainedDraft = state.drafts.find((item) => Number(item.chapter) === Number(chapter));
      form.action = `/api/jobs/${encodeURIComponent(pausedParent?.job_id || pausedParent?.id || exactParentId)}/recover`;
      if (!retainedDraft) {
        form.dataset.recoveryMode = "retained-complete-draft-capture";
        form.dataset.confirm = "先把已保留的完整候选安全转换为可编辑恢复草稿。这一步不会调用模型或提交正文，也不会重试这次失败的修订或上下文装配，确认吗？";
        form.dataset.successMessage = "可编辑恢复草稿正在建立。";
        setText("[data-recover-button]", "生成可编辑恢复草稿");
      } else {
        classifyChapter.disabled = true;
        form.dataset.recoveryMode = "retained-complete-draft";
        form.dataset.confirm = "";
        form.dataset.successMessage = "";
        setText("[data-recover-button]", retainedDraft.state === "validated" ? "请使用下方确认提交" : "请先编辑并重新验证下方草稿");
        recoverButton.disabled = true;
        recoverButton.title = "已保留的完整候选必须通过下方草稿验证后才能确认提交；不会盲目重试这次失败的修订或装配。";
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
      form.dataset.recoveryMode = issue.uncertain_without_run ? "classify-without-run" : "classify";
      form.dataset.confirm = issue.uncertain_without_run
        ? `第 ${chapter} 章没有完整章节运行记录。无模型安全分类会结束这次失败尝试并允许新的写作；不会调用模型，也不会改写正式正文。确认吗？`
        : "先执行无模型恢复分类，确认可信场景边界。这一步不会调用模型，确认吗？";
      form.dataset.successMessage = "恢复分类任务已启动。";
      setText("[data-recover-button]", "先做无模型恢复分类");
    }
    const forceResetPanel = one("[data-force-reset-panel]");
    const forceResetForm = one("[data-force-reset-form]");
    const forceResetChapter = one("[data-force-reset-chapter]", forceResetForm);
    const forceResetButton = one("[data-force-reset-button]", forceResetForm);
    const classifiedCheckpoint = Boolean(issue.checkpoint) && issue.checkpoint.action !== "reset";
    const uncertainWithoutRun = issue.uncertain_without_run === true && issue.formal_prose_exists !== true;
    const resetCleared = (issue.checkpoint?.action === "reset" || issue.reset_cleared === true)
      && issue.formal_prose_exists !== true
      && !durablePolishedDraftAvailable(issue)
      && !retainedCompleteDraftAvailable(issue);
    const stalePausedSession = Boolean(pausedParent)
      && ["awaiting_input", "needs_recovery"].includes(pausedParent.state);
    const forceResetAllowed = Boolean(
      forceResetPanel
      && forceResetForm
      && (classifiedCheckpoint || uncertainWithoutRun)
      && (issue.recoverable !== false || uncertainWithoutRun)
      && issue.formal_prose_exists !== true
      && issue.execution_compatible !== false
      && pausedParent?.execution_compatible !== false
      && !provenanceFailure
      && !contextBudget
      && !durablePolishedDraftAvailable(issue)
      && !retainedCompleteDraftAvailable(issue)
      && !providerPartialResponseFailure
      && !localBudgetAfterProviderFailure
      && !contextBudgetAfterProviderFailure
      && !structuredContextAfterProviderFailure
      && !repairAuthorityFailure
      && !resetCleared
    );
    const closeStalledSession = Boolean(
      forceResetPanel
      && forceResetForm
      && resetCleared
      && stalePausedSession
      && issue.execution_compatible !== false
      && pausedParent?.execution_compatible !== false
      && !provenanceFailure
      && !contextBudget
    );
    if (forceResetPanel) {
      forceResetPanel.hidden = !(forceResetAllowed || closeStalledSession);
      if (closeStalledSession) {
        forceResetChapter.value = String(chapter);
        forceResetForm.action = `/api/jobs/${encodeURIComponent(pausedParent.job_id || pausedParent.id)}/cancel`;
        const forceResetField = one('input[name="force_reset"]', forceResetForm);
        if (forceResetField) forceResetField.disabled = true;
        forceResetForm.dataset.recoveryMode = "close-stalled-session";
        forceResetForm.dataset.confirm = `第 ${chapter} 章失败尝试的锁已经解除。确认关闭这次已停住的生产任务，以便重新开写第 ${chapter} 章？不会删除失败证据，也不会改写正式正文。`;
        forceResetForm.dataset.successMessage = `第 ${chapter} 章失败尝试已结束，可以开始新的写作。`;
        forceResetButton.disabled = false;
        setText("[data-force-reset-heading]", "关闭这次停住的生产任务并重新开写");
        setText("[data-force-reset-copy]", `本章没有可复用的完整章节记录，失败锁已解除。关闭这次停住的生产任务后，就可以重新开写第 ${chapter} 章；证据保留可追溯，正式正文与快照不会被改写。`);
        setText("[data-force-reset-button]", "关闭这次停住的生产任务");
      } else if (forceResetAllowed) {
        forceResetChapter.value = String(chapter);
        forceResetForm.action = issue.recover_url
          || (issue.project_recovery
            ? `/api/projects/${encodeURIComponent(state.projectId)}/recovery`
            : `/api/jobs/${encodeURIComponent(issue.job_id || issue.id || pausedParent?.job_id || pausedParent?.id)}/recover`);
        const forceResetField = one('input[name="force_reset"]', forceResetForm);
        if (forceResetField) forceResetField.disabled = false;
        forceResetForm.dataset.recoveryMode = uncertainWithoutRun ? "force-reset-without-run" : "force-reset";
        forceResetForm.dataset.confirm = `确认结束第 ${chapter} 章失败尝试并允许新尝试？只会追加重置标记，不删除失败证据，也不改写正式正文。`;
        forceResetForm.dataset.successMessage = `第 ${chapter} 章强制重置任务已启动。`;
        forceResetButton.disabled = false;
        setText("[data-force-reset-heading]", "结束本章失败尝试并允许新尝试");
        setText("[data-force-reset-copy]", uncertainWithoutRun
          ? `上次模型调用结果不确定，且没有留下完整章节运行记录。不要盲目重试。可先做无模型安全分类，或结束第 ${chapter} 章失败尝试并允许新尝试；证据保留可追溯，正式正文与快照不会被改写。`
          : `安全分类已完成。若需要丢弃当前失败 run 的复用边界并重新开写第 ${chapter} 章，可结束本章并允许新尝试；证据保留可追溯，正式正文与快照不会被改写。`);
        setText("[data-force-reset-button]", "结束本章并允许新尝试");
      }
    }
    renderRecoveryDraft(issue);
    renderRetainedPaidDraftInspect(issue, one("[data-pending-detail]"));
    if (issue.execution_compatible === false || pausedParent?.execution_compatible === false) {
      UI.updateStatusBadge(one(".recovery-hero .status-badge"), "blocked", "旧任务版本不兼容");
      setText("[data-pending-detail-eyebrow]", "版本兼容检查");
      setText("[data-pending-title]", `第 ${chapter} 章旧任务已隔离`);
      setText("[data-pending-summary]", incompatibleJobMessage);
      setText("[data-pending-section-title]", "原始记录保留供核对");
      setText("[data-pending-safe-chip]", "旧任务未获得当前版本的执行许可");
      setText("[data-recovery-action-label]", "当前版本不能恢复此任务");
      setText("[data-recovery-action-detail]", incompatibleJobMessage);
      form.dataset.confirm = "";
      all("input, button", form).forEach((control) => { control.disabled = true; });
      const forceResetPanelLocked = one("[data-force-reset-panel]");
      if (forceResetPanelLocked) forceResetPanelLocked.hidden = true;
      setText("[data-recover-button]", "旧任务版本不兼容");
    }
    all(".pending-item", one("[data-pending-list]")).forEach((node) => node.classList.toggle("is-active", node.dataset.pendingId === String(issue.id)));
  };

  const renderPending = (payload) => {
    const pausedJobs = arrayFrom(payload, "items", "pending_items", "jobs").map((job) => {
      const normalized = {
        ...job,
        job_id: job.job_id || job.id,
        chapter: job.chapter || job.next_chapter,
        title: job.execution_compatible === false ? `第 ${job.next_chapter || "—"} 章旧任务版本不兼容` : job.title || `第 ${job.next_chapter || "—"} 章需要作者决定`,
        reason: job.execution_compatible === false ? incompatibleJobMessage : providerUncertainCopy({
          ...job,
          latest_run: job.latest_run || (
            Number(payload.chapter_recovery?.latest_run?.chapter_index || payload.chapter_recovery?.chapter) === Number(job.next_chapter || job.chapter)
              ? payload.chapter_recovery?.latest_run
              : null
          ),
          uncertain_without_run: job.uncertain_without_run === true || (
            payload.chapter_recovery?.uncertain_without_run === true
            && Number(payload.chapter_recovery?.chapter) === Number(job.next_chapter || job.chapter)
          ),
        }) || job.reason || job.error?.message || (job.state === "needs_recovery" ? "Provider 状态不确定，不会自动重试" : "生产已暂停")
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
    const recovery = payload.chapter_recovery;
    const recoveryChapter = pendingChapterNumber(recovery);
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
    const sceneSchemaValidation = sceneSchemaValidationDetailsOf(pausedRecoveryJob) || sceneSchemaValidationDetailsOfRun(latestRecoveryRun);
    const sceneSchemaFailure = Boolean(sceneSchemaValidation);
    const bridgePrevalidation = bridgePrevalidationDetailsOf(pausedRecoveryJob) || bridgePrevalidationDetailsOfRun(latestRecoveryRun);
    const bridgePrevalidationFailure = Boolean(bridgePrevalidation);
    const providerPartialResponse = providerPartialResponseDetailsOf(pausedRecoveryJob) || providerPartialResponseDetailsOfRun(latestRecoveryRun);
    const providerPartialResponseFailure = Boolean(providerPartialResponse);
    const localBudgetAfterProvider = localBudgetAfterProviderDetailsOf(pausedRecoveryJob) || localBudgetAfterProviderDetailsOfRun(latestRecoveryRun);
    const localBudgetAfterProviderFailure = Boolean(localBudgetAfterProvider);
    const contextBudgetAfterProvider = contextBudgetAfterProviderDetailsOf(pausedRecoveryJob) || contextBudgetAfterProviderDetailsOfRun(latestRecoveryRun);
    const contextBudgetAfterProviderFailure = Boolean(contextBudgetAfterProvider);
    const structuredContextAfterProvider = contextBudgetAfterProviderFailure ? null : (structuredContextAfterProviderDetailsOf(pausedRecoveryJob) || structuredContextAfterProviderDetailsOfRun(latestRecoveryRun));
    const structuredContextAfterProviderFailure = Boolean(structuredContextAfterProvider);
    const contextHeadroom = contextBudgetAfterProviderFailure ? null : contextHeadroomDetailsOfRun(latestRecoveryRun);
    const contextHeadroomFailure = Boolean(contextHeadroom);
    const repairContextHeadroomFailure = contextHeadroomFailure && contextHeadroomIsRepair(contextHeadroom);
    const repairAuthority = repairAuthorityDetailsOfRun(latestRecoveryRun);
    const repairAuthorityFailure = Boolean(repairAuthority);
    const staleSceneEvidence = staleSceneEvidenceDetailsOf(pausedRecoveryJob) || staleSceneEvidenceDetailsOfRun(latestRecoveryRun);
    const staleSceneEvidenceFailure = Boolean(staleSceneEvidence);
    const sceneBoundaryValidation = sceneBoundaryValidationDetailsOf(pausedRecoveryJob) || sceneBoundaryValidationDetailsOfRun(latestRecoveryRun);
    const sceneBoundaryValidationFailure = Boolean(sceneBoundaryValidation);
    const qualityRejection = qualityRejectionDetailsOfRun(latestRecoveryRun);
    const qualityRejectionFailure = Boolean(qualityRejection && recovery?.formal_prose_exists === false);
    const modelRejectedFirstDraft = latestRecoveryRun?.chapter_index === recoveryChapter
      && latestRecoveryRun?.status === "failed" && latestRecoveryRun?.committed === false
      && latestRecoveryRun?.failed_action === "generate_chapter" && latestRecoveryRun?.chapter_chars === 0
      && latestRecoveryRun?.error_message === "OpenAI chat completion failed (configuration; ProviderRejectedError).";
    let recoveryIssue = null;
    if (recoveryChapter && !provenanceAtRecoveryChapter && !contextBudgetAtRecoveryChapter && !qualityRevisionAtRecoveryChapter && recovery?.recoverable && recovery.needs_reclassification && pausedRecoveryJob) {
      recoveryIssue = {
        ...pausedRecoveryJob,
        id: pausedRecoveryJob.id,
        job_id: pausedRecoveryJob.job_id || pausedRecoveryJob.id,
        chapter: recoveryChapter,
        checkpoint: null,
        evidence_not_classified: true,
        project_recovery: false,
        uncertain_without_run: recovery.uncertain_without_run === true,
        formal_prose_exists: recovery.formal_prose_exists === true,
        reset_cleared: recovery.reset_cleared === true,
        scene_schema_failure: sceneSchemaFailure,
        scene_schema_validation: sceneSchemaValidation,
        bridge_prevalidation: bridgePrevalidation,
        bridge_prevalidation_failure: bridgePrevalidationFailure,
        provider_partial_response: providerPartialResponse,
        provider_partial_response_failure: providerPartialResponseFailure,
        local_budget_after_provider: localBudgetAfterProvider,
        local_budget_after_provider_failure: localBudgetAfterProviderFailure,
        context_budget_after_provider: contextBudgetAfterProvider,
        context_budget_after_provider_failure: contextBudgetAfterProviderFailure,
        structured_context_after_provider: structuredContextAfterProvider,
        structured_context_after_provider_failure: structuredContextAfterProviderFailure,
        context_headroom_failure: contextHeadroomFailure,
        context_headroom: contextHeadroom,
        repair_authority_failure: repairAuthorityFailure,
        repair_authority: repairAuthority,
        stale_scene_evidence_failure: staleSceneEvidenceFailure,
        stale_scene_evidence: staleSceneEvidence,
        scene_boundary_validation_failure: sceneBoundaryValidationFailure,
        scene_boundary_validation: sceneBoundaryValidation,
        repair_not_converging: repairConvergenceFailure,
        quality_revision: qualityRejectionFailure,
        quality_rejection: qualityRejection,
        latest_run: latestRecoveryRun,
        retained_paid_draft_preview: recovery.retained_paid_draft_preview || null,
        title: repairConvergenceFailure ? (repairConvergenceFailure.verified ? `第 ${recoveryChapter} 章自动修订未收敛，等待无模型恢复分类` : `第 ${recoveryChapter} 章修订故障正在安全核对`) : sceneSchemaFailure ? `第 ${recoveryChapter} 章场景响应格式错误，等待安全分类` : bridgePrevalidationFailure ? `第 ${recoveryChapter} 章因衔接预检失败而停止，等待无模型恢复分类` : providerPartialResponseFailure ? `第 ${recoveryChapter} 章因模型部分响应中断而停止，已保留已付费内容，等待无模型恢复分类` : contextBudgetAfterProviderFailure ? `第 ${recoveryChapter} 章在已付费调用后因上下文预算中断，已保留已付费内容，等待无模型恢复分类` : structuredContextAfterProviderFailure ? `第 ${recoveryChapter} 章在已付费调用后因结构化上下文装配中断，已保留已付费内容，等待无模型恢复分类` : localBudgetAfterProviderFailure ? `第 ${recoveryChapter} 章在已产生已付费调用后因本地预算停止，已保留已付费内容，等待无模型恢复分类` : repairContextHeadroomFailure ? `第 ${recoveryChapter} 章修订输入超过安全余量，等待安全分类` : contextHeadroomFailure ? `第 ${recoveryChapter} 章边界重生输入超过安全余量，等待安全分类` : repairAuthorityFailure ? `第 ${recoveryChapter} 章修订上下文装配失败，等待安全分类` : staleSceneEvidenceFailure ? `第 ${recoveryChapter} 章自动修订因场景证据对不齐，等待无模型恢复分类` : sceneBoundaryValidationFailure ? `第 ${recoveryChapter} 章因场景边界校验失败，等待无模型恢复分类` : qualityRejectionFailure ? `第 ${recoveryChapter} 章质量门发现阻断问题，等待生成编辑草稿` : retainedUnsettledPaidDraft({ latest_run: latestRecoveryRun }) ? `第 ${recoveryChapter} 章上次调用结果不确定，已保留 ${retainedUnsettledPaidDraft({ latest_run: latestRecoveryRun }).chapterChars} 字候选，等待无模型恢复分类` : recovery.uncertain_without_run ? `第 ${recoveryChapter} 章上次模型调用结果不确定` : `第 ${recoveryChapter} 章已安全暂停`,
        reason: repairConvergenceFailure
          ? repairNotConvergingSummary(repairConvergenceFailure)
          : sceneSchemaFailure
          ? `本次 ${Number(latestRecoveryRun.provider_calls) || 0} 次模型调用均已结算；场景响应的 JSON 格式错误。`
          : bridgePrevalidationFailure
          ? bridgePrevalidationSummary(bridgePrevalidation)
          : providerPartialResponseFailure
          ? providerPartialResponseSummary(providerPartialResponse)
          : contextBudgetAfterProviderFailure
          ? contextBudgetAfterProviderSummary(contextBudgetAfterProvider)
          : structuredContextAfterProviderFailure
          ? structuredContextAfterProviderSummary(structuredContextAfterProvider)
          : localBudgetAfterProviderFailure
          ? localBudgetAfterProviderSummary(localBudgetAfterProvider)
          : repairContextHeadroomFailure
          ? `本次 ${contextHeadroom.providerCalls} 次模型调用均已结算；修订输入为 ${contextHeadroom.requiredTokens} tokens，超过 ${contextHeadroom.safeTarget} 安全目标 ${contextHeadroom.overageTokens} tokens。`
          : contextHeadroomFailure
          ? `本次 ${Number(latestRecoveryRun.provider_calls) || 0} 次模型调用均已结算；边界重生输入为 ${contextHeadroom.requiredTokens} tokens，超过 ${contextHeadroom.safeTarget} 安全目标 ${contextHeadroom.overageTokens} tokens。`
          : repairAuthorityFailure
          ? `本次 ${repairAuthority.providerCalls} 次模型调用均已结算；${repairAuthority.chapterChars} 字候选已保留，修订所需权威记录 ${repairAuthority.requiredChars} 字符超过旧预算 ${repairAuthority.budgetChars} 字符。`
          : staleSceneEvidenceFailure
          ? `本次 ${staleSceneEvidence.providerCalls} 次模型调用均已结算；${staleSceneEvidence.chapterChars} 字候选已保留。${staleSceneEvidenceReasonLabel(staleSceneEvidence)}，系统已拒绝采用这次修订。`
          : sceneBoundaryValidationFailure
          ? sceneBoundaryValidationSummary(sceneBoundaryValidation)
          : qualityRejectionFailure
          ? `本次 ${qualityRejection.providerCalls} 次模型调用均已结算；${qualityRejection.chapterChars} 字候选已保留。阻断原因：${qualityProblemSummary(qualityRejection)}`
          : providerUncertainPaidDraftCopy({ latest_run: latestRecoveryRun })
          ? providerUncertainPaidDraftCopy({ latest_run: latestRecoveryRun })
          : recovery.uncertain_without_run
          ? "上次模型调用结果不确定，没有完整回执和章节运行记录。不要盲目重试。"
          : modelRejectedFirstDraft ? "模型服务拒绝了正文请求；本次运行没有生成可审阅的正文。"
          : "需要核对本次运行留下了哪些内容",
        summary: repairConvergenceFailure
          ? (repairConvergenceFailure.verified ? `${repairConvergenceFailure.providerCalls} 次模型调用全部结算，${repairConvergenceFailure.candidateChars} 字原候选已保留，正式正文未提交。先做无模型恢复分类，不直接重试失败修订稿。` : "调用、候选正文与提交状态正在安全核对；完成前不会恢复或重试。")
          : staleSceneEvidenceFailure
          ? "正式正文没有提交；已保留候选仍可核对。先执行无模型恢复分类，不要盲目重试这次失败修订。"
          : sceneBoundaryValidationFailure
          ? "正式正文没有提交，场景边界校验未通过。先执行无模型恢复分类，不要盲目重试这次失败的场景生成。"
          : bridgePrevalidationFailure
          ? "正式正文没有提交，也没有模型调用。先执行无模型恢复分类，核对接续问题，不要盲目重试这次失败的衔接预检。"
          : providerPartialResponseFailure
          ? "正式正文没有提交；已留下部分响应失败回执。先执行无模型恢复分类，核对应保留的已付费内容，不要当成无回执的不确定调用，也不要盲目重试。"
          : contextBudgetAfterProviderFailure
          ? "正式正文没有提交；已付费模型调用之后，本地上下文预算拒绝继续。先执行无模型恢复分类，核对应保留的已付费内容，不要当成调用前的预算拒绝，也不要盲目重试。"
          : structuredContextAfterProviderFailure
          ? "正式正文没有提交；已付费模型调用之后，结构化上下文装配超过本地字符预算。先执行无模型恢复分类，核对应保留的已付费内容，不要当成调用前的预算拒绝，也不要盲目重试。"
          : localBudgetAfterProviderFailure
          ? "正式正文没有提交；本地预算在已有已付费调用后拒绝继续。先执行无模型恢复分类，核对应保留的已付费内容，不要当成调用前的预算拒绝，也不要盲目重试。"
          : sceneSchemaFailure || contextHeadroomFailure || repairAuthorityFailure || qualityRejectionFailure
          ? "正式正文没有提交；先执行无模型恢复分类，核对候选正文、回执与最新运行后再继续。"
          : providerUncertainPaidDraftCopy({ latest_run: latestRecoveryRun })
          ? "正式正文没有提交。先执行无模型恢复分类，核对应保留的已付费正文与回执；不要先结束失败尝试来隐藏已付费正文，也不要盲目重试这次不确定调用。"
          : recovery.uncertain_without_run
          ? "电脑中断时模型调用没有留下完整回执。请先做无模型安全分类；分类会结束这次失败尝试，也可以直接结束本章并允许新尝试。正式正文与快照不会被改写。"
          : modelRejectedFirstDraft ? "模型服务拒绝了这次请求，本次运行没有生成正文。请核对服务地址、密钥和模型权限；下方可先核对本地记录，不会再次请求模型。"
          : "尚未确认有可恢复的正文。先核对运行记录，看看哪些内容可以保留；这一步不会调用模型或提交章节。",
        reusable_scene_count: 0
      };
    } else if (recoveryChapter && !provenanceAtRecoveryChapter && !contextBudgetAtRecoveryChapter && !qualityRevisionAtRecoveryChapter && recovery?.recoverable) {
      recoveryIssue = {
        id: `chapter-${recoveryChapter}-recovery`,
        ...recovery,
        job_id: pausedRecoveryJob?.job_id || pausedRecoveryJob?.id || null,
        project_recovery: !pausedRecoveryJob,
        resume_ready: pausedRecoveryJob?.resume_ready === true,
        scene_schema_failure: sceneSchemaFailure,
        scene_schema_validation: sceneSchemaValidation,
        bridge_prevalidation: bridgePrevalidation,
        bridge_prevalidation_failure: bridgePrevalidationFailure,
        provider_partial_response: providerPartialResponse,
        provider_partial_response_failure: providerPartialResponseFailure,
        local_budget_after_provider: localBudgetAfterProvider,
        local_budget_after_provider_failure: localBudgetAfterProviderFailure,
        context_budget_after_provider: contextBudgetAfterProvider,
        context_budget_after_provider_failure: contextBudgetAfterProviderFailure,
        structured_context_after_provider: structuredContextAfterProvider,
        structured_context_after_provider_failure: structuredContextAfterProviderFailure,
        context_headroom_failure: contextHeadroomFailure,
        context_headroom: contextHeadroom,
        repair_authority_failure: repairAuthorityFailure,
        repair_authority: repairAuthority,
        stale_scene_evidence_failure: staleSceneEvidenceFailure,
        stale_scene_evidence: staleSceneEvidence,
        scene_boundary_validation_failure: sceneBoundaryValidationFailure,
        scene_boundary_validation: sceneBoundaryValidation,
        repair_not_converging: repairConvergenceFailure,
        quality_revision: qualityRejectionFailure,
        quality_rejection: qualityRejection,
        latest_run: latestRecoveryRun,
        retained_paid_draft_preview: recovery.retained_paid_draft_preview || null,
        title: retainedCompleteDraftAvailable(recovery) ? (durablePolishedDraftAvailable(recovery) ? `第 ${recoveryChapter} 章已有完整润色稿，请验证后继续` : `第 ${recoveryChapter} 章已保留完整候选稿，请验证后继续`) : repairConvergenceFailure ? (repairConvergenceFailure.verified ? `第 ${recoveryChapter} 章自动修订未收敛，已安全分类` : `第 ${recoveryChapter} 章修订故障正在安全核对`) : sceneSchemaFailure ? `第 ${recoveryChapter} 章场景响应格式错误，已安全分类` : bridgePrevalidationFailure ? `第 ${recoveryChapter} 章因衔接预检失败而停止，已安全分类` : providerPartialResponseFailure ? `第 ${recoveryChapter} 章因模型部分响应中断而停止，已保留已付费内容，已安全分类` : contextBudgetAfterProviderFailure ? `第 ${recoveryChapter} 章在已付费调用后因上下文预算中断，已保留已付费内容，已安全分类` : structuredContextAfterProviderFailure ? `第 ${recoveryChapter} 章在已付费调用后因结构化上下文装配中断，已保留已付费内容，已安全分类` : localBudgetAfterProviderFailure ? `第 ${recoveryChapter} 章在已产生已付费调用后因本地预算停止，已保留已付费内容，已安全分类` : repairContextHeadroomFailure ? `第 ${recoveryChapter} 章修订输入超过安全余量，已安全分类` : contextHeadroomFailure ? `第 ${recoveryChapter} 章边界重生输入超过安全余量，已安全分类` : repairAuthorityFailure ? `第 ${recoveryChapter} 章修订上下文装配失败，已安全分类` : staleSceneEvidenceFailure ? `第 ${recoveryChapter} 章自动修订因场景证据对不齐，已安全分类` : sceneBoundaryValidationFailure ? `第 ${recoveryChapter} 章因场景边界校验失败，已安全分类` : qualityRejectionFailure ? `第 ${recoveryChapter} 章质量门发现阻断问题，候选草稿可编辑` : durablePolishedDraftAvailable(recovery) ? `第 ${recoveryChapter} 章已有完整润色稿，请验证后继续` : retainedUnsettledPaidDraft({ latest_run: latestRecoveryRun }) ? `第 ${recoveryChapter} 章上次调用结果不确定，已保留 ${retainedUnsettledPaidDraft({ latest_run: latestRecoveryRun }).chapterChars} 字候选，等待无模型恢复分类` : recovery.uncertain_without_run ? `第 ${recoveryChapter} 章上次模型调用结果不确定` : `第 ${recoveryChapter} 章可安全恢复`,
        reason: repairConvergenceFailure
          ? repairNotConvergingSummary(repairConvergenceFailure)
          : sceneSchemaFailure
          ? `本次 ${Number(latestRecoveryRun.provider_calls) || 0} 次模型调用均已结算；场景响应的 JSON 格式错误。`
          : bridgePrevalidationFailure
          ? bridgePrevalidationSummary(bridgePrevalidation)
          : providerPartialResponseFailure
          ? providerPartialResponseSummary(providerPartialResponse)
          : contextBudgetAfterProviderFailure
          ? contextBudgetAfterProviderSummary(contextBudgetAfterProvider)
          : structuredContextAfterProviderFailure
          ? structuredContextAfterProviderSummary(structuredContextAfterProvider)
          : localBudgetAfterProviderFailure
          ? localBudgetAfterProviderSummary(localBudgetAfterProvider)
          : repairContextHeadroomFailure
          ? `本次 ${contextHeadroom.providerCalls} 次模型调用均已结算；修订输入超过 ${contextHeadroom.safeTarget} 安全目标 ${contextHeadroom.overageTokens} tokens。`
          : contextHeadroomFailure
          ? `本次 ${Number(latestRecoveryRun.provider_calls) || 0} 次模型调用均已结算；边界重生输入超过 ${contextHeadroom.safeTarget} 安全目标 ${contextHeadroom.overageTokens} tokens。`
          : repairAuthorityFailure
          ? `本次 ${repairAuthority.providerCalls} 次模型调用均已结算；${repairAuthority.chapterChars} 字候选已保留，修订所需权威记录超过旧预算 ${repairAuthority.overageChars} 字符。`
          : staleSceneEvidenceFailure
          ? `本次 ${staleSceneEvidence.providerCalls} 次模型调用均已结算；${staleSceneEvidence.chapterChars} 字候选已保留。${staleSceneEvidenceReasonLabel(staleSceneEvidence)}，系统已拒绝采用这次修订。`
          : sceneBoundaryValidationFailure
          ? sceneBoundaryValidationSummary(sceneBoundaryValidation)
          : qualityRejectionFailure
          ? `本次 ${qualityRejection.providerCalls} 次模型调用均已结算；${qualityRejection.chapterChars} 字候选已保留。阻断原因：${qualityProblemSummary(qualityRejection)}`
          : durablePolishedDraftAvailable(recovery)
          ? polishedDraftGuidance(recovery)
          : providerUncertainPaidDraftCopy({ latest_run: latestRecoveryRun })
          ? providerUncertainPaidDraftCopy({ latest_run: latestRecoveryRun })
          : recovery.uncertain_without_run
          ? "上次模型调用结果不确定，没有完整回执和章节运行记录。不要盲目重试。"
          : recovery.checkpoint ? `已分类 ${recovery.checkpoint.reusable_scene_count || 0} 个回执完整场景候选` : "生成未提交，先执行无模型恢复分类",
        summary: repairConvergenceFailure
          ? (repairConvergenceFailure.verified ? `${repairConvergenceFailure.providerCalls} 次模型调用全部结算，${repairConvergenceFailure.candidateChars} 字原候选已保留，正式正文未提交；不会采用失败修订稿。` : "调用、候选正文与提交状态正在安全核对；完成前不会恢复或重试。")
          : qualityRejectionFailure
          ? "请在下方恢复草稿中修正阻断问题，保存并重新验证；正式正文在通过前不会提交。"
          : staleSceneEvidenceFailure
          ? `正式正文没有提交；${staleSceneEvidence.chapterChars} 字候选已保留并完成恢复核对。继续时会重新校验，不会直接重试这次失败修订。`
          : sceneBoundaryValidationFailure
          ? "正式正文没有提交，场景边界校验未通过并已完成恢复核对。继续时会重新校验，不会盲目重试这次失败的场景生成。"
          : sceneSchemaFailure || contextHeadroomFailure || repairAuthorityFailure || providerPartialResponseFailure || localBudgetAfterProviderFailure || contextBudgetAfterProviderFailure || structuredContextAfterProviderFailure
          ? `正式正文没有提交；${Number(recovery.checkpoint?.reusable_scene_count || 0)} 个有回执的场景候选已完成恢复核对，继续时会重新验证后再进入修订与提交。`
          : durablePolishedDraftAvailable(recovery)
          ? polishedDraftGuidance(recovery)
          : providerUncertainPaidDraftCopy({ latest_run: latestRecoveryRun })
          ? "正式正文没有提交。已付费候选仍保留在隔离运行区，但不能当作已验证候选。先执行无模型恢复分类，不要先结束失败尝试来隐藏已付费正文，也不要盲目重试这次不确定调用。"
          : recovery.uncertain_without_run
          ? "电脑中断时模型调用没有留下完整回执。请先做无模型安全分类；分类会结束这次失败尝试，也可以直接结束本章并允许新尝试。正式正文与快照不会被改写。"
          : recovery.checkpoint ? "恢复检查点已建立；候选会先重跑当前状态边界，通过后才会复用。" : "工作台将先在不调用模型的情况下分类现有回执，不会改写正文。",
        reusable_scene_count: recovery.checkpoint?.reusable_scene_count || 0,
        next_scene: (recovery.checkpoint?.reusable_scene_count || 0) + 1
      };
    } else if (!provenanceAtRecoveryChapter && !contextBudgetAtRecoveryChapter && !qualityRevisionAtRecoveryChapter && pausedRecoveryJob && (recovery?.reset_cleared === true || recovery?.checkpoint?.action === "reset") && recovery?.formal_prose_exists !== true && !durablePolishedDraftAvailable(recovery)) {
      recoveryIssue = {
        ...pausedRecoveryJob,
        id: pausedRecoveryJob.id,
        job_id: pausedRecoveryJob.job_id || pausedRecoveryJob.id,
        chapter: recoveryChapter,
        checkpoint: recovery.checkpoint,
        project_recovery: false,
        recoverable: false,
        formal_prose_exists: recovery.formal_prose_exists,
        reset_cleared: true,
        uncertain_without_run: false,
        latest_run: latestRecoveryRun,
        title: `第 ${recoveryChapter} 章失败尝试已结束，可开始新尝试`,
        reason: "失败锁已解除，没有可复用的完整章节记录。关闭这次停住的生产任务后即可重新开写。",
        summary: "不要盲目重试上次不确定的模型调用。证据保留可追溯，正式正文与快照不会被改写。",
        reusable_scene_count: 0
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
    const pendingHeadingIdle = one("[data-pending-heading-idle]");
    const pendingHeadingActive = one("[data-pending-heading-active]");
    if (pendingHeadingIdle) pendingHeadingIdle.hidden = state.pending.length > 0;
    if (pendingHeadingActive) pendingHeadingActive.hidden = state.pending.length === 0;
    setText("[data-pending-lede]", state.pending.length === 0 ? "连续生产没有被阻塞。没有需要你现在决定的事项。" : provenanceIssues.length ? (provenanceUnverified ? "检测到疑似 Git 来源记录环境异常；调用、费用与产物证据正在安全核对，完成前不能重新执行。" : provenanceReady ? "Git 环境已修复；重新执行需要你的明确确认，点击前不会新增模型调用或费用。" : "Git 来源记录环境异常；本次失败证据已核对，系统修复前不能重新执行。") : contextBudgetIssues.length ? (contextBudgetUnverified ? "本地输入超过安全上限；调用、费用与产物证据正在安全核对，完成前不能重新执行。" : contextBudgetReady ? "上下文修复已完成；重新执行需要你的明确确认，点击前不会新增模型调用或费用。" : "本地输入在调用模型前超过安全上限；系统修复前不能重新执行。") : recoveryIssue?.repair_not_converging ? (recoveryIssue.repair_not_converging.verified ? "自动修订引入了更严重问题，系统已保留原候选并停止；下一步先做无模型恢复分类，不会盲目重试。" : "修订故障的调用、候选正文与提交证据正在安全核对；完成前不能恢复或重试。") : recoveryIssue?.context_headroom?.stage === "repair" ? "自动修订的输入超过本地安全余量，修订请求尚未发出；系统已保留候选正文。下一步先做无模型恢复分类，不会盲目重试。" : recoveryIssue?.stale_scene_evidence ? "自动修订与已生成场景证据对不齐，系统已保留候选正文并停止；下一步先做无模型恢复分类，不会盲目重试这次失败修订。" : retainedCompleteDraftAvailable(recoveryIssue) ? retainedDraftGuidance(recoveryIssue) : recoveryIssue?.structured_context_after_provider ? "在已付费调用后，结构化上下文装配超过本地字符预算，已保留已付费内容；下一步先做无模型恢复分类，不会盲目重试。" : qualityRevisionIssues.length || recoveryIssue?.quality_revision ? "质量门要求修订候选草稿；正式正文在重新验证通过前不会提交。" : durablePolishedDraftAvailable(recoveryIssue) ? polishedDraftGuidance(recoveryIssue) : recoveryIssue?.uncertain_without_run ? "上次模型调用结果不确定，没有完整回执。不要盲目重试；请先做无模型安全分类，或在此结束失败尝试并允许新的一次写作。" : recoveryIssue?.reset_cleared ? "失败锁已解除。关闭这次停住的生产任务后，就可以重新开写本章。" : "自动生产在不确定处停下，这里只展示必须由作者决定的内容。");
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
    else {
      one("[data-recovery-draft-panel]").hidden = true;
      const inspectPanel = one("[data-pending-detail] [data-retained-paid-draft-panel]");
      if (inspectPanel) inspectPanel.hidden = true;
    }
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
    state.runs = [...jobRecords, ...runRecords.map((run) => {
      const started = run.started_at ? Date.parse(run.started_at) : NaN;
      const finished = run.finished_at ? Date.parse(run.finished_at) : NaN;
      const elapsed = Number.isFinite(started) && Number.isFinite(finished) && finished >= started
        ? `${Math.round((finished - started) / 1000)} 秒` : "—";
      return {
        ...run,
        history_source: "run",
        chapter: run.chapter_index ?? run.chapter,
        created_at_label: run.created_at_label || run.time_label
          || (Number.isFinite(started) ? new Date(started).toLocaleString("zh-CN") : "—"),
        duration_label: run.duration_label || elapsed
      };
    })];
    const stats = payload.stats;
    const hasStats = Boolean(stats) && typeof stats === "object";
    setText("[data-history-total]", hasStats && stats.total_runs != null ? stats.total_runs : "—");
    setText("[data-history-total-note]", hasStats ? (stats.range_label || "") : "");
    setText("[data-history-pass-rate]", hasStats && stats.first_pass_rate ? stats.first_pass_rate : "—");
    setText("[data-history-pass-note]", hasStats ? (stats.first_pass_note || "") : "");
    setText("[data-history-recoveries]", hasStats && stats.recoveries != null ? stats.recoveries : "—");
    setText("[data-history-recoveries-note]", hasStats ? (stats.intervention_note || "") : "");
    setText("[data-history-cost]", hasStats && stats.cost_label ? stats.cost_label : "—");
    setText("[data-history-cost-note]", hasStats ? (stats.token_label || "") : "");
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
    const preview = retainedPaidDraftPreview(payload);
    if (preview) {
      const inspect = el("section", "panel recovery-draft-panel");
      inspect.dataset.historyRetainedPaidDraft = "";
      const heading = el("div", "section-heading compact");
      const headingCopy = el("div");
      headingCopy.append(el("p", "eyebrow", "已付费正文（只读）"));
      headingCopy.append(el("h2", "", run?.chapter_index ? `第 ${run.chapter_index} 章保留的已付费候选` : "保留的已付费候选"));
      heading.append(headingCopy);
      inspect.append(heading);
      inspect.append(el("p", "muted-copy", "这是该次运行隔离区里保留的已付费正文，只供阅读核对，不能当作已验证候选，也不能确认提交。这不是后来提交的正式正文。"));
      const stage = uncertainFailedStageLabel(preview.failedAction || preview.failed_action);
      const chars = Number(preview.chars || preview.text.length) || preview.text.length;
      const parts = [`${chars} 字`];
      if (stage) parts.push(`${stage}阶段中断`);
      if (preview.source_run_id) parts.push(`运行 ${String(preview.source_run_id).slice(0, 28)}`);
      if (Number(preview.unsettled_attempt_count) === 1) parts.push("最后一次调用尚未结算");
      inspect.append(el("p", "", parts.join(" · ")));
      const label = el("label", "field");
      label.append(el("span", "", "保留的已付费正文"));
      const textarea = el("textarea");
      textarea.className = "prose-editor";
      textarea.rows = 18;
      textarea.readOnly = true;
      textarea.value = preview.text;
      label.append(textarea);
      inspect.append(label);
      panel.append(inspect);
    }
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
    if (activation) activation.hidden = Boolean(health.production_eligible || state.project?.reviewed_format);
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

  one("[data-reviewed-project-recover]")?.addEventListener("click", withFeedback(async (event) => {
    const button = event.currentTarget;
    const projectId = state.projectId;
    const errorBox = one("[data-reviewed-project-error]");
    button.disabled = true;
    if (errorBox) errorBox.hidden = true;
    try {
      await api(`/api/projects/${encodeURIComponent(projectId)}/publication-recovery`, {
        method: "POST", json: { author_confirmed: true }
      });
      projectCache.delete(projectId);
      if (state.projectId === projectId) {
        await loadProject(projectId, true);
        await route();
      }
    } catch (error) {
      if (errorBox && state.projectId === projectId) {
        errorBox.textContent = UI.errorMessage(error?.payload, error?.status);
        errorBox.hidden = false;
      }
    } finally { button.disabled = false; }
  }));

  const loadHealth = async () => {
    const { payload } = await api(`/api/projects/${encodeURIComponent(state.projectId)}/health`);
    state.detail = { ...(state.detail || {}), health: payload.health || payload };
    projectCache.set(state.projectId, state.detail);
    renderHealth();
    return state.detail.health;
  };

  const route = async () => {
    if (!bootstrapReady || state.restoreControllerOnly) return;
    if (chapterProgressTimer != null) window.clearTimeout(chapterProgressTimer);
    chapterProgressTimer = null;
    clearError();
    const next = parseRoute();
    const routeRevision = ++state.routeRevision;
    invalidateChapterCardSubmission();
    invalidateCandidateAction();
    invalidateFactAction();
    const isCurrentRoute = () => state.routeRevision === routeRevision;
    state.route = next;
    window.NovelAgentModelUsage?.setRoute(next);
    window.NovelAgentChapterBudget?.setContext(null);
    state.chapterBudgetBlocked = false;
    showLoading(true);
    try {
      if (next.area === "bookshelf") {
        setView("bookshelf");
        await loadProjects();
        return;
      }
      if (!state.projects.length) await loadProjects();
      if (!isCurrentRoute()) return;
      const loadedProject = await loadProject(
        next.projectId,
        next.projectId !== state.projectId,
        routeRevision,
      );
      if (!loadedProject || !isCurrentRoute()) return;
      setView(next.area);
      if (next.area === "overview") {
        const detail = await loadProject(next.projectId, true, routeRevision);
        if (!detail || !isCurrentRoute()) return;
        try { detail.health = await loadHealth(); } catch { /* Overview remains useful when health parsing fails. */ }
        try {
          const chapterPayload = (await api(`/api/projects/${encodeURIComponent(state.projectId)}/chapters`)).payload;
          detail.overview = detail.overview || {};
          detail.overview.recent_chapters = arrayFrom(chapterPayload, "items", "chapters").filter((chapter) => chapter.has_prose !== false).slice(-5).reverse();
        } catch { /* A missing chapter list must not hide the rest of the overview. */ }
        renderOverview(detail);
      }
      else if (next.area === "chapters") await loadChapters();
      else if (next.area === "chapter") await loadChapter(next.chapter, routeRevision);
      else if (next.area === "outlines") await loadOutlines();
      else if (next.area === "production") await loadProduction();
      else if (next.area === "pending") await loadPending();
      else if (next.area === "history") await loadHistory();
      else if (next.area === "health") await loadHealth();
    } catch (error) {
      if (isCurrentRoute()) showError(error);
    } finally {
      if (isCurrentRoute()) showLoading(false);
    }
  };

  one("[data-spa-retry]")?.addEventListener("click", withFeedback(route));
  window.addEventListener("hashchange", withFeedback(route));

  all('button[data-chapter-step]').forEach(button => button.addEventListener("click", () => {
    const target = { intent: '[data-chapter-card]', candidate: state.chapterCandidate?.draft
      ? '[data-candidate-editor]' : '[data-chapter-candidate-panel]', facts: '[data-chapter-fact-panel]' }[button.dataset.chapterStep];
    if (!target || button.disabled) return;
    one('[data-tab="outline"]')?.click();
    const panel = one(target);
    if (panel && !panel.hidden) { panel.setAttribute('tabindex', '-1'); panel.focus({ preventScroll: true }); panel.scrollIntoView({ block: 'start', behavior: 'smooth' }); }
  }));

  one("[data-spa-chapter-search]")?.addEventListener("input", (event) => {
    const query = event.target.value.trim().toLocaleLowerCase("zh-CN");
    all("[data-chapter-index] .chapter-index-row").forEach((row) => { row.hidden = Boolean(query) && !(row.dataset.searchValue || "").toLocaleLowerCase("zh-CN").includes(query); });
  });
  all("[data-spa-chapter-filters] button").forEach((button) => button.addEventListener("click", () => {
    all("[data-spa-chapter-filters] button").forEach((item) => item.classList.toggle("is-active", item === button));
    all("[data-chapter-index] .chapter-index-row").forEach((row) => { row.hidden = button.dataset.value !== "all" && row.dataset.status !== button.dataset.value; });
  }));
  const beginCandidateAction = ({ candidate = null, draft = null, requireCardBinding = false, requireConfirmation = false } = {}) => {
    state.candidateActionNonce += 1;
    return Object.freeze({
      nonce: String(state.candidateActionNonce),
      route_revision: state.routeRevision,
      project_id: state.projectId,
      chapter: Number(state.route?.chapter),
      candidate_id: candidate?.candidate_id || null,
      draft_id: draft?.id || null,
      card_sha256: state.chapterCard?.confirmation?.card_sha256 || null,
      confirmation_sha256: state.chapterConfirmation?.confirmation_sha256 || null,
      require_card_binding: requireCardBinding,
      require_confirmation: requireConfirmation
    });
  };
  const setCandidateButtonBusy = (button, busy) => {
    if (!button) return;
    button.disabled = busy;
    button.classList.toggle("is-busy", busy);
    renderChapterWorkflow();
  };
  const finishCandidateAction = (button, action) => {
    if (!candidateActionTargetsCurrentChapter({ action })) return;
    if (button) { button.disabled = false; button.classList.remove("is-busy"); }
    renderChapterCandidate(state.chapterCandidate, state.chapterCandidateError, { keepActionError: true });
    void window.NovelAgentChapterBudget?.refresh();
  };
  const showCandidateActionError = (message) => {
    const errorBox = one("[data-candidate-action-error]");
    if (!errorBox) return;
    errorBox.textContent = message;
    errorBox.hidden = false;
    errorBox.focus?.();
  };
  const candidateFromPayload = (payload) => payload?.candidate || payload;

  one('[data-author-draft-text]')?.addEventListener('input', event => { if (!event.target.value) delete event.target.dataset.fragmentChapter; });
  one('[data-author-draft-open]')?.addEventListener('click', () => {
    const panel=one('[data-author-draft-panel]');
    panel.hidden=false; one('[data-author-draft-text]',panel)?.focus();
  });
  one('[data-author-draft-import]')?.addEventListener('click', withFeedback(async (event) => {
    const button=event.currentTarget;
    if (button.disabled || !currentConfirmationMatchesCard() || state.chapterRecoveryRequired || state.chapterCandidateError) return;
    const input=one('[data-author-draft-text]');
    if (input.dataset.fragmentChapter && input.dataset.fragmentChapter !== `${state.projectId}/${Number(state.route?.chapter)}`) { setText('[data-author-draft-error]','这段中断原文来自另一章。请回到原章节导入，或清空导入框后填写本章稿件。'); return; }
    const text=input.value, origin=one('[data-author-draft-origin]').value;
    if (!text.trim() || !origin.trim()) { setText('[data-author-draft-error]','请填写正文和稿件来源。'); return; }
    const confirmation=JSON.parse(JSON.stringify(state.chapterConfirmation));
    const action=beginCandidateAction({requireCardBinding:true,requireConfirmation:true});
    // Identical content/confirmation retries keep the original request identity.
    const bytes=new TextEncoder().encode(JSON.stringify({text,origin,confirmation}));
    const sha=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))).map(b=>b.toString(16).padStart(2,'0')).join('');
    setCandidateButtonBusy(button,true); setText('[data-author-draft-error]','');
    try {
      const {payload}=await api(`/api/projects/${encodeURIComponent(action.project_id)}/chapters/${action.chapter}/author-drafts`,{
        method:'POST',json:{confirmation,text,origin_label:origin,idempotency_key:`author-import-${sha}`}});
      const candidate=candidateFromPayload(payload);
      if (!candidateActionTargetsCurrentChapter({action,candidate})) return;
      renderChapterCandidate(candidate,''); one('[data-author-draft-panel]').hidden=true;
      await refreshChapterFactsForCurrentDraft(); UI.showToast(payload.message);
    } catch(error) {
      if (candidateActionTargetsCurrentChapter({action})) setText('[data-author-draft-error]',UI.errorMessage(error?.payload,error?.status));
    } finally { finishCandidateAction(button,action); }
  }));

  one("[data-chapter-estimate]")?.addEventListener("click", withFeedback(estimateForCurrentChapter));
  one("[data-candidate-generate]")?.addEventListener("click", withFeedback(async (event) => {
    const button = event.currentTarget;
    if (!button || button.disabled) return;
    if (!currentConfirmationMatchesCard() || state.chapterCandidateError) {
      UI.showToast(state.chapterCandidateError ? "Candidate 状态尚未恢复，不能重复调用模型。" : "请先确认当前章节卡。", "error");
      return;
    }
    const key = quoteKey();
    const accepted = await UI.confirmAction("这一步会调用你当前配置的模型服务，按该服务的实际用量和单价计费。费用估算可选；超时也可能计费。结果只保存为正文候选，不会正式入稿。确认继续吗？");
    if (!accepted || !key || quoteKey() !== key || button.disabled) return;
    const confirmation = JSON.parse(JSON.stringify(state.chapterConfirmation));
    const action = beginCandidateAction({ requireCardBinding: true, requireConfirmation: true });
    const operationKey = candidateOperationKey(action.project_id, action.chapter, confirmation.confirmation_sha256);
    setCandidateButtonBusy(button, true);
    try {
      const { payload } = await api(candidateCollectionPath(action.project_id, action.chapter), {
        method: "POST",
        headers: { "Idempotency-Key": operationKey },
        json: { confirmation, idempotency_key: operationKey }
      });
      const candidate = candidateFromPayload(payload);
      if (!candidateActionTargetsCurrentChapter({ action, candidate })) return;
      state.chapterCandidateError = "";
      renderChapterCandidate(candidate, "");
      await refreshChapterFactsForCurrentDraft();
      if (["completed", "stale", "failed", "provider_uncertain", "cancelled", "abandoned"].includes(candidate.status)) {
        clearCandidateOperationKey(action.project_id, action.chapter, confirmation.confirmation_sha256);
      }
      UI.showToast(payload.message || "正文候选状态已更新。");
    } catch (error) {
      if (!candidateActionTargetsCurrentChapter({ action })) return;
      const message = UI.errorMessage(error?.payload, error?.status);
      const errorCode = error?.payload?.error?.code;
      if (error?.status === 409 && errorCode === "chapter_card_changed") {
        const form = one("[data-chapter-card-confirm-form]");
        const preservedIntent = {
          goal: one('textarea[name="goal"]', form)?.value || confirmation.chapter_intent?.goal || "",
          validation_focus: all('input[name="validation_focus"]', form).filter((input) => input.checked).map((input) => input.value),
          notes_text: one('textarea[name="notes_text"]', form)?.value || (confirmation.chapter_intent?.notes || []).join("\n")
        };
        clearChapterConfirmation(action.project_id, action.chapter);
        state.chapterConfirmation = null;
        renderChapterConfirmationState(null);
        clearCandidateOperationKey(action.project_id, action.chapter, confirmation.confirmation_sha256);
        await loadChapter(action.chapter, action.route_revision);
        if (!candidateActionTargetsCurrentChapter({ action })) return;
        const currentForm = one("[data-chapter-card-confirm-form]");
        one('textarea[name="goal"]', currentForm).value = preservedIntent.goal;
        const selected = new Set(preservedIntent.validation_focus);
        all('input[name="validation_focus"]', currentForm).forEach((input) => { input.checked = selected.has(input.value); });
        one('textarea[name="notes_text"]', currentForm).value = preservedIntent.notes_text;
        currentForm.dataset.inputRevision = String(Number(currentForm.dataset.inputRevision || 0) + 1);
        showCandidateActionError(`${message} 你的意图编辑已保留；请核对最新章节卡后重新确认。`);
      } else {
        showCandidateActionError(message);
      }
      UI.showToast(message, "error");
    } finally {
      finishCandidateAction(button, action);
    }
  }));

  one("[data-candidate-resume]")?.addEventListener("click", withFeedback(async (event) => {
    const button = event.currentTarget;
    if (!button || button.disabled) return;
    const candidate = state.chapterCandidate;
    if (!candidate || candidate.status !== "needs_recovery" || !candidateBindingMatchesCard(candidate)) return;
    const accepted = await UI.confirmAction("继续会重新进入受保护的 Provider 边界，并先读取本地 Intent/Receipt；系统不会盲目重试。确认继续吗？");
    if (!accepted) return;
    const action = beginCandidateAction({ candidate, requireCardBinding: true });
    setCandidateButtonBusy(button, true);
    try {
      const { payload } = await api(`${candidateDetailPath(action.project_id, action.chapter, action.candidate_id)}/resume`, {
        method: "POST",
        json: { expected_intent_hash: candidate.intent_hash, author_confirmed: true }
      });
      const updated = candidateFromPayload(payload);
      if (!candidateActionTargetsCurrentChapter({ action, candidate: updated })) return;
      renderChapterCandidate(updated, "");
      await refreshChapterFactsForCurrentDraft();
      UI.showToast(payload.message || "正文候选状态已更新。");
    } catch (error) {
      if (!candidateActionTargetsCurrentChapter({ action })) return;
      const message = UI.errorMessage(error?.payload, error?.status);
      showCandidateActionError(message);
      UI.showToast(message, "error");
    } finally {
      finishCandidateAction(button, action);
    }
  }));

  one("[data-candidate-create-draft]")?.addEventListener("click", withFeedback(async (event) => {
    const button = event.currentTarget;
    if (!button || button.disabled) return;
    const candidate = state.chapterCandidate;
    if (!candidate || candidate.status !== "completed" || candidate.draft) return;
    const accepted = await UI.confirmAction("把这份不可变正文候选复制为可编辑工作台草稿？这一步不会修改正式正文或权威状态。");
    if (!accepted) return;
    const action = beginCandidateAction({ candidate });
    setCandidateButtonBusy(button, true);
    try {
      const { payload } = await api(`${candidateDetailPath(action.project_id, action.chapter, action.candidate_id)}/draft`, {
        method: "POST",
        json: { expected_prose_sha256: candidate.prose?.sha256, author_confirmed: true }
      });
      const updated = candidateFromPayload(payload);
      if (!candidateActionTargetsCurrentChapter({ action, candidate: updated })) return;
      renderChapterCandidate(updated, "");
      await refreshChapterFactsForCurrentDraft();
      UI.showToast(payload.message || "可编辑工作台草稿已建立。");
    } catch (error) {
      if (!candidateActionTargetsCurrentChapter({ action })) return;
      const message = UI.errorMessage(error?.payload, error?.status);
      showCandidateActionError(message);
      UI.showToast(message, "error");
    } finally {
      finishCandidateAction(button, action);
    }
  }));

  one("[data-candidate-abandon-provider]")?.addEventListener("click", withFeedback(async (event) => {
    const button = event.currentTarget;
    if (!button || button.disabled) return;
    const candidate = state.chapterCandidate;
    if (!candidate || candidate.status !== "provider_uncertain" || !candidate.uncertainty?.sha256) return;
    const accepted = await UI.confirmAction("放弃表示这次未知结果不再采用，调用证据和预算占用继续保留。放弃本身不保证可以重试。确认放弃吗？", "danger");
    if (!accepted) return;
    const action = beginCandidateAction({ candidate });
    setCandidateButtonBusy(button, true);
    try {
      const { payload } = await api(`${candidateDetailPath(action.project_id, action.chapter, action.candidate_id)}/abandon-provider`, {
        method: "POST",
        json: {
          expected_intent_hash: candidate.intent_hash,
          expected_record_hash: candidate.record_hash,
          expected_uncertainty_sha256: candidate.uncertainty.sha256,
          author_confirmed: true
        }
      });
      const updated = candidateFromPayload(payload);
      if (!candidateActionTargetsCurrentChapter({ action, candidate: updated })) return;
      renderChapterCandidate(updated, "");
      UI.showToast(payload.message || "不确定 Provider 结果已放弃采用；证据仍被保留。");
    } catch (error) {
      if (!candidateActionTargetsCurrentChapter({ action })) return;
      const message = UI.errorMessage(error?.payload, error?.status);
      showCandidateActionError(message);
      UI.showToast(message, "error");
    } finally {
      finishCandidateAction(button, action);
    }
  }));

  const continuationAction = async (event, start) => {
    const button = event.currentTarget, candidate = state.chapterCandidate;
    if (!button || button.disabled || !candidate?.continuation) return;
    if (candidate.continuation.state !== (start ? 'ready' : 'available')) return;
    const accepted = await UI.confirmAction(start
      ? '现在发送这一次续试请求？将沿用原章意图，保留旧请求的全部预算占用；可能产生额外费用。失败后不会自动再试。'
      : '上一笔请求可能已收费且结果未知。确认不采用该未知结果，保留其全部占用，并在本章剩余限额内允许同一章再试一次？这一步只保存许可，不发送请求。', 'danger');
    if (!accepted || state.chapterCandidate?.candidate_id !== candidate.candidate_id) return;
    const action = beginCandidateAction({ candidate });
    setCandidateButtonBusy(button, true);
    try {
      const json = start ? { expected_grant_sha256: candidate.continuation.grant_sha256, author_confirmed: true }
        : { expected_ledger_sha256: candidate.continuation.expected_ledger_sha256,
          expected_intent_hash: candidate.intent_hash, expected_record_hash: candidate.record_hash,
          expected_uncertainty_sha256: candidate.uncertainty.sha256, author_confirmed: true };
      const { payload } = await api(`${candidateDetailPath(action.project_id, action.chapter, action.candidate_id)}/continuation${start ? '/run' : ''}`, { method: 'POST', json });
      if (!candidateActionTargetsCurrentChapter({ action })) return;
      state.chapterCandidateError = '';
      renderChapterCandidate(candidateFromPayload(payload), '');
      if (start) await refreshChapterFactsForCurrentDraft();
      UI.showToast(payload.message);
    } catch (error) {
      if (!candidateActionTargetsCurrentChapter({ action })) return;
      showCandidateActionError(`${UI.errorMessage(error?.payload, error?.status)} 请刷新读取保存的状态；不要重复生成。`);
    } finally {
      finishCandidateAction(button, { ...action, candidate_id: null });
      void window.NovelAgentChapterBudget?.refresh();
    }
  };
  one('[data-continuation-authorize]')?.addEventListener('click', withFeedback(event => continuationAction(event, false)));
  one('[data-continuation-run]')?.addEventListener('click', withFeedback(event => continuationAction(event, true)));
  one('[data-continuation-result]')?.addEventListener('click', withFeedback(async (event) => {
    const candidate = state.chapterCandidate, target = candidate?.continuation?.target_candidate_id;
    if (!target) return;
    const action = beginCandidateAction({ candidate });
    try {
      const { payload } = await api(candidateDetailPath(action.project_id, action.chapter, target));
      if (!candidateActionTargetsCurrentChapter({ action })) return;
      renderChapterCandidate(candidateFromPayload(payload), '');
      await refreshChapterFactsForCurrentDraft();
    } catch (error) {
      if (candidateActionTargetsCurrentChapter({ action })) showCandidateActionError(UI.errorMessage(error?.payload, error?.status));
    }
  }));

  one("[data-candidate-draft-form]")?.addEventListener("input", (event) => {
    const form = event.currentTarget;
    form.classList.add("is-dirty");
    setText("[data-candidate-draft-save-state]", "有尚未保存的修改", form);
  });

  one("[data-candidate-draft-form]")?.addEventListener("submit", withFeedback(async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    if (!form.reportValidity()) return;
    const candidate = state.chapterCandidate;
    const draft = candidate?.draft;
    if (!candidate || !draft || form.dataset.draftId !== draft.id) return;
    const localText = one('textarea[name="text"]', form).value;
    const action = beginCandidateAction({ candidate, draft });
    const button = event.submitter || one("[data-candidate-draft-save]", form);
    const errorBox = one("[data-candidate-draft-error]", form);
    if (errorBox) { errorBox.hidden = true; errorBox.textContent = ""; }
    setCandidateButtonBusy(button, true);
    try {
      const { payload } = await api(`/api/candidate-drafts/${encodeURIComponent(action.draft_id)}`, {
        method: "PUT",
        json: { text: localText, expected_revision: draft.revision }
      });
      const updatedDraft = payload.draft || payload.candidate?.draft || payload;
      if (!candidateActionTargetsCurrentChapter({ action, draft: updatedDraft })) return;
      form.classList.remove("is-dirty");
      renderChapterCandidate({ ...candidate, draft: updatedDraft }, "");
      await refreshChapterFactsForCurrentDraft();
      UI.showToast(payload.message || "工作台草稿已保存。");
    } catch (error) {
      if (!candidateActionTargetsCurrentChapter({ action })) return;
      const message = UI.errorMessage(error?.payload, error?.status);
      if (error?.status === 409) {
        try {
          const { payload } = await api(candidateDetailPath(action.project_id, action.chapter, action.candidate_id));
          const fresh = candidateFromPayload(payload);
          if (!candidateActionTargetsCurrentChapter({ action, candidate: fresh })) return;
          renderChapterCandidate(fresh, "", { preserveDraftText: localText });
        } catch { /* Keep the author's textarea intact even if metadata refresh fails. */ }
      }
      const currentErrorBox = one("[data-candidate-draft-error]");
      if (currentErrorBox) {
        currentErrorBox.textContent = error?.status === 409 ? `${message} 你的文字已原样保留；请先在新页面核对已保存版本，确认后再保存本页修改。` : message;
        currentErrorBox.hidden = false;
        currentErrorBox.focus?.();
      }
      UI.showToast(message, "error");
    } finally {
      finishCandidateAction(button, action);
    }
  }));

  const setFactButtonBusy = (button, busy) => {
    if (!button) return;
    button.disabled = busy;
    button.classList.toggle("is-busy", busy);
    renderChapterWorkflow();
    const form = one("[data-fact-review-form]");
    if (busy && form) {
      form.setAttribute("aria-busy", "true");
      all("input, textarea, select, button", form).forEach(control => {
        control.dataset.factPreviouslyDisabled = String(control.disabled); control.disabled = true;
      });
    }
  };
  const showFactError = (message, selector = "[data-fact-action-error]") => {
    const box = one(selector);
    if (!box) return;
    box.textContent = String(message).includes('event proposal requires at least one explicit anchor')
      ? '请为事件补充发生地点、参与人物或关联对象中的至少一项，再保存。你填写的内容仍保留在页面中。'
      : message;
    box.hidden = false;
    box.focus?.();
  };
  const finishFactAction = (button, action) => {
    if (!factActionTargetsCurrentDraft({ action })) return;
    if (button) { button.disabled = false; button.classList.remove("is-busy"); }
    const form = one("[data-fact-review-form]");
    form?.removeAttribute("aria-busy");
    all("[data-fact-previously-disabled]", form).forEach(control => {
      control.disabled = control.dataset.factPreviouslyDisabled === "true" || state.chapterFactReview?.state !== "editing";
      delete control.dataset.factPreviouslyDisabled;
    });
    updateFactReviewLocalState();
    renderChapterWorkflow();
    void window.NovelAgentChapterBudget?.refresh();
  };
  const extractionFromPayload = (payload) => payload?.fact_extraction || payload;
  const reviewFromPayload = (payload) => payload?.fact_review || payload;

  one('[data-fact-manual]')?.addEventListener('click', withFeedback(async (event) => {
    const button = event.currentTarget;
    const allowed = () => canStartManualFactReview({ draft: state.chapterCandidate?.draft,
      review: state.chapterFactReview, extraction: state.chapterFactExtraction, error: state.chapterFactError });
    if (!button || button.disabled || !allowed()) return;
    if (one('[data-candidate-draft-form].is-dirty')) { UI.showToast('请先保存正文修改，再开始事实审阅。', 'error'); return; }
    const action = beginFactAction();
    if (!action) return;
    const accepted = await UI.confirmAction('将基于当前已保存正文打开空白事实清单，由你补充并逐项审阅。不调用模型，不改变旧请求费用，也不会直接保存为正式章节。继续吗？');
    if (!accepted || !factActionTargetsCurrentDraft({ action }) || !allowed()
        || one('[data-candidate-draft-form].is-dirty')) return;
    setFactButtonBusy(button, true);
    try {
      const { payload } = await api(`/api/candidate-drafts/${encodeURIComponent(action.draft_id)}/fact-reviews`, {
        method: 'POST', json: { expected_draft_revision: action.draft_revision,
          expected_draft_sha256: action.draft_sha256, proposals: [], author_confirmed: true }
      });
      const review = reviewFromPayload(payload);
      if (!factActionTargetsCurrentDraft({ action, review })) return;
      renderChapterFacts(state.chapterFactExtraction, review, '');
      UI.showToast('已打开自行审阅清单，请补充正文中的关键变化。');
    } catch (error) {
      if (!factActionTargetsCurrentDraft({ action })) return;
      showFactError(UI.errorMessage(error?.payload, error?.status));
    } finally { finishFactAction(button, action); }
  }));

  one("[data-fact-open-review]")?.addEventListener("click", withFeedback(async (event) => {
    const extraction = state.chapterFactExtraction;
    if (extraction?.status !== "completed" || extraction.source_state !== "current" || state.chapterFactReview) return;
    if (one('[data-candidate-draft-form].is-dirty')) { UI.showToast("请先保存正文修改，再核对事实是否仍有效。", "error"); return; }
    const action = beginFactAction();
    if (!action) return;
    const button = event.currentTarget;
    setFactButtonBusy(button, true);
    try {
      const { payload } = await api(`${factExtractionCollectionPath(action.draft_id)}/${encodeURIComponent(extraction.extraction_id)}/review`, {
        method: "POST", json: { expected_record_hash: extraction.record_hash, author_confirmed: true }
      });
      const updated = extractionFromPayload(payload), review = updated?.fact_review || null;
      if (!factActionTargetsCurrentDraft({ action, extraction: updated, review })) return;
      renderChapterFacts(updated, review, "");
      UI.showToast(payload.message || "已打开保存的事实审核。");
    } catch (error) {
      if (!factActionTargetsCurrentDraft({ action })) return;
      showFactError(UI.errorMessage(error?.payload, error?.status));
    } finally { finishFactAction(button, action); }
  }));

  one("[data-fact-extract]")?.addEventListener("click", withFeedback(async (event) => {
    const button = event.currentTarget;
    if (!button || button.disabled) return;
    const draft = state.chapterCandidate?.draft;
    const draftForm = one("[data-candidate-draft-form]");
    if (!draft) return;
    if (draftForm?.classList.contains("is-dirty")) {
      UI.showToast("请先保存正文草稿，再提取事实候选。", "error");
      return;
    }
    const accepted = await UI.confirmAction("这一步会调用 Provider，但模型只提取待审事实卡，不会修改正文或权威状态。确认从当前已保存草稿继续吗？");
    if (!accepted || draftForm?.classList.contains("is-dirty")) return;
    const action = beginFactAction();
    if (!action) return;
    const operationKey = factOperationKey(draft);
    setFactButtonBusy(button, true);
    try {
      const { payload } = await api(factExtractionCollectionPath(action.draft_id), {
        method: "POST",
        headers: { "Idempotency-Key": operationKey },
        json: {
          expected_draft_revision: action.draft_revision,
          expected_draft_sha256: action.draft_sha256,
          idempotency_key: operationKey,
          author_confirmed: true
        }
      });
      const extraction = extractionFromPayload(payload);
      const review = extraction?.fact_review || null;
      if (!factActionTargetsCurrentDraft({ action, extraction, review })) return;
      renderChapterFacts(extraction, review, "");
      if (["completed", "failed", "provider_uncertain", "cancelled", "abandoned"].includes(extraction.status)) clearFactOperationKey(draft);
      UI.showToast(payload.message || "事实候选状态已更新。");
    } catch (error) {
      if (!factActionTargetsCurrentDraft({ action })) return;
      const message = UI.errorMessage(error?.payload, error?.status);
      showFactError(message);
      UI.showToast(message, "error");
    } finally {
      finishFactAction(button, action);
    }
  }));

  one("[data-fact-resume]")?.addEventListener("click", withFeedback(async (event) => {
    const button = event.currentTarget;
    if (!button || button.disabled) return;
    const extraction = state.chapterFactExtraction;
    if (!extraction || extraction.status !== "needs_recovery") return;
    const accepted = await UI.confirmAction("恢复会先读取本地 Intent 和 Receipt；只有确实没有结算结果时才重新进入受保护的 Provider 边界。确认继续吗？");
    if (!accepted) return;
    const action = beginFactAction();
    if (!action) return;
    setFactButtonBusy(button, true);
    try {
      const path = `${factExtractionCollectionPath(action.draft_id)}/${encodeURIComponent(extraction.extraction_id)}/resume`;
      const { payload } = await api(path, {
        method: "POST",
        json: { expected_intent_hash: extraction.intent_hash, author_confirmed: true }
      });
      const updated = extractionFromPayload(payload);
      const review = updated?.fact_review || null;
      if (!factActionTargetsCurrentDraft({ action, extraction: updated, review })) return;
      renderChapterFacts(updated, review, "");
      UI.showToast(payload.message || "事实提取状态已更新。");
    } catch (error) {
      if (!factActionTargetsCurrentDraft({ action })) return;
      const message = UI.errorMessage(error?.payload, error?.status);
      showFactError(message);
      UI.showToast(message, "error");
    } finally {
      finishFactAction(button, action);
    }
  }));

  one("[data-fact-cancel]")?.addEventListener("click", withFeedback(async (event) => {
    const button = event.currentTarget;
    if (!button || button.disabled) return;
    const extraction = state.chapterFactExtraction;
    if (!extraction || extraction.status !== "needs_recovery") return;
    const accepted = await UI.confirmAction("取消只会关闭尚未开始的事实提取，并保留 Intent 证据；不会伪造 Provider 回执。确认取消吗？", "danger");
    if (!accepted) return;
    const action = beginFactAction();
    if (!action) return;
    setFactButtonBusy(button, true);
    try {
      const path = `${factExtractionCollectionPath(action.draft_id)}/${encodeURIComponent(extraction.extraction_id)}/cancel`;
      const { payload } = await api(path, {
        method: "POST",
        json: { expected_intent_hash: extraction.intent_hash, author_confirmed: true }
      });
      const updated = extractionFromPayload(payload);
      if (!factActionTargetsCurrentDraft({ action, extraction: updated })) return;
      renderChapterFacts(updated, updated?.fact_review || null, "");
      UI.showToast(payload.message || "事实提取已取消。");
    } catch (error) {
      if (!factActionTargetsCurrentDraft({ action })) return;
      const message = UI.errorMessage(error?.payload, error?.status);
      showFactError(message);
      UI.showToast(message, "error");
    } finally {
      finishFactAction(button, action);
    }
  }));

  one("[data-fact-abandon-provider]")?.addEventListener("click", withFeedback(async (event) => {
    const button = event.currentTarget;
    if (!button || button.disabled) return;
    const extraction = state.chapterFactExtraction;
    if (!extraction || extraction.status !== "provider_uncertain" || !extraction.uncertainty?.sha256) return;
    const accepted = await UI.confirmAction("放弃表示这次未知 Provider 结果永不采用；Intent、调用证据和不确定记录会永久保留。确认放弃吗？", "danger");
    if (!accepted) return;
    const action = beginFactAction();
    if (!action) return;
    setFactButtonBusy(button, true);
    try {
      const path = `${factExtractionCollectionPath(action.draft_id)}/${encodeURIComponent(extraction.extraction_id)}/abandon-provider`;
      const { payload } = await api(path, {
        method: "POST",
        json: {
          expected_intent_hash: extraction.intent_hash,
          expected_record_hash: extraction.record_hash,
          expected_uncertainty_sha256: extraction.uncertainty.sha256,
          author_confirmed: true
        }
      });
      const updated = extractionFromPayload(payload);
      if (!factActionTargetsCurrentDraft({ action, extraction: updated })) return;
      renderChapterFacts(updated, updated?.fact_review || null, "");
      UI.showToast(payload.message || "不确定 Provider 结果已放弃采用。");
    } catch (error) {
      if (!factActionTargetsCurrentDraft({ action })) return;
      const message = UI.errorMessage(error?.payload, error?.status);
      showFactError(message);
      UI.showToast(message, "error");
    } finally {
      finishFactAction(button, action);
    }
  }));

  one("[data-fact-card-list]")?.addEventListener("input", (event) => {
    const form = event.currentTarget.closest("[data-fact-review-form]");
    if (!form || state.chapterFactReview?.state !== "editing") return;
    if (event.target.matches("[data-fact-select]")) { updateFactReviewLocalState(); return; }
    form.classList.add("is-dirty");
    const card = event.target.closest("[data-fact-card]");
    if (card) card.dataset.authorEdited = "true";
    if (card && event.target.matches("[data-fact-summary]")) {
      const heading = one("h3", card);
      if (heading) heading.textContent = event.target.value.trim() || "未命名事实";
    }
    updateFactReviewLocalState();
  });
  one("[data-fact-card-list]")?.addEventListener("change", (event) => {
    if (state.chapterFactReview?.state === "editing" && !event.target.matches("[data-fact-select]")) {
      one("[data-fact-review-form]").classList.add("is-dirty");
      const card = event.target.closest("[data-fact-card]"); if (card) card.dataset.authorEdited = "true";
    }
    updateFactReviewLocalState();
  });
  one("[data-fact-filter]")?.addEventListener("change", () => updateFactReviewLocalState());
  one("[data-fact-add]")?.addEventListener("click", () => {
    if (state.chapterFactReview?.state !== "editing" || !factEditorContext()) return;
    const list = one("[data-fact-card-list]");
    if (all("[data-fact-card]", list).length >= 100) { showFactError("每组审核最多 100 项事实。", "[data-fact-review-error]"); return; }
    one(".fact-empty", list)?.remove();
    const id = crypto.randomUUID().replaceAll("-", "");
    appendFactCard(list, { fact_id: `fact_${id}`, local_key: `author_${id}`, kind: one("[data-fact-add-kind]").value,
      summary: "", proposal: {}, evidence_spans: [], review: { origin: "author", decision: "pending", author_edited: true, reason: null } }, false);
    one("[data-fact-filter]").value = "all";
    one("[data-fact-review-form]").classList.add("is-dirty");
    updateFactReviewLocalState();
    one("[data-fact-summary]", list.lastElementChild)?.focus();
  });
  all("[data-fact-batch]").forEach(button => button.addEventListener("click", withFeedback(async () => {
    const review = state.chapterFactReview;
    const selected = all("[data-fact-card]").filter(card => !card.hidden && one("[data-fact-select]", card)?.checked);
    if (review?.state !== "editing" || !selected.length) return;
    const selectedIds = selected.map(card => card.dataset.factId);
    const decision = button.dataset.factBatch;
    const accepted = await UI.confirmAction(`将你选中的 ${selected.length} 项明确标记为“${factDecisionLabels[decision]}”。隐藏项和未选项保持原决定。继续吗？`);
    if (!accepted || state.chapterFactReview?.id !== review.id || state.chapterFactReview?.revision !== review.revision) return;
    all("[data-fact-card]").filter(card => selectedIds.includes(card.dataset.factId)).forEach(card => {
      card.dataset.decision = decision; one("[data-fact-select]", card).checked = false;
    });
    one("[data-fact-review-form]").classList.add("is-dirty"); updateFactReviewLocalState();
  })));
  one("[data-fact-support-editor]")?.addEventListener("input", () => {
    const form = one("[data-fact-review-form]");
    form.dataset.supportDirty = "true";
    setText("[data-fact-support-state]", "身份或初始情况有未保存的决定");
    updateFactReviewLocalState();
  });
  one("[data-fact-support-save]")?.addEventListener("click", withFeedback(async (event) => {
    const review = state.chapterFactReview;
    if (review?.state !== "editing" || !factEditorContext()?.support_supported) return;
    let support, localFacts;
    try {
      support = one("[data-fact-support-editor]")._editor.read();
      localFacts = collectFactReview(review);
    } catch (error) { showFactError(error.message, "[data-fact-review-error]"); return; }
    const form = one("[data-fact-review-form]");
    const dirty = form.classList.contains("is-dirty");
    const action = beginFactAction(); if (!action) return;
    const button = event.currentTarget; setFactButtonBusy(button, true);
    try {
      const { payload } = await api(`${factReviewDetailPath(review.id)}/support`, { method: "PUT",
        json: { support, expected_revision: review.revision, author_confirmed: true } });
      const updated = reviewFromPayload(payload);
      if (!factActionTargetsCurrentDraft({ action, review: updated })) return;
      renderChapterFacts(state.chapterFactExtraction, updated, "", { preserveFacts: localFacts, dirty });
      UI.showToast(payload.message);
    } catch (error) {
      if (!factActionTargetsCurrentDraft({ action })) return;
      showFactError(`${UI.errorMessage(error?.payload, error?.status) || error.message} 你的本地补充仍保留。`, "[data-fact-review-error]");
    } finally { finishFactAction(button, action); }
  }));
  one("[data-fact-card-list]")?.addEventListener("click", (event) => {
    const decisionButton = event.target.closest("[data-fact-decision]");
    const deleteButton = event.target.closest("[data-fact-delete]");
    if ((!decisionButton && !deleteButton) || state.chapterFactReview?.state !== "editing") return;
    const card = event.target.closest("[data-fact-card]");
    const form = card?.closest("[data-fact-review-form]");
    if (!card || !form) return;
    card.dataset.decision = deleteButton ? "rejected" : decisionButton.dataset.factDecision;
    const reason = one("[data-fact-reason]", card);
    if (deleteButton && reason) reason.value = "author_deleted";
    form.classList.add("is-dirty");
    updateFactReviewLocalState();
  });

  const saveCurrentFactReview = async (action, localFacts) => {
    const review = state.chapterFactReview;
    if (!review || review.state !== "editing") throw new Error("当前事实审核不可编辑。");
    const { payload } = await api(factReviewDetailPath(review.id), {
      method: "PUT",
      json: { facts: localFacts, expected_revision: review.revision }
    });
    const updated = reviewFromPayload(payload);
    if (!factActionTargetsCurrentDraft({ action, review: updated })) return null;
    return { review: updated, message: payload.message };
  };

  one("[data-fact-review-form]")?.addEventListener("submit", withFeedback(async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    if (!form.reportValidity() || state.chapterFactReview?.state !== "editing") return;
    if (form.getAttribute("aria-busy") === "true") return;
    if (form.dataset.supportDirty === "true") { showFactError("请先保存身份与初始情况决定，再保存事实审核。", "[data-fact-review-error]"); return; }
    let localFacts;
    try { localFacts = collectFactReview(state.chapterFactReview); }
    catch (error) { showFactError(error.message, "[data-fact-review-error]"); return; }
    const action = beginFactAction();
    if (!action) return;
    const button = event.submitter || one("[data-fact-save]", form);
    setFactButtonBusy(button, true);
    try {
      const result = await saveCurrentFactReview(action, localFacts);
      if (!result) return;
      form.classList.remove("is-dirty");
      renderChapterFacts(state.chapterFactExtraction, result.review, "");
      UI.showToast(result.message || "事实候选卡已保存。");
    } catch (error) {
      if (!factActionTargetsCurrentDraft({ action })) return;
      const message = UI.errorMessage(error?.payload, error?.status) || error.message;
      if (error?.status === 409) {
        try {
          const detail = await api(factReviewDetailPath(state.chapterFactReview.id));
          const fresh = reviewFromPayload(detail.payload);
          if (factActionTargetsCurrentDraft({ action, review: fresh })) renderChapterFacts(state.chapterFactExtraction, fresh, "", { preserveFacts: localFacts, dirty: true, keepActionError: true });
        } catch { /* Keep the author's local fact edits visible when refresh fails. */ }
      }
      showFactError(error?.status === 409 ? `${message} 你的事实编辑已原样保留；请核对最新 revision 后再次保存。` : message, "[data-fact-review-error]");
      UI.showToast(message, "error");
    } finally {
      finishFactAction(button, action);
    }
  }));

  one("[data-fact-confirm]")?.addEventListener("click", withFeedback(async (event) => {
    const button = event.currentTarget;
    if (!button || button.disabled) return;
    const form = one("[data-fact-review-form]");
    const draft = state.chapterCandidate?.draft;
    const review = state.chapterFactReview;
    if (!form || !draft || !review || review.state !== "editing") return;
    if (form.getAttribute("aria-busy") === "true" || form.dataset.supportDirty === "true") return;
    let localFacts;
    try { localFacts = collectFactReview(review); }
    catch (error) { showFactError(error.message, "[data-fact-review-error]"); return; }
    if (localFacts.some((item) => item.review.decision === "pending")) {
      showFactError("每项事实都必须先明确接受、拒绝或删除。", "[data-fact-review-error]");
      return;
    }
    const accepted = await UI.confirmAction("确认会锁定当前事实审核结果。此步骤仍不会写入正式正文或权威状态；后续提交还需单独确认。继续吗？");
    if (!accepted) return;
    const action = beginFactAction();
    if (!action) return;
    setFactButtonBusy(button, true);
    try {
      let current = review;
      let saveMessage = "";
      // Saved draft cards can still contain names typed before their identities were declared.
      // Confirm exactly the references the author now sees, even after reopening the page.
      const stableProposal = proposal => JSON.stringify(proposal, (_key, value) =>
        value && typeof value === "object" && !Array.isArray(value)
          ? Object.fromEntries(Object.keys(value).sort().map(key => [key, value[key]])) : value);
      const originals = new Map(review.facts.map(fact => [fact.fact_id, stableProposal(fact.proposal)]));
      if (form.classList.contains("is-dirty") || localFacts.some(fact => originals.get(fact.fact_id) !== stableProposal(fact.proposal))) {
        const saved = await saveCurrentFactReview(action, localFacts);
        if (!saved) return;
        current = saved.review;
        saveMessage = saved.message || "";
      }
      const { payload } = await api(`${factReviewDetailPath(current.id)}/confirm`, {
        method: "POST",
        json: {
          expected_revision: current.revision,
          expected_facts_sha256: current.facts_sha256,
          expected_draft_revision: action.draft_revision,
          expected_draft_sha256: action.draft_sha256,
          author_confirmed: true
        }
      });
      const confirmed = reviewFromPayload(payload);
      if (!factActionTargetsCurrentDraft({ action, review: confirmed })) return;
      form.classList.remove("is-dirty");
      renderChapterFacts(state.chapterFactExtraction, confirmed, "");
      UI.showToast(payload.message || saveMessage || "事实候选已由作者确认并锁定。");
    } catch (error) {
      if (!factActionTargetsCurrentDraft({ action })) return;
      const message = UI.errorMessage(error?.payload, error?.status) || error.message;
      const hint = /chapter_fact_identity_unknown/.test(`${error?.payload?.error?.code || ""} ${message}`)
        ? "有事实引用了尚未登记的人物、地点或物品。请先在下方登记并保存，再在事实卡输入已登记的名称或从下拉框选择。" : message;
      showFactError(`${hint}${error?.status === 409 ? " 你的本地编辑仍保留在页面中。" : ""}`, "[data-fact-review-error]");
      UI.showToast(hint, "error");
    } finally {
      finishFactAction(button, action);
    }
  }));

  one("[data-chapter-card-confirm-form]")?.addEventListener("input", (event) => {
    const form = event.currentTarget;
    form.dataset.inputRevision = String(Number(form.dataset.inputRevision || 0) + 1);
    const projectId = form.dataset.projectId;
    const chapter = form.dataset.chapter;
    if (!projectId || !chapter) return;
    clearChapterConfirmation(projectId, chapter);
    state.chapterConfirmation = null;
    renderChapterConfirmationState(null);
    renderChapterCandidate(state.chapterCandidate, state.chapterCandidateError);
  });
  one("[data-spa-history-search]")?.addEventListener("input", (event) => {
    const query = event.target.value.trim().toLocaleLowerCase("zh-CN");
    all("[data-history-rows] tr[data-search-value]").forEach((row) => { row.hidden = Boolean(query) && !(row.dataset.searchValue || "").toLocaleLowerCase("zh-CN").includes(query); });
  });
  one("[data-history-rows]")?.addEventListener("click", withFeedback(async (event) => {
    const button = event.target.closest("[data-history-detail-id]");
    if (!button || button.disabled) return;
    try {
      clearError();
      await loadHistoryDetail(button.dataset.historyDetailId, button.dataset.historyDetailSource);
    } catch (error) {
      showError(error);
    }
  }));

  one("[data-spa-approve-outline]")?.addEventListener("click", withFeedback(async () => {
    const batch = state.batch;
    if (!batch) return;
    if (one("[data-outline-editor]").classList.contains("is-dirty")) { UI.showToast("请先保存改动，再确认章纲。", "error"); return; }
    const count = arrayFrom(batch.items || batch.proposal, "items").length;
    if (!await UI.confirmAction(`确认发布这 ${count} 章章纲？确认后将使用当前已保存的内容写作。`)) return;
    try {
      const expected = batch.proposal_sha256 || batch.sha256 || "";
      const { payload } = await api(`/api/outline-batches/${encodeURIComponent(batch.id)}/approve`, { method: "POST", headers: { "X-Expected-Revision": String(batch.revision || 1), "X-Expected-SHA256": expected, "If-Match": `"${expected}"` }, json: { expected_revision: batch.revision || 1, expected_sha256: expected, source_state_sha: batch.source_state_sha || "" } });
      UI.showToast(payload.message || `${count} 章章纲已确认并冻结。`);
      await loadOutlines();
    } catch (error) { showError(error); UI.showToast(UI.errorMessage(error.payload, error.status), "error"); }
  }));

  one("[data-spa-adopt-existing-outline]")?.addEventListener("click", withFeedback(async () => {
    const batch = state.batch;
    if (!batch) return;
    if (one("[data-outline-editor]").classList.contains("is-dirty")) { UI.showToast("请先保存改动，再确认章纲。", "error"); return; }
    if (!await UI.confirmAction("将保留格式一致的已有细纲，并只发布缺失章节；所有文件会写入同一冻结回执。确认继续吗？")) return;
    try {
      const expected = batch.proposal_sha256 || batch.sha256 || "";
      const { payload } = await api(`/api/outline-batches/${encodeURIComponent(batch.id)}/adopt-existing`, { method: "POST", headers: { "X-Expected-Revision": String(batch.revision || 1), "X-Expected-SHA256": expected, "If-Match": `"${expected}"` }, json: { expected_revision: batch.revision || 1, expected_sha256: expected, source_state_sha: batch.source_state_sha || "" } });
      UI.showToast(payload.message || "已有细纲已保留并纳入冻结批次。");
      await loadOutlines();
    } catch (error) { showError(error); UI.showToast(UI.errorMessage(error.payload, error.status), "error"); }
  }));

  one("[data-close-cohort]")?.addEventListener("click", withFeedback(async () => {
    const cohort = state.cohort;
    if (!cohort || cohort.status !== "active") return;
    const confirmed = await UI.confirmAction("结束后本轮不能再修改。如果任一门槛未通过，本轮将记为失败，修复后需创建新 cohort。仍要继续吗？", "danger");
    if (!confirmed) return;
    try {
      const { payload } = await api(`/api/cohorts/${encodeURIComponent(cohort.id)}/close`, { method: "POST", json: { expected_revision: cohort.revision } });
      UI.showToast(payload.cohort?.status === "passed" ? "本轮 Cohort 已通过。" : "本轮 Cohort 已关闭，结果已固化。");
      await loadProduction();
    } catch (error) { showError(error); UI.showToast(UI.errorMessage(error.payload, error.status), "error"); }
  }));

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
    if (form.matches("[data-create-outline-form], [data-manual-outline], [data-outline-editor]")) await loadOutlines();
    else if (form.matches("[data-session-form], [data-cohort-create-form], [data-close-paused-job-form]")) await loadProduction();
    else if (form.matches("[data-cohort-evaluation-form]")) await loadCohort(state.cohort.id);
    else if (form.matches("[data-recover-form]")) {
      await loadPending();
      if (payload.job || payload.job_id) {
        await loadProject(state.projectId, true);
        window.location.hash = projectHash("production");
      }
    }
    else if (form.matches("[data-force-reset-form]")) {
      await loadPending();
      if (payload.job || payload.job_id) await loadProject(state.projectId, true);
      if (form.dataset.recoveryMode === "close-stalled-session") window.location.hash = projectHash("production");
    }
    else if (form.matches("[data-draft-update-form]")) await loadPending();
    else if (form.matches("[data-draft-validate-form], [data-draft-confirm-form]")) { await loadPending(); if (payload.job) window.location.hash = projectHash("production"); }
    else if (form.matches("[data-chapter-card-confirm-form]")) {
      const confirmation = payload.confirmation;
      const submission = payload.submission_context || {};
      const projectId = submission.project_id;
      const chapter = Number(submission.chapter);
      const responseTargetsCurrentCard = Boolean(confirmation)
        && chapterSubmissionTargetsCurrentCard({ submission, confirmation, form });
      if (!responseTargetsCurrentCard) return;
      if (form.dataset.inputRevision === submission.input_revision) {
        writeChapterConfirmation(projectId, chapter, confirmation);
        state.chapterConfirmation = confirmation;
        renderChapterConfirmationState(confirmation);
        renderChapterCandidate(state.chapterCandidate, state.chapterCandidateError);
        UI.showToast(payload.message || "章节意图已确认。");
      } else {
        state.chapterConfirmation = null;
        renderChapterConfirmationState(null);
        renderChapterCandidate(state.chapterCandidate, state.chapterCandidateError);
        UI.showToast("章节卡或输入在确认期间发生变化，旧确认未被采用；请重新确认。", "error");
      }
    }
    else if (form.matches("[data-amendment-form]")) await loadChapter(state.route?.chapter);
    else if (form.matches("[data-health-activate-form]")) await loadHealth();
  });

  doc.addEventListener("workbench:error", async (event) => {
    const form = event.target.closest("form[data-chapter-card-confirm-form]");
    if (!form || event.detail?.status !== 409) return;
    if (event.detail?.error_code !== "chapter_card_changed") return;
    const submission = event.detail?.submission_context || {};
    if (!chapterSubmissionTargetsCurrentCard({ submission, form })) return;
    const projectId = submission.project_id;
    const chapter = Number(submission.chapter);
    const draft = {
      goal: one('textarea[name="goal"]', form)?.value || "",
      validation_focus: all('input[name="validation_focus"]', form).filter((input) => input.checked).map((input) => input.value),
      notes_text: one('textarea[name="notes_text"]', form)?.value || ""
    };
    clearChapterConfirmation(projectId, chapter);
    state.chapterConfirmation = null;
    renderChapterConfirmationState(null);
    renderChapterCandidate(state.chapterCandidate, state.chapterCandidateError);
    form.dataset.submissionNonce = String(Number(form.dataset.submissionNonce || 0) + 1);
    form.setAttribute("aria-busy", "true");
    all("input, textarea, button", form).forEach((field) => { field.disabled = true; });
    let refreshed = false;
    try {
      await loadChapter(chapter);
      if (!chapterSubmissionTargetsCurrentCard({
        submission: {
          ...submission,
          card_sha256: state.chapterCard?.confirmation?.card_sha256,
          submission_nonce: form.dataset.submissionNonce
        },
        form
      })) return;
      one('textarea[name="goal"]', form).value = draft.goal;
      const selectedFocus = new Set(draft.validation_focus);
      all('input[name="validation_focus"]', form).forEach((input) => { input.checked = selectedFocus.has(input.value); });
      one('textarea[name="notes_text"]', form).value = draft.notes_text;
      form.dataset.inputRevision = String(Number(form.dataset.inputRevision || 0) + 1);
      refreshed = true;
      const errorBox = one("[data-form-error]", form);
      if (errorBox) {
        errorBox.textContent = `${event.detail?.message || "可信章节输入已变化。"} 你的意图编辑已保留；请核对最新章节卡后重新确认。`;
        errorBox.hidden = false;
      }
    } catch (error) {
      const routeStillCurrent = projectId === state.projectId
        && state.route?.area === "chapter"
        && state.route?.projectId === projectId
        && chapter === Number(state.route?.chapter);
      if (routeStillCurrent) showError(error);
    } finally {
      const formStillCurrent = projectId === state.projectId
        && state.route?.area === "chapter"
        && state.route?.projectId === projectId
        && chapter === Number(state.route?.chapter)
        && form.dataset.projectId === projectId
        && Number(form.dataset.chapter) === chapter;
      if (formStillCurrent) {
        form.removeAttribute("aria-busy");
        all(".is-busy", form).forEach((element) => element.classList.remove("is-busy"));
        if (!refreshed) {
          all('textarea, input[type="checkbox"]', form).forEach((field) => { field.disabled = false; });
          const submit = one('[type="submit"]', form);
          if (submit) submit.disabled = true;
        }
      }
    }
  });

  let bootstrapReady = false;
  const applyRestoreScope = value => {
    state.restoreControllerOnly = !!value?.controller;
    const notice = one("[data-restore-scope-notice]");
    if (notice && value) {
      notice.textContent = value.message;
      notice.hidden = false;
      const activeRestore = value.state === "active" && value.read_only === false;
      all('[data-maintenance-controls]').forEach(control => { control.hidden = !(activeRestore || state.restoreControllerOnly); });
      all('[data-backup-controls]').forEach(control => { control.hidden = !activeRestore; });
      all('[data-recovery-controls]').forEach(control => { control.hidden = !(activeRestore || state.restoreControllerOnly); });
      all('[data-dialog-open="create-project-dialog"], [data-dialog-open="import-project-dialog"]')
        .forEach((control) => { control.hidden = true; });
    }
    if (state.restoreControllerOnly) {
      if (chapterProgressTimer != null) window.clearTimeout(chapterProgressTimer);
      chapterProgressTimer = null;
      state.routeRevision++;
      views.forEach(view => { view.hidden = true; });
      all('.primary-nav, [data-spa-project-switcher], [data-health-link], [data-active-job-pill]')
        .forEach(node => { node.hidden = true; });
      showLoading(false);
      clearError();
      window.NovelAgentModelUsage?.setRoute({area:'recovery'});
      window.NovelAgentChapterBudget?.setContext(null);
    }
  };
  document.addEventListener('workbench:restore-scope',event => applyRestoreScope(event.detail));
  api("/api/bootstrap").then(({ payload }) => {
    updateProvider(payload.provider || {});
    const roots = payload.allowed_workspaces || [];
    const location = one('#create-project-dialog input[name="root_path"]');
    if (location && !location.value && roots.length) location.defaultValue = roots[0];
    const folders = one("#author-workspaces");
    if (folders) folders.replaceChildren(...roots.map(value => new Option(value, value)));
    bootstrapReady = true;
    document.dispatchEvent(new CustomEvent('workbench:restore-scope',{detail:payload.restore_scope}));
    return route();
  }).catch(error => { updateProvider({ ready: false }); showError(error); });
})();
