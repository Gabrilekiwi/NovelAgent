from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from core.director.director import validate_decision
from core.engine.chapter_candidate_context import (
    CHAPTER_CANDIDATE_CONTEXT_VERSION,
    ChapterCandidateContextBuilder,
    ChapterCandidateContextError,
)
from core.memory_v2.canonical import canonical_json_hash
from core.project_profile import project_language
from core.schema import SchemaValidationError, validate_schema
from workbench.projects import ProjectRecord, ProjectRegistry


CHAPTER_INTENT_CARD_SCHEMA_VERSION = "1.0"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VALIDATION_FOCUS = ("continuity", "spatial", "logic")


class ChapterCardError(ValueError):
    code = "chapter_card_invalid"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.code = code or self.code
        super().__init__(f"{self.code}: {message}")


class ChapterCardConflict(ChapterCardError):
    code = "chapter_card_changed"


@dataclass(frozen=True)
class ChapterIntentCard:
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.payload)


class ChapterCardService:
    """Build and confirm a compact, zero-write view of trusted chapter inputs."""

    def __init__(self, projects: ProjectRegistry) -> None:
        self.projects = projects

    def get(self, project_id: str, chapter_index: int) -> ChapterIntentCard:
        chapter_index = _positive_chapter_index(chapter_index)
        project = self.projects.get(project_id)
        try:
            trusted = ChapterCandidateContextBuilder(project.root).build(chapter_index)
        except ChapterCandidateContextError as exc:
            if exc.code == "chapter_candidate_context_not_strict":
                message = "项目的可信状态尚未激活；请先在健康检查中完成校准并激活，再确认章节卡。"
            elif "authority" in exc.code:
                message = "事件权威状态无法安全读取；请先在健康检查中恢复或重新验证权威状态。"
            else:
                message = "无法构建可信章节输入；请先检查项目健康状态和本章章纲。"
            raise ChapterCardError(
                message,
                code="chapter_card_context_not_ready",
            ) from exc
        except ValueError as exc:
            raise ChapterCardError(
                "无法构建可信章节输入；请先检查项目健康状态和本章章纲。",
                code="chapter_card_context_not_ready",
            ) from exc
        context = trusted.story_project_context
        blueprint = context.get("chapter_blueprint")
        read_set = context.get("read_set")
        identity = context.get("project_identity")
        if not isinstance(blueprint, Mapping):
            raise ChapterCardError("trusted context has no chapter blueprint")
        if not isinstance(read_set, Mapping):
            raise ChapterCardError("trusted context has no source read set")
        if not isinstance(identity, Mapping):
            raise ChapterCardError("trusted context has no persisted project identity")

        compact_blueprint = _blueprint_payload(blueprint, chapter_index)
        language = _language_payload(context, project)
        context_summary = _context_summary(context, compact_blueprint, chapter_index)
        binding = _binding_payload(
            trusted_context_sha256=trusted.context_sha256,
            authority_projection_sha256=trusted.authority_projection_sha256,
            context=context,
            read_set=read_set,
            identity=identity,
            compact_blueprint=compact_blueprint,
        )
        intent_defaults = {
            "goal": compact_blueprint["core_event"] or "",
            "validation_focus": list(_VALIDATION_FOCUS),
            "notes": [],
        }
        issues = _readiness_issues(
            compact_blueprint=compact_blueprint,
            language=language,
            context_summary=context_summary,
        )
        blocking = any(issue["blocking"] for issue in issues)
        confirmation_basis = {
            "schema_version": CHAPTER_INTENT_CARD_SCHEMA_VERSION,
            "book_id": str(identity.get("book_id") or ""),
            "chapter_index": chapter_index,
            "blueprint": compact_blueprint,
            "intent_defaults": intent_defaults,
            "language": language,
            "context_summary": context_summary,
            "binding": binding,
        }
        card = {
            "schema_version": CHAPTER_INTENT_CARD_SCHEMA_VERSION,
            "record_type": "chapter_intent_card",
            "project": {"id": project.id, "title": project.name},
            "book_id": str(identity.get("book_id") or ""),
            "chapter_index": chapter_index,
            "readiness": {
                "status": "needs_input" if blocking else "ready",
                "can_generate": not blocking,
                "issues": issues,
            },
            "blueprint": compact_blueprint,
            "intent_defaults": intent_defaults,
            "language": language,
            "context_summary": context_summary,
            "binding": binding,
            "confirmation": {
                "required": True,
                "card_sha256": canonical_json_hash(
                    confirmation_basis,
                    exclude_environment_fields=False,
                ),
            },
        }
        _validate_card(card)
        return ChapterIntentCard(card)

    def confirm(
        self,
        project_id: str,
        chapter_index: int,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Validate an author's acknowledgement without persisting or generating."""

        chapter_index = _positive_chapter_index(chapter_index)
        if not isinstance(request, Mapping):
            raise ChapterCardError(
                "confirmation request must be an object",
                code="chapter_card_confirmation_invalid",
            )
        allowed = {
            "confirmed",
            "expected_card_sha256",
            "goal",
            "validation_focus",
            "notes_text",
        }
        unknown = set(request) - allowed
        if unknown:
            raise ChapterCardError(
                f"confirmation request contains unsupported fields: {sorted(unknown)}",
                code="chapter_card_confirmation_invalid",
            )
        if request.get("confirmed") is not True:
            raise ChapterCardError(
                "confirmed must be true",
                code="chapter_card_confirmation_required",
            )
        expected = request.get("expected_card_sha256")
        if not isinstance(expected, str) or not _SHA256.fullmatch(expected):
            raise ChapterCardError(
                "expected_card_sha256 must be a lowercase SHA-256 digest",
                code="chapter_card_confirmation_invalid",
            )

        card = self.get(project_id, chapter_index).to_dict()
        current = card["confirmation"]["card_sha256"]
        if expected != current:
            raise ChapterCardConflict(
                "可信章节输入已发生变化；请重新载入并核对最新章节卡。"
            )
        if not card["readiness"]["can_generate"]:
            raise ChapterCardError(
                "chapter card has blocking input issues",
                code="chapter_card_not_ready",
            )

        chapter_intent = _normalize_chapter_intent(request, chapter_index)
        payload = {
            "schema_version": CHAPTER_INTENT_CARD_SCHEMA_VERSION,
            "record_type": "chapter_intent_confirmation",
            "book_id": card["book_id"],
            "chapter_index": chapter_index,
            "confirmed": True,
            "card_sha256": current,
            "context_sha256": card["binding"]["context_sha256"],
            "chapter_intent": chapter_intent,
        }
        payload["confirmation_sha256"] = canonical_json_hash(
            payload,
            exclude_environment_fields=False,
        )
        try:
            validate_schema(payload, "chapter_intent_confirmation.schema.json")
        except SchemaValidationError as exc:
            raise ChapterCardError(
                f"confirmation contract validation failed: {exc}",
                code="chapter_card_confirmation_invalid",
            ) from exc
        return payload

    def verify_confirmation(
        self,
        project_id: str,
        chapter_index: int,
        confirmation: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Revalidate a submitted confirmation against one current card snapshot.

        The executor independently checks ``context_sha256`` while preparing its
        trusted input.  This method additionally proves that the author-facing
        card hash is still current before Workbench starts that execution.
        """

        chapter_index = _positive_chapter_index(chapter_index)
        if not isinstance(confirmation, Mapping):
            raise ChapterCardError(
                "confirmation must be an object",
                code="chapter_card_confirmation_invalid",
            )
        payload = copy.deepcopy(dict(confirmation))
        try:
            validate_schema(payload, "chapter_intent_confirmation.schema.json")
        except SchemaValidationError as exc:
            raise ChapterCardError(
                f"confirmation contract validation failed: {exc}",
                code="chapter_card_confirmation_invalid",
            ) from exc
        expected_hash = canonical_json_hash(
            payload,
            exclude_fields=("confirmation_sha256",),
            exclude_environment_fields=False,
        )
        if payload["confirmation_sha256"] != expected_hash:
            raise ChapterCardError(
                "confirmation hash does not match its content",
                code="chapter_card_confirmation_invalid",
            )
        try:
            normalized_intent = _canonical_confirmation_intent(
                payload["chapter_intent"],
                payload["chapter_index"],
            )
        except (SchemaValidationError, ValueError, TypeError) as exc:
            raise ChapterCardError(
                f"confirmation chapter intent is invalid: {exc}",
                code="chapter_card_confirmation_invalid",
            ) from exc
        if normalized_intent != payload["chapter_intent"]:
            raise ChapterCardError(
                "confirmation chapter intent is not canonical",
                code="chapter_card_confirmation_invalid",
            )

        card = self.get(project_id, chapter_index).to_dict()
        if (
            payload["book_id"] != card["book_id"]
            or payload["chapter_index"] != chapter_index
            or payload["card_sha256"] != card["confirmation"]["card_sha256"]
            or payload["context_sha256"] != card["binding"]["context_sha256"]
        ):
            raise ChapterCardConflict(
                "可信章节输入已发生变化；请重新载入并核对最新章节卡。"
            )
        if not card["readiness"]["can_generate"]:
            raise ChapterCardError(
                "chapter card has blocking input issues",
                code="chapter_card_not_ready",
            )
        return payload


def _blueprint_payload(
    blueprint: Mapping[str, Any],
    chapter_index: int,
) -> dict[str, Any]:
    beats: list[dict[str, Any]] = []
    raw_beats = blueprint.get("required_beats")
    if isinstance(raw_beats, Sequence) and not isinstance(raw_beats, (str, bytes)):
        for position, raw in enumerate(raw_beats, start=1):
            if isinstance(raw, Mapping):
                index = raw.get("index")
                text = _text(raw.get("text"))
            else:
                index = position
                text = _text(raw)
            if not isinstance(index, int) or isinstance(index, bool) or index < 1:
                index = position
            beats.append({"index": index, "text": text})
    return {
        "title": _text(blueprint.get("title")),
        "core_event": _optional_text(blueprint.get("core_event")),
        "required_beats": beats,
        "ending_pressure": _optional_text(blueprint.get("ending_pressure")),
    }


def _language_payload(
    context: Mapping[str, Any],
    project: ProjectRecord,
) -> dict[str, Any]:
    identity = context.get("project_identity")
    authority = identity.get("authority") if isinstance(identity, Mapping) else None
    authority_mode = (
        _text(authority.get("mode"))
        if isinstance(authority, Mapping)
        else "legacy_markdown_v1"
    ) or "legacy_markdown_v1"
    effective = ""
    source = "legacy_story_project_default"
    if authority_mode == "event_v1":
        memory_v2 = context.get("memory_v2")
        projection = memory_v2.get("projection") if isinstance(memory_v2, Mapping) else None
        if isinstance(projection, Mapping):
            effective = _text(projection.get("language"))
        source = "event_authority"
    if not effective:
        overlay = context.get("snapshot_overlay")
        effective = project_language(dict(overlay) if isinstance(overlay, Mapping) else None)
    preference = _optional_text(project.metadata.get("language"))
    return {
        "effective": effective,
        "source": source,
        "workbench_preference": preference,
        "preference_matches_effective": (
            preference is None or preference.casefold() == effective.casefold()
        ),
    }


def _context_summary(
    context: Mapping[str, Any],
    blueprint: Mapping[str, Any],
    chapter_index: int,
) -> dict[str, Any]:
    raw_blueprint = context.get("chapter_blueprint")
    chapter_read_set = (
        raw_blueprint.get("chapter_context_read_set")
        if isinstance(raw_blueprint, Mapping)
        else None
    )
    explicit = isinstance(chapter_read_set, Mapping) and chapter_read_set.get("mode") == "explicit"
    continuity = chapter_read_set.get("continuity") if explicit else None
    constraints = chapter_read_set.get("narrative_constraints") if explicit else []
    entities = chapter_read_set.get("expected_new_entities") if explicit else []
    state_ids = chapter_read_set.get("required_state_item_ids") if explicit else []
    event_ids = chapter_read_set.get("required_event_item_ids") if explicit else []
    previous = context.get("previous_chapter_context")
    tracking = context.get("tracking_files")
    settings = context.get("setting_files")
    warnings = context.get("warnings")
    return {
        "scope": "explicit" if explicit else "full",
        "previous_chapter": {
            "index": chapter_index - 1 if chapter_index > 1 else None,
            "present": isinstance(previous, Mapping),
        },
        "continuity": _continuity_payload(continuity),
        "narrative_constraints": _constraint_payloads(constraints),
        "expected_new_entities": _entity_payloads(entities),
        "required_state_item_count": _sequence_length(state_ids),
        "required_event_item_count": _sequence_length(event_ids),
        "tracking_file_count": len(tracking) if isinstance(tracking, Mapping) else 0,
        "setting_file_count": len(settings) if isinstance(settings, Mapping) else 0,
        "source_warning_count": _sequence_length(warnings),
    }


def _binding_payload(
    *,
    trusted_context_sha256: str,
    authority_projection_sha256: str | None,
    context: Mapping[str, Any],
    read_set: Mapping[str, Any],
    identity: Mapping[str, Any],
    compact_blueprint: Mapping[str, Any],
) -> dict[str, Any]:
    raw_blueprint = context.get("chapter_blueprint")
    chapter_read_set = (
        raw_blueprint.get("chapter_context_read_set")
        if isinstance(raw_blueprint, Mapping)
        else None
    )
    authority = identity.get("authority")
    authority_mode = (
        _text(authority.get("mode"))
        if isinstance(authority, Mapping)
        else "legacy_markdown_v1"
    ) or "legacy_markdown_v1"
    contract_sha256 = (
        _optional_text(chapter_read_set.get("contract_sha256"))
        if isinstance(chapter_read_set, Mapping)
        else None
    )
    return {
        "context_builder_version": CHAPTER_CANDIDATE_CONTEXT_VERSION,
        "runtime_cache_policy": "bypass",
        "context_sha256": trusted_context_sha256,
        "context_digest": _text(read_set.get("context_digest")),
        "membership_fingerprint": _text(read_set.get("membership_fingerprint")),
        "identity_revision": _text(read_set.get("identity_revision")),
        "authority_mode": authority_mode,
        "authority_projection_sha256": authority_projection_sha256,
        "chapter_context_contract_sha256": contract_sha256,
    }


def _readiness_issues(
    *,
    compact_blueprint: Mapping[str, Any],
    language: Mapping[str, Any],
    context_summary: Mapping[str, Any],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []

    def add(code: str, field: str, message: str, *, blocking: bool) -> None:
        issues.append(
            {
                "code": code,
                "field": field,
                "severity": "error" if blocking else "warning",
                "blocking": blocking,
                "message": message,
            }
        )

    if not compact_blueprint.get("title"):
        add("chapter_title_missing", "blueprint.title", "章节标题为空。", blocking=True)
    if not compact_blueprint.get("core_event"):
        add("chapter_core_event_missing", "blueprint.core_event", "核心事件为空。", blocking=True)
    beats = compact_blueprint.get("required_beats")
    if not isinstance(beats, list) or not beats or any(not beat.get("text") for beat in beats):
        add("chapter_required_beats_missing", "blueprint.required_beats", "至少需要一个非空的必写节拍。", blocking=True)
    if not compact_blueprint.get("ending_pressure"):
        add("chapter_ending_pressure_missing", "blueprint.ending_pressure", "结尾压力为空。", blocking=True)
    if not language.get("effective"):
        add("chapter_language_missing", "language.effective", "作品语言为空。", blocking=True)
    if context_summary.get("scope") == "full":
        add(
            "chapter_context_scope_full",
            "context_summary.scope",
            "本章未声明精简读取集，将使用完整可信上下文；可继续，但输入会更大。",
            blocking=False,
        )
    if language.get("preference_matches_effective") is False:
        add(
            "workbench_language_preference_mismatch",
            "language.workbench_preference",
            "工作台语言偏好与当前实际生成语言不同；本次确认以实际生成语言为准。",
            blocking=False,
        )
    warning_count = context_summary.get("source_warning_count")
    if isinstance(warning_count, int) and not isinstance(warning_count, bool) and warning_count > 0:
        add(
            "story_project_source_warnings",
            "context_summary.source_warning_count",
            f"可信来源解析有 {warning_count} 条警告；可继续确认，但建议先在健康检查中查看并处理。",
            blocking=False,
        )
    return issues


def _normalize_chapter_intent(
    request: Mapping[str, Any],
    chapter_index: int,
) -> dict[str, Any]:
    raw_goal = request.get("goal")
    if not isinstance(raw_goal, str):
        raise ChapterCardError(
            "goal must be text",
            code="chapter_card_confirmation_invalid",
        )
    goal = raw_goal.strip()
    focus = request.get("validation_focus")
    if isinstance(focus, str):
        focus = [focus]
    if not isinstance(focus, Sequence) or isinstance(focus, (str, bytes)):
        focus = []
    if not focus:
        raise ChapterCardError(
            "至少选择一个校验重点。",
            code="chapter_card_confirmation_invalid",
        )
    if any(not isinstance(item, str) for item in focus):
        raise ChapterCardError(
            "validation_focus items must be text",
            code="chapter_card_confirmation_invalid",
        )
    if len(set(focus)) != len(focus):
        raise ChapterCardError(
            "validation_focus must not contain duplicates",
            code="chapter_card_confirmation_invalid",
        )
    notes_text = request.get("notes_text")
    if not isinstance(notes_text, str):
        raise ChapterCardError(
            "notes_text must be text",
            code="chapter_card_confirmation_invalid",
        )
    notes = [line.strip() for line in notes_text.splitlines() if line.strip()]
    try:
        decision = validate_decision(
            {
                "chapter_index": chapter_index,
                "goal": goal,
                "actions": ["generate_chapter", "validate"],
                "validation_focus": list(focus),
                "max_repair_attempts": 0,
                "notes": notes,
            }
        )
    except (TypeError, ValueError) as exc:
        raise ChapterCardError(
            f"chapter intent is invalid: {exc}",
            code="chapter_card_confirmation_invalid",
        ) from exc
    return {
        "goal": decision["goal"],
        "validation_focus": list(decision["validation_focus"]),
        "notes": list(decision["notes"]),
    }


def _canonical_confirmation_intent(
    value: Mapping[str, Any],
    chapter_index: int,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("chapter_intent must be an object")
    decision = validate_decision(
        {
            "chapter_index": chapter_index,
            "goal": value.get("goal"),
            "actions": ["generate_chapter", "validate"],
            "validation_focus": value.get("validation_focus"),
            "max_repair_attempts": 0,
            "notes": value.get("notes"),
        }
    )
    return {
        "goal": decision["goal"],
        "validation_focus": list(decision["validation_focus"]),
        "notes": list(decision["notes"]),
    }


def _continuity_payload(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    characters = value.get("last_scene_character_ids")
    return {
        "last_scene_location": _text(value.get("last_scene_location")),
        "last_scene_character_ids": [
            _text(item)
            for item in characters
            if _text(item)
        ] if isinstance(characters, Sequence) and not isinstance(characters, (str, bytes)) else [],
        "required_opening_bridge": _text(value.get("required_opening_bridge")),
    }


def _constraint_payloads(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        result.append(
            {
                "constraint_id": _text(item.get("constraint_id")),
                "lifecycle_action": _text(item.get("lifecycle_action")),
                "instruction": _text(item.get("instruction")),
            }
        )
    return result


def _entity_payloads(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        result.append(
            {
                "kind": _text(item.get("kind")),
                "entity_id": _text(item.get("entity_id")),
                "display_name": _text(item.get("display_name")),
            }
        )
    return result


def _sequence_length(value: Any) -> int:
    return len(value) if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else 0


def _validate_card(card: dict[str, Any]) -> None:
    try:
        validate_schema(card, "chapter_intent_card.schema.json")
    except SchemaValidationError as exc:
        raise ChapterCardError(f"chapter card contract validation failed: {exc}") from exc


def _positive_chapter_index(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ChapterCardError("chapter_index must be a positive integer")
    return value


def _text(value: Any) -> str:
    return str(value or "").strip()


def _optional_text(value: Any) -> str | None:
    text = _text(value)
    return text or None


__all__ = [
    "CHAPTER_INTENT_CARD_SCHEMA_VERSION",
    "ChapterCardConflict",
    "ChapterCardError",
    "ChapterCardService",
    "ChapterIntentCard",
]
