from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "workbench" / "static"


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


def test_quality_revision_pause_uses_parent_resume_without_recovery_classification() -> None:
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert 'job?.state === "awaiting_input" && reviewStatusOf(job) === "needs_revision"' in spa
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
    assert "const qualityRevision = paused && !provenanceFailure && !contextBudget && !repairConvergence && !repairAuthority && (Boolean(qualityRejection) || isQualityRevisionPause(session));" in spa
    assert "const qualityRevision = !provenanceFailure && !contextBudget && !repairConvergence && (Boolean(qualityRejection) || issue.quality_revision === true || isQualityRevisionPause(issue));" in spa
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
    assert "const qualityRevision = paused && !provenanceFailure && !contextBudget && !repairConvergence && !repairAuthority && (Boolean(qualityRejection) || isQualityRevisionPause(session));" in spa
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
    assert "payload.chapter_recovery || payload.chapter_20_recovery" in spa
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
