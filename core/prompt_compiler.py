from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any

from core.context_budget import ContextBudget, ContextBudgetError, default_context_budget
from core.schema import validate_schema
from core.structured_context import (
    StructuredContextError,
    compact_markdown_section,
    rank_texts,
    sha256_text,
)


PROMPT_CONTEXT_SCHEMA_VERSION = "1.0"
MANDATORY_SECTIONS = frozenset(
    {
        "Project Profile",
        "Director Decision",
        "Story State",
        "Spatial State",
        "StoryProject Chapter Blueprint",
        "Requirements",
        "小说生成规则契约",
    }
)
SCENE_SECTIONS = MANDATORY_SECTIONS | {"Memory Index"}
REPAIR_SECTIONS = frozenset(
    {"Project Profile", "Story State", "Spatial State", "StoryProject Chapter Blueprint", "Requirements", "小说生成规则契约"}
)


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
) -> PromptContextBundle:
    effective_budget = budget or default_context_budget()
    digest = hashlib.sha256(input_pack.encode("utf-8")).hexdigest()
    raw_sections = _markdown_sections(input_pack)
    query = "\n\n".join(
        section_text
        for name, section_text in raw_sections
        if name in {"Director Decision", "Story State", "StoryProject Chapter Blueprint", "Requirements"}
    )
    sections = [
        (name, _compact_oversized_section(name, text, query=query))
        for name, text in raw_sections
    ]
    if not sections:
        sections = [("Context", input_pack)]
        mandatory = {"Context"}
    else:
        mandatory = set(MANDATORY_SECTIONS)
    plan = _compile_stage(
        sections,
        stage="plan",
        required=mandatory,
        preferred={name for name, _ in sections},
        digest=digest,
        budget=effective_budget,
        exact_counter=exact_counter,
        original_chars=len(input_pack),
        query=query,
    )
    scene = _compile_stage(
        sections,
        stage="scene",
        required=mandatory,
        preferred=set(SCENE_SECTIONS),
        digest=digest,
        budget=effective_budget,
        exact_counter=exact_counter,
        original_chars=len(input_pack),
        query=query,
    )
    repair_required = mandatory if "Context" in mandatory else mandatory & set(REPAIR_SECTIONS)
    repair_preferred = {"Context"} if "Context" in mandatory else set(REPAIR_SECTIONS)
    repair = _compile_stage(
        sections,
        stage="repair",
        required=repair_required,
        preferred=repair_preferred,
        digest=digest,
        budget=effective_budget,
        exact_counter=exact_counter,
        original_chars=len(input_pack),
        query=query,
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
) -> CompiledPromptContext:
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
    required_report = budget.measure(required_text, stage=stage, exact_counter=exact_counter)
    if not required_report["within_budget"]:
        raise ContextBudgetError(
            "story_project_context_budget_exceeded",
            f"mandatory {stage} context requires {required_report['budgeted_input_tokens']} tokens",
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
        report = budget.measure(rendered, stage=stage, exact_counter=exact_counter)
        if report["within_budget"]:
            chosen_indexes = candidate_indexes

    chosen = [item for index, item in enumerate(sections) if index in chosen_indexes]
    rendered, selection_manifest = _render_sections(
        chosen,
        all_sections=sections,
        digest=digest,
        original_chars=original_chars,
        stage=stage,
    )
    report = budget.measure(rendered, stage=stage, exact_counter=exact_counter)
    if not report["within_budget"]:
        raise ContextBudgetError(
            "story_project_context_budget_exceeded",
            f"mandatory {stage} context exceeds hard input limit",
        )
    return CompiledPromptContext(
        text=rendered,
        report=report,
        selected_sections=tuple(name for name, _ in chosen),
        selection_manifest=selection_manifest,
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


def _compact_oversized_section(name: str, text: str, *, query: str = "") -> str:
    """Bound cumulative writeback by selecting complete JSON/paragraph entries."""
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
    "PROMPT_CONTEXT_SCHEMA_VERSION",
    "PromptContextBundle",
    "compile_prompt_contexts",
]
