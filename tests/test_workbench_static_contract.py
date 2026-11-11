from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "workbench" / "static"


def test_chapter_card_ui_is_source_bound_and_never_claims_to_generate() -> None:
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert "/chapters/${chapterNumber}/card`" in spa
    assert "chapter.chapter_card = payload.chapter_card || payload" in spa
    assert "expected_card_sha256" in html
    assert "data-chapter-card-confirm-form" in html
    assert "data-defer-success-toast" in html
    assert 'data-card-confirm-button disabled' in html
    assert 'if (!form.hasAttribute("data-defer-success-toast")) showToast(message);' in (STATIC / "workbench.js").read_text(encoding="utf-8")
    assert "不会调用模型，也不会创建候选或提交正文" in html
    assert "window.sessionStorage.setItem(chapterConfirmationKey" in spa
    assert "writeChapterConfirmation" in spa
    assert "form.dataset.inputRevision === submission.input_revision" in spa
    assert 'projectId === state.projectId' in spa
    assert 'state.route?.area === "chapter"' in spa
    assert 'state.route?.projectId === projectId' in spa
    assert "if (!stillCurrent) return;" in spa
    assert "const routeRevision = ++state.routeRevision;" in spa
    assert "expectedRouteRevision != null && state.routeRevision !== expectedRouteRevision" in spa
    assert "if (!responseTargetsCurrentCard) return;" in spa
    assert "旧确认未被采用" in spa
    assert 'UI.showToast(payload.message || "章节意图已确认。");' in spa
    assert 'event.detail?.status !== 409' in spa
    assert "event.detail?.submission_context" in spa
    assert "clearChapterConfirmation(projectId, chapter);" in spa
    assert "你的意图编辑已保留" in spa
    assert "let refreshed = false;" in spa
    assert "if (!refreshed)" in spa
    assert 'all(".is-busy", form).forEach' in spa
    assert "chapterSubmissionTargetsCurrentCard" in spa
    assert "readChapterConfirmation(projectId, card.chapter_index, card)" in spa
    assert 'value?.record_type === "chapter_intent_confirmation"' in spa
    assert "value?.context_sha256 === card?.binding?.context_sha256" in spa
    assert 'setText("code[data-card-sha]"' in spa
    assert 'setText("[data-card-sha]"' not in spa
    ui = (STATIC / "workbench.js").read_text(encoding="utf-8")
    assert "const boundJsonBody = isChapterCardConfirmation ? formAsJson(form) : null;" in ui
    assert "submission_context: submissionContext" in ui
    assert "const submissionIsCurrent = () => !submissionContext ||" in ui
    assert "if (!submissionIsCurrent()) return;" in ui
    assert "if (responseTargetsCurrentForm)" in ui
    assert "if (submissionIsCurrent())" in ui
    assert "form?.dataset?.submissionNonce === submission?.submission_nonce" in spa
    assert "const invalidateChapterCardSubmission = () =>" in spa
    assert "invalidateChapterCardSubmission();" in spa
    assert "data-amendment-panel" in html
    assert 'amendmentPanel.hidden = !hasProse' in spa


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_chapter_card_submission_matcher_rejects_stale_routes_and_cards() -> None:
    spa_path = STATIC / "workbench-spa.js"
    script = r'''
const fs = require("fs");
const vm = require("vm");
let source = fs.readFileSync(process.argv[1], "utf8");
const marker = "  const chapterConfirmationKey =";
if (!source.includes(marker)) throw new Error("chapter matcher export marker is missing");
source = source.replace(marker, "  globalThis.__chapterMatcher = chapterSubmissionTargetsCurrentCard;\n  return;\n" + marker);
const sandbox = {
  document: {
    body: { hasAttribute: () => true },
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: () => ({})
  },
  window: { NovelAgentUI: {} }
};
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
const match = sandbox.__chapterMatcher;
const submission = { project_id: "book-a", chapter: 2, card_sha256: "a".repeat(64), submission_nonce: "7" };
const confirmation = { chapter_index: 2, card_sha256: "a".repeat(64) };
const form = { dataset: { projectId: "book-a", chapter: "2", cardSha: "a".repeat(64), submissionNonce: "7" } };
const currentState = {
  projectId: "book-a",
  route: { area: "chapter", projectId: "book-a", chapter: 2 },
  chapterCard: { confirmation: { card_sha256: "a".repeat(64) } }
};
if (!match({ submission, confirmation, form, currentState })) throw new Error("current submission was rejected");
if (match({ submission, confirmation, form, currentState: { ...currentState, projectId: "book-b" } })) throw new Error("stale project was accepted");
if (match({ submission, confirmation, form, currentState: { ...currentState, route: { area: "chapter", projectId: "book-a", chapter: 3 } } })) throw new Error("stale chapter was accepted");
if (match({ submission, confirmation, form: { dataset: { ...form.dataset, cardSha: "b".repeat(64) } }, currentState })) throw new Error("stale card was accepted");
if (match({ submission, confirmation, form: { dataset: { ...form.dataset, submissionNonce: "8" } }, currentState })) throw new Error("stale nonce was accepted");
if (match({ submission, confirmation: { ...confirmation, card_sha256: "b".repeat(64) }, form, currentState })) throw new Error("mismatched response was accepted");
'''
    completed = subprocess.run(
        ["node", "-e", script, str(spa_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_candidate_ui_uses_compact_discovery_then_detail_and_exact_author_actions() -> None:
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert "data-chapter-candidate-panel" in html
    assert "data-candidate-resume" in html
    assert "data-candidate-create-draft" in html
    assert "data-candidate-abandon-provider" in html
    assert "本页不提供硬取消" in html

    presentation = spa.split("const candidateStatusPresentation =", 1)[1].split(
        "const renderChapterCandidate =", 1
    )[0]
    assert 'needs_recovery: { status: "needs_recovery"' in presentation
    assert 'provider_uncertain: { status: "blocked"' in presentation
    assert "系统不会自动调用 Provider" in presentation
    assert "系统没有自动重试" in presentation

    loader = spa.split("const loadChapterCandidate =", 1)[1].split(
        "const loadChapter =", 1
    )[0]
    assert "const { payload } = await api(collection);" in loader
    assert 'arrayFrom(payload, "items", "candidates")[0]' in loader
    assert "const detail = await api(candidateDetailPath(projectId, chapterNumber, candidateId));" in loader
    assert "return detail.payload?.candidate || detail.payload;" in loader
    assert "input_pack" not in loader

    resume = spa.split('one("[data-candidate-resume]")', 1)[1].split(
        'one("[data-candidate-create-draft]")', 1
    )[0]
    resume_compact = " ".join(resume.split())
    assert 'candidate.status !== "needs_recovery"' in resume
    assert (
        "json: { expected_intent_hash: candidate.intent_hash, author_confirmed: true }"
        in resume_compact
    )

    create_draft = spa.split('one("[data-candidate-create-draft]")', 1)[1].split(
        'one("[data-candidate-abandon-provider]")', 1
    )[0]
    create_draft_compact = " ".join(create_draft.split())
    assert 'candidate.status !== "completed"' in create_draft
    assert (
        "json: { expected_prose_sha256: candidate.prose?.sha256, author_confirmed: true }"
        in create_draft_compact
    )


def test_candidate_conflicts_and_draft_cas_preserve_author_work() -> None:
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")
    stream = (STATIC / "workbench.js").read_text(encoding="utf-8")

    generate = spa.split('one("[data-candidate-generate]")', 1)[1].split(
        'one("[data-candidate-resume]")', 1
    )[0]
    assert 'if (error?.status === 409 && errorCode === "chapter_card_changed")' in generate
    changed_branch, ordinary_conflict_branch = generate.split("} else {", 1)
    assert "clearChapterConfirmation(action.project_id, action.chapter);" in changed_branch
    assert "clearChapterConfirmation(" not in ordinary_conflict_branch
    assert "showCandidateActionError(message);" in ordinary_conflict_branch

    chapter_card_error_handler = spa.split(
        'doc.addEventListener("workbench:error"', 1
    )[1]
    before_confirmation_clear = chapter_card_error_handler.split(
        "clearChapterConfirmation(projectId, chapter);", 1
    )[0]
    assert 'event.detail?.error_code !== "chapter_card_changed"' in before_confirmation_clear
    assert "error_code: error?.payload?.error?.code" in stream

    draft_save = spa.split('one("[data-candidate-draft-form]")?.addEventListener("submit"', 1)[1].split(
        'one("[data-chapter-card-confirm-form]")?.addEventListener("input"', 1
    )[0]
    draft_save_compact = " ".join(draft_save.split())
    assert 'method: "PUT"' in draft_save
    assert "json: { text: localText, expected_revision: draft.revision }" in draft_save_compact
    assert "if (error?.status === 409)" in draft_save
    assert "renderChapterCandidate(fresh, \"\", { preserveDraftText: localText });" in draft_save
    assert "你的文字已原样保留" in draft_save


def test_fact_review_ui_uses_extraction_evidence_and_explicit_author_decisions() -> None:
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert "data-chapter-fact-panel" in html
    assert "data-fact-extract" in html
    assert "data-fact-resume" in html
    assert "data-fact-cancel" in html
    assert "data-fact-abandon-provider" in html
    assert "data-fact-review-form" in html
    assert "删除”会留下明确拒绝记录，不会物理删除证据" in html
    assert "正式正文和权威状态仍不会自动写入" in html

    loader = spa.split("const loadChapterFactState =", 1)[1].split(
        "const refreshChapterFactsForCurrentDraft =", 1
    )[0]
    assert "factExtractionCollectionPath(draft.id)" in loader
    assert "extractionItems[extractionItems.length - 1]" in loader
    assert "encodeURIComponent(latest.extraction_id)" in loader
    assert "/fact-reviews`" in loader
    assert "factReviewDetailPath(latestReview.id)" in loader

    assert 'appendFactRole(roles, "发生地点"' in spa
    assert 'appendFactRole(roles, "现场参与者"' in spa
    assert 'appendFactRole(roles, "远程参与者"' in spa
    assert 'appendFactRole(roles, "运输中物品"' in spa
    assert 'appendFactRole(roles, "逻辑关联对象"' in spa
    assert 'card.dataset.decision = deleteButton ? "rejected"' in spa
    assert 'reason.value = "author_deleted"' in spa
    assert 'reason.name = `reason_${fact.fact_id}`' in spa
    assert 'const reviewBlocksStart = activeReview && activeReview.state !== "stale";' in spa
    assert "const canStart = !reviewBlocksStart" in spa

    extraction_action = spa.split('one("[data-fact-extract]")', 1)[1].split(
        'one("[data-fact-resume]")', 1
    )[0]
    assert 'headers: { "Idempotency-Key": operationKey }' in extraction_action
    assert "expected_draft_revision: action.draft_revision" in extraction_action
    assert "expected_draft_sha256: action.draft_sha256" in extraction_action
    assert "author_confirmed: true" in extraction_action
    assert "请先保存正文草稿" in extraction_action

    save = spa.split("const saveCurrentFactReview =", 1)[1].split(
        'one("[data-fact-review-form]")', 1
    )[0]
    assert 'method: "PUT"' in save
    assert "facts: localFacts, expected_revision: review.revision" in save
    confirm = spa.split('one("[data-fact-confirm]")', 1)[1].split(
        'one("[data-chapter-card-confirm-form]")', 1
    )[0]
    assert "expected_facts_sha256: current.facts_sha256" in confirm
    assert "expected_draft_revision: action.draft_revision" in confirm
    assert "expected_draft_sha256: action.draft_sha256" in confirm
    assert "后续提交还需单独确认" in confirm
    assert "你的事实编辑已原样保留" in spa


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_fact_action_matcher_rejects_late_or_changed_draft_responses() -> None:
    spa_path = STATIC / "workbench-spa.js"
    script = r'''
const fs = require("fs");
const vm = require("vm");
let source = fs.readFileSync(process.argv[1], "utf8");
const marker = "  const beginFactAction =";
if (!source.includes(marker)) throw new Error("fact matcher export marker is missing");
source = source.replace(marker, "  globalThis.__factMatcher = factActionTargetsCurrentDraft;\n  return;\n" + marker);
const sandbox = {
  document: {
    body: { hasAttribute: () => true },
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: () => ({})
  },
  window: { NovelAgentUI: {}, sessionStorage: {} }
};
sandbox.globalThis = sandbox;
vm.runInNewContext(source, sandbox);
const match = sandbox.__factMatcher;
const sha = "a".repeat(64);
const action = {
  nonce: "3", route_revision: 9, project_id: "book-a", chapter: 2,
  draft_id: "candidate_draft_" + "1".repeat(32), draft_revision: 4, draft_sha256: sha
};
const draft = { id: action.draft_id, revision: 4, sha256: sha };
const currentState = {
  factActionNonce: 3, routeRevision: 9, projectId: "book-a",
  route: { area: "chapter", projectId: "book-a", chapter: 2 },
  chapterCandidate: { draft }
};
const extraction = { source: { draft_id: draft.id, draft_revision: 4, draft_sha256: sha } };
if (!match({ action, extraction, currentState })) throw new Error("current fact response was rejected");
if (match({ action, extraction, currentState: { ...currentState, factActionNonce: 4 } })) throw new Error("old action nonce was accepted");
if (match({ action, extraction, currentState: { ...currentState, route: { area: "chapter", projectId: "book-a", chapter: 3 } } })) throw new Error("old chapter route was accepted");
if (match({ action, extraction, currentState: { ...currentState, chapterCandidate: { draft: { ...draft, revision: 5 } } } })) throw new Error("changed draft revision was accepted");
if (match({ action, extraction: { source: { ...extraction.source, draft_sha256: "b".repeat(64) } }, currentState })) throw new Error("mismatched extraction source was accepted");
'''
    completed = subprocess.run(
        [shutil.which("node") or "node", "-e", script, str(spa_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_candidate_status_matrix_exposes_recovery_and_provider_uncertainty() -> None:
    spa_path = STATIC / "workbench-spa.js"
    script = r'''
const fs = require("fs");
const vm = require("vm");
let source = fs.readFileSync(process.argv[1], "utf8");
const marker = "  const renderChapterCandidate =";
if (!source.includes(marker)) throw new Error("candidate presentation export marker is missing");
source = source.replace(marker, "  globalThis.__candidatePresentation = candidateStatusPresentation;\n  return;\n" + marker);
const sandbox = {
  document: {
    body: { hasAttribute: () => true },
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: () => ({})
  },
  window: { NovelAgentUI: {} }
};
sandbox.globalThis = sandbox;
vm.runInNewContext(source, sandbox);
const present = sandbox.__candidatePresentation;
const recovery = present({ status: "needs_recovery" });
const uncertain = present({ status: "provider_uncertain" });
if (recovery.status !== "needs_recovery" || recovery.label !== "等待作者恢复") {
  throw new Error(`bad needs_recovery presentation: ${JSON.stringify(recovery)}`);
}
if (!recovery.message.includes("不会自动调用 Provider")) {
  throw new Error("needs_recovery does not disclose its author-confirmed Provider boundary");
}
if (uncertain.status !== "blocked" || uncertain.label !== "结果不确定") {
  throw new Error(`bad provider_uncertain presentation: ${JSON.stringify(uncertain)}`);
}
if (!uncertain.message.includes("没有自动重试")) {
  throw new Error("provider_uncertain does not disclose that automatic retry is stopped");
}
'''
    completed = subprocess.run(
        [shutil.which("node") or "node", "-e", script, str(spa_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_candidate_generation_matcher_rejects_late_or_reconfirmed_responses() -> None:
    spa_path = STATIC / "workbench-spa.js"
    script = r'''
const fs = require("fs");
const vm = require("vm");
let source = fs.readFileSync(process.argv[1], "utf8");
const marker = "  const invalidateChapterCardSubmission =";
if (!source.includes(marker)) throw new Error("candidate matcher export marker is missing");
source = source.replace(marker, "  globalThis.__candidateMatcher = candidateActionTargetsCurrentChapter;\n  return;\n" + marker);
const sandbox = {
  document: {
    body: { hasAttribute: () => true },
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: () => ({})
  },
  window: { NovelAgentUI: {} }
};
sandbox.globalThis = sandbox;
vm.runInNewContext(source, sandbox);
const match = sandbox.__candidateMatcher;
const confirmationSha = "c".repeat(64);
const replacementSha = "d".repeat(64);
const cardSha = "a".repeat(64);
const action = {
  nonce: "7",
  route_revision: 11,
  project_id: "book-a",
  chapter: 2,
  candidate_id: null,
  draft_id: null,
  card_sha256: cardSha,
  confirmation_sha256: confirmationSha,
  require_card_binding: true,
  require_confirmation: true
};
const candidate = {
  candidate_id: `candidate_${"1".repeat(40)}`,
  project_id: "book-a",
  chapter_index: 2,
  confirmation: { confirmation_sha256: confirmationSha }
};
const currentState = {
  candidateActionNonce: 7,
  routeRevision: 11,
  projectId: "book-a",
  route: { area: "chapter", projectId: "book-a", chapter: 2 },
  chapterCard: { confirmation: { card_sha256: cardSha } },
  chapterConfirmation: { confirmation_sha256: confirmationSha },
  chapterCandidate: null
};
if (!match({ action, candidate, currentState })) throw new Error("current generation response was rejected");
if (match({ action, candidate: { ...candidate, confirmation: { confirmation_sha256: replacementSha } }, currentState })) {
  throw new Error("late response with another confirmation_sha256 was accepted");
}
if (match({ action, candidate, currentState: { ...currentState, chapterConfirmation: { confirmation_sha256: replacementSha } } })) {
  throw new Error("response from the previous confirmation was accepted after reconfirmation");
}
if (match({ action, candidate, currentState: { ...currentState, candidateActionNonce: 8 } })) {
  throw new Error("response from an older Candidate action was accepted");
}
if (match({ action, candidate: { ...candidate, confirmation: {} }, currentState })) {
  throw new Error("generation response without confirmation_sha256 was accepted");
}
'''
    completed = subprocess.run(
        [shutil.which("node") or "node", "-e", script, str(spa_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_uncertain_chapter_event_latches_the_browser_in_recovery() -> None:
    ui = (STATIC / "workbench.js").read_text(encoding="utf-8")

    assert '"chapter.needs_recovery": "needs_recovery"' in ui
    assert '"chapter.needs_revision": "awaiting_input"' in ui
    assert '"recovery.completed": "completed"' in ui
    assert "payload.state || payload.job_status || payload.status" in ui
    assert 'terminalStatuses.has(terminalJobState)' in ui


def test_production_view_renders_paused_jobs_as_attention_not_running() -> None:
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "workbench.css").read_text(encoding="utf-8")

    assert 'const pausedStates = new Set(["awaiting_input", "needs_recovery"]);' in spa
    assert "第 ${currentChapter || \"—\"} 章已安全暂停" in spa
    assert "本轮已经停止，不需要紧急终止" in spa
    assert 'pill.href = paused ? projectHash("pending") : projectHash("production")' in spa
    assert "recovery.needs_reclassification && pausedRecoveryJob" in spa
    assert '["awaiting_input", "needs_recovery"].includes(job.state)' in spa
    assert 'project_recovery: !pausedRecoveryJob' in spa
    assert 'job_id: pausedRecoveryJob?.job_id || pausedRecoveryJob?.id || null' in spa
    assert "先执行无模型分类" in spa
    assert "先执行无模型恢复分类，核对已结算回执和可复用场景" in spa
    assert "不会重新生成场景；将调用真实模型重新运行导演决策与语义校验" in spa
    assert 'issue.checkpoint?.reason === "complete_scene_sequence_requires_revalidation"' in spa
    assert "只重生未通过的场景" in spa
    assert "将载入 ${reusableCount} 份候选并逐场重跑边界校验" in spa
    assert "const displayedSceneCount = allScenesReusable ? expectedSceneCount : reusableCount + 1" in spa
    assert 'data-production-attention hidden' in html
    assert 'data-production-controls' in html
    assert '.progress.is-indeterminate span' in css


def test_production_view_reloads_canonical_state_after_a_terminal_job() -> None:
    ui = (STATIC / "workbench.js").read_text(encoding="utf-8")
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")

    assert '["completed", "committed"].includes(terminalStatus) ? "本轮已完成" : "本轮已停止"' in ui
    assert "else if (finishedStates.has(terminalStatus))" in spa
    assert "await loadProject(state.projectId, true);" in spa
    assert 'if (state.route?.area === "production") await loadProduction();' in spa


def test_outline_planner_does_not_reuse_a_stale_draft_range() -> None:
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    loader = spa.split("const loadOutlines = async () =>", 1)[1].split("const renderCohortEvaluation", 1)[0]

    assert "const isCurrentDraftRange = (item) => Number(item?.start_chapter || 0) === nextChapter;" in spa
    assert '["draft", "ready"].includes(item.status) && isCurrentDraftRange(item)' in loader
    assert "coversNextChapter" in loader
    assert "coveringApproved" in loader
    assert "batches[0] || null" not in loader
    assert "function updateOutlinePlanningCopy(remaining)" in spa
    assert "查看剩余章纲" in spa
    assert "data-outline-action-title" in html
    assert "data-outline-plan-button" in html
    assert "data-outlines-heading" in html
    assert "data-outline-remaining-note" in html
    assert 'all("[data-project-route]").forEach((link) => { link.href = projectHash(link.dataset.projectRoute); });' in spa
    assert html.count('data-project-route="outlines"') >= 2


def test_recovery_confirmation_resumes_the_paused_parent_and_toast_keeps_three_items() -> None:
    ui = (STATIC / "workbench.js").read_text(encoding="utf-8")
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert 'pausedParent?.resume_ready === true' in spa
    assert '/resume`' in spa
    assert 'form.dataset.recoveryMode = "resume"' in spa
    assert 'await loadProject(state.projectId, true);' in spa
    assert 'root.dataset.lastEventId = session.last_event_id || ""' in spa
    assert '"job.author_confirmed_resume": "queued"' in ui
    assert 'data-recovery-resume-field disabled' in html
    assert 'toast.append(copy, close);' in ui
    assert 'toast.append(doc.createElement("span"), copy, close);' not in ui


def test_pending_page_does_not_flash_chapter_20_placeholder() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")

    pending_start = html.find('data-spa-view="pending"')
    pending_end = html.find('data-spa-view="history"')
    assert pending_start != -1 and pending_end > pending_start
    pending = html[pending_start:pending_end]

    assert 'class="pending-layout" hidden>' in pending
    assert 'data-pending-empty>' in pending
    assert 'data-pending-empty hidden' not in pending
    assert "第 20 章" not in pending
    assert "场景 3" not in pending
    assert 'value="20"' not in pending
    assert "chapter 20 recovery" not in pending.lower()
    assert 'layout.hidden = state.pending.length === 0; empty.hidden = state.pending.length > 0;' in spa


def test_quality_revision_pause_uses_parent_resume_without_recovery_classification() -> None:
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert 'job?.state === "awaiting_input" && ["needs_revision", "blocked", "error"].includes(reviewStatusOf(job))' in spa
    assert 'quality_revision: true' in spa
    assert 'Provider 已成功结束，正式正文未提交' in spa
    assert 'form.dataset.recoveryMode = "quality-repair"' in spa
    assert 'form.action = `/api/jobs/${encodeURIComponent(pausedParent.job_id || pausedParent.id)}/resume`' in spa
    assert 'resumeField.disabled = false' in spa
    assert 'setText("[data-recover-button]", "自动修复并重新校验")' in spa
    assert '此操作会调用真实模型' in spa
    assert '!qualityRevisionAtRecoveryChapter && recovery?.recoverable' in spa
    assert '"有写作任务需要处理"' in spa
    assert 'data-production-attention-title' in html
    assert 'data-pending-detail-eyebrow' in html
    assert 'name="confirmed" value="true" data-recovery-resume-field disabled' in html


def test_rejected_validation_uses_the_editable_draft_lane() -> None:
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")

    assert "const qualityRejectionDetailsOfRun = (run) =>" in spa
    assert 'run?.status !== "rejected"' in spa
    assert "Number(run?.unsettled_attempt_count) !== 0" in spa
    assert "quality_rejection: qualityRejection" in spa
    assert "质量门发现阻断问题" in spa
    assert "qualityProblemSummary(qualityRejection)" in spa
    assert "次模型调用都有完整回执且已经结算" in spa
    assert 'form.dataset.recoveryMode = "quality-classify"' in spa
    assert 'form.dataset.recoveryMode = "quality-draft"' in spa
    assert "生成可编辑恢复草稿" in spa
    assert "请先编辑并重新验证下方草稿" in spa
    assert "质量问题必须通过下方 revision/SHA 草稿流程处理" in spa


def test_recovery_source_upgrade_conflict_is_visible_without_overwriting_author_text() -> None:
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    stream = (STATIC / "workbench.js").read_text(encoding="utf-8")

    assert "issue.draft_source_conflict" in spa
    assert "你的编辑已原样保留" in spa
    assert "sourceConflict.incoming_text" in spa
    assert "data-draft-source-conflict hidden" in html
    assert "已保留你的编辑，没有自动覆盖" in html
    assert "data-draft-source-conflict-text" in html
    assert stream.count('"draft.source_upgrade_conflict"') >= 2


def test_context_budget_pause_is_explained_before_provider_or_quality_branches() -> None:
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")
    structured_error = {"code": "pre_provider_context_budget_exceeded"}

    assert 'new Set(["pre_provider_context_budget_exceeded", "context_budget_exceeded", "story_project_context_budget_exceeded"])' in spa
    assert f'"{structured_error["code"]}"' in spa
    assert 'value.code === "engine_result_unreadable"' in spa
    assert "input requires\\s+(\\d+)\\s+tokens;\\s*hard limit is\\s+(\\d+)" in spa
    assert 'numberFrom("hard", "hard_limit"' in spa
    assert 'numberFrom("excess", "overage_tokens"' in spa
    assert "zeroCallEvidenceVerified(details)" in spa
    assert "contextBudget?.verified && session?.resume_ready === true" in spa
    assert 'connection.lastChild.textContent = "本轮已停止"' in spa
    assert "if (paused)" in spa
    assert "本地输入上下文为 ${budget.requiredTokens} tokens，上限 ${budget.hardLimit} tokens，超出 ${budget.overageTokens} tokens。" in spa
    assert "本次尝试在请求发出前停止：新增模型调用 0 次、新增费用 0" in spa
    assert "安全核对完成前不能重新执行" in spa
    assert "const qualityRevision = paused && !provenanceFailure && !contextBudget && !repairConvergence && !repairAuthority && !staleSceneEvidence && (Boolean(qualityRejection) || isQualityRevisionPause(session));" in spa
    assert "const qualityRevision = !provenanceFailure && !contextBudget && !repairConvergence && !staleSceneEvidenceFailure && (Boolean(qualityRejection) || issue.quality_revision === true || isQualityRevisionPause(issue));" in spa
    assert 'form.dataset.recoveryMode = "context-budget"' in spa
    assert 'setText("[data-recover-button]", `确认重新执行第 ${chapter} 章`)' in spa
    assert 'contextBudget.verified ? "等待系统修复" : "等待安全核对"' in spa
    assert "if (contextBudget.verified && pausedParent?.resume_ready === true)" in spa
    assert 'contextBudgetReady ? "可重新执行" : contextBudget.verified ? "等待系统修复" : "安全核对中"' in spa
    assert '重新执行需要你的明确确认' in spa
    assert 'title: "模型调用与费用"' in spa
    assert 'title: "草稿与正文"' in spa

    stream = (STATIC / "workbench.js").read_text(encoding="utf-8")
    assert '"chapter.pre_provider_context_budget_exceeded": "本地上下文预算超出安全上限"' in stream
    assert '"chapter.pre_provider_context_budget_exceeded": "awaiting_input"' in stream
    assert '"chapter.pre_provider_context_budget_exceeded", "chapter.pre_provider_provenance_unavailable"' in stream



def test_structured_character_budget_failure_is_explained_in_characters_not_tokens() -> None:
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")

    assert 'details.unit === "characters"' in spa
    assert "装配本章必填 JSON 条目时超过了" in spa
    assert "装配本章必填 Markdown/JSON 上下文时超过了" in spa
    assert "字符的本地预算" in spa
    assert "本章必填上下文超过了" in spa
    assert "字符的本地装配预算" in spa
    classifier = spa.split("const contextBudgetDetailsOf = (job) =>", 1)[1].split("const contextBudgetSummary", 1)[0]
    assert 'unit === "tokens" ? requiredTokens : null' in classifier
    assert "requiredTokens," not in classifier.split("const unit =", 1)[1]


def test_settled_scene_retry_headroom_failure_is_not_reported_as_pre_provider() -> None:
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")

    assert "const contextHeadroomDetailsOfRun = (run) =>" in spa
    assert 'run?.error_type !== "ContextBudgetError"' in spa
    assert "story_project_context_headroom_exceeded" in spa
    assert "safe target is\\s+(\\d+)" in spa
    assert "context_headroom_failure: contextHeadroomFailure" in spa
    assert "边界重生输入超过安全余量" in spa
    assert "边界重生请求被本地预算门拦下" in spa
    assert "本次 ${Number(latestRecoveryRun.provider_calls) || 0} 次模型调用均已结算" in spa
    assert "先执行无模型恢复分类，核对已结算回执和可复用场景" in spa


def test_settled_repair_context_headroom_failure_explains_the_real_stopping_stage() -> None:
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")

    assert "const contextHeadroomDetailsOfRun = (run) =>" in spa
    assert "story_project_repair_context_headroom_exceeded" in spa
    assert "repair input requires" in spa
    assert 'stage === "repair"' in spa
    assert "const contextHeadroomIsRepair = (failure) => failure?.stage === \"repair\"" in spa
    assert "修订输入超过安全余量" in spa
    assert "修订输入为 ${contextHeadroom.requiredTokens} tokens" in spa
    assert "这次自动修订输入需要 ${contextHeadroom.requiredTokens} tokens" in spa
    assert "修订模型请求尚未发出" in spa
    assert "候选正文已保留，修订请求尚未发出" in spa
    assert "也不会直接重试这次超限的修订" in spa
    assert 'form.dataset.recoveryMode = "repair-context-headroom-classify"' in spa
    assert "自动修订的输入超过本地安全余量，修订请求尚未发出" in spa
    # Scene-retry copy must remain, and must not be the only headroom wording.
    assert "边界重生输入超过安全余量" in spa
    assert spa.count("修订输入超过安全余量") >= 3


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_repair_context_headroom_classifier_recognizes_official_failure_and_fails_closed() -> None:
    spa_path = STATIC / "workbench-spa.js"
    script = r"""
const fs = require("fs");
const vm = require("vm");
let source = fs.readFileSync(process.argv[1], "utf8");
const marker = "  const reviewStatusOf =";
if (!source.includes(marker)) throw new Error("classifier export marker is missing");
source = source.replace(marker, "  globalThis.__evidence = { contextHeadroomDetailsOfRun };\n  return;\n" + marker);
const sandbox = {
  document: {
    body: { hasAttribute: () => true },
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: () => ({})
  },
  window: { NovelAgentUI: {} }
};
sandbox.globalThis = sandbox;
vm.runInNewContext(source, sandbox);
const officialRepairRun = {
  error_type: "ContextBudgetError",
  error_message: "story_project_repair_context_headroom_exceeded: repair input requires 32761 tokens; safe target is 30000; hard limit is 32000",
  unsettled_attempt_count: 0,
  committed: false,
  provider_calls: 7,
  chapter_chars: 4867
};
const sceneRun = {
  error_type: "ContextBudgetError",
  error_message: "story_project_context_headroom_exceeded: scene input requires 31000 tokens; safe target is 30000; hard limit is 32000",
  unsettled_attempt_count: 0
};
const classify = sandbox.__evidence.contextHeadroomDetailsOfRun;
const output = {
  officialRepair: classify(officialRepairRun),
  sceneRetry: classify(sceneRun),
  unsettled: classify({ ...officialRepairRun, unsettled_attempt_count: 1 }),
  committed: classify({ ...officialRepairRun, committed: true }),
  noCandidate: classify({ ...officialRepairRun, chapter_chars: 0 }),
  missingCalls: classify({ ...officialRepairRun, provider_calls: undefined }),
  wrongType: classify({ ...officialRepairRun, error_type: "ValueError" }),
  incompleteMessage: classify({ ...officialRepairRun, error_message: "story_project_repair_context_headroom_exceeded" }),
  unreadableOnly: classify({
    error_type: "engine_result_unreadable",
    error_message: "story_project_repair_context_headroom_exceeded: repair input requires 32761 tokens; safe target is 30000; hard limit is 32000",
    unsettled_attempt_count: 0,
    committed: false,
    provider_calls: 7,
    chapter_chars: 4867
  })
};
process.stdout.write(JSON.stringify(output));
"""
    completed = subprocess.run(
        [shutil.which("node") or "node", "-e", script, str(spa_path)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout)

    assert result["officialRepair"]["stage"] == "repair"
    assert result["officialRepair"]["requiredTokens"] == 32761
    assert result["officialRepair"]["safeTarget"] == 30000
    assert result["officialRepair"]["hardLimit"] == 32000
    assert result["officialRepair"]["overageTokens"] == 2761
    assert result["officialRepair"]["providerCalls"] == 7
    assert result["officialRepair"]["chapterChars"] == 4867
    assert result["sceneRetry"]["stage"] == "scene"
    assert result["sceneRetry"]["requiredTokens"] == 31000
    assert result["sceneRetry"]["overageTokens"] == 1000
    assert "providerCalls" not in result["sceneRetry"]
    assert result["unsettled"] is None
    assert result["committed"] is None
    assert result["noCandidate"] is None
    assert result["missingCalls"] is None
    assert result["wrongType"] is None
    assert result["incompleteMessage"] is None
    assert result["unreadableOnly"] is None


def test_settled_repair_authority_failure_explains_the_real_stopping_stage() -> None:
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")

    assert "const repairAuthorityDetailsOfRun = (run) =>" in spa
    assert 'run?.error_type !== "StructuredContextError"' in spa
    assert "required_authoritative_records_exceed_budget" in spa
    assert "repair_authority_failure: repairAuthorityFailure" in spa
    assert "修订上下文装配失败" in spa
    assert "质量修订请求在本地预算检查中停止" in spa
    assert "候选正文已保留，修订请求尚未发出" in spa
    assert "次模型调用都有完整回执且已经结算" in spa
    assert "正式正文尚未提交" in spa
    assert "修订模型请求尚未发出" in spa
    assert "Promise.all([" in spa
    assert "/pending`)" in spa




def test_settled_stale_scene_evidence_failure_explains_retained_candidate() -> None:
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")
    stream = (STATIC / "workbench.js").read_text(encoding="utf-8")

    assert "const staleSceneEvidenceDetailsOfRun = (run) =>" in spa
    assert 'run?.error_type !== "SceneEvidenceAlignmentError"' in spa
    assert 'message.startsWith("stale_scene_evidence:")' in spa
    assert "const staleSceneEvidenceDetailsOf = (job) =>" in spa
    assert 'value.code === "stale_scene_evidence"' in spa
    assert "stale_scene_evidence_failure: staleSceneEvidenceFailure" in spa
    assert "自动修订因场景证据对不齐而停止" in spa
    assert "修订跨越场景边界" in spa
    assert "候选正文已保留，越界修订已被拒绝" in spa
    assert "次模型调用都有完整回执且已经结算" in spa
    assert "字候选正文保存在隔离运行区，正式正文尚未提交" in spa
    assert "系统已拒绝采用这次修订" in spa
    assert "先做无模型恢复分类" in spa
    assert "不会直接重试这次失败的修订" in spa
    assert "不要盲目重试这次失败修订" in spa
    assert 'form.dataset.recoveryMode = "stale-scene-evidence-classify"' in spa
    assert "!staleSceneEvidence && !qualityRejection && jobStatus === \"needs_recovery\"" in spa

    assert stream.count('"chapter.stale_scene_evidence"') >= 3
    assert '"chapter.stale_scene_evidence": "自动修订与场景证据对不齐，任务已安全暂停"' in stream
    assert '"chapter.stale_scene_evidence": "awaiting_input"' in stream


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_stale_scene_evidence_browser_classifier_fails_closed() -> None:
    spa_path = STATIC / "workbench-spa.js"
    script = r"""
const fs = require("fs");
const vm = require("vm");
let source = fs.readFileSync(process.argv[1], "utf8");
const marker = "  const reviewStatusOf =";
if (!source.includes(marker)) throw new Error("classifier export marker is missing");
source = source.replace(marker, "  globalThis.__evidence = { staleSceneEvidenceDetailsOf, staleSceneEvidenceDetailsOfRun };\n  return;\n" + marker);
const sandbox = {
  document: {
    body: { hasAttribute: () => true },
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: () => ({})
  },
  window: { NovelAgentUI: {} }
};
sandbox.globalThis = sandbox;
vm.runInNewContext(source, sandbox);
const completeRun = {
  error_type: "SceneEvidenceAlignmentError",
  error_message: "stale_scene_evidence: repair operation crosses a Scene boundary or separator",
  unsettled_attempt_count: 0,
  committed: false,
  provider_calls: 6,
  chapter_chars: 3423
};
const completeDetails = {
  failure_origin: "stale_scene_evidence",
  provider_calls: 6,
  settled_provider_calls: 6,
  unsettled_attempt_count: 0,
  candidate_chars: 3423,
  formal_prose_exists: false,
  accepted: false,
  committed: false,
  writeback_attempted: false,
  writeback_applied: false,
  automatic_retry: false,
  safe_to_retry: false,
  recovery_required: true,
  source_error_type: "SceneEvidenceAlignmentError",
  alignment_reason: "repair operation crosses a Scene boundary or separator"
};
const output = {
  fromRun: sandbox.__evidence.staleSceneEvidenceDetailsOfRun(completeRun),
  unsettled: sandbox.__evidence.staleSceneEvidenceDetailsOfRun({ ...completeRun, unsettled_attempt_count: 1 }),
  noCandidate: sandbox.__evidence.staleSceneEvidenceDetailsOfRun({ ...completeRun, chapter_chars: 0 }),
  unreadableJob: sandbox.__evidence.staleSceneEvidenceDetailsOf({ error: { code: "engine_result_unreadable", stderr_tail: "SceneEvidenceAlignmentError: stale_scene_evidence: repair operation crosses a Scene boundary or separator" } }),
  incompleteJob: sandbox.__evidence.staleSceneEvidenceDetailsOf({ error: { code: "stale_scene_evidence", details: { ...completeDetails, writeback_applied: undefined } } }),
  verifiedJob: sandbox.__evidence.staleSceneEvidenceDetailsOf({ error: { code: "stale_scene_evidence", details: completeDetails } }),
  legacyViaRun: sandbox.__evidence.staleSceneEvidenceDetailsOf({ error: { code: "engine_result_unreadable" }, latest_run: completeRun })
};
process.stdout.write(JSON.stringify(output));
"""
    completed = subprocess.run(
        [shutil.which("node") or "node", "-e", script, str(spa_path)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout)

    assert result["fromRun"]["providerCalls"] == 6
    assert result["fromRun"]["chapterChars"] == 3423
    assert result["fromRun"]["reason"] == "repair operation crosses a Scene boundary or separator"
    assert result["unsettled"] is None
    assert result["noCandidate"] is None
    assert result["unreadableJob"] is None
    assert result["incompleteJob"] is None
    assert result["verifiedJob"]["verified"] is True
    assert result["verifiedJob"]["providerCalls"] == 6
    assert result["verifiedJob"]["chapterChars"] == 3423
    assert result["legacyViaRun"]["chapterChars"] == 3423
    assert result["legacyViaRun"]["providerCalls"] == 6


def test_repair_not_converging_uses_only_verified_structured_evidence_and_no_blind_retry() -> None:
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")
    stream = (STATIC / "workbench.js").read_text(encoding="utf-8")

    assert "const repairNotConvergingDetailsOf = (job) =>" in spa
    classifier = spa.split("const repairNotConvergingDetailsOf = (job) =>", 1)[1].split("const repairProblemLabels", 1)[0]
    assert 'value.code === "chapter_repair_not_converging"' in classifier
    assert "stderr" not in classifier
    for evidence in (
        'details.failure_origin === "chapter_repair_convergence"',
        "settledProviderCalls === providerCalls",
        "details.unsettled_attempt_count === 0",
        "details.formal_prose_exists === false",
        "details.accepted === false",
        "details.committed === false",
        "details.writeback_attempted === false",
        "details.writeback_applied === false",
        "details.automatic_retry === false",
        "details.safe_to_retry === false",
        "details.recovery_required === true",
    ):
        assert evidence in classifier

    assert "自动修订把阻断问题从 ${failure.originalProblemCount} 项降到 ${failure.finalProblemCount} 项" in spa
    assert "但新增了更严重的${repairNewProblemLabel(failure)}问题" in spa
    assert "字原候选已保留 · 正式正文未提交" in spa
    assert "次调用全部有已结算回执，没有未结算调用" in spa
    assert "系统已拒绝采用修订稿" in spa
    assert "reason: repairNotConverging ? repairNotConvergingSummary(repairNotConverging) : normalized.reason" in spa
    assert "summary: repairNotConverging ? repairNotConvergingSummary(repairNotConverging) : normalized.summary" in spa
    assert 'form.dataset.recoveryMode = "repair-convergence-audit"' in spa
    assert 'form.dataset.recoveryMode = "repair-convergence-classify"' in spa
    assert 'form.dataset.recoveryMode = "repair-convergence-resume"' in spa
    assert "先做无模型恢复分类" in spa
    assert "不会盲目重试" in spa
    assert "结构化安全证据尚未闭环；按钮保持禁用" in spa
    assert 'recoverButton.disabled = true' in spa

    assert stream.count('"chapter.repair_not_converging"') >= 3
    assert '"chapter.repair_not_converging": "自动修订未收敛，任务已安全暂停"' in stream
    assert '"chapter.repair_not_converging": "awaiting_input"' in stream


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_repair_not_converging_browser_classifier_fails_closed() -> None:
    spa_path = STATIC / "workbench-spa.js"
    script = r'''
const fs = require("fs");
const vm = require("vm");
let source = fs.readFileSync(process.argv[1], "utf8");
const marker = "  const reviewStatusOf =";
if (!source.includes(marker)) throw new Error("classifier export marker is missing");
source = source.replace(marker, "  globalThis.__repairEvidence = repairNotConvergingDetailsOf;\n  return;\n" + marker);
const sandbox = {
  document: {
    body: { hasAttribute: () => true },
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: () => ({})
  },
  window: { NovelAgentUI: {} }
};
sandbox.globalThis = sandbox;
vm.runInNewContext(source, sandbox);
const complete = {
  failure_origin: "chapter_repair_convergence",
  run_id: "chapter_23_run",
  provider_calls: 10,
  settled_provider_calls: 10,
  unsettled_attempt_count: 0,
  candidate_chars: 3433,
  formal_prose_exists: false,
  accepted: false,
  committed: false,
  writeback_attempted: false,
  writeback_applied: false,
  original_problem_count: 6,
  final_problem_count: 2,
  convergence_reason: "new_more_severe_problem",
  new_problem_codes: ["missing_opening_bridge", "missing_last_scene_continuity"],
  automatic_retry: false,
  safe_to_retry: false,
  recovery_required: true
};
const classify = (error) => sandbox.__repairEvidence({ error });
const output = {
  legacy: classify({ code: "engine_result_unreadable", stderr_tail: "RepairNotConvergingError: problem_counts=6 -> 2" }),
  verified: classify({ code: "chapter_repair_not_converging", details: complete }),
  unsettled: classify({ code: "chapter_repair_not_converging", details: { ...complete, unsettled_attempt_count: 1 } }),
  mismatchedSettled: classify({ code: "chapter_repair_not_converging", details: { ...complete, settled_provider_calls: 9 } }),
  missingWriteback: classify({ code: "chapter_repair_not_converging", details: { ...complete, writeback_attempted: undefined } }),
  misleadingStderr: classify({ code: "chapter_repair_not_converging", stderr_tail: "all calls settled; no commit", details: { ...complete, committed: undefined } })
};
process.stdout.write(JSON.stringify(output));
'''
    completed = subprocess.run(
        [shutil.which("node") or "node", "-e", script, str(spa_path)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout)

    assert result["legacy"] is None
    assert result["verified"]["verified"] is True
    assert result["verified"]["providerCalls"] == 10
    assert result["verified"]["candidateChars"] == 3433
    assert result["verified"]["originalProblemCount"] == 6
    assert result["verified"]["finalProblemCount"] == 2
    assert result["unsettled"]["verified"] is False
    assert result["mismatchedSettled"]["verified"] is False
    assert result["missingWriteback"]["verified"] is False
    assert result["misleadingStderr"]["verified"] is False


def test_pre_provider_provenance_git_failure_is_system_blocked_not_recovery() -> None:
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")
    stream = (STATIC / "workbench.js").read_text(encoding="utf-8")

    assert 'new Set(["pre_provider_provenance_unavailable"])' in spa
    assert 'value.code === "engine_result_unreadable"' in spa
    assert "Git metadata capture is unavailable" in spa
    assert "suspected: true" in spa
    assert "verified: Boolean(structuredError && zeroCallEvidenceVerified(details))" in spa
    for evidence in (
        "details.provider_reached === false",
        "details.provider_calls === 0",
        "details.unsettled_attempt_count === 0",
        "details.formal_prose_exists === false",
        "details.committed === false",
        "details.writeback_applied === false",
    ):
        assert evidence in spa
    assert "const provenanceFailure = paused ? preProviderProvenanceDetailsOf(session) : null;" in spa
    assert "provenanceFailure?.verified && session?.resume_ready === true" in spa
    assert "const contextBudget = paused && !provenanceFailure ? contextBudgetDetailsOf(session) : null;" in spa
    assert "const qualityRevision = paused && !provenanceFailure && !contextBudget && !repairConvergence && !repairAuthority && !staleSceneEvidence && (Boolean(qualityRejection) || isQualityRevisionPause(session));" in spa
    assert "const providerUncertain = paused && !provenanceFailure && !contextBudget" in spa

    assert "检测到疑似 Git 来源记录环境异常，正在进行安全核对。" in spa
    assert "本次尝试在模型请求前停止：新增模型调用 0 次、新增费用 0" in spa
    assert "模型调用、费用和产物状态尚未完全确认" in spa
    assert 'provenanceFailure ? "本机运行环境错误"' in spa
    assert 'provenanceFailure.verified ? "本次新增调用 0 次 · 新增费用 0 · 无草稿正文"' in spa
    assert 'title: "疑似 Git 环境错误"' in spa

    assert "pre_provider_provenance: preProviderProvenanceDetailsOf(normalized)" in spa
    assert "!job.pre_provider_provenance && !job.context_budget && isQualityRevisionPause(job)" in spa
    assert "!provenanceAtRecoveryChapter && !contextBudgetAtRecoveryChapter" in spa
    assert "state.pending = [...provenanceIssues, ...contextBudgetIssues" in spa
    assert "payload.chapter_recovery || payload.chapter_20_recovery" not in spa
    assert "pendingResponse.payload?.chapter_recovery || null" in spa
    assert "const recovery = payload.chapter_recovery;" in spa
    assert 'issue.scene_schema_failure === true' in spa
    assert 'Scene response was not valid structured JSON' in spa
    assert '场景响应格式错误，已安全分类' in spa
    assert '次调用已结算' in spa
    assert 'form.dataset.recoveryMode = "pre-provider-provenance"' in spa
    assert "if (provenanceFailure.verified && pausedParent?.resume_ready === true)" in spa
    assert 'setText("[data-recover-button]", `确认重新执行第 ${chapter} 章`)' in spa
    assert 'provenanceFailure.verified ? "等待系统修复" : "等待安全核对"' in spa
    assert 'action: provenanceReady ? "环境已修复" : "等待系统修复"' in spa
    assert "recoverButton.disabled = true;" in spa
    assert stream.count('"chapter.pre_provider_provenance_unavailable"') >= 3
    assert '"chapter.pre_provider_provenance_unavailable": "awaiting_input"' in stream


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_pre_provider_browser_classifiers_fail_closed_without_complete_evidence() -> None:
    spa_path = STATIC / "workbench-spa.js"
    script = r'''
const fs = require("fs");
const vm = require("vm");
let source = fs.readFileSync(process.argv[1], "utf8");
const marker = "  const reviewStatusOf =";
if (!source.includes(marker)) throw new Error("classifier export marker is missing");
source = source.replace(marker, "  globalThis.__evidence = { contextBudgetDetailsOf, preProviderProvenanceDetailsOf };\n  return;\n" + marker);
const sandbox = {
  document: {
    body: { hasAttribute: () => true },
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: () => ({})
  },
  window: { NovelAgentUI: {} }
};
sandbox.globalThis = sandbox;
vm.runInNewContext(source, sandbox);
const complete = {
  provider_reached: false,
  provider_calls: 0,
  unsettled_attempt_count: 0,
  chapter_chars: 0,
  formal_prose_exists: false,
  accepted: false,
  committed: false,
  persistence_committed: false,
  writeback_applied: false
};
const output = {
  legacyGit: sandbox.__evidence.preProviderProvenanceDetailsOf({ error: { code: "engine_result_unreadable", stderr_tail: "ProvenanceCaptureError: Git metadata capture is unavailable" } }),
  incompleteGit: sandbox.__evidence.preProviderProvenanceDetailsOf({ error: { code: "pre_provider_provenance_unavailable", details: { ...complete, writeback_applied: undefined } } }),
  verifiedGit: sandbox.__evidence.preProviderProvenanceDetailsOf({ error: { code: "pre_provider_provenance_unavailable", details: complete } }),
  legacyBudget: sandbox.__evidence.contextBudgetDetailsOf({ error: { code: "engine_result_unreadable", stderr_tail: "story_project_context_budget_exceeded: scene input requires 33212 tokens; hard limit is 32000" } }),
  verifiedBudget: sandbox.__evidence.contextBudgetDetailsOf({ error: { code: "pre_provider_context_budget_exceeded", details: { ...complete, required: 33212, hard: 32000, excess: 1212 } } })
};
process.stdout.write(JSON.stringify(output));
'''
    completed = subprocess.run(
        [shutil.which("node") or "node", "-e", script, str(spa_path)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout)

    assert result["legacyGit"] == {
        "dependency": "git",
        "suspected": True,
        "verified": False,
        "legacy": True,
    }
    assert result["incompleteGit"]["verified"] is False
    assert result["verifiedGit"]["verified"] is True
    assert result["legacyBudget"]["verified"] is False
    assert result["legacyBudget"]["requiredTokens"] == 33212
    assert result["verifiedBudget"]["verified"] is True


def test_overview_pending_count_does_not_special_case_chapter_20() -> None:
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")

    assert "overview.chapter_20_recovery?.recoverable" not in spa
    assert "const recoveryAttention" not in spa
    assert "issue.chapter || 20" not in spa
    assert "recovery?.chapter || 20" not in spa
    assert "overview.pending_count ?? project.pending_count" in spa
    assert 'setText("[data-overview-pending]", numberLabel(pendingTotal));' in spa
    assert "const pendingChapterNumber" in spa
    assert "const pendingChapterLabel" in spa
    assert "${pendingChapterLabel(issue)}" in spa


def test_chapter_page_does_not_treat_written_or_later_chapters_as_health_failures() -> None:
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")

    assert "const hasFormalProse" in spa
    assert 'chapter_card_error_code = "chapter_card_already_written"' in spa
    assert "error?.payload?.error?.code" in spa
    assert "本章已有正式正文，无需再确认生成输入。" in spa
    assert "本章已提交正文" in spa
    assert "还不能开始这一章" in spa
    assert "请到生产页使用「预检并开始」续写。" in spa
    assert "请先到生产页续写第 ${state.project.next_chapter} 章。" in spa
    # The generic health fallback remains only for true context-not-ready failures.
    render_card = spa.split("const renderChapterCard", 1)[1].split("const renderChapter =", 1)[0]
    assert "请先检查项目健康状态与本章章纲。" in render_card
    assert "alreadyWritten" in render_card
    assert "previousMissing" in render_card
    load_chapter = spa.split("const loadChapter =", 1)[1]
    assert "if (hasFormalProse)" in load_chapter
    assert "/chapters/${chapterNumber}/card`" in load_chapter
