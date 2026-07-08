from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from core.schema import SchemaValidationError, validate_schema


DEFAULT_NARRATIVE_RULE_PACK_PATH = Path("rules/default_narrative_rule_pack.json")

CATEGORY_LABELS = {
    "continuity": "章节连续性",
    "character": "人物一致性",
    "spatial": "空间连续性",
    "conflict": "冲突推进",
    "foreshadowing": "伏笔处理",
    "style": "视角与风格",
    "language": "语言与风格",
    "output_contract": "输出格式",
    "pacing": "节奏与推进",
    "safety": "安全边界",
    "custom": "自定义规则",
}


class NarrativeRulePackError(ValueError):
    pass


def load_narrative_rule_pack(path: str | Path) -> dict[str, Any]:
    rule_path = Path(path)
    try:
        payload = json.loads(rule_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise NarrativeRulePackError(f"rule pack not found: {rule_path}") from exc
    except json.JSONDecodeError as exc:
        raise NarrativeRulePackError(f"invalid rule pack JSON: {exc}") from exc
    return validate_narrative_rule_pack(payload)


def validate_narrative_rule_pack(rule_pack: dict[str, Any]) -> dict[str, Any]:
    try:
        validate_schema(rule_pack, "narrative_rule_pack.schema.json")
    except SchemaValidationError as exc:
        raise NarrativeRulePackError(str(exc)) from exc

    rules = rule_pack.get("rules") if isinstance(rule_pack, dict) else None
    if not isinstance(rules, list):
        raise NarrativeRulePackError("narrative rule pack rules must be a list")

    codes: list[str] = []
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise NarrativeRulePackError(f"rules[{index}] must be an object")
        code = rule.get("code")
        if not isinstance(code, str) or not _is_snake_case(code):
            raise NarrativeRulePackError(f"rules[{index}].code must be stable snake_case")
        codes.append(code)

    duplicates = sorted(code for code, count in Counter(codes).items() if count > 1)
    if duplicates:
        raise NarrativeRulePackError(f"duplicate rule code(s): {', '.join(duplicates)}")

    return rule_pack


def load_default_narrative_rule_pack() -> dict[str, Any]:
    return load_narrative_rule_pack(DEFAULT_NARRATIVE_RULE_PACK_PATH)


def get_enabled_rules(rule_pack: dict[str, Any]) -> list[dict[str, Any]]:
    validated = validate_narrative_rule_pack(rule_pack)
    return [rule for rule in validated["rules"] if rule.get("enabled") is True]


def render_narrative_contract(rule_pack: dict[str, Any], *, include_disabled: bool = False) -> str:
    validated = validate_narrative_rule_pack(rule_pack)
    rules = validated["rules"] if include_disabled else get_enabled_rules(validated)
    lines = [
        "# 小说生成规则契约",
        "",
        f"- Rule Pack: `{validated['rule_pack_id']}`",
        f"- Version: `{validated['version']}`",
        f"- Language: `{validated['language']}`",
        "",
        "## 输出契约",
    ]
    output_contract = validated["output_contract"]
    lines.append("")
    lines.append("必须输出：")
    lines.extend(f"- {item}" for item in output_contract.get("must_output", []))
    lines.append("")
    lines.append("禁止输出：")
    lines.extend(f"- {item}" for item in output_contract.get("must_not_output", []))

    for category in _category_order(rules):
        category_rules = [rule for rule in rules if rule["category"] == category]
        lines.extend(["", f"## {CATEGORY_LABELS.get(category, category)}"])
        for rule in category_rules:
            disabled_suffix = "" if rule.get("enabled") else " [disabled]"
            lines.append("")
            lines.append(f"### {rule['title']}{disabled_suffix}")
            lines.append(f"- Code: `{rule['code']}`")
            lines.append(f"- Severity: `{rule['severity']}`")
            applies_to = ", ".join(str(item) for item in rule.get("applies_to", []))
            lines.append(f"- Applies to: {applies_to}")
            quality_codes = rule.get("quality_check_codes") or []
            if quality_codes:
                lines.append(f"- Quality checks: {', '.join(str(item) for item in quality_codes)}")
            lines.append("")
            lines.append(str(rule["instruction"]))
    return "\n".join(lines).rstrip() + "\n"


def _category_order(rules: list[dict[str, Any]]) -> list[str]:
    preferred = [
        "continuity",
        "character",
        "spatial",
        "conflict",
        "foreshadowing",
        "language",
        "style",
        "output_contract",
        "pacing",
        "safety",
        "custom",
    ]
    present = {str(rule.get("category")) for rule in rules}
    return [category for category in preferred if category in present] + sorted(present - set(preferred))


def _is_snake_case(value: str) -> bool:
    return re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", value) is not None


__all__ = [
    "DEFAULT_NARRATIVE_RULE_PACK_PATH",
    "NarrativeRulePackError",
    "get_enabled_rules",
    "load_default_narrative_rule_pack",
    "load_narrative_rule_pack",
    "render_narrative_contract",
    "validate_narrative_rule_pack",
]
