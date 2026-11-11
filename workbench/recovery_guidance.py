from __future__ import annotations

from typing import Any, Mapping

DURABLE_POLISHED_DRAFT_REASON = "durable_polished_draft_available"

ATTENTION_VALIDATE_DRAFT = "已有完整润色稿，请验证后继续"
ATTENTION_CONFIRM_DRAFT = "润色稿已验证，请确认提交"
ATTENTION_AWAITING_INPUT = "需要作者检查并确认"
ATTENTION_NEEDS_RECOVERY = "Provider 状态待安全恢复"
ATTENTION_GENERIC = "有待处理事项"

GUIDANCE_VALIDATE_DRAFT = (
    "已有完整润色稿，不确定调用已写入恢复记录。请先验证或确认已有草稿，不要结束失败尝试或新开一章。"
)
GUIDANCE_CONFIRM_DRAFT = (
    "已有完整润色稿并通过验证。请确认提交已有草稿继续本章，不要结束失败尝试或新开一章。"
)


def durable_polished_draft_available(recovery: Mapping[str, Any] | None) -> bool:
    """True when a classified, complete polished draft is the current recovery product."""

    if not isinstance(recovery, Mapping):
        return False
    if recovery.get("formal_prose_exists") is True:
        return False
    if recovery.get("uncertain_without_run") is True:
        return False
    checkpoint = recovery.get("checkpoint")
    if not isinstance(checkpoint, Mapping):
        return False
    if checkpoint.get("action") == "reset":
        return False
    return (
        checkpoint.get("has_complete_draft") is True
        and checkpoint.get("reason") == DURABLE_POLISHED_DRAFT_REASON
    )


def polished_draft_guidance(*, validated: bool = False) -> str:
    return GUIDANCE_CONFIRM_DRAFT if validated else GUIDANCE_VALIDATE_DRAFT


def attention_label_for_project(
    state: str | None,
    *,
    recovery: Mapping[str, Any] | None = None,
    draft_state: str | None = None,
) -> str | None:
    if durable_polished_draft_available(recovery):
        if draft_state == "validated":
            return ATTENTION_CONFIRM_DRAFT
        return ATTENTION_VALIDATE_DRAFT
    if not state:
        return None
    return {
        "awaiting_input": ATTENTION_AWAITING_INPUT,
        "needs_recovery": ATTENTION_NEEDS_RECOVERY,
    }.get(str(state), ATTENTION_GENERIC)
