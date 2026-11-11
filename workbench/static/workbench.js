(() => {
  "use strict";

  const doc = document;
  const body = doc.body;
  const one = (selector, root = doc) => root.querySelector(selector);
  const all = (selector, root = doc) => Array.from(root.querySelectorAll(selector));
  const token = one('meta[name="workbench-token"]')?.content || "";
  const csrfToken = one('meta[name="csrf-token"]')?.content || "";
  const terminalStatuses = new Set(["committed", "completed", "failed", "blocked", "cancelled", "paused_recoverable", "paused_needs_revision", "awaiting_author", "awaiting_input", "needs_recovery"]);
  const statusLabels = {
    idle: "空闲", draft: "草稿", ready: "就绪", queued: "排队中",
    preflight: "预检中", generating: "生成中", validating: "校验中",
    reviewing: "审核中", awaiting_author: "等待作者", committing: "提交中",
    committed: "已提交", pass: "通过", warning: "警告", needs_revision: "需修订",
    paused: "已暂停", paused_recoverable: "可恢复", paused_needs_revision: "待修订",
    blocked: "阻塞", failed: "失败", error: "异常", cancelled: "已停止",
    completed: "已完成", awaiting_input: "等待作者", needs_recovery: "需要恢复",
    running: "生成中", starting: "预检中", pause_requested: "将在本章后暂停"
  };
  const normalizeStatus = (status) => ({
    completed: "committed",
    running: "generating",
    starting: "preflight",
    pause_requested: "paused",
    awaiting_input: "awaiting_author",
    needs_recovery: "paused_recoverable"
  }[status] || status);

  const requestHeaders = (extra = {}) => {
    const headers = new Headers({ Accept: "application/json", ...extra });
    if (token) headers.set("X-Workbench-Token", token);
    if (csrfToken) headers.set("X-CSRF-Token", csrfToken);
    return headers;
  };

  const safeText = (value, fallback = "操作未完成。") => {
    if (typeof value !== "string") return fallback;
    const cleaned = value.replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F]/g, "").trim();
    return cleaned ? cleaned.slice(0, 800) : fallback;
  };

  const errorMessage = (payload, status) => {
    if (payload?.error?.code === "workbench_maintenance_active") return safeText(payload.error.message, "工作台正在维护，暂不能启动新任务或保存编辑。");
    if (status === 409) return safeText(payload?.error?.message, "内容已被其他操作更新。请刷新页面，确认最新版本后再试。");
    if (status === 423) return "该作品正在执行另一项写入任务，请稍后再试。";
    if (status === 401 || status === 403) return "本地工作台会话已失效，请从启动器重新打开。";
    if (Array.isArray(payload?.detail)) {
      const fields = payload.detail.slice(0, 5).map((item) => {
        const location = Array.isArray(item.loc) ? item.loc.filter((part) => part !== "body").join(".") : "";
        return `${location ? `${location}：` : ""}${item.msg || "内容不符合要求"}`;
      });
      return safeText(fields.join("；"));
    }
    return safeText(payload?.message || payload?.detail || payload?.error?.message || payload?.error, `请求失败（HTTP ${status || "未知"}）。`);
  };

  const showToast = (message, tone = "success") => {
    const region = one("[data-toast-region]");
    if (!region) return;
    const toast = doc.createElement("div");
    toast.className = `toast${tone === "error" ? " is-error" : ""}`;
    toast.setAttribute("role", tone === "error" ? "alert" : "status");
    const copy = doc.createElement("p");
    copy.textContent = safeText(message, tone === "error" ? "操作未完成。" : "操作已完成。");
    const close = doc.createElement("button");
    close.type = "button";
    close.setAttribute("aria-label", "关闭提示");
    close.textContent = "×";
    close.addEventListener("click", () => toast.remove());
    toast.append(copy, close);
    region.append(toast);
    window.setTimeout(() => toast.remove(), tone === "error" ? 9000 : 4500);
  };

  const confirmAction = (message, tone = "default") => {
    const dialog = one("#confirm-dialog");
    if (!dialog || typeof dialog.showModal !== "function") return Promise.resolve(window.confirm(message));
    const copy = one("[data-confirm-message]", dialog);
    const button = one("[data-confirm-button]", dialog);
    copy.textContent = safeText(message, "请确认是否继续。");
    button.textContent = tone === "danger" ? "仍要继续" : "确认";
    dialog.classList.toggle("is-danger", tone === "danger");
    if (dialog.open) dialog.close("cancel");
    return new Promise((resolve) => {
      dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), { once: true });
      dialog.showModal();
    });
  };

  const setNestedValue = (target, path, value) => {
    const parts = path.split(".");
    let cursor = target;
    parts.forEach((part, index) => {
      const isLast = index === parts.length - 1;
      const nextIsIndex = /^\d+$/.test(parts[index + 1] || "");
      const key = /^\d+$/.test(part) ? Number(part) : part;
      if (isLast) {
        if (Object.prototype.hasOwnProperty.call(cursor, key)) {
          cursor[key] = Array.isArray(cursor[key]) ? [...cursor[key], value] : [cursor[key], value];
        } else cursor[key] = value;
      } else {
        if (cursor[key] == null) cursor[key] = nextIsIndex ? [] : {};
        cursor = cursor[key];
      }
    });
  };

  const formAsJson = (form) => {
    const data = {};
    for (const [name, rawValue] of new FormData(form).entries()) {
      if (rawValue instanceof File) continue;
      let value = rawValue;
      if (name.endsWith("required_beats") || name.endsWith("severe_defects")) value = rawValue.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
      const leafName = name.split(".").at(-1);
      if (/^(confirmed|recovery_confirmed|pause_after_chapter|force_reset)$/.test(leafName)) value = rawValue === "true" || rawValue === "1" || rawValue === "on";
      if (/^(revision|base_revision|expected_revision|from_chapter|start_chapter|end_chapter|count|chapter_count|chapter_index|edit_minutes|actual_cost|weekly_budget)$/.test(leafName) || name.startsWith("scores.")) {
        const numberValue = Number(rawValue);
        if (Number.isFinite(numberValue)) value = numberValue;
      }
      setNestedValue(data, name, value);
    }
    all('input[type="checkbox"][name]', form).forEach((input) => {
      if (!input.checked && !(input.name in data)) setNestedValue(data, input.name, false);
    });
    return data;
  };

  const parseResponse = async (response) => {
    if (response.status === 204) return {};
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      try { return await response.json(); } catch { return {}; }
    }
    const text = await response.text();
    return text ? { message: text.slice(0, 800) } : {};
  };

  const sameOriginNavigate = (url) => {
    if (!url) return false;
    try {
      const target = new URL(url, window.location.origin);
      if (target.origin !== window.location.origin || !["http:", "https:"].includes(target.protocol)) return false;
      window.location.assign(target.href);
      return true;
    } catch { return false; }
  };

  const updateCas = (form, payload, response) => {
    const revision = payload?.revision ?? payload?.record?.revision;
    const responseEtag = response.headers.get("etag")?.replace(/^W\//, "").replace(/^"|"$/g, "");
    const sha = payload?.sha || payload?.sha256 || payload?.proposal_sha256 || payload?.record?.sha || responseEtag;
    if (revision != null) {
      form.dataset.casRevision = String(revision);
      const input = one('input[name="revision"], input[name="base_revision"]', form);
      if (input) input.value = String(revision);
    }
    if (sha) {
      form.dataset.casSha = String(sha);
      const input = one('input[name="source_state_sha"], input[name="sha"], input[name="base_sha"]', form);
      if (input) input.value = String(sha);
    }
  };

  const asyncSubmit = async (form, submitter) => {
    const isChapterCardConfirmation = form.matches("[data-chapter-card-confirm-form]");
    const submissionNonce = isChapterCardConfirmation
      ? String(Number(form.dataset.submissionNonce || 0) + 1)
      : null;
    if (submissionNonce) form.dataset.submissionNonce = submissionNonce;
    const submissionContext = isChapterCardConfirmation ? Object.freeze({
      action: form.action,
      method: (form.dataset.method || form.method || "POST").toUpperCase(),
      project_id: form.dataset.projectId || "",
      chapter: Number(form.dataset.chapter),
      card_sha256: form.dataset.cardSha || "",
      input_revision: form.dataset.inputRevision || "0",
      submission_nonce: submissionNonce
    }) : null;
    const submissionIsCurrent = () => !submissionContext || (
      form.dataset.submissionNonce === submissionContext.submission_nonce
      && form.dataset.projectId === submissionContext.project_id
      && Number(form.dataset.chapter) === submissionContext.chapter
      && form.dataset.cardSha === submissionContext.card_sha256
    );
    const boundJsonBody = isChapterCardConfirmation ? formAsJson(form) : null;
    const confirmMessage = submitter?.dataset.confirm || form.dataset.confirm;
    if (confirmMessage && form.dataset.confirmedOnce !== "true") {
      const confirmed = await confirmAction(confirmMessage, submitter?.dataset.confirmTone || form.dataset.confirmTone);
      if (!confirmed) return;
    }
    if (!submissionIsCurrent()) return;
    delete form.dataset.confirmedOnce;

    const button = submitter || one('[type="submit"]', form);
    const errorBox = one("[data-form-error]", form);
    if (errorBox) { errorBox.hidden = true; errorBox.textContent = ""; }
    if (button) { button.disabled = true; button.classList.add("is-busy"); }
    form.setAttribute("aria-busy", "true");

    try {
      const method = submissionContext?.method || (form.dataset.method || form.method || "POST").toUpperCase();
      const headers = requestHeaders();
      let requestBody;
      if (form.hasAttribute("data-json-form")) {
        headers.set("Content-Type", "application/json");
        const jsonBody = boundJsonBody || formAsJson(form);
        const jsonFileField = form.dataset.jsonFileField;
        if (jsonFileField) {
          const fileInput = one('input[type="file"][data-json-file]', form);
          const selected = fileInput?.files?.[0];
          if (!selected) throw { payload: { error: { message: "请选择校准报告 JSON 文件。" } }, status: 400 };
          if (selected.size > 2 * 1024 * 1024) throw { payload: { error: { message: "校准报告不能超过 2 MB。" } }, status: 400 };
          let parsed;
          try { parsed = JSON.parse(await selected.text()); }
          catch { throw { payload: { error: { message: "所选文件不是有效的 JSON 校准报告。" } }, status: 400 }; }
          if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
            throw { payload: { error: { message: "校准报告顶层必须是 JSON 对象。" } }, status: 400 };
          }
          setNestedValue(jsonBody, jsonFileField, parsed);
        }
        requestBody = JSON.stringify(jsonBody);
      } else requestBody = new FormData(form);

      if (form.dataset.casRevision) headers.set("X-Expected-Revision", form.dataset.casRevision);
      if (form.dataset.casSha) {
        headers.set("X-Expected-SHA256", form.dataset.casSha);
        headers.set("If-Match", `"${form.dataset.casSha.replace(/"/g, "")}"`);
      }
      const response = await fetch(submissionContext?.action || form.action, { method, headers, body: method === "GET" ? undefined : requestBody, credentials: "same-origin" });
      const payload = await parseResponse(response);
      if (!response.ok) throw { payload, status: response.status };

      const responseTargetsCurrentForm = submissionIsCurrent();
      if (responseTargetsCurrentForm) {
        updateCas(form, payload, response);
        form.classList.remove("is-dirty");
        const saveState = one("[data-save-state]", form) || one("[data-save-state]");
        if (saveState) saveState.textContent = "所有修改已保存";
        const message = payload.message || form.dataset.successMessage || "操作已完成。";
        if (!form.hasAttribute("data-defer-success-toast")) showToast(message);
      }
      const successDetail = submissionContext
        ? { ...payload, submission_context: submissionContext }
        : payload;
      form.dispatchEvent(new CustomEvent("workbench:success", { bubbles: true, detail: successDetail }));

      if (responseTargetsCurrentForm) {
        const redirectUrl = payload.redirect_url || payload.location || (response.redirected ? response.url : null);
        if (redirectUrl && sameOriginNavigate(redirectUrl)) return;
        if (form.hasAttribute("data-reload-on-success")) window.location.reload();
      }
    } catch (error) {
      const message = errorMessage(error?.payload, error?.status);
      if (submissionIsCurrent()) {
        if (errorBox) { errorBox.textContent = message; errorBox.hidden = false; errorBox.focus?.(); }
        showToast(message, "error");
      }
      form.dispatchEvent(new CustomEvent("workbench:error", {
        bubbles: true,
        detail: {
          message,
          status: error?.status,
          error_code: error?.payload?.error?.code,
          ...(submissionContext ? { submission_context: submissionContext } : {})
        }
      }));
    } finally {
      if (submissionIsCurrent()) {
        form.removeAttribute("aria-busy");
        if (button) { button.disabled = false; button.classList.remove("is-busy"); }
      }
    }
  };

  all("form[data-async-form]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      asyncSubmit(form, event.submitter);
    });
  });

  all("[data-submit-form]").forEach((button) => {
    button.addEventListener("click", async () => {
      const target = doc.getElementById(button.dataset.submitForm);
      if (!target) return;
      if (button.dataset.confirm) {
        const confirmed = await confirmAction(button.dataset.confirm, button.dataset.confirmTone);
        if (!confirmed) return;
        target.dataset.confirmedOnce = "true";
      }
      target.requestSubmit();
    });
  });

  // Native dialogs: no untrusted string is ever inserted as HTML.
  all("[data-dialog-open]").forEach((button) => button.addEventListener("click", () => {
    const dialog = doc.getElementById(button.dataset.dialogOpen);
    if (dialog?.showModal) dialog.showModal();
  }));
  all("[data-dialog-close]").forEach((button) => button.addEventListener("click", () => button.closest("dialog")?.close()));
  all("dialog.sheet-dialog").forEach((dialog) => dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  }));

  // Responsive navigation.
  const sidebarToggle = one("[data-toggle-sidebar]");
  const sidebarScrim = one("[data-sidebar-scrim]");
  const closeSidebar = () => { body.classList.remove("sidebar-open"); sidebarToggle?.setAttribute("aria-expanded", "false"); };
  sidebarToggle?.addEventListener("click", () => {
    const open = body.classList.toggle("sidebar-open");
    sidebarToggle.setAttribute("aria-expanded", String(open));
  });
  sidebarScrim?.addEventListener("click", closeSidebar);
  window.addEventListener("keydown", (event) => { if (event.key === "Escape" && body.classList.contains("sidebar-open")) closeSidebar(); });

  // Tabs with keyboard support and stable URL hashes.
  all("[data-tabs]").forEach((shell) => {
    const tabs = all("[data-tab]", shell);
    const panels = all("[data-tab-panel]", shell);
    const available = new Set(tabs.map((tab) => tab.dataset.tab));
    const activate = (name, focus = false) => {
      if (!available.has(name)) return;
      tabs.forEach((tab) => {
        const active = tab.dataset.tab === name;
        tab.setAttribute("aria-selected", String(active));
        tab.tabIndex = active ? 0 : -1;
        if (active && focus) tab.focus();
      });
      panels.forEach((panel) => { panel.hidden = panel.dataset.tabPanel !== name; });
      // The SPA owns the URL hash for project routing. Nested chapter tabs
      // keep their state in the DOM so a tab click can never erase the book,
      // area, or chapter route that must survive refresh/restart.
      if (!body.hasAttribute("data-spa") && history.replaceState) {
        history.replaceState(null, "", `${window.location.pathname}${window.location.search}#${name}`);
      }
    };
    const hashTab = body.hasAttribute("data-spa") ? "" : window.location.hash.slice(1);
    activate(available.has(hashTab) ? hashTab : shell.dataset.defaultTab || tabs[0]?.dataset.tab);
    tabs.forEach((tab, index) => {
      tab.addEventListener("click", () => activate(tab.dataset.tab));
      tab.addEventListener("keydown", (event) => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        let targetIndex = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
        activate(tabs[targetIndex].dataset.tab, true);
      });
    });
    all("[data-tab-target]", shell).forEach((button) => button.addEventListener("click", () => activate(button.dataset.tabTarget, true)));
  });

  // Outline cards and dirty state.
  all("[data-outline-toggle]").forEach((button) => button.addEventListener("click", () => {
    const card = button.closest(".outline-card");
    const collapsed = card.classList.toggle("is-collapsed");
    button.setAttribute("aria-expanded", String(!collapsed));
    button.setAttribute("aria-label", `${collapsed ? "展开" : "收起"}${card.querySelector(".outline-number strong")?.textContent || ""}章`);
  }));
  all(".outline-editor").forEach((form) => {
    const state = one("[data-save-state]", form) || one("[data-save-state]");
    form.addEventListener("input", () => {
      form.classList.add("is-dirty");
      if (state) state.textContent = "有未保存的修改";
    });
  });

  // Search and filters are deliberately client-only; server data remains canonical.
  const applyListFilters = (container) => {
    const search = (container.dataset.searchTerm || "").toLocaleLowerCase("zh-CN");
    const filter = container.dataset.activeFilter || "all";
    const rows = all("[data-search-value]", container);
    rows.forEach((row) => {
      const matchesSearch = !search || (row.dataset.searchValue || "").toLocaleLowerCase("zh-CN").includes(search);
      const matchesFilter = filter === "all" || row.dataset.filterValue === filter;
      const selectFilters = all("[data-filter-select]").every((select) => {
        const value = select.value;
        return value === "all" || row.dataset[`filter${select.dataset.filterColumn[0].toUpperCase()}${select.dataset.filterColumn.slice(1)}`] === value;
      });
      row.hidden = !(matchesSearch && matchesFilter && selectFilters);
    });
  };
  all("[data-table-search]").forEach((input) => {
    const container = doc.getElementById(input.dataset.searchTarget);
    if (!container) return;
    input.addEventListener("input", () => { container.dataset.searchTerm = input.value.trim(); applyListFilters(container); });
  });
  all(".segmented [data-filter]").forEach((button) => button.addEventListener("click", () => {
    const group = button.closest(".segmented");
    const container = group.closest(".panel")?.querySelector("[data-filter-list]");
    if (!container) return;
    all("[data-filter]", group).forEach((item) => item.classList.toggle("is-active", item === button));
    container.dataset.activeFilter = button.dataset.filter;
    applyListFilters(container);
  }));
  all("[data-filter-select]").forEach((select) => select.addEventListener("change", () => {
    const table = select.closest(".panel")?.querySelector("table");
    if (table) applyListFilters(table);
  }));

  // Character counters never copy text outside the page.
  all("textarea[maxlength]").forEach((textarea) => {
    const counter = textarea.parentElement?.querySelector("[data-character-count]");
    if (!counter) return;
    const update = () => { counter.textContent = `${textarea.value.length} / ${textarea.maxLength} 字`; };
    textarea.addEventListener("input", update);
    update();
  });

  // Elapsed session time.
  all("[data-elapsed-seconds]").forEach((clock) => {
    let seconds = Number(clock.dataset.elapsedSeconds) || 0;
    const render = () => {
      const hours = Math.floor(seconds / 3600);
      const minutes = Math.floor((seconds % 3600) / 60);
      const remainder = seconds % 60;
      clock.textContent = `${hours ? `${String(hours).padStart(2, "0")}:` : ""}${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
      seconds += 1;
    };
    render();
    window.setInterval(render, 1000);
  });

  const updateStatusBadge = (root, status, label) => {
    const badge = root?.matches?.("[data-status-badge], .status-badge") ? root : one("[data-status-badge]", root);
    if (!badge || !status) return;
    status = normalizeStatus(status);
    Array.from(badge.classList).filter((name) => name.startsWith("status-") && !["status-badge", "status-normal", "status-small"].includes(name)).forEach((name) => badge.classList.remove(name));
    badge.classList.add(`status-${status.replaceAll("_", "-")}`);
    badge.dataset.statusBadge = status;
    const copy = one("[data-status-label]", badge);
    if (copy) copy.textContent = safeText(label || statusLabels[status] || status, "未知");
  };

  const appendEvent = (root, payload) => {
    const feed = one("[data-event-feed]", root);
    if (!feed || !payload.title) return;
    one("[data-empty-event]", feed)?.remove();
    if (payload.id && one(`[data-event-id="${CSS.escape(String(payload.id))}"]`, feed)) return;
    const item = doc.createElement("li");
    if (payload.id) item.dataset.eventId = String(payload.id);
    const time = doc.createElement("time");
    time.textContent = safeText(payload.time_label || new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }), "—");
    const dot = doc.createElement("span");
    dot.className = `event-dot event-${payload.level || "info"}`;
    dot.setAttribute("aria-hidden", "true");
    const copy = doc.createElement("div");
    const title = doc.createElement("strong");
    title.textContent = safeText(payload.title, "运行更新");
    copy.append(title);
    if (payload.detail) {
      const detail = doc.createElement("p");
      detail.textContent = safeText(payload.detail, "");
      copy.append(detail);
    }
    item.append(time, dot, copy);
    feed.prepend(item);
    all("li", feed).slice(60).forEach((oldItem) => oldItem.remove());
  };

  const applyJobPayload = (root, payload, eventId) => {
    if (!payload || typeof payload !== "object") return;
    if (payload.payload && typeof payload.payload === "object" && !Array.isArray(payload.payload)) {
      const eventName = payload.event || payload.event_type || "engine.progress";
      const eventTitles = {
        "job.queued": "任务已进入队列",
        "job.starting": "正在启动预检",
        "job.author_confirmed_resume": "作者已确认恢复原任务",
        "chapter.started": "开始生成当前章",
        "chapter.committed": "章节已事务提交",
        "chapter.pre_provider_context_budget_exceeded": "本地上下文预算超出安全上限",
        "chapter.pre_provider_provenance_unavailable": "Git 来源记录环境异常，任务已暂停",
        "chapter.repair_not_converging": "自动修订未收敛，任务已安全暂停",
        "chapter.stale_scene_evidence": "自动修订与场景证据对不齐，任务已安全暂停",
        "chapter.scene_boundary_validation": "场景边界校验未通过，任务已安全暂停",
        "chapter.scene_schema_validation": "场景响应格式错误，任务已安全暂停",
        "chapter.bridge_prevalidation": "开写前衔接预检未通过，任务已安全暂停",
        "chapter.provider_partial_response": "模型部分响应中断，已保留已付费内容，任务已安全暂停",
        "chapter.needs_revision": "章节需要作者修订",
        "chapter.needs_recovery": "章节已安全暂停，等待恢复",
        "draft.source_upgrade_conflict": "作者草稿已保留，发现更新恢复源",
        "recovery.completed": "恢复分类已完成",
        "job.completed": "生产批次已完成",
        "job.failed": "生产批次失败",
        "job.needs_recovery": "任务需要安全恢复"
      };
      const inferredStage = eventName === "job.starting" ? "preflight" : eventName === "chapter.started" ? "generating" : eventName === "chapter.committed" ? "committing" : undefined;
      const inferredState = {
        "job.queued": "queued",
        "job.starting": "starting",
        "job.author_confirmed_resume": "queued",
        "chapter.started": "running",
        "job.completed": "completed",
        "job.failed": "failed",
        "job.needs_recovery": "needs_recovery",
        "job.cancelled_provider_uncertain": "cancelled",
        "job.paused": "awaiting_input",
        "chapter.pre_provider_context_budget_exceeded": "awaiting_input",
        "chapter.pre_provider_provenance_unavailable": "awaiting_input",
        "chapter.repair_not_converging": "awaiting_input",
        "chapter.stale_scene_evidence": "awaiting_input",
        "chapter.scene_boundary_validation": "awaiting_input",
        "chapter.scene_schema_validation": "awaiting_input",
        "chapter.bridge_prevalidation": "awaiting_input",
        "chapter.provider_partial_response": "awaiting_input",
        "chapter.needs_revision": "awaiting_input",
        "chapter.needs_recovery": "needs_recovery",
        "recovery.completed": "completed"
      }[eventName];
      payload = {
        ...payload.payload,
        id: payload.id,
        event_type: eventName,
        title: payload.payload.title || payload.payload.message || eventTitles[eventName] || eventName,
        stage: payload.payload.stage || inferredStage,
        state: payload.payload.state || inferredState,
        time_label: payload.time_label || (payload.created_at ? new Date(payload.created_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : undefined)
      };
    }
    if (eventId) root.dataset.lastEventId = String(eventId);
    if (Array.isArray(payload.events)) payload.events.forEach((event) => applyJobPayload(root, event, event.id));
    if (payload.job && typeof payload.job === "object") applyJobPayload(root, payload.job, payload.last_event_id);
    const rawStatus = payload.state || payload.job_status || payload.status;
    const status = normalizeStatus(rawStatus);
    if (rawStatus) {
      updateStatusBadge(root, status, payload.status_label || statusLabels[rawStatus]);
      root.dataset.jobStatus = rawStatus;
    }
    if (payload.stage) {
      const stages = all("[data-stage]", root);
      const currentIndex = stages.findIndex((item) => item.dataset.stage === payload.stage);
      stages.forEach((item, index) => {
        const state = payload.stage_state && index === currentIndex ? payload.stage_state : index < currentIndex ? "complete" : index === currentIndex ? "current" : "pending";
        item.className = `stage stage-${state}`;
        item.dataset.stageState = state;
      });
      const current = stages[currentIndex];
      const detail = current && one("[data-stage-detail]", current);
      if (detail && payload.detail) detail.textContent = safeText(payload.detail, detail.textContent);
    }
    if (payload.progress_percent != null) {
      const percent = Math.max(0, Math.min(100, Number(payload.progress_percent) || 0));
      const bar = one(".progress", root);
      bar?.classList.remove("is-indeterminate");
      bar?.removeAttribute("aria-valuetext");
      bar?.setAttribute("aria-valuenow", String(percent));
      one(".progress span", root)?.style.setProperty("--progress", `${percent}%`);
      const percentCopy = one("[data-progress-percent]", root);
      if (percentCopy) percentCopy.textContent = `${Math.round(percent)}%`;
    }
    if (payload.progress_label) {
      const label = one("[data-progress-label]", root);
      if (label) label.textContent = safeText(payload.progress_label);
    }
    if (payload.title || payload.live_title) {
      const title = one("[data-live-title]", root);
      if (title) title.textContent = safeText(payload.live_title || payload.title, "运行更新");
    }
    if (payload.detail || payload.live_detail) {
      const detail = one("[data-live-detail]", root);
      if (detail) detail.textContent = safeText(payload.live_detail || payload.detail, "");
    }
    if (payload.chapter != null && payload.chapter_status) {
      const queueItem = one(`[data-queue-chapter="${CSS.escape(String(payload.chapter))}"]`, root);
      if (queueItem) {
        Array.from(queueItem.classList).filter((name) => name.startsWith("queue-")).forEach((name) => queueItem.classList.remove(name));
        queueItem.classList.add(`queue-${payload.chapter_status.replaceAll("_", "-")}`);
        const label = one("small", queueItem);
        if (label) label.textContent = safeText(payload.chapter_status_label || statusLabels[payload.chapter_status] || payload.chapter_status);
        const result = one(".queue-result", queueItem);
        if (result) result.textContent = payload.chapter_status === "committed" ? "✓" : "•";
      }
    }
    const metricMap = [["model_calls", "[data-model-calls]"], ["token_count_label", "[data-token-count]"], ["cost_label", "[data-cost]"]];
    metricMap.forEach(([key, selector]) => { if (payload[key] != null) { const node = one(selector, root); if (node) node.textContent = safeText(String(payload[key]), "—"); } });
    if (payload.title && (payload.event_type || payload.type || payload.level)) appendEvent(root, { ...payload, id: eventId || payload.id });
    const terminalJobState = payload.state || payload.job_status;
    if (terminalJobState && terminalStatuses.has(terminalJobState)) root.dispatchEvent(new CustomEvent("workbench:job-terminal", { detail: { status: terminalJobState } }));
  };

  const initJobStream = (root) => {
    const eventsUrl = root.dataset.eventsUrl;
    const statusUrl = root.dataset.statusUrl;
    if (!eventsUrl || !statusUrl) return;
    if (typeof root._workbenchStopStream === "function") root._workbenchStopStream();
    let source = null;
    let pollingTimer = null;
    let errorCount = 0;
    let stopped = false;
    const connection = one("[data-connection-state]", root);
    const setConnection = (label, state = "live") => {
      if (!connection) return;
      connection.classList.toggle("is-polling", state === "polling");
      connection.classList.toggle("is-offline", state === "offline");
      const pulse = one(".pulse", connection);
      connection.lastChild.textContent = label;
      if (pulse) pulse.hidden = state === "offline";
    };
    const stop = () => {
      stopped = true;
      source?.close();
      source = null;
      if (pollingTimer) window.clearTimeout(pollingTimer);
      pollingTimer = null;
    };
    root._workbenchStopStream = stop;
    if (root._workbenchTerminalStopHandler) root.removeEventListener("workbench:job-terminal", root._workbenchTerminalStopHandler);
    root._workbenchTerminalStopHandler = (event) => {
      stop();
      const terminalStatus = event.detail?.status;
      setConnection(["completed", "committed"].includes(terminalStatus) ? "本轮已完成" : "本轮已停止", "offline");
    };
    root.addEventListener("workbench:job-terminal", root._workbenchTerminalStopHandler);

    const poll = async () => {
      if (stopped) return;
      try {
        const url = new URL(statusUrl, window.location.origin);
        if (root.dataset.lastEventId) url.searchParams.set("after_event_id", root.dataset.lastEventId);
        const response = await fetch(url, { headers: requestHeaders(), credentials: "same-origin" });
        const payload = await parseResponse(response);
        if (!response.ok) throw { payload, status: response.status };
        applyJobPayload(root, payload, payload.last_event_id);
        setConnection("轮询更新", "polling");
      } catch (error) {
        setConnection("连接中断，正在重试", "offline");
      } finally {
        if (!stopped) pollingTimer = window.setTimeout(poll, document.hidden ? 8000 : 3500);
      }
    };

    const fallbackToPolling = () => {
      source?.close();
      source = null;
      if (!pollingTimer && !stopped) poll();
    };

    if (!("EventSource" in window)) { fallbackToPolling(); return; }
    const url = new URL(eventsUrl, window.location.origin);
    if (root.dataset.lastEventId) url.searchParams.set("after", root.dataset.lastEventId);
    source = new EventSource(url, { withCredentials: true });
    source.addEventListener("open", () => { errorCount = 0; setConnection("实时连接", "live"); });
    const receive = (event) => {
      try {
        const payload = JSON.parse(event.data);
        applyJobPayload(root, payload, event.lastEventId || payload.id);
      } catch { /* Ignore malformed lines; canonical status polling remains available. */ }
    };
    source.addEventListener("message", receive);
    [
      "status", "progress", "stage", "event", "queue", "metrics", "attention", "complete",
      "job.queued", "job.starting", "job.pause_requested", "job.paused", "job.completed",
      "job.failed", "job.needs_recovery", "job.cancel_requested", "job.cancelled_provider_uncertain",
       "job.author_confirmed_resume", "chapter.started", "chapter.committed", "chapter.needs_revision",
       "chapter.pre_provider_context_budget_exceeded", "chapter.pre_provider_provenance_unavailable", "chapter.repair_not_converging", "chapter.stale_scene_evidence", "chapter.scene_boundary_validation", "chapter.scene_schema_validation", "chapter.bridge_prevalidation", "chapter.provider_partial_response",
        "chapter.needs_recovery", "draft.source_upgrade_conflict", "recovery.completed", "loop_start", "step_start", "step_end",
      "step_failed", "loop_end", "engine.progress"
    ].forEach((type) => source.addEventListener(type, receive));
    source.addEventListener("error", () => {
      errorCount += 1;
      setConnection("正在重连…", "offline");
      if (errorCount >= 3) fallbackToPolling();
    });
  };
  all("[data-job-stream]").forEach(initJobStream);

  window.NovelAgentUI = Object.freeze({
    confirmAction,
    errorMessage,
    initJobStream,
    parseResponse,
    requestHeaders,
    safeText,
    showToast,
    updateStatusBadge
  });
})();
