from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any

from core.context_budget import ContextBudget, ContextBudgetError, default_context_budget
from core.schema import validate_schema


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "report": dict(self.report),
            "selected_sections": list(self.selected_sections),
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
    sections = _markdown_sections(input_pack)
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
    )
    scene = _compile_stage(
        sections,
        stage="scene",
        required=mandatory,
        preferred=set(SCENE_SECTIONS),
        digest=digest,
        budget=effective_budget,
        exact_counter=exact_counter,
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
) -> CompiledPromptContext:
    available = {name for name, _ in sections}
    required_available = required & available
    required_text = _render_sections(
        [(name, text) for name, text in sections if name in required_available],
        digest=digest,
    )
    required_report = budget.measure(required_text, stage=stage, exact_counter=exact_counter)
    if not required_report["within_budget"]:
        raise ContextBudgetError(
            "story_project_context_budget_exceeded",
            f"mandatory {stage} context requires {required_report['budgeted_input_tokens']} tokens",
        )
    chosen = [(name, text) for name, text in sections if name in preferred or name in required_available]
    while True:
        rendered = _render_sections(chosen, digest=digest)
        report = budget.measure(rendered, stage=stage, exact_counter=exact_counter)
        if report["within_budget"]:
            return CompiledPromptContext(
                text=rendered,
                report=report,
                selected_sections=tuple(name for name, _ in chosen),
            )
        optional_indexes = [index for index, (name, _) in enumerate(chosen) if name not in required_available]
        if not optional_indexes:
            raise ContextBudgetError(
                "story_project_context_budget_exceeded",
                f"mandatory {stage} context exceeds hard input limit",
            )
        largest = max(optional_indexes, key=lambda index: len(chosen[index][1].encode("utf-8")))
        chosen.pop(largest)


def _markdown_sections(text: str) -> list[tuple[str, str]]:
    matches = list(re.finditer(r"(?m)^# ([^\r\n]+)\r?$", text))
    if not matches:
        return []
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.append((match.group(1).strip(), text[match.start() : end].rstrip()))
    return sections


def _render_sections(sections: list[tuple[str, str]], *, digest: str) -> str:
    body = "\n\n".join(text for _, text in sections).strip()
    return f"# Context Digest\n{digest}\n\n{body}" if body else f"# Context Digest\n{digest}"


__all__ = [
    "CompiledPromptContext",
    "MANDATORY_SECTIONS",
    "PROMPT_CONTEXT_SCHEMA_VERSION",
    "PromptContextBundle",
    "compile_prompt_contexts",
]
