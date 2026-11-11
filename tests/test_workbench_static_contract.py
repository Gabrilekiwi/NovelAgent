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


def test_pending_empty_heading_does_not_claim_production_stopped() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")
    template = (ROOT / "workbench" / "templates" / "pending.html").read_text(encoding="utf-8")

    pending_start = html.find('data-spa-view="pending"')
    pending_end = html.find('data-spa-view="history"')
    assert pending_start != -1 and pending_end > pending_start
    pending = html[pending_start:pending_end]
    heading = pending.split("data-pending-empty", 1)[0]

    assert "data-pending-heading-idle" in pending
    assert "data-pending-heading-active hidden" in pending
    assert "没有待处理项" in heading
    assert "连续生产没有被阻塞。没有需要你现在决定的事项。" in heading
    assert "自动生产在不确定处停下" not in heading
    assert "pendingHeadingIdle.hidden = state.pending.length > 0" in spa
    assert "pendingHeadingActive.hidden = state.pending.length === 0" in spa
    assert 'state.pending.length === 0 ? "连续生产没有被阻塞。没有需要你现在决定的事项。"' in spa
    assert "自动生产在不确定处停下，这里只展示必须由作者决定的内容。" in spa
    assert "{% if pending_items|default([]) %}{{ pending_items|length }} 项需要你处理{% else %}没有待处理项{% endif %}" in template
    assert "{% if pending_items|default([]) %}自动生产在不确定处停下。这里只展示必须由作者做决定的内容。{% else %}连续生产没有被阻塞。没有需要你现在决定的事项。{% endif %}" in template


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
    assert "!staleSceneEvidence && !sceneBoundaryValidation && !sceneSchemaValidation && !bridgePrevalidation && !providerPartialResponse && !qualityRejection && jobStatus === \"needs_recovery\"" in spa

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



def test_settled_scene_boundary_validation_failure_explains_next_step() -> None:
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")
    stream = (STATIC / "workbench.js").read_text(encoding="utf-8")

    assert "const sceneBoundaryValidationDetailsOfRun = (run) =>" in spa
    assert 'run?.error_type !== "SceneBoundaryValidationError"' in spa
    assert 'message.startsWith("scene boundary validation failed:")' in spa
    assert "const sceneBoundaryValidationDetailsOf = (job) =>" in spa
    assert 'value.code === "scene_boundary_validation"' in spa
    assert "scene_boundary_validation_failure: sceneBoundaryValidationFailure" in spa
    assert "因场景边界校验失败而停止" in spa
    assert "场景边界校验未通过" in spa
    assert "调用已结算，场景边界未通过，正文未提交" in spa
    assert "先做无模型恢复分类" in spa
    assert "不会盲目重试这次失败的场景生成" in spa
    assert 'form.dataset.recoveryMode = "scene-boundary-validation-classify"' in spa
    assert "边界重生" not in spa.split("sceneBoundaryValidationFailure ?")[1].split(";")[0]

    assert stream.count('"chapter.scene_boundary_validation"') >= 3
    assert '"chapter.scene_boundary_validation": "场景边界校验未通过，任务已安全暂停"' in stream
    assert '"chapter.scene_boundary_validation": "awaiting_input"' in stream


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_scene_boundary_validation_browser_classifier_fails_closed() -> None:
    spa_path = STATIC / "workbench-spa.js"
    script = r"""
const fs = require("fs");
const vm = require("vm");
let source = fs.readFileSync(process.argv[1], "utf8");
const marker = "  const reviewStatusOf =";
if (!source.includes(marker)) throw new Error("classifier export marker is missing");
source = source.replace(marker, "  globalThis.__evidence = { sceneBoundaryValidationDetailsOf, sceneBoundaryValidationDetailsOfRun };\n  return;\n" + marker);
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
  error_type: "SceneBoundaryValidationError",
  error_message: "scene boundary validation failed: scene_boundary_state_mismatch x2, location_state_rollback; first=scene_boundary_state_mismatch: Scene 4 places X at Y without a matching location delta.; evidence={}",
  unsettled_attempt_count: 0,
  committed: false,
  provider_calls: 2,
  chapter_chars: 0
};
const completeDetails = {
  failure_origin: "scene_boundary_validation",
  provider_calls: 2,
  settled_provider_calls: 2,
  unsettled_attempt_count: 0,
  candidate_chars: 0,
  formal_prose_exists: false,
  accepted: false,
  committed: false,
  writeback_attempted: false,
  writeback_applied: false,
  automatic_retry: false,
  safe_to_retry: false,
  recovery_required: true,
  source_error_type: "SceneBoundaryValidationError",
  finding_codes: ["scene_boundary_state_mismatch", "location_state_rollback"],
  failed_scene: 4
};
const output = {
  fromRun: sandbox.__evidence.sceneBoundaryValidationDetailsOfRun(completeRun),
  unsettled: sandbox.__evidence.sceneBoundaryValidationDetailsOfRun({ ...completeRun, unsettled_attempt_count: 1 }),
  staleRun: sandbox.__evidence.sceneBoundaryValidationDetailsOfRun({ ...completeRun, error_type: "SceneEvidenceAlignmentError", error_message: "stale_scene_evidence: repair operation crosses a Scene boundary or separator" }),
  unreadableJob: sandbox.__evidence.sceneBoundaryValidationDetailsOf({ error: { code: "engine_result_unreadable", stderr_tail: "SceneBoundaryValidationError: scene boundary validation failed: scene_boundary_state_mismatch" } }),
  incompleteJob: sandbox.__evidence.sceneBoundaryValidationDetailsOf({ error: { code: "scene_boundary_validation", details: { ...completeDetails, writeback_applied: undefined } } }),
  verifiedJob: sandbox.__evidence.sceneBoundaryValidationDetailsOf({ error: { code: "scene_boundary_validation", details: completeDetails } }),
  legacyViaRun: sandbox.__evidence.sceneBoundaryValidationDetailsOf({ error: { code: "engine_result_unreadable" }, latest_run: completeRun })
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

    assert result["fromRun"]["providerCalls"] == 2
    assert result["fromRun"]["chapterChars"] == 0
    assert result["fromRun"]["failedScene"] == 4
    assert result["fromRun"]["findingCodes"] == [
        "scene_boundary_state_mismatch",
        "location_state_rollback",
    ]
    assert result["unsettled"] is None
    assert result["staleRun"] is None
    assert result["unreadableJob"] is None
    assert result["incompleteJob"] is None
    assert result["verifiedJob"]["verified"] is True
    assert result["verifiedJob"]["providerCalls"] == 2
    assert result["legacyViaRun"]["verified"] is True




def test_settled_bridge_prevalidation_failure_explains_next_step() -> None:
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")
    stream = (STATIC / "workbench.js").read_text(encoding="utf-8")

    assert "const bridgePrevalidationDetailsOfRun = (run) =>" in spa
    assert 'message.startsWith("bridge pre-validation failed:")' in spa
    assert "const bridgePrevalidationDetailsOf = (job) =>" in spa
    assert 'value.code === "bridge_prevalidation"' in spa
    assert "bridge_prevalidation: bridgePrevalidation" in spa
    assert "开写前衔接预检未通过" in spa
    assert "模型请求尚未发出，衔接预检未通过" in spa
    assert "先做无模型恢复分类" in spa
    assert "不会盲目重试这次失败的衔接预检" in spa
    assert 'form.dataset.recoveryMode = "bridge-prevalidation-classify"' in spa
    assert "边界重生" not in spa.split("bridgePrevalidationFailure ?")[1].split(";")[0]

    assert stream.count('"chapter.bridge_prevalidation"') >= 3
    assert '"chapter.bridge_prevalidation": "开写前衔接预检未通过，任务已安全暂停"' in stream
    assert '"chapter.bridge_prevalidation": "awaiting_input"' in stream


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_bridge_prevalidation_browser_classifier_fails_closed() -> None:
    spa_path = STATIC / "workbench-spa.js"
    script = r"""
const fs = require("fs");
const vm = require("vm");
let source = fs.readFileSync(process.argv[1], "utf8");
const marker = "  const reviewStatusOf =";
if (!source.includes(marker)) throw new Error("classifier export marker is missing");
source = source.replace(marker, "  globalThis.__evidence = { bridgePrevalidationDetailsOf, bridgePrevalidationDetailsOfRun };\n  return;\n" + marker);
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
  error_type: "ValueError",
  error_message: "bridge pre-validation failed: missing_last_scene_continuity",
  unsettled_attempt_count: 0,
  committed: false,
  provider_calls: 0,
  chapter_chars: 0
};
const completeDetails = {
  failure_origin: "bridge_prevalidation",
  provider_calls: 0,
  settled_provider_calls: 0,
  unsettled_attempt_count: 0,
  candidate_chars: 0,
  provider_reached: false,
  formal_prose_exists: false,
  accepted: false,
  committed: false,
  writeback_attempted: false,
  writeback_applied: false,
  automatic_retry: false,
  safe_to_retry: false,
  recovery_required: true,
  source_error_type: "ValueError",
  source_error_message: "bridge pre-validation failed: missing_last_scene_continuity",
  problem_codes: ["missing_last_scene_continuity"]
};
const output = {
  fromRun: sandbox.__evidence.bridgePrevalidationDetailsOfRun(completeRun),
  withCalls: sandbox.__evidence.bridgePrevalidationDetailsOfRun({ ...completeRun, provider_calls: 1 }),
  schemaRun: sandbox.__evidence.bridgePrevalidationDetailsOfRun({ ...completeRun, error_message: "Scene response was not valid structured JSON" }),
  unreadableJob: sandbox.__evidence.bridgePrevalidationDetailsOf({ error: { code: "engine_result_unreadable", stderr_tail: "ValueError: bridge pre-validation failed: missing_last_scene_continuity" } }),
  incompleteJob: sandbox.__evidence.bridgePrevalidationDetailsOf({ error: { code: "bridge_prevalidation", details: { ...completeDetails, writeback_applied: undefined } } }),
  verifiedJob: sandbox.__evidence.bridgePrevalidationDetailsOf({ error: { code: "bridge_prevalidation", details: completeDetails } }),
  legacyViaRun: sandbox.__evidence.bridgePrevalidationDetailsOf({ error: { code: "engine_result_unreadable" }, latest_run: completeRun })
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

    assert result["fromRun"]["providerCalls"] == 0
    assert result["fromRun"]["problemCodes"] == ["missing_last_scene_continuity"]
    assert result["withCalls"] is None
    assert result["schemaRun"] is None
    assert result["unreadableJob"] is None
    assert result["incompleteJob"] is None
    assert result["verifiedJob"]["verified"] is True
    assert result["verifiedJob"]["providerCalls"] == 0
    assert result["legacyViaRun"]["verified"] is True




def test_settled_provider_partial_response_failure_explains_next_step() -> None:
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")
    stream = (STATIC / "workbench.js").read_text(encoding="utf-8")

    assert "const providerPartialResponseDetailsOfRun = (run) =>" in spa
    assert "const providerPartialResponseDetailsOf = (job) =>" in spa
    assert 'value.code === "provider_partial_response"' in spa
    assert "provider_partial_response: providerPartialResponse" in spa
    assert "模型部分响应中断" in spa
    assert "调用已有终态回执，部分响应已保留" in spa
    assert "不是结果不确定" in spa
    assert "先做无模型恢复分类" in spa
    assert 'form.dataset.recoveryMode = "provider-partial-response-classify"' in spa
    assert "边界重生" not in spa.split("providerPartialResponseFailure ?")[1].split(";")[0]

    assert stream.count('"chapter.provider_partial_response"') >= 3
    assert '"chapter.provider_partial_response": "模型部分响应中断，已保留已付费内容，任务已安全暂停"' in stream
    assert '"chapter.provider_partial_response": "awaiting_input"' in stream


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_provider_partial_response_browser_classifier_fails_closed() -> None:
    spa_path = STATIC / "workbench-spa.js"
    script = r"""
const fs = require("fs");
const vm = require("vm");
let source = fs.readFileSync(process.argv[1], "utf8");
const marker = "  const reviewStatusOf =";
if (!source.includes(marker)) throw new Error("classifier export marker is missing");
source = source.replace(marker, "  globalThis.__evidence = { providerPartialResponseDetailsOf, providerPartialResponseDetailsOfRun };\n  return;\n" + marker);
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
  error_type: "ModelCallError",
  error_message: "OpenAI chat completion failed (provider_partial_response; ProviderPartialResponseError).",
  unsettled_attempt_count: 1,
  committed: false,
  provider_calls: 4,
  chapter_chars: 12
};
const completeDetails = {
  failure_origin: "provider_partial_response",
  provider_calls: 4,
  settled_provider_calls: 4,
  unsettled_attempt_count: 1,
  candidate_chars: 12,
  provider_reached: true,
  formal_prose_exists: false,
  accepted: false,
  committed: false,
  writeback_attempted: false,
  writeback_applied: false,
  automatic_retry: false,
  safe_to_retry: false,
  recovery_required: true,
  partial_receipt_status: "partial_response_failed",
  source_error_type: "ModelCallError",
  source_error_message: "OpenAI chat completion failed (provider_partial_response; ProviderPartialResponseError)."
};
const output = {
  fromRun: sandbox.__evidence.providerPartialResponseDetailsOfRun(completeRun),
  zeroCalls: sandbox.__evidence.providerPartialResponseDetailsOfRun({ ...completeRun, provider_calls: 0 }),
  uncertainRun: sandbox.__evidence.providerPartialResponseDetailsOfRun({ ...completeRun, error_message: "OpenAI chat completion failed (provider_call_uncertain; ProviderCallUncertainError)." }),
  unreadableJob: sandbox.__evidence.providerPartialResponseDetailsOf({ error: { code: "engine_result_unreadable", stderr_tail: "ModelCallError: OpenAI chat completion failed (provider_partial_response; ProviderPartialResponseError)." } }),
  incompleteJob: sandbox.__evidence.providerPartialResponseDetailsOf({ error: { code: "provider_partial_response", details: { ...completeDetails, writeback_applied: undefined } } }),
  verifiedJob: sandbox.__evidence.providerPartialResponseDetailsOf({ error: { code: "provider_partial_response", details: completeDetails } }),
  legacyViaRun: sandbox.__evidence.providerPartialResponseDetailsOf({ error: { code: "engine_result_unreadable" }, latest_run: completeRun })
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

    assert result["fromRun"]["providerCalls"] == 4
    assert result["fromRun"]["chapterChars"] == 12
    assert result["zeroCalls"] is None
    assert result["uncertainRun"] is None
    assert result["unreadableJob"] is None
    assert result["incompleteJob"] is None
    assert result["verifiedJob"]["verified"] is True
    assert result["verifiedJob"]["providerCalls"] == 4
    assert result["legacyViaRun"]["verified"] is True




def test_settled_local_budget_after_provider_failure_explains_next_step() -> None:
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")
    stream = (STATIC / "workbench.js").read_text(encoding="utf-8")

    assert "const localBudgetAfterProviderDetailsOfRun = (run) =>" in spa
    assert "const localBudgetAfterProviderDetailsOf = (job) =>" in spa
    assert 'value.code === "local_budget_after_provider"' in spa
    assert "local_budget_after_provider: localBudgetAfterProvider" in spa
    assert "已付费后本地预算中断" in spa
    assert "调用已有终态回执，已付费内容已保留" in spa
    assert "不是调用前的预算拒绝" in spa
    assert 'form.dataset.recoveryMode = "local-budget-after-provider-classify"' in spa
    assert "尚未调用模型" not in spa.split("localBudgetAfterProviderFailure ?")[1].split(";")[0]

    assert stream.count('"chapter.local_budget_after_provider"') >= 3
    assert '"chapter.local_budget_after_provider": "本地预算在已有调用后中断，已保留已付费内容，任务已安全暂停"' in stream
    assert '"chapter.local_budget_after_provider": "awaiting_input"' in stream


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_local_budget_after_provider_browser_classifier_fails_closed() -> None:
    spa_path = STATIC / "workbench-spa.js"
    script = r"""
const fs = require("fs");
const vm = require("vm");
let source = fs.readFileSync(process.argv[1], "utf8");
const marker = "  const reviewStatusOf =";
if (!source.includes(marker)) throw new Error("classifier export marker is missing");
source = source.replace(marker, "  globalThis.__evidence = { localBudgetAfterProviderDetailsOf, localBudgetAfterProviderDetailsOfRun };\n  return;\n" + marker);
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
  error_type: "ModelCallError",
  error_message: "OpenAI chat completion failed (local_budget; run_input_token_budget_exceeded: max_total_input_tokens exceeded).",
  unsettled_attempt_count: 0,
  committed: false,
  provider_calls: 4,
  chapter_chars: 12
};
const completeDetails = {
  failure_origin: "local_budget_after_provider",
  provider_calls: 4,
  settled_provider_calls: 4,
  succeeded_provider_calls: 3,
  unsettled_attempt_count: 0,
  candidate_chars: 12,
  provider_reached: true,
  formal_prose_exists: false,
  accepted: false,
  committed: false,
  writeback_attempted: false,
  writeback_applied: false,
  automatic_retry: false,
  safe_to_retry: false,
  recovery_required: true,
  rejected_receipt_status: "budget_rejected",
  source_error_type: "ModelCallError",
  source_error_message: "OpenAI chat completion failed (local_budget; run_input_token_budget_exceeded: max_total_input_tokens exceeded)."
};
const output = {
  fromRun: sandbox.__evidence.localBudgetAfterProviderDetailsOfRun(completeRun),
  zeroCalls: sandbox.__evidence.localBudgetAfterProviderDetailsOfRun({ ...completeRun, provider_calls: 1 }),
  obsoleteRun: sandbox.__evidence.localBudgetAfterProviderDetailsOfRun({ ...completeRun, error_message: "OpenAI chat completion failed (provider_error; ContextBudgetError)." }),
  unreadableJob: sandbox.__evidence.localBudgetAfterProviderDetailsOf({ error: { code: "engine_result_unreadable", stderr_tail: "ModelCallError: OpenAI chat completion failed (local_budget; run_input_token_budget_exceeded: max_total_input_tokens exceeded)." } }),
  incompleteJob: sandbox.__evidence.localBudgetAfterProviderDetailsOf({ error: { code: "local_budget_after_provider", details: { ...completeDetails, writeback_applied: undefined } } }),
  verifiedJob: sandbox.__evidence.localBudgetAfterProviderDetailsOf({ error: { code: "local_budget_after_provider", details: completeDetails } }),
  legacyViaRun: sandbox.__evidence.localBudgetAfterProviderDetailsOf({ error: { code: "engine_result_unreadable" }, latest_run: completeRun })
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

    assert result["fromRun"]["providerCalls"] == 4
    assert result["fromRun"]["chapterChars"] == 12
    assert result["zeroCalls"] is None
    assert result["obsoleteRun"] is None
    assert result["unreadableJob"] is None
    assert result["incompleteJob"] is None
    assert result["verifiedJob"]["verified"] is True
    assert result["verifiedJob"]["providerCalls"] == 4
    assert result["legacyViaRun"]["verified"] is True


def test_settled_scene_schema_validation_failure_explains_next_step() -> None:
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")
    stream = (STATIC / "workbench.js").read_text(encoding="utf-8")

    assert "const sceneSchemaValidationDetailsOfRun = (run) =>" in spa
    assert 'run?.error_type !== "ValueError"' in spa
    assert 'message !== "Scene response was not valid structured JSON"' in spa
    assert "const sceneSchemaValidationDetailsOf = (job) =>" in spa
    assert 'value.code === "scene_schema_validation"' in spa
    assert "scene_schema_validation: sceneSchemaValidation" in spa
    assert "场景响应格式错误" in spa
    assert "调用已结算，格式错误没有进入正文" in spa
    assert "先做无模型恢复分类" in spa
    assert "不会盲目重试这次格式错误的场景生成" in spa
    assert 'form.dataset.recoveryMode = "scene-schema-validation-classify"' in spa
    assert "边界重生" not in spa.split("sceneSchemaFailure ?")[1].split(";")[0]

    assert stream.count('"chapter.scene_schema_validation"') >= 3
    assert '"chapter.scene_schema_validation": "场景响应格式错误，任务已安全暂停"' in stream
    assert '"chapter.scene_schema_validation": "awaiting_input"' in stream


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_scene_schema_validation_browser_classifier_fails_closed() -> None:
    spa_path = STATIC / "workbench-spa.js"
    script = r"""
const fs = require("fs");
const vm = require("vm");
let source = fs.readFileSync(process.argv[1], "utf8");
const marker = "  const reviewStatusOf =";
if (!source.includes(marker)) throw new Error("classifier export marker is missing");
source = source.replace(marker, "  globalThis.__evidence = { sceneSchemaValidationDetailsOf, sceneSchemaValidationDetailsOfRun };\n  return;\n" + marker);
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
  error_type: "ValueError",
  error_message: "Scene response was not valid structured JSON",
  unsettled_attempt_count: 0,
  committed: false,
  provider_calls: 3,
  chapter_chars: 0
};
const completeDetails = {
  failure_origin: "scene_schema_validation",
  provider_calls: 3,
  settled_provider_calls: 3,
  unsettled_attempt_count: 0,
  candidate_chars: 0,
  formal_prose_exists: false,
  accepted: false,
  committed: false,
  writeback_attempted: false,
  writeback_applied: false,
  automatic_retry: false,
  safe_to_retry: false,
  recovery_required: true,
  source_error_type: "ValueError",
  source_error_message: "Scene response was not valid structured JSON"
};
const output = {
  fromRun: sandbox.__evidence.sceneSchemaValidationDetailsOfRun(completeRun),
  unsettled: sandbox.__evidence.sceneSchemaValidationDetailsOfRun({ ...completeRun, unsettled_attempt_count: 1 }),
  bridgeRun: sandbox.__evidence.sceneSchemaValidationDetailsOfRun({ ...completeRun, error_message: "bridge pre-validation failed: missing_last_scene_continuity" }),
  unreadableJob: sandbox.__evidence.sceneSchemaValidationDetailsOf({ error: { code: "engine_result_unreadable", stderr_tail: "ValueError: Scene response was not valid structured JSON" } }),
  incompleteJob: sandbox.__evidence.sceneSchemaValidationDetailsOf({ error: { code: "scene_schema_validation", details: { ...completeDetails, writeback_applied: undefined } } }),
  verifiedJob: sandbox.__evidence.sceneSchemaValidationDetailsOf({ error: { code: "scene_schema_validation", details: completeDetails } }),
  legacyViaRun: sandbox.__evidence.sceneSchemaValidationDetailsOf({ error: { code: "engine_result_unreadable" }, latest_run: completeRun })
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

    assert result["fromRun"]["providerCalls"] == 3
    assert result["fromRun"]["chapterChars"] == 0
    assert result["unsettled"] is None
    assert result["bridgeRun"] is None
    assert result["unreadableJob"] is None
    assert result["incompleteJob"] is None
    assert result["verifiedJob"]["verified"] is True
    assert result["verifiedJob"]["providerCalls"] == 3
    assert result["legacyViaRun"]["verified"] is True


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



def test_settled_context_budget_after_provider_failure_explains_next_step() -> None:
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")
    stream = (STATIC / "workbench.js").read_text(encoding="utf-8")

    assert "const contextBudgetAfterProviderDetailsOfRun = (run) =>" in spa
    assert "const contextBudgetAfterProviderDetailsOf = (job) =>" in spa
    assert 'value.code === "context_budget_after_provider"' in spa
    assert "context_budget_after_provider: contextBudgetAfterProvider" in spa
    assert "已付费后上下文预算中断" in spa
    assert "调用已结算，已付费内容已保留" in spa
    assert "不是调用前的预算拒绝" in spa
    assert 'form.dataset.recoveryMode = "context-budget-after-provider-classify"' in spa
    assert "尚未调用模型" not in spa.split("contextBudgetAfterProviderFailure ?")[1].split(";")[0]

    assert stream.count('"chapter.context_budget_after_provider"') >= 3
    assert '"chapter.context_budget_after_provider": "已付费调用后上下文预算中断，已保留已付费内容，任务已安全暂停"' in stream
    assert '"chapter.context_budget_after_provider": "awaiting_input"' in stream


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_context_budget_after_provider_browser_classifier_fails_closed() -> None:
    spa_path = STATIC / "workbench-spa.js"
    script = r"""
const fs = require("fs");
const vm = require("vm");
let source = fs.readFileSync(process.argv[1], "utf8");
const marker = "  const reviewStatusOf =";
if (!source.includes(marker)) throw new Error("classifier export marker is missing");
source = source.replace(marker, "  globalThis.__evidence = { contextBudgetAfterProviderDetailsOf, contextBudgetAfterProviderDetailsOfRun, contextBudgetDetailsOf };\n  return;\n" + marker);
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
  error_type: "ContextBudgetError",
  error_message: "story_project_context_budget_exceeded: repair input requires 39267 tokens; hard limit is 32000",
  unsettled_attempt_count: 0,
  committed: false,
  provider_calls: 6,
  chapter_chars: 4398
};
const completeDetails = {
  failure_origin: "context_budget_after_provider",
  input_stage: "repair",
  limit_kind: "hard_limit",
  stage: "repair_hard_limit",
  required_tokens: 39267,
  hard_limit_tokens: 32000,
  overage_tokens: 7267,
  provider_calls: 6,
  settled_provider_calls: 6,
  succeeded_provider_calls: 6,
  unsettled_attempt_count: 0,
  candidate_chars: 4398,
  provider_reached: true,
  formal_prose_exists: false,
  accepted: false,
  committed: false,
  writeback_attempted: false,
  writeback_applied: false,
  automatic_retry: false,
  safe_to_retry: false,
  recovery_required: true,
  source_error_type: "ContextBudgetError",
  source_error_message: "story_project_context_budget_exceeded: repair input requires 39267 tokens; hard limit is 32000"
};
const output = {
  fromRun: sandbox.__evidence.contextBudgetAfterProviderDetailsOfRun(completeRun),
  zeroCalls: sandbox.__evidence.contextBudgetAfterProviderDetailsOfRun({ ...completeRun, provider_calls: 0 }),
  obsoleteRun: sandbox.__evidence.contextBudgetAfterProviderDetailsOfRun({ ...completeRun, error_type: "ModelCallError", error_message: "OpenAI chat completion failed (provider_error; ContextBudgetError)." }),
  wrapRun: sandbox.__evidence.contextBudgetAfterProviderDetailsOfRun({ ...completeRun, error_type: "ModelCallError", error_message: "OpenAI chat completion failed (local_budget; story_project_context_budget_exceeded: scene input requires 32830 tokens; hard limit is 32000)." }),
  unreadableJob: sandbox.__evidence.contextBudgetAfterProviderDetailsOf({ error: { code: "engine_result_unreadable", stderr_tail: "ContextBudgetError: story_project_context_budget_exceeded: repair input requires 39267 tokens; hard limit is 32000" } }),
  incompleteJob: sandbox.__evidence.contextBudgetAfterProviderDetailsOf({ error: { code: "context_budget_after_provider", details: { ...completeDetails, writeback_applied: undefined } } }),
  verifiedJob: sandbox.__evidence.contextBudgetAfterProviderDetailsOf({ error: { code: "context_budget_after_provider", details: completeDetails } }),
  legacyViaRun: sandbox.__evidence.contextBudgetAfterProviderDetailsOf({ error: { code: "engine_result_unreadable" }, latest_run: completeRun }),
  preProviderNotStolen: sandbox.__evidence.contextBudgetDetailsOf({ error: { code: "engine_result_unreadable", stderr_tail: "story_project_context_budget_exceeded: scene input requires 32830 tokens; hard limit is 32000" }, latest_run: { error_type: "ContextBudgetError", provider_calls: 9, error_message: "story_project_context_budget_exceeded: scene input requires 32830 tokens; hard limit is 32000" } })
};
process.stdout.write(JSON.stringify(output));
"""
    completed = subprocess.run(
        [shutil.which("node") or "node", "-e", script, str(spa_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)
    assert result["fromRun"]["verified"] is True
    assert result["fromRun"]["providerCalls"] == 6
    assert result["fromRun"]["chapterChars"] == 4398
    assert result["fromRun"]["inputStage"] == "repair"
    assert result["fromRun"]["limitKind"] == "hard_limit"
    assert result["zeroCalls"] is None
    assert result["obsoleteRun"] is None
    assert result["wrapRun"] is None
    assert result["unreadableJob"] is None
    assert result["incompleteJob"] is None
    assert result["verifiedJob"]["verified"] is True
    assert result["verifiedJob"]["chapterChars"] == 4398
    assert result["legacyViaRun"]["verified"] is True
    assert result["preProviderNotStolen"] is None


def test_settled_structured_context_after_provider_failure_explains_next_step() -> None:
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")
    stream = (STATIC / "workbench.js").read_text(encoding="utf-8")

    assert "const structuredContextAfterProviderDetailsOfRun = (run) =>" in spa
    assert "const structuredContextAfterProviderDetailsOf = (job) =>" in spa
    assert 'value.code === "structured_context_after_provider"' in spa
    assert "structured_context_after_provider: structuredContextAfterProvider" in spa
    assert "已付费后结构化上下文中断" in spa
    assert "结构化上下文装配中断" in spa
    assert "不是调用前的预算拒绝" in spa
    assert 'form.dataset.recoveryMode = "structured-context-after-provider-classify"' in spa
    assert "&& !structuredContextAfterProviderFailure" in spa
    assert "&& !repairAuthorityFailure" in spa.split("const forceResetAllowed")[1].split(");")[0]
    assert "先做无模型恢复分类" in spa.split("structured-context-after-provider-classify")[1][:800]

    assert stream.count('"chapter.structured_context_after_provider"') >= 3
    assert '"chapter.structured_context_after_provider": "已付费调用后结构化上下文装配中断，已保留已付费内容，任务已安全暂停"' in stream
    assert '"chapter.structured_context_after_provider": "awaiting_input"' in stream


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_structured_context_after_provider_browser_classifier_fails_closed() -> None:
    spa_path = STATIC / "workbench-spa.js"
    script = r"""
const fs = require("fs");
const vm = require("vm");
let source = fs.readFileSync(process.argv[1], "utf8");
const marker = "  const reviewStatusOf =";
if (!source.includes(marker)) throw new Error("classifier export marker is missing");
source = source.replace(marker, "  globalThis.__evidence = { structuredContextAfterProviderDetailsOf, structuredContextAfterProviderDetailsOfRun, contextBudgetAfterProviderDetailsOfRun };\n  return;\n" + marker);
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
const generateRun = {
  error_type: "StructuredContextError",
  error_message: "required_json_item_exceeds_budget: required entries exceed 1472 characters",
  unsettled_attempt_count: 0,
  committed: false,
  provider_calls: 1,
  chapter_chars: 0
};
const repairRun = {
  error_type: "StructuredContextError",
  error_message: "required_authoritative_records_exceed_budget: required records need 6425 characters including audit metadata, exceeding the 5978 character authority budget: characters/LinXia",
  unsettled_attempt_count: 0,
  committed: false,
  provider_calls: 2,
  chapter_chars: 3400
};
const completeDetails = {
  failure_origin: "structured_context_after_provider",
  code: "required_authoritative_records_exceed_budget",
  input_stage: "repair",
  limit_kind: "character_budget",
  stage: "repair_authority",
  required_chars: 6425,
  hard_limit_chars: 5978,
  overage_chars: 447,
  provider_calls: 2,
  settled_provider_calls: 2,
  succeeded_provider_calls: 2,
  unsettled_attempt_count: 0,
  candidate_chars: 3400,
  provider_reached: true,
  formal_prose_exists: false,
  accepted: false,
  committed: false,
  writeback_attempted: false,
  writeback_applied: false,
  automatic_retry: false,
  safe_to_retry: false,
  recovery_required: true,
  source_error_type: "StructuredContextError",
  source_error_message: "required_authoritative_records_exceed_budget: required records need 6425 characters including audit metadata, exceeding the 5978 character authority budget: characters/LinXia"
};
const output = {
  fromGenerate: sandbox.__evidence.structuredContextAfterProviderDetailsOfRun(generateRun),
  fromRepair: sandbox.__evidence.structuredContextAfterProviderDetailsOfRun(repairRun),
  zeroCalls: sandbox.__evidence.structuredContextAfterProviderDetailsOfRun(Object.assign({}, generateRun, { provider_calls: 0 })),
  wrapRun: sandbox.__evidence.structuredContextAfterProviderDetailsOfRun(Object.assign({}, generateRun, { error_type: "ModelCallError", error_message: "ModelCallError: (local_budget; required_json_item_exceeds_budget: required entries exceed 1472 characters)" })),
  tokenBudgetNotStolen: sandbox.__evidence.structuredContextAfterProviderDetailsOfRun(Object.assign({}, repairRun, { error_type: "ContextBudgetError", error_message: "story_project_context_budget_exceeded: repair input requires 39267 tokens; hard limit is 32000" })),
  unreadableJob: sandbox.__evidence.structuredContextAfterProviderDetailsOf({ error: { code: "engine_result_unreadable", stderr_tail: "StructuredContextError: required_json_item_exceeds_budget: required entries exceed 1472 characters" } }),
  incompleteJob: sandbox.__evidence.structuredContextAfterProviderDetailsOf({ error: { code: "structured_context_after_provider", details: Object.assign({}, completeDetails, { writeback_applied: undefined }) } }),
  verifiedJob: sandbox.__evidence.structuredContextAfterProviderDetailsOf({ error: { code: "structured_context_after_provider", details: completeDetails } }),
  legacyViaRun: sandbox.__evidence.structuredContextAfterProviderDetailsOf({ error: { code: "engine_result_unreadable" }, latest_run: repairRun }),
  cbapNotStolen: sandbox.__evidence.contextBudgetAfterProviderDetailsOfRun(repairRun)
};
process.stdout.write(JSON.stringify(output));
"""
    completed = subprocess.run(
        [shutil.which("node") or "node", "-e", script, str(spa_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)
    assert result["fromGenerate"]["verified"] is True
    assert result["fromGenerate"]["providerCalls"] == 1
    assert result["fromGenerate"]["chapterChars"] == 0
    assert result["fromGenerate"]["inputStage"] == "generate"
    assert result["fromGenerate"]["hardLimitChars"] == 1472
    assert result["fromRepair"]["verified"] is True
    assert result["fromRepair"]["providerCalls"] == 2
    assert result["fromRepair"]["chapterChars"] == 3400
    assert result["fromRepair"]["inputStage"] == "repair"
    assert result["fromRepair"]["requiredChars"] == 6425
    assert result["zeroCalls"] is None
    assert result["wrapRun"] is None
    assert result["tokenBudgetNotStolen"] is None
    assert result["unreadableJob"] is None
    assert result["incompleteJob"] is None
    assert result["verifiedJob"]["verified"] is True
    assert result["verifiedJob"]["chapterChars"] == 3400
    assert result["legacyViaRun"]["verified"] is True
    assert result["cbapNotStolen"] is None


def test_unsettled_paid_draft_uncertain_copy_discloses_chars_and_keeps_classify_primary() -> None:
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")

    assert "const retainedUnsettledPaidDraft = (issue) =>" in spa
    assert "const providerUncertainPaidDraftCopy = (issue) =>" in spa
    assert "已付费的 ${paidDraft.chapterChars} 字候选正文仍保留在隔离运行区" in spa
    assert "但不能当作已验证候选" in spa
    assert "也不要先结束失败尝试来隐藏已付费正文。请先做无模型恢复分类。" in spa
    assert "上次调用结果不确定，已保留 ${retainedUnsettledPaidDraft({ latest_run: latestRecoveryRun }).chapterChars} 字候选，等待无模型恢复分类" in spa
    # The no-run wording must remain, and must not be used as the paid-draft next step.
    assert "上次模型调用结果不确定，没有完整回执。不要盲目重试；请先结束本章失败尝试，再开始新的一次写作。" in spa
    assert "没有完整回执和章节运行记录。不要盲目重试。" in spa
    assert 'forceResetForm.dataset.recoveryMode = uncertainWithoutRun ? "force-reset-without-run" : "force-reset"' in spa
    assert spa.count("请先做无模型恢复分类") >= 1
    # PPR disclosure stays truthful and distinct from uncertain copy.
    assert "字候选正文已保留，" in spa
    assert "不要当成无回执的不确定调用" in spa


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_provider_uncertain_copy_discloses_official_paid_draft_and_fails_closed() -> None:
    spa_path = STATIC / "workbench-spa.js"
    script = r"""
const fs = require("fs");
const vm = require("vm");
let source = fs.readFileSync(process.argv[1], "utf8");
const marker = "  const api = async (path, options = {}) => {";
if (!source.includes(marker)) throw new Error("uncertain copy export marker is missing");
source = source.replace(
  marker,
  "  globalThis.__evidence = { retainedUnsettledPaidDraft, providerUncertainCopy, providerUncertainPaidDraftCopy };\n  return;\n" + marker
);
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
const officialCh60 = {
  error_type: "ModelCallError",
  error_message: "OpenAI chat completion failed (provider_call_uncertain; ProviderCallUncertainError).",
  unsettled_attempt_count: 1,
  committed: false,
  provider_calls: 6,
  chapter_chars: 3500,
  failed_action: "validate"
};
const ev = sandbox.__evidence;
const output = {
  officialPaid: ev.providerUncertainCopy({ latest_run: officialCh60 }),
  officialDraft: ev.retainedUnsettledPaidDraft({ latest_run: officialCh60 }),
  withoutRun: ev.providerUncertainCopy({
    uncertain_without_run: true,
    error: { message: "ProviderCallUncertainError: provider_call_uncertain" }
  }),
  noChars: ev.retainedUnsettledPaidDraft({ latest_run: { ...officialCh60, chapter_chars: 0 } }),
  settled: ev.retainedUnsettledPaidDraft({ latest_run: { ...officialCh60, unsettled_attempt_count: 0 } }),
  committed: ev.retainedUnsettledPaidDraft({ latest_run: { ...officialCh60, committed: true } }),
  ppr: ev.retainedUnsettledPaidDraft({
    latest_run: {
      ...officialCh60,
      error_message: "OpenAI chat completion failed (provider_partial_response; ProviderPartialResponseError).",
      error_type: "ModelCallError",
      chapter_chars: 6318,
      failed_action: "repair_if_needed"
    }
  }),
  tooShort: ev.retainedUnsettledPaidDraft({ latest_run: { ...officialCh60, chapter_chars: 499 } })
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

    assert result["officialDraft"]["chapterChars"] == 3500
    assert result["officialDraft"]["failedAction"] == "validate"
    paid = result["officialPaid"]
    assert "3500 字候选正文仍保留在隔离运行区" in paid
    assert "语义审核阶段中断" in paid
    assert "不能当作已验证候选" in paid
    assert "请先做无模型恢复分类" in paid
    assert "请先结束本章失败尝试" not in paid
    assert "没有完整回执" not in paid
    without_run = result["withoutRun"]
    assert "没有完整回执" in without_run
    assert "请先结束本章失败尝试" in without_run
    assert result["noChars"] is None
    assert result["settled"] is None
    assert result["committed"] is None
    assert result["ppr"] is None
    assert result["tooShort"] is None



def test_retained_paid_draft_inspect_is_view_only_and_does_not_open_confirm() -> None:
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")

    assert "const retainedPaidDraftPreview = (issue) =>" in spa
    assert "const renderRetainedPaidDraftInspect = (issue, mount) =>" in spa
    assert "已付费正文（只读）" in spa
    assert "不能当作已验证候选，也不能确认提交" in spa
    assert "textarea.readOnly = true" in spa
    assert "retained_paid_draft_preview: recovery.retained_paid_draft_preview || null" in spa
    assert "renderRetainedPaidDraftInspect(issue, one(\"[data-pending-detail]\"))" in spa or 'renderRetainedPaidDraftInspect(issue, one("[data-pending-detail]"))' in spa
    # Confirm-submit remains on the existing recovery-draft path only.
    assert 'form.matches("[data-draft-validate-form], [data-draft-confirm-form]")' in spa
    assert "preview.confirm_submit_allowed === true" in spa
    assert "preview.verified_candidate === true" in spa
    # Inspect copy must not tell the author to confirm-submit the unsettled draft.
    inspect_start = spa.find("const renderRetainedPaidDraftInspect")
    inspect_end = spa.find("  const api = async (path, options = {}) => {", inspect_start)
    inspect = spa[inspect_start:inspect_end]
    assert "确认提交" in inspect
    assert "data-draft-confirm-form" not in inspect
    assert "data-draft-validate-form" not in inspect
    assert "force_reset" not in inspect


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_retained_paid_draft_preview_helper_fails_closed_for_verified_or_short_drafts() -> None:
    spa_path = STATIC / "workbench-spa.js"
    script = r"""
const fs = require("fs");
const vm = require("vm");
let source = fs.readFileSync(process.argv[1], "utf8");
const marker = "  const api = async (path, options = {}) => {";
if (!source.includes(marker)) throw new Error("inspect export marker is missing");
source = source.replace(
  marker,
  "  globalThis.__evidence = { retainedPaidDraftPreview };\n  return;\n" + marker
);
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
const text = "候选正文".repeat(900).slice(0, 3500);
const base = {
  text,
  chars: 3500,
  failed_action: "validate",
  unsettled_attempt_count: 1,
  committed: false,
  verified_candidate: false,
  confirm_submit_allowed: false
};
const ev = sandbox.__evidence;
const output = {
  officialShaped: ev.retainedPaidDraftPreview({ retained_paid_draft_preview: base }),
  tooShort: ev.retainedPaidDraftPreview({ retained_paid_draft_preview: { ...base, text: text.slice(0, 499), chars: 499 } }),
  verified: ev.retainedPaidDraftPreview({ retained_paid_draft_preview: { ...base, verified_candidate: true } }),
  confirmable: ev.retainedPaidDraftPreview({ retained_paid_draft_preview: { ...base, confirm_submit_allowed: true } }),
  committed: ev.retainedPaidDraftPreview({ retained_paid_draft_preview: { ...base, committed: true } }),
  missing: ev.retainedPaidDraftPreview({ latest_run: { chapter_chars: 3500 } })
};
process.stdout.write(JSON.stringify({
  officialChars: output.officialShaped && output.officialShaped.chars,
  officialTextLen: output.officialShaped && output.officialShaped.text.length,
  tooShort: output.tooShort,
  verified: output.verified,
  confirmable: output.confirmable,
  committed: output.committed,
  missing: output.missing
}));
"""
    completed = subprocess.run(
        [shutil.which("node") or "node", "-e", script, str(spa_path)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout)
    assert result["officialChars"] == 3500
    assert result["officialTextLen"] == 3500
    assert result["tooShort"] is None
    assert result["verified"] is None
    assert result["confirmable"] is None
    assert result["committed"] is None
    assert result["missing"] is None


def test_archived_retained_author_draft_is_not_shown_as_current_work() -> None:
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")
    assert "chapter.retained_author_draft.state !== \"archived\"" in spa
    assert "data-retained-author-draft-panel" in spa
