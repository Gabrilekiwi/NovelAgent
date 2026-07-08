from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from core.rules.narrative_rules import (
    load_default_narrative_rule_pack,
    load_narrative_rule_pack,
    validate_narrative_rule_pack,
)
from core.state.input_pack import build_input_pack


SEVERITY_RANK = {
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}


class RuleAwareInputPackError(ValueError):
    pass


def render_generation_rules_for_input_pack(
    rule_pack: dict,
    *,
    min_severity: str | None = None,
    categories: list[str] | None = None,
    include_disabled: bool = False,
    applies_to: str = "generation",
    max_rules: int | None = None,
) -> str:
    rules = _select_rules(
        rule_pack,
        min_severity=min_severity,
        categories=categories,
        include_disabled=include_disabled,
        applies_to=applies_to,
        max_rules=max_rules,
    )
    if not rules:
        return ""

    lines = [
        "以下规则优先级高于一般写作倾向，生成章节时必须遵守。",
    ]
    lines.append("")
    lines.append("规则：")
    for rule in rules:
        lines.append(f"- [{rule['severity']}] {rule['title']} (`{rule['code']}`): {rule['instruction']}")
    return "\n".join(lines)


def build_rule_aware_input_pack(
    snapshot: dict,
    *,
    rule_pack: dict | None = None,
    rule_pack_path: str | Path | None = None,
    use_default_rules: bool = False,
    min_severity: str | None = "high",
    categories: list[str] | None = None,
    max_rules: int | None = None,
) -> str:
    selected_pack = _resolve_rule_pack(
        rule_pack=rule_pack,
        rule_pack_path=rule_pack_path,
        use_default_rules=use_default_rules,
    )
    if selected_pack is None:
        return build_input_pack(snapshot)

    rules = render_generation_rules_for_input_pack(
        selected_pack,
        min_severity=min_severity,
        categories=categories,
        applies_to="generation",
        max_rules=max_rules,
    )
    return build_input_pack(copy.deepcopy(snapshot), narrative_rules=rules)


def count_generation_rules_for_input_pack(
    rule_pack: dict,
    *,
    min_severity: str | None = "high",
    categories: list[str] | None = None,
    include_disabled: bool = False,
    applies_to: str = "generation",
    max_rules: int | None = None,
) -> int:
    return len(
        _select_rules(
            rule_pack,
            min_severity=min_severity,
            categories=categories,
            include_disabled=include_disabled,
            applies_to=applies_to,
            max_rules=max_rules,
        )
    )


def _resolve_rule_pack(
    *,
    rule_pack: dict | None,
    rule_pack_path: str | Path | None,
    use_default_rules: bool,
) -> dict | None:
    if rule_pack is not None:
        return validate_narrative_rule_pack(rule_pack)
    if rule_pack_path is not None:
        return load_narrative_rule_pack(rule_pack_path)
    if use_default_rules:
        return load_default_narrative_rule_pack()
    return None


def _select_rules(
    rule_pack: dict,
    *,
    min_severity: str | None,
    categories: list[str] | None,
    include_disabled: bool,
    applies_to: str,
    max_rules: int | None,
) -> list[dict[str, Any]]:
    validated = validate_narrative_rule_pack(rule_pack)
    min_rank = _severity_rank(min_severity) if min_severity else None
    allowed_categories = {str(category) for category in categories} if categories else None
    selected: list[tuple[int, dict[str, Any]]] = []

    for index, rule in enumerate(validated["rules"]):
        if not include_disabled and rule.get("enabled") is not True:
            continue
        if applies_to and applies_to not in (rule.get("applies_to") or []):
            continue
        if allowed_categories is not None and rule.get("category") not in allowed_categories:
            continue
        if min_rank is not None and _severity_rank(str(rule.get("severity"))) < min_rank:
            continue
        selected.append((index, rule))

    if max_rules is not None:
        if max_rules < 0:
            raise RuleAwareInputPackError("max_rules must be >= 0")
        selected = sorted(selected, key=lambda item: (-_severity_rank(str(item[1].get("severity"))), item[0]))[:max_rules]
        selected = sorted(selected, key=lambda item: item[0])

    return [rule for _, rule in selected]


def _severity_rank(severity: str | None) -> int:
    if severity not in SEVERITY_RANK:
        raise RuleAwareInputPackError(f"unsupported severity: {severity}")
    return SEVERITY_RANK[severity]


__all__ = [
    "RuleAwareInputPackError",
    "SEVERITY_RANK",
    "build_rule_aware_input_pack",
    "count_generation_rules_for_input_pack",
    "render_generation_rules_for_input_pack",
]
