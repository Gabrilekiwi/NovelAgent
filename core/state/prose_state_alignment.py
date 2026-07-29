from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any


_ARABIC_DIGITS = "0123456789０１２３４５６７８９"
_CHINESE_DIGITS = "零〇一二两三四五六七八九"
_CHINESE_UNITS = "十百千万亿"
_NUMBER_TOKEN = rf"[{_ARABIC_DIGITS},，]+|[{_CHINESE_DIGITS}{_CHINESE_UNITS}]+"
_ROSTER_SUFFIX = r"(?:\s*(?:幸存者)?(?:队伍|小队|团队|队))?"
_CLAUSE_END = r"(?=\s*(?:$|[。！？，；、,.!?;】\]]))"
_COUNT_FIRST_CLAUSE_START = (
    r"(?:^|(?<=[。！？!?；;\n，,：:“‘（(【\[]))\s*"
)
_COUNT_FIRST_SUBJECT_FOLLOW = (
    r"(?=\s*(?:$|[。！？，；、,.!?;：:】\]]|"
    r"目前|当前|现在|如今|仍|还|都|全部|全都|已|已经|"
    r"正在|正|被|未|没有|将|留|待|位于|处于|集中|单独|一起))"
)

_CURRENT_MARKERS = ("目前", "当前", "现在", "如今", "现今", "眼下", "此刻")
_HISTORY_MARKERS = (
    "此前",
    "从前",
    "过去",
    "当时",
    "那时",
    "彼时",
    "原先",
    "原本",
    "最初",
    "先前",
    "之前",
    "上一章",
    "上一次",
    "昨日",
    "昨天",
    "曾经",
    "一度",
    "起初",
    "末日前",
    "回忆",
    "旧记录",
)
_APPROXIMATE_MARKERS = (
    "大约",
    "大概",
    "差不多",
    "将近",
    "至少",
    "至多",
    "最多",
    "不满",
    "不足",
    "超过",
    "不超过",
    "不到",
    "不下于",
    "估计",
    "估摸",
)
_UNCERTAIN_MARKERS = (
    "可能",
    "也许",
    "或许",
    "听说",
    "据说",
    "传闻",
    "好像",
    "似乎",
    "猜测",
    "号称",
    "谎称",
    "未必",
    "不一定",
)
_CONDITIONAL_MARKERS = ("如果", "假如", "若是", "要是", "倘若", "假设")
_NEGATION_MARKERS = ("并非", "不是说", "没有说")
_IMMEDIATE_APPROXIMATE_SUFFIX = re.compile(
    r"^\s*(?:左右|上下|以上|以下|有余|余|多|来)"
)
_IMMEDIATE_QUESTION_SUFFIX = re.compile(r"^\s*(?:吗|么|呢|？|\?)")
_MARKER_SCOPE_SEPARATOR = re.compile(r"[。！？!?；;\n]")
_SINGLE_ROSTER_GENERIC_ALIASES = {
    "队伍",
    "小队",
    "主队",
    "团队",
    "幸存者队",
    "幸存者队伍",
}


@dataclass(frozen=True, slots=True)
class RosterCountClaim:
    roster_id: str
    declared_count: int
    start_char: int
    end_char: int
    quote: str
    alias: str
    confidence: str
    pattern_id: str

    def to_evidence(self) -> dict[str, Any]:
        return asdict(self)


def extract_roster_count_claims(
    text: str,
    roster_aliases: Mapping[str, Any],
) -> list[RosterCountClaim]:
    """Extract only exact, current roster-count declarations with one owner.

    ``roster_aliases`` maps stable roster IDs to an alias string, an iterable of
    aliases, or a roster record containing ``name``/``label``/``aliases``.
    Ambiguous aliases are deliberately ignored.
    """

    prose = str(text or "")
    alias_owners = _alias_owners(roster_aliases)
    unique_aliases = {
        alias: next(iter(owners))
        for alias, owners in alias_owners.items()
        if len(owners) == 1
    }
    if not prose or not unique_aliases:
        return []

    aliases = sorted(unique_aliases, key=lambda item: (-len(item), item.casefold()))
    alias_pattern = "|".join(re.escape(alias) for alias in aliases)
    anchored_alias = (
        rf"(?<![0-9A-Za-z_\u3400-\u9fff])(?P<alias>{alias_pattern})"
    )
    count_first_alias = rf"(?P<alias>{alias_pattern})"
    number = rf"(?P<number>{_NUMBER_TOKEN})"
    patterns = (
        (
            "count_first_subject",
            re.compile(
                rf"{_COUNT_FIRST_CLAUSE_START}{number}\s*名\s*"
                rf"{count_first_alias}{_COUNT_FIRST_SUBJECT_FOLLOW}",
                re.IGNORECASE,
            ),
        ),
        (
            "status_bracket",
            re.compile(
                rf"[【\[]\s*{anchored_alias}{_ROSTER_SUFFIX}\s*"
                rf"(?:总人数|人数|成员数)?\s*[:：=]\s*{number}\s*人\s*[】\]]",
                re.IGNORECASE,
            ),
        ),
        (
            "labeled_count",
            re.compile(
                rf"{anchored_alias}{_ROSTER_SUFFIX}\s*"
                rf"(?:总人数|人数|成员数)\s*(?:为|是|有|[:：=])\s*"
                rf"{number}\s*人?",
                re.IGNORECASE,
            ),
        ),
        (
            "explicit_total",
            re.compile(
                rf"{anchored_alias}{_ROSTER_SUFFIX}\s*"
                rf"(?:(?:目前|当前|现在|如今|现今|眼下)\s*)?"
                rf"(?:现有|共有|共计|合计|总计|总共有|总共|一共|"
                rf"还剩|只剩|剩余|剩下|增至|增加到|达到|变为|变成)\s*"
                rf"{number}\s*人",
                re.IGNORECASE,
            ),
        ),
        (
            "current_has",
            re.compile(
                rf"{anchored_alias}{_ROSTER_SUFFIX}\s*"
                rf"(?:目前|当前|现在|如今|现今|眼下)\s*有\s*"
                rf"{number}\s*人",
                re.IGNORECASE,
            ),
        ),
        (
            "status_colon",
            re.compile(
                rf"{anchored_alias}{_ROSTER_SUFFIX}\s*[:：=]\s*{number}\s*人"
                rf"{_CLAUSE_END}",
                re.IGNORECASE,
            ),
        ),
        (
            "paused_status",
            re.compile(
                rf"{anchored_alias}{_ROSTER_SUFFIX}\s*"
                rf"(?:[,，、;；]|[-—–]{{1,2}})\s*{number}\s*人"
                rf"{_CLAUSE_END}",
                re.IGNORECASE,
            ),
        ),
        (
            "bare_has",
            re.compile(
                rf"{anchored_alias}{_ROSTER_SUFFIX}\s*有\s*{number}\s*人"
                rf"{_CLAUSE_END}",
                re.IGNORECASE,
            ),
        ),
        (
            "compact_status",
            re.compile(
                rf"{anchored_alias}{_ROSTER_SUFFIX}\s*{number}\s*人"
                rf"{_CLAUSE_END}",
                re.IGNORECASE,
            ),
        ),
    )

    claims: list[RosterCountClaim] = []
    seen_occurrences: set[tuple[str, int, int, int]] = set()
    for pattern_id, pattern in patterns:
        for match in pattern.finditer(prose):
            declared_count = _parse_integer(match.group("number"))
            if declared_count is None:
                continue
            alias = match.group("alias")
            roster_id = unique_aliases.get(alias)
            if roster_id is None:
                roster_id = unique_aliases.get(alias.casefold())
            if roster_id is None or _is_non_current_or_inexact(
                prose,
                start=match.start(),
                end=match.end(),
            ):
                continue
            occurrence = (
                roster_id,
                match.start("alias"),
                match.start("number"),
                declared_count,
            )
            if occurrence in seen_occurrences:
                continue
            seen_occurrences.add(occurrence)
            claims.append(
                RosterCountClaim(
                    roster_id=roster_id,
                    declared_count=declared_count,
                    start_char=match.start(),
                    end_char=match.end(),
                    quote=match.group(0),
                    alias=alias,
                    confidence="high",
                    pattern_id=pattern_id,
                )
            )
    return sorted(claims, key=lambda claim: (claim.start_char, claim.end_char))


def validate_roster_count_claims(
    *,
    chapter_text: str,
    state_before: Mapping[str, Any],
    state_after: Mapping[str, Any],
    roster_changes: Any = (),
    roster_aliases: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return authority-compatible blocking findings for prose/count conflicts."""

    aliases = _aliases_from_states(state_before, state_after)
    if roster_aliases:
        aliases = _merge_alias_maps(aliases, roster_aliases)
    claims = extract_roster_count_claims(chapter_text, aliases)
    if not claims:
        return []

    before_counts = _roster_counts(state_before)
    after_counts = _roster_counts(state_after)
    claims_by_roster: dict[str, list[RosterCountClaim]] = {}
    for claim in claims:
        claims_by_roster.setdefault(claim.roster_id, []).append(claim)

    findings: list[dict[str, Any]] = []
    for roster_id, roster_claims in claims_by_roster.items():
        final_count = after_counts.get(roster_id)
        if final_count is None:
            continue
        allowed_counts = _allowed_transition_counts(
            roster_id,
            before_count=before_counts.get(roster_id),
            final_count=final_count,
            roster_changes=roster_changes,
        )
        unexpected = [
            claim
            for claim in roster_claims
            if claim.declared_count not in allowed_counts
        ]
        for claim in unexpected:
            findings.append(
                _finding(
                    f"Roster {roster_id} prose count does not match its "
                    "authoritative transition.",
                    {
                        "kind": "prose_state_mismatch",
                        "roster_id": roster_id,
                        "declared_count": claim.declared_count,
                        "expected_count": final_count,
                        "authoritative_count": final_count,
                        "allowed_transition_counts": sorted(allowed_counts),
                        "claim": claim.to_evidence(),
                    },
                )
            )

    return findings


def _finding(message: str, evidence: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "code": "roster_count_mismatch",
        "message": message,
        "blocking": True,
        "evidence": copy.deepcopy(dict(evidence)),
    }


def _alias_owners(roster_aliases: Mapping[str, Any]) -> dict[str, set[str]]:
    owners: dict[str, set[str]] = {}
    for raw_roster_id, raw_aliases in roster_aliases.items():
        roster_id = str(raw_roster_id or "").strip()
        if not roster_id:
            continue
        aliases = _aliases_for_record(roster_id, raw_aliases)
        for alias in aliases:
            owners.setdefault(alias, set()).add(roster_id)
            owners.setdefault(alias.casefold(), set()).add(roster_id)
    return owners


def _aliases_for_record(roster_id: str, value: Any) -> set[str]:
    aliases = {roster_id}
    if isinstance(value, str):
        aliases.add(value)
    elif isinstance(value, Mapping):
        for field in (
            "name",
            "label",
            "canonical_name",
            "display_name",
            "roster_name",
            "title",
        ):
            raw = value.get(field)
            if isinstance(raw, str):
                aliases.add(raw)
        for field in ("aliases", "names", "labels"):
            aliases.update(_strings(value.get(field)))
    else:
        aliases.update(_strings(value))
    return {alias.strip() for alias in aliases if alias and alias.strip()}


def _aliases_from_states(*states: Mapping[str, Any]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for state in states:
        for roster_id, record in _roster_records(state).items():
            result.setdefault(roster_id, set()).update(
                _aliases_for_record(roster_id, record)
            )
    if len(result) == 1:
        next(iter(result.values())).update(_SINGLE_ROSTER_GENERIC_ALIASES)
    return result


def _merge_alias_maps(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> dict[str, set[str]]:
    result = {
        str(roster_id): set(_aliases_for_record(str(roster_id), aliases))
        for roster_id, aliases in left.items()
    }
    for roster_id, aliases in right.items():
        stable_id = str(roster_id)
        result.setdefault(stable_id, set()).update(
            _aliases_for_record(stable_id, aliases)
        )
    return result


def _roster_records(state: Mapping[str, Any]) -> dict[str, Any]:
    value = state.get("roster")
    if not isinstance(value, Mapping):
        value = state.get("rosters")
    if not isinstance(value, Mapping):
        return {}
    return {
        str(roster_id): record
        for roster_id, record in value.items()
        if str(roster_id or "").strip()
    }


def _roster_counts(state: Mapping[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for roster_id, record in _roster_records(state).items():
        count = _record_count(record)
        if count is not None:
            result[roster_id] = count
    return result


def _record_count(record: Any) -> int | None:
    if not isinstance(record, Mapping):
        return _as_non_negative_integer(record)
    for field in ("computed_count", "declared_count", "count"):
        count = _as_non_negative_integer(record.get(field))
        if count is not None:
            return count
    members = record.get("members")
    if isinstance(members, Mapping):
        return len(members)
    if isinstance(members, list):
        return len(members)
    return None


def _allowed_transition_counts(
    roster_id: str,
    *,
    before_count: int | None,
    final_count: int,
    roster_changes: Any,
) -> set[int]:
    allowed = {final_count}
    current = before_count
    if current is not None:
        allowed.add(current)
    for change in _objects(roster_changes):
        change_roster_id = str(
            change.get("roster_id") or change.get("id") or ""
        ).strip()
        if change_roster_id != roster_id:
            continue
        previous = _as_non_negative_integer(
            change.get("previous_count", change.get("before_count"))
        )
        if previous is not None:
            current = previous
            allowed.add(previous)
        delta = _as_integer(change.get("delta"))
        if delta is not None and current is not None and current + delta >= 0:
            current += delta
            allowed.add(current)
    return allowed


def _is_non_current_or_inexact(text: str, *, start: int, end: int) -> bool:
    before = text[max(0, start - 48) : start]
    inside = text[start:end]
    after = text[end : min(len(text), end + 12)]
    scope_start = 0
    for separator in _MARKER_SCOPE_SEPARATOR.finditer(before):
        scope_start = separator.end()
    marker_prefix = before[scope_start:]
    local_scope = f"{marker_prefix}{inside}"

    if any(marker in local_scope for marker in _APPROXIMATE_MARKERS):
        return True
    if any(marker in local_scope for marker in _UNCERTAIN_MARKERS):
        return True
    if any(marker in local_scope for marker in _CONDITIONAL_MARKERS):
        return True
    if any(marker in local_scope for marker in _NEGATION_MARKERS):
        return True
    if _IMMEDIATE_APPROXIMATE_SUFFIX.match(after):
        return True
    if _IMMEDIATE_QUESTION_SUFFIX.match(after):
        return True

    history_scope = local_scope
    last_history = max(
        (history_scope.rfind(marker) for marker in _HISTORY_MARKERS),
        default=-1,
    )
    last_current = max(
        (history_scope.rfind(marker) for marker in _CURRENT_MARKERS),
        default=-1,
    )
    return last_history >= 0 and last_current < last_history


def _parse_integer(token: str) -> int | None:
    raw = str(token or "").strip()
    if not raw:
        return None
    translated = raw.translate(
        str.maketrans("０１２３４５６７８９，", "0123456789,")
    )
    if re.fullmatch(r"\d+(?:,\d{3})*", translated):
        return int(translated.replace(",", ""))
    if not re.fullmatch(rf"[{_CHINESE_DIGITS}{_CHINESE_UNITS}]+", raw):
        return None

    digit_values = {
        "零": 0,
        "〇": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if not any(character in _CHINESE_UNITS for character in raw):
        return int("".join(str(digit_values[character]) for character in raw))

    small_units = {"十": 10, "百": 100, "千": 1000}
    large_units = {"万": 10_000, "亿": 100_000_000}
    total = 0
    section = 0
    digit: int | None = None
    for character in raw:
        if character in digit_values:
            digit = digit_values[character]
            continue
        if character in small_units:
            value = 1 if digit is None else digit
            section += value * small_units[character]
            digit = None
            continue
        if character in large_units:
            section += 0 if digit is None else digit
            total += (section or 1) * large_units[character]
            section = 0
            digit = None
            continue
        return None
    return total + section + (0 if digit is None else digit)


def _as_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _as_non_negative_integer(value: Any) -> int | None:
    integer = _as_integer(value)
    return integer if integer is not None and integer >= 0 else None


def _objects(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Iterable) or isinstance(
        value,
        (str, bytes, Mapping),
    ):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _strings(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if not isinstance(value, Iterable) or isinstance(value, (bytes, Mapping)):
        return set()
    return {
        str(item)
        for item in value
        if isinstance(item, str) and item.strip()
    }


__all__ = [
    "RosterCountClaim",
    "extract_roster_count_claims",
    "validate_roster_count_claims",
]
