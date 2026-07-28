from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Iterable, Mapping

from core.context_budget import ContextBudget, ContextBudgetError, default_context_budget
from core.schema import validate_schema
from core.state.authoritative_context import (
    AUTHORITATIVE_PLAN_SECTION_MAX_CHARS,
    AUTHORITATIVE_REPAIR_SECTION_MAX_CHARS,
    AUTHORITATIVE_SCENE_SECTION_MAX_CHARS,
    compact_authoritative_state_section,
)
from core.structured_context import (
    StructuredContextError,
    compact_markdown_section,
    rank_texts,
    sha256_text,
)


PROMPT_CONTEXT_SCHEMA_VERSION = "1.0"
PROMPT_FINAL_REQUEST_HEADROOM_TOKENS = 512
PROMPT_CONTEXT_SELECTION_KEYS = frozenset(
    {
        "schema_version",
        "policy",
        "source_sha256",
        "original_chars",
        "omitted_count",
    }
)
MANDATORY_SECTIONS = frozenset(
    {
        "Project Profile",
        "Director Decision",
        "Story State",
        "Spatial State",
        "Authoritative State",
        "StoryProject Chapter Blueprint",
        "Requirements",
        "小说生成规则契约",
    }
)
SCENE_SECTIONS = MANDATORY_SECTIONS | {"Memory Index"}
REPAIR_SECTIONS = frozenset(
    {"Project Profile", "Story State", "Spatial State", "StoryProject Chapter Blueprint", "Requirements", "小说生成规则契约"}
)
REPAIR_SECTIONS = REPAIR_SECTIONS | {"Authoritative State"}


@dataclass(frozen=True)
class CompiledPromptContext:
    text: str
    report: dict[str, Any]
    selected_sections: tuple[str, ...]
    selection_manifest: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "report": dict(self.report),
            "selected_sections": list(self.selected_sections),
            "selection_manifest": dict(self.selection_manifest),
        }


@dataclass(frozen=True)
class PromptContextBundle:
    context_digest: str
    plan: CompiledPromptContext
    scene: CompiledPromptContext
    repair: CompiledPromptContext

    def to_dict(self) -> dict[str, Any]:
        return validate_schema(
            {
                "schema_version": PROMPT_CONTEXT_SCHEMA_VERSION,
                "context_digest": self.context_digest,
                "plan": self.plan.to_dict(),
                "scene": self.scene.to_dict(),
                "repair": self.repair.to_dict(),
            },
            "prompt_context_bundle.schema.json",
        )


def compile_prompt_contexts(
    input_pack: str,
    *,
    budget: ContextBudget | None = None,
    exact_counter=None,
    query_hint: str = "",
    stage_protocol_texts: Mapping[str, Iterable[str]] | None = None,
) -> PromptContextBundle:
    effective_budget = budget or default_context_budget()
    protocol_texts = {
        str(stage): tuple(str(item) for item in items)
        for stage, items in (stage_protocol_texts or {}).items()
    }
    digest = hashlib.sha256(input_pack.encode("utf-8")).hexdigest()
    raw_sections = _markdown_sections(input_pack)
    query = "\n\n".join(
        [
            *[
                section_text
                for name, section_text in raw_sections
                if name
                in {
                    "Director Decision",
                    "Story State",
                    "StoryProject Chapter Blueprint",
                    "Requirements",
                }
            ],
            *([str(query_hint)] if str(query_hint).strip() else []),
        ]
    )
    if not raw_sections:
        plan_sections = [("Context", input_pack)]
        scene_sections = list(plan_sections)
        repair_sections = list(plan_sections)
        mandatory = {"Context"}
    else:
        plan_sections = _compact_sections_for_stage(
            raw_sections,
            query=query,
            authoritative_max_chars=AUTHORITATIVE_PLAN_SECTION_MAX_CHARS,
            require_query_references=True,
            require_open_events=True,
        )
        scene_sections = _compact_sections_for_stage(
            raw_sections,
            query=query,
            authoritative_max_chars=AUTHORITATIVE_SCENE_SECTION_MAX_CHARS,
            require_query_references=False,
            require_open_events=False,
        )
        repair_sections = _compact_sections_for_stage(
            raw_sections,
            query=query,
            authoritative_max_chars=AUTHORITATIVE_REPAIR_SECTION_MAX_CHARS,
            # The model repair path immediately replaces this baseline with a
            # query-specific projection from the raw authority source.
            require_query_references=False,
            require_open_events=False,
        )
        mandatory = set(MANDATORY_SECTIONS)
    plan = _compile_stage_with_dynamic_authority(
        source_sections=raw_sections,
        initial_sections=plan_sections,
        authoritative_max_chars=AUTHORITATIVE_PLAN_SECTION_MAX_CHARS,
        require_query_references=True,
        require_open_events=True,
        stage="plan",
        required=mandatory,
        preferred={name for name, _ in plan_sections},
        digest=digest,
        budget=effective_budget,
        exact_counter=exact_counter,
        original_chars=len(input_pack),
        query=query,
        protocol_texts=protocol_texts.get("plan", ()),
    )
    scene = _compile_stage(
        scene_sections,
        stage="scene",
        required=mandatory,
        preferred=set(SCENE_SECTIONS),
        digest=digest,
        budget=effective_budget,
        exact_counter=exact_counter,
        original_chars=len(input_pack),
        query=query,
        protocol_texts=protocol_texts.get("scene", ()),
        budgeted_input_limit=_stage_input_limit(
            effective_budget,
            protocol_texts.get("scene", ()),
        ),
    )
    repair_required = mandatory if "Context" in mandatory else mandatory & set(REPAIR_SECTIONS)
    repair_preferred = {"Context"} if "Context" in mandatory else set(REPAIR_SECTIONS)
    repair = _compile_stage(
        repair_sections,
        stage="repair",
        required=repair_required,
        preferred=repair_preferred,
        digest=digest,
        budget=effective_budget,
        exact_counter=exact_counter,
        original_chars=len(input_pack),
        query=query,
        protocol_texts=protocol_texts.get("repair", ()),
        budgeted_input_limit=_stage_input_limit(
            effective_budget,
            protocol_texts.get("repair", ()),
        ),
    )
    return PromptContextBundle(context_digest=digest, plan=plan, scene=scene, repair=repair)


def _compile_stage(
    sections: list[tuple[str, str]],
    *,
    stage: str,
    required: set[str],
    preferred: set[str],
    digest: str,
    budget: ContextBudget,
    exact_counter,
    original_chars: int,
    query: str,
    protocol_texts: tuple[str, ...] = (),
    budgeted_input_limit: int | None = None,
) -> CompiledPromptContext:
    effective_input_limit = min(
        budget.hard_input_limit,
        (
            budgeted_input_limit
            if budgeted_input_limit is not None
            else budget.hard_input_limit
        ),
    )
    available = {name for name, _ in sections}
    required_available = required & available
    required_indexes = {
        index for index, (name, _text) in enumerate(sections) if name in required_available
    }
    required_chosen = [
        item for index, item in enumerate(sections) if index in required_indexes
    ]
    required_text, _required_manifest = _render_sections(
        required_chosen,
        all_sections=sections,
        digest=digest,
        original_chars=original_chars,
        stage=stage,
    )
    required_report = budget.measure(
        required_text,
        stage=stage,
        exact_counter=exact_counter,
        protocol_texts=protocol_texts,
    )
    if (
        not required_report["within_budget"]
        or required_report["budgeted_input_tokens"] > effective_input_limit
    ):
        raise ContextBudgetError(
            "story_project_context_budget_exceeded",
            f"mandatory {stage} context requires "
            f"{required_report['budgeted_input_tokens']} tokens; "
            f"request-aware limit is {effective_input_limit}",
        )
    chosen_indexes = set(required_indexes)
    optional_indexes = [
        index
        for index, (name, _text) in enumerate(sections)
        if index not in required_indexes and name in preferred
    ]
    ranked_optional = rank_texts(
        [sections[index][1] for index in optional_indexes],
        query=query,
        prefer_recent=True,
    )
    for ranked_index in ranked_optional:
        candidate_index = optional_indexes[ranked_index]
        candidate_indexes = chosen_indexes | {candidate_index}
        candidate = [
            item for index, item in enumerate(sections) if index in candidate_indexes
        ]
        rendered, _selection = _render_sections(
            candidate,
            all_sections=sections,
            digest=digest,
            original_chars=original_chars,
            stage=stage,
        )
        report = budget.measure(
            rendered,
            stage=stage,
            exact_counter=exact_counter,
            protocol_texts=protocol_texts,
        )
        if (
            report["within_budget"]
            and report["budgeted_input_tokens"] <= effective_input_limit
        ):
            chosen_indexes = candidate_indexes

    chosen = [item for index, item in enumerate(sections) if index in chosen_indexes]
    rendered, selection_manifest = _render_sections(
        chosen,
        all_sections=sections,
        digest=digest,
        original_chars=original_chars,
        stage=stage,
    )
    report = budget.measure(
        rendered,
        stage=stage,
        exact_counter=exact_counter,
        protocol_texts=protocol_texts,
    )
    if (
        not report["within_budget"]
        or report["budgeted_input_tokens"] > effective_input_limit
    ):
        raise ContextBudgetError(
            "story_project_context_budget_exceeded",
            f"mandatory {stage} context exceeds request-aware input limit "
            f"{effective_input_limit}",
        )
    return CompiledPromptContext(
        text=rendered,
        report=report,
        selected_sections=tuple(name for name, _ in chosen),
        selection_manifest=selection_manifest,
    )


def _compile_stage_with_dynamic_authority(
    *,
    source_sections: list[tuple[str, str]],
    initial_sections: list[tuple[str, str]],
    authoritative_max_chars: int,
    require_query_references: bool,
    require_open_events: bool,
    stage: str,
    required: set[str],
    preferred: set[str],
    digest: str,
    budget: ContextBudget,
    exact_counter,
    original_chars: int,
    query: str,
    protocol_texts: tuple[str, ...],
) -> CompiledPromptContext:
    input_limit = _stage_input_limit(budget, protocol_texts)

    def compile_sections(
        sections: list[tuple[str, str]],
    ) -> CompiledPromptContext:
        return _compile_stage(
            sections,
            stage=stage,
            required=required,
            preferred=preferred,
            digest=digest,
            budget=budget,
            exact_counter=exact_counter,
            original_chars=original_chars,
            query=query,
            protocol_texts=protocol_texts,
            budgeted_input_limit=input_limit,
        )

    initial_error: ContextBudgetError | None = None
    try:
        return compile_sections(initial_sections)
    except ContextBudgetError as exc:
        initial_error = exc
        if (
            not protocol_texts
            or not any(name == "Authoritative State" for name, _ in source_sections)
            or isinstance(exc.__cause__, StructuredContextError)
        ):
            raise

    low = 1
    high = authoritative_max_chars - 1
    best: CompiledPromptContext | None = None
    while low <= high:
        middle = (low + high) // 2
        try:
            candidate_sections = _compact_sections_for_stage(
                source_sections,
                query=query,
                authoritative_max_chars=middle,
                require_query_references=require_query_references,
                require_open_events=require_open_events,
            )
        except ContextBudgetError as exc:
            if isinstance(exc.__cause__, StructuredContextError):
                low = middle + 1
                continue
            raise
        try:
            candidate = compile_sections(candidate_sections)
        except ContextBudgetError:
            high = middle - 1
            continue
        best = candidate
        low = middle + 1
    if best is not None:
        return best
    assert initial_error is not None
    raise initial_error


def _stage_input_limit(
    budget: ContextBudget,
    protocol_texts: tuple[str, ...],
) -> int:
    if not protocol_texts:
        return budget.hard_input_limit
    return max(
        1,
        budget.hard_input_limit - PROMPT_FINAL_REQUEST_HEADROOM_TOKENS,
    )


def _markdown_sections(text: str) -> list[tuple[str, str]]:
    matches = list(re.finditer(r"(?m)^# ([^\r\n]+)\r?$", text))
    if not matches:
        return []
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.append((match.group(1).strip(), text[match.start() : end].rstrip()))
    return sections


def _render_sections(
    sections: list[tuple[str, str]],
    *,
    all_sections: list[tuple[str, str]],
    digest: str,
    original_chars: int,
    stage: str,
) -> tuple[str, dict[str, Any]]:
    selected = [
        {
            "id": f"section:{name}",
            "name": name,
            "sha256": sha256_text(text),
            "original_chars": len(text),
        }
        for name, text in sections
    ]
    manifest = {
        "schema_version": "1.0",
        "policy": f"prompt_{stage}_section_relevance_v1",
        "source_sha256": digest,
        "original_chars": original_chars,
        "selected_items": selected,
        "omitted_count": max(0, len(all_sections) - len(sections)),
    }
    body = "\n\n".join(text for _, text in sections).strip()
    prefix = (
        f"# Context Digest\n{digest}\n\n"
        "# Prompt Context Selection\n"
        + json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
    )
    return (f"{prefix}\n\n{body}" if body else prefix), manifest


def _compact_sections_for_stage(
    sections: list[tuple[str, str]],
    *,
    query: str,
    authoritative_max_chars: int,
    require_query_references: bool,
    require_open_events: bool,
) -> list[tuple[str, str]]:
    return [
        (
            name,
            _compact_oversized_section(
                name,
                text,
                query=query,
                authoritative_max_chars=authoritative_max_chars,
                require_query_references=require_query_references,
                require_open_events=require_open_events,
            ),
        )
        for name, text in sections
    ]


def _compact_oversized_section(
    name: str,
    text: str,
    *,
    query: str = "",
    authoritative_max_chars: int = AUTHORITATIVE_PLAN_SECTION_MAX_CHARS,
    require_query_references: bool = True,
    require_open_events: bool = True,
) -> str:
    """Bound cumulative writeback by selecting complete JSON/paragraph entries."""
    if name == "Authoritative State" and len(text) > authoritative_max_chars:
        try:
            return compact_authoritative_state_section(
                text,
                max_chars=authoritative_max_chars,
                query=query,
                require_query_references=require_query_references,
                require_open_events=require_open_events,
            )
        except StructuredContextError as exc:
            raise ContextBudgetError(
                "story_project_context_budget_exceeded",
                f"required authoritative records exceed the section budget: {exc}",
            ) from exc
    if name != "StoryProject Chapter Blueprint" or len(text) <= 8_000:
        return text
    try:
        return compact_markdown_section(
            name,
            text,
            max_chars=8_000,
            query=query,
            required_json_keys={"chapter_blueprint", "read_set_context_digest"},
            policy="story_project_blueprint_json_items_v1",
        )
    except StructuredContextError as exc:
        raise ContextBudgetError(
            "story_project_context_budget_exceeded",
            f"required structured entries in {name} exceed the section budget: {exc}",
        ) from exc


__all__ = [
    "CompiledPromptContext",
    "MANDATORY_SECTIONS",
    "PROMPT_CONTEXT_SELECTION_KEYS",
    "PROMPT_CONTEXT_SCHEMA_VERSION",
    "PromptContextBundle",
    "compile_prompt_contexts",
]
