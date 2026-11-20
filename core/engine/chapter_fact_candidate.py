from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from typing import Any, Mapping, Sequence

from core.schema import SchemaValidationError, validate_schema
from core.engine.chapter_fact_contract import (
    fact_contract_example,
    fact_payload_contract,
    fact_payload_contracts,
    validate_fact_payload,
)


CHAPTER_FACT_PROPOSAL_SCHEMA_VERSION = "1.0"
FACT_KINDS = (
    "event",
    "character_state",
    "relationship",
    "roster",
    "location",
    "inventory",
    "counter",
)
FACT_REVIEW_DECISIONS = ("pending", "accepted", "rejected")

_FACT_ID = re.compile(r"^fact_[0-9a-f]{32}$")
_LOCAL_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_MAX_FACTS = 100
_MAX_VALUE_CHARS = 10_000
_MAX_CHAPTER_CHARS = 200_000
_MAX_AUTHORITY_VIEW_CHARS = 100_000

class ChapterFactCandidateError(ValueError):
    code = "chapter_fact_candidate_invalid"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.code = code or self.code
        super().__init__(f"{self.code}: {message}")


def build_fact_extraction_messages(
    *,
    chapter_index: int,
    chapter_text: str,
    authority_view: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Build a prose-to-fact proposal request with no write authority.

    The caller must supply a deliberately reduced authority view.  This
    contract asks the model only to identify reviewable facts; it never asks
    the model to calculate authoritative before/after state or mutate files.
    """

    chapter_index = _positive_integer(chapter_index, "chapter_index")
    prose = _chapter_text(chapter_text)
    if not isinstance(authority_view, Mapping):
        raise ChapterFactCandidateError("authority_view must be an object")
    authority_json = _canonical_json(_json_value(dict(authority_view), "authority_view"))
    if len(authority_json) > _MAX_AUTHORITY_VIEW_CHARS:
        raise ChapterFactCandidateError(
            "reduced authority_view exceeds the fact extraction budget",
            code="chapter_fact_authority_view_too_large",
        )

    example = fact_contract_example()
    return [
        {
            "role": "system",
            "content": (
                "你是小说事实候选提取器，不是正文作者，也没有写入权威状态的权限。"
                "只提出正文逐字证据支持、需要作者确认的变化；不补写情节，不推测未写明事实，"
                "不输出连续性评价或文风建议。只输出合法 JSON。事件中只有 onsite_participants "
                "表示在 occurrence_location 物理在场；remote_participants 只表示远程参与，"
                "transported_items 只表示运输中的物品，related_entities 只表示逻辑关联。"
                "绝不使用旧 subjects、objects、location 字段。数量、库存和位置变化必须在正文中"
                "有明确证据。每条 evidence_quotes 必须逐字复制正文中唯一出现的一小段。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"章节：第 {chapter_index} 章\n"
                "输出事实种类仅限 event、character_state、relationship、roster、location、"
                "inventory、counter。除 event 外的变化用 source_event_key 关联本次 facts 中的"
                " event local_key；若无可核实变化，返回空 facts。所有结果都只是作者待审候选。\n\n"
                "每种 proposal 的字段与操作如下，所有 required 字段必填，不得添加 before、"
                "previous_quantity、previous_value、最终库存或名单人数。from_location 只是正文"
                "声明的出发地；当前权威位置由程序读取。相同事件对相同目标的同一变化只能提出"
                "一次；两次独立变化必须各有事件和证据。候选数组顺序不是状态更新顺序。\n"
                f"七类 proposal 契约：\n{_canonical_json(fact_payload_contracts())}\n\n"
                "以下是独立教学样本，不是当前章节；不要把样本人物、事件或证据混入结果。\n"
                f"示例正文：\n{example['chapter_text']}\n"
                f"输出契约示例：\n{_canonical_json(example['proposal'])}\n\n"
                f"精简权威视图（只用于消歧，不可改写）：\n{authority_json}\n\n"
                f"作者当前工作台正文：\n{prose}"
            ),
        },
    ]


def parse_fact_proposal(raw: str, *, chapter_text: str) -> list[dict[str, Any]]:
    if not isinstance(raw, str) or not raw.strip():
        raise ChapterFactCandidateError("fact proposal is empty")
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines.pop()
        text = "\n".join(lines).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ChapterFactCandidateError(
            f"fact proposal is not valid JSON: {exc}",
            code="chapter_fact_candidate_json_invalid",
        ) from exc
    return normalize_fact_proposal(value, chapter_text=chapter_text, origin="model")


def normalize_fact_proposal(
    value: Any,
    *,
    chapter_text: str,
    origin: str = "model",
) -> list[dict[str, Any]]:
    prose = _chapter_text(chapter_text)
    if origin not in {"model", "author"}:
        raise ChapterFactCandidateError("fact origin must be model or author")
    try:
        checked = validate_schema(value, "chapter_fact_proposal.schema.json")
    except SchemaValidationError as exc:
        raise ChapterFactCandidateError(str(exc)) from exc
    facts = checked["facts"]
    if len(facts) > _MAX_FACTS:
        raise ChapterFactCandidateError(f"fact proposal cannot exceed {_MAX_FACTS} items")

    local_keys = [_local_key(item.get("local_key")) for item in facts]
    if len(set(local_keys)) != len(local_keys):
        raise ChapterFactCandidateError("fact proposal local_key values must be unique")
    event_keys = {
        local_key
        for local_key, item in zip(local_keys, facts, strict=True)
        if item.get("kind") == "event"
    }

    normalized: list[dict[str, Any]] = []
    signatures: set[str] = set()
    chapter_sha256 = _sha256_text(prose)
    for index, (local_key, raw_fact) in enumerate(
        zip(local_keys, facts, strict=True), start=1
    ):
        kind = str(raw_fact["kind"])
        summary = _bounded_text(raw_fact["summary"], "summary", maximum=500)
        proposal = _normalize_proposal(kind, raw_fact["proposal"])
        _validate_source_event_reference(kind, proposal, event_keys)
        evidence_spans = _resolve_evidence_quotes(raw_fact["evidence_quotes"], prose)
        core = {
            "local_key": local_key,
            "kind": kind,
            "summary": summary,
            "evidence_spans": evidence_spans,
            "proposal": proposal,
        }
        signature = _fact_signature(kind, proposal, evidence_spans)
        if signature in signatures:
            raise ChapterFactCandidateError("fact proposal contains a duplicate fact")
        signatures.add(signature)
        fact_id = "fact_" + hashlib.sha256(
            f"{chapter_sha256}\0{index}\0{_canonical_json(core)}".encode("utf-8")
        ).hexdigest()[:32]
        item = {
            "fact_id": fact_id,
            **core,
            "review": {
                "origin": origin,
                "decision": "pending",
                "author_edited": origin == "author",
                "reason": None,
            },
        }
        validate_schema(item, "chapter_fact_review_item.schema.json")
        normalized.append(item)
    _reject_overlapping_duplicate_events(normalized)
    return normalized


def normalize_fact_review_items(
    value: Any,
    *,
    chapter_text: str,
) -> list[dict[str, Any]]:
    """Validate an editable fact-card list while preserving stable fact ids."""

    prose = _chapter_text(chapter_text)
    if not isinstance(value, list):
        raise ChapterFactCandidateError("fact review items must be an array")
    if len(value) > _MAX_FACTS:
        raise ChapterFactCandidateError(f"fact review cannot exceed {_MAX_FACTS} items")
    for item in value:
        try:
            validate_schema(item, "chapter_fact_review_item.schema.json")
        except SchemaValidationError as exc:
            raise ChapterFactCandidateError(str(exc)) from exc

    fact_ids = [str(item["fact_id"]) for item in value]
    local_keys = [_local_key(item["local_key"]) for item in value]
    if len(set(fact_ids)) != len(fact_ids) or any(
        _FACT_ID.fullmatch(item) is None for item in fact_ids
    ):
        raise ChapterFactCandidateError("fact review fact_id values must be unique and canonical")
    if len(set(local_keys)) != len(local_keys):
        raise ChapterFactCandidateError("fact review local_key values must be unique")
    event_keys = {
        local_key
        for local_key, item in zip(local_keys, value, strict=True)
        if item["kind"] == "event"
    }

    normalized: list[dict[str, Any]] = []
    signatures: set[str] = set()
    for fact_id, local_key, raw_fact in zip(fact_ids, local_keys, value, strict=True):
        kind = str(raw_fact["kind"])
        summary = _bounded_text(raw_fact["summary"], "summary", maximum=500)
        proposal = _normalize_proposal(kind, raw_fact["proposal"])
        _validate_source_event_reference(kind, proposal, event_keys)
        evidence = _validated_evidence_spans(raw_fact["evidence_spans"], prose)
        review = _normalize_review(raw_fact["review"])
        signature = _fact_signature(kind, proposal, evidence)
        if signature in signatures:
            raise ChapterFactCandidateError("fact review contains a duplicate fact")
        signatures.add(signature)
        normalized.append(
            {
                "fact_id": fact_id,
                "local_key": local_key,
                "kind": kind,
                "summary": summary,
                "evidence_spans": evidence,
                "proposal": proposal,
                "review": review,
            }
        )
    _reject_overlapping_duplicate_events(normalized)
    return normalized


def require_confirmed_fact_review(
    value: Any,
    *,
    chapter_text: str,
) -> list[dict[str, Any]]:
    """Return accepted facts only after every card has an explicit decision."""

    facts = normalize_fact_review_items(value, chapter_text=chapter_text)
    pending = [item["fact_id"] for item in facts if item["review"]["decision"] == "pending"]
    if pending:
        raise ChapterFactCandidateError(
            "every fact card requires an explicit author decision before confirmation",
            code="chapter_fact_review_incomplete",
        )
    accepted_event_keys = {
        item["local_key"]
        for item in facts
        if item["kind"] == "event" and item["review"]["decision"] == "accepted"
    }
    for item in facts:
        if item["review"]["decision"] != "accepted" or item["kind"] == "event":
            continue
        source_event_key = item["proposal"].get("source_event_key")
        if source_event_key is not None and source_event_key not in accepted_event_keys:
            raise ChapterFactCandidateError(
                f"accepted fact {item['fact_id']} depends on a rejected event",
                code="chapter_fact_review_dependency_rejected",
            )
    return [copy.deepcopy(item) for item in facts if item["review"]["decision"] == "accepted"]


def _normalize_proposal(kind: str, value: Any) -> dict[str, Any]:
    payload = _normalize_proposal_values(kind, value)
    try:
        return validate_fact_payload(kind, payload)
    except SchemaValidationError as exc:
        raise ChapterFactCandidateError(str(exc)) from exc


def _fact_signature(kind: str, proposal: Mapping[str, Any], evidence: list[dict[str, Any]]) -> str:
    # Different excerpts must not count the same event's state change twice.
    # Distinct source events remain distinct even when their deltas coincide.
    payload = copy.deepcopy(dict(proposal))
    if kind == "roster":
        payload["member_ids"] = sorted(payload["member_ids"])
    if kind == "event":
        for field in ("onsite_participants", "remote_participants", "transported_items", "related_entities"):
            payload[field] = sorted(payload[field], key=_canonical_json)
    signature = {"kind": kind, "proposal": payload}
    if kind == "event":
        signature["evidence_spans"] = sorted(evidence, key=lambda item: (item["start"], item["end"]))
    return _canonical_json(signature)


def _reject_overlapping_duplicate_events(facts: list[dict[str, Any]]) -> None:
    events: dict[str, list[dict[str, Any]]] = {}
    for fact in facts:
        if fact["kind"] != "event":
            continue
        signature = _fact_signature("event", fact["proposal"], [])
        for previous in events.get(signature, []):
            if any(max(a["start"], b["start"]) < min(a["end"], b["end"])
                   for a in previous["evidence_spans"] for b in fact["evidence_spans"]):
                raise ChapterFactCandidateError("fact proposal contains a duplicate event with overlapping evidence")
        events.setdefault(signature, []).append(fact)


def _normalize_proposal_values(kind: str, value: Any) -> dict[str, Any]:
    if kind not in FACT_KINDS:
        raise ChapterFactCandidateError(f"unsupported fact kind: {kind!r}")
    if not isinstance(value, Mapping):
        raise ChapterFactCandidateError(f"{kind} proposal must be an object")
    raw = dict(value)
    _exact_keys(raw, set(fact_payload_contract(kind)["required"]), kind)
    if kind == "event":
        return _normalize_event(raw)
    if kind == "character_state":
        return {
            "entity_id": _entity_id(raw["entity_id"], "entity_id"),
            "canonical_name": _bounded_text(raw["canonical_name"], "canonical_name", 200),
            "field": _bounded_text(raw["field"], "field", 100),
            "after": _json_value(raw["after"], "after"),
            "source_event_key": _required_local_key(raw["source_event_key"], "source_event_key"),
        }
    if kind == "relationship":
        source = _entity_id(raw["source_entity_id"], "source_entity_id")
        target = _entity_id(raw["target_entity_id"], "target_entity_id")
        if source == target:
            raise ChapterFactCandidateError("relationship endpoints must differ")
        return {
            "relationship_id": _entity_id(raw["relationship_id"], "relationship_id"),
            "source_entity_id": source,
            "target_entity_id": target,
            "relation_type": _bounded_text(raw["relation_type"], "relation_type", 100),
            "field": _bounded_text(raw["field"], "field", 100),
            "after": _json_value(raw["after"], "after"),
            "source_event_key": _required_local_key(raw["source_event_key"], "source_event_key"),
        }
    if kind == "roster":
        operation = str(raw["operation"])
        if operation not in fact_payload_contract(kind)["properties"]["operation"]["enum"]:
            raise ChapterFactCandidateError("roster operation must be add or remove")
        members = _unique_entity_ids(raw["member_ids"], "member_ids", require_nonempty=True)
        return {
            "roster_id": _entity_id(raw["roster_id"], "roster_id"),
            "operation": operation,
            "member_ids": members,
            "source_event_key": _required_local_key(raw["source_event_key"], "source_event_key"),
        }
    if kind == "location":
        before = _optional_entity_id(raw["from_location"], "from_location")
        after = _entity_id(raw["to_location"], "to_location")
        if before == after:
            raise ChapterFactCandidateError("location proposal must change the location")
        return {
            "entity_id": _entity_id(raw["entity_id"], "entity_id"),
            "from_location": before,
            "to_location": after,
            "source_event_key": _required_local_key(raw["source_event_key"], "source_event_key"),
        }
    if kind == "inventory":
        delta = _integer(raw["delta"], "delta")
        if delta == 0:
            raise ChapterFactCandidateError("inventory delta must not be zero")
        return {
            "owner_id": _entity_id(raw["owner_id"], "owner_id"),
            "item_id": _entity_id(raw["item_id"], "item_id"),
            "delta": delta,
            "source_event_key": _required_local_key(raw["source_event_key"], "source_event_key"),
        }
    operation = str(raw["operation"])
    if operation not in fact_payload_contract(kind)["properties"]["operation"]["enum"]:
        raise ChapterFactCandidateError("counter operation must be increment or set")
    number = _finite_number(raw["value"], "value")
    if operation == "increment" and number == 0:
        raise ChapterFactCandidateError("counter increment must not be zero")
    return {
        "counter_id": _entity_id(raw["counter_id"], "counter_id"),
        "operation": operation,
        "value": number,
        "source_event_key": _required_local_key(raw["source_event_key"], "source_event_key"),
    }


def _normalize_event(raw: dict[str, Any]) -> dict[str, Any]:
    properties = fact_payload_contract("event")["properties"]
    status = str(raw["status"])
    if status not in properties["status"]["enum"]:
        raise ChapterFactCandidateError("event status is invalid")
    occurrence_location = _optional_entity_id(
        raw["occurrence_location"], "occurrence_location"
    )
    onsite = _normalize_role_items(
        raw["onsite_participants"],
        label="onsite_participants",
        roles=frozenset(properties["onsite_participants"]["items"]["properties"]["role"]["enum"]),
        keys=set(properties["onsite_participants"]["items"]["required"]),
    )
    remote = _normalize_role_items(
        raw["remote_participants"],
        label="remote_participants",
        roles=frozenset(properties["remote_participants"]["items"]["properties"]["role"]["enum"]),
        keys=set(properties["remote_participants"]["items"]["required"]),
        channel=True,
    )
    transported = _normalize_transported_items(raw["transported_items"])
    related = _normalize_role_items(
        raw["related_entities"],
        label="related_entities",
        roles=frozenset(properties["related_entities"]["items"]["properties"]["relation"]["enum"]),
        keys=set(properties["related_entities"]["items"]["required"]),
        role_field="relation",
    )
    onsite_ids = {item["entity_id"] for item in onsite}
    remote_ids = {item["entity_id"] for item in remote}
    if onsite_ids & remote_ids:
        raise ChapterFactCandidateError(
            "one event cannot mark the same entity as both onsite and remote"
        )
    if not occurrence_location and not onsite and not remote and not transported and not related:
        raise ChapterFactCandidateError("event proposal requires at least one explicit anchor")
    return {
        "event_type": _bounded_text(raw["event_type"], "event_type", 100),
        "status": status,
        "occurrence_location": occurrence_location,
        "onsite_participants": onsite,
        "remote_participants": remote,
        "transported_items": transported,
        "related_entities": related,
    }


def _normalize_role_items(
    value: Any,
    *,
    label: str,
    roles: frozenset[str],
    keys: set[str],
    role_field: str = "role",
    channel: bool = False,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ChapterFactCandidateError(f"{label} must be an array")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ChapterFactCandidateError(f"{label}[{index}] must be an object")
        raw = dict(item)
        _exact_keys(raw, keys, f"{label}[{index}]")
        entity_id = _entity_id(raw["entity_id"], f"{label}[{index}].entity_id")
        role = str(raw[role_field])
        if role not in roles:
            raise ChapterFactCandidateError(f"{label}[{index}].{role_field} is invalid")
        if entity_id in seen:
            raise ChapterFactCandidateError(f"{label} contains duplicate entity_id values")
        seen.add(entity_id)
        normalized: dict[str, Any] = {"entity_id": entity_id, role_field: role}
        if channel:
            normalized["channel"] = _optional_text(raw["channel"], "channel", 100)
        result.append(normalized)
    return result


def _normalize_transported_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ChapterFactCandidateError("transported_items must be an array")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    keys = set(fact_payload_contract("event")["properties"]["transported_items"]["items"]["required"])
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ChapterFactCandidateError(f"transported_items[{index}] must be an object")
        raw = dict(item)
        _exact_keys(raw, keys, f"transported_items[{index}]")
        item_id = _entity_id(raw["item_id"], f"transported_items[{index}].item_id")
        if item_id in seen:
            raise ChapterFactCandidateError("transported_items contains duplicate item_id values")
        seen.add(item_id)
        before = _optional_entity_id(raw["from_location"], "from_location")
        after = _optional_entity_id(raw["to_location"], "to_location")
        if before is None and after is None:
            raise ChapterFactCandidateError(
                "transported item requires from_location or to_location"
            )
        if before is not None and before == after:
            raise ChapterFactCandidateError("transported item endpoints must differ")
        result.append(
            {
                "item_id": item_id,
                "from_location": before,
                "to_location": after,
                "carrier_id": _optional_entity_id(raw["carrier_id"], "carrier_id"),
            }
        )
    return result


def _validate_source_event_reference(
    kind: str,
    proposal: Mapping[str, Any],
    event_keys: set[str],
) -> None:
    if kind == "event":
        return
    source = proposal.get("source_event_key")
    if source is not None and source not in event_keys:
        raise ChapterFactCandidateError(
            f"{kind} source_event_key must reference an event in the same proposal"
        )


def _resolve_evidence_quotes(value: Any, chapter_text: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ChapterFactCandidateError("evidence_quotes must contain at least one quote")
    spans: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for index, raw_quote in enumerate(value):
        quote = _bounded_text(raw_quote, f"evidence_quotes[{index}]", 1000, strip=False)
        start = chapter_text.find(quote)
        if start < 0:
            raise ChapterFactCandidateError(
                f"evidence_quotes[{index}] does not occur in the chapter"
            )
        if chapter_text.find(quote, start + 1) >= 0:
            raise ChapterFactCandidateError(
                f"evidence_quotes[{index}] is ambiguous; use a uniquely occurring quote"
            )
        span = (start, start + len(quote))
        if span in seen:
            raise ChapterFactCandidateError("evidence_quotes contains a duplicate quote")
        seen.add(span)
        spans.append({"start": span[0], "end": span[1], "quote": quote})
    return spans


def _validated_evidence_spans(value: Any, chapter_text: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ChapterFactCandidateError("evidence_spans must contain at least one span")
    result: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ChapterFactCandidateError(f"evidence_spans[{index}] must be an object")
        raw = dict(item)
        _exact_keys(raw, {"start", "end", "quote"}, f"evidence_spans[{index}]")
        start = _nonnegative_integer(raw["start"], f"evidence_spans[{index}].start")
        end = _positive_integer(raw["end"], f"evidence_spans[{index}].end")
        quote = _bounded_text(raw["quote"], f"evidence_spans[{index}].quote", 1000, strip=False)
        if end <= start or end > len(chapter_text) or chapter_text[start:end] != quote:
            raise ChapterFactCandidateError(
                f"evidence_spans[{index}] does not match the bound chapter text"
            )
        if (start, end) in seen:
            raise ChapterFactCandidateError("evidence_spans contains a duplicate span")
        seen.add((start, end))
        result.append({"start": start, "end": end, "quote": quote})
    return result


def _normalize_review(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ChapterFactCandidateError("review must be an object")
    raw = dict(value)
    _exact_keys(raw, {"origin", "decision", "author_edited", "reason"}, "review")
    origin = str(raw["origin"])
    decision = str(raw["decision"])
    if origin not in {"model", "author"} or decision not in FACT_REVIEW_DECISIONS:
        raise ChapterFactCandidateError("review origin or decision is invalid")
    if not isinstance(raw["author_edited"], bool):
        raise ChapterFactCandidateError("review.author_edited must be boolean")
    return {
        "origin": origin,
        "decision": decision,
        "author_edited": raw["author_edited"],
        "reason": _optional_text(raw["reason"], "review.reason", 500),
    }


def _chapter_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChapterFactCandidateError("chapter_text must contain prose")
    if len(value) > _MAX_CHAPTER_CHARS:
        raise ChapterFactCandidateError("chapter_text exceeds the fact review budget")
    return value


def _local_key(value: Any) -> str:
    text = str(value or "")
    if _LOCAL_KEY.fullmatch(text) is None:
        raise ChapterFactCandidateError("local_key is not a canonical safe identifier")
    return text


def _optional_local_key(value: Any) -> str | None:
    if value is None:
        return None
    return _local_key(value)


def _required_local_key(value: Any, label: str) -> str:
    if value is None:
        raise ChapterFactCandidateError(f"{label} is required for this fact kind")
    return _local_key(value)


def _entity_id(value: Any, label: str) -> str:
    return _bounded_text(value, label, 200)


def _optional_entity_id(value: Any, label: str) -> str | None:
    return _optional_text(value, label, 200)


def _unique_entity_ids(value: Any, label: str, *, require_nonempty: bool) -> list[str]:
    if not isinstance(value, list):
        raise ChapterFactCandidateError(f"{label} must be an array")
    result = [_entity_id(item, f"{label}[]") for item in value]
    if require_nonempty and not result:
        raise ChapterFactCandidateError(f"{label} must not be empty")
    if len(set(result)) != len(result):
        raise ChapterFactCandidateError(f"{label} must not contain duplicates")
    return result


def _bounded_text(
    value: Any,
    label: str,
    maximum: int,
    *,
    strip: bool = True,
) -> str:
    if not isinstance(value, str):
        raise ChapterFactCandidateError(f"{label} must be text")
    text = value.strip() if strip else value
    if not text or len(text) > maximum:
        raise ChapterFactCandidateError(
            f"{label} must contain 1 to {maximum} characters"
        )
    return text


def _optional_text(value: Any, label: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, label, maximum)


def _finite_number(value: Any, label: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ChapterFactCandidateError(f"{label} must be a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ChapterFactCandidateError(f"{label} must be finite")
    return value


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChapterFactCandidateError(f"{label} must be an integer")
    return value


def _positive_integer(value: Any, label: str) -> int:
    number = _integer(value, label)
    if number < 1:
        raise ChapterFactCandidateError(f"{label} must be positive")
    return number


def _nonnegative_integer(value: Any, label: str) -> int:
    number = _integer(value, label)
    if number < 0:
        raise ChapterFactCandidateError(f"{label} must be non-negative")
    return number


def _json_value(value: Any, label: str) -> Any:
    try:
        encoded = _canonical_json(value)
    except (TypeError, ValueError) as exc:
        raise ChapterFactCandidateError(f"{label} must be valid finite JSON") from exc
    if len(encoded) > _MAX_VALUE_CHARS:
        raise ChapterFactCandidateError(f"{label} exceeds {_MAX_VALUE_CHARS} JSON characters")
    return json.loads(encoded)


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ChapterFactCandidateError(
            f"{label} proposal fields differ; missing={missing}, extra={extra}"
        )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "CHAPTER_FACT_PROPOSAL_SCHEMA_VERSION",
    "FACT_KINDS",
    "FACT_REVIEW_DECISIONS",
    "ChapterFactCandidateError",
    "build_fact_extraction_messages",
    "normalize_fact_proposal",
    "normalize_fact_review_items",
    "parse_fact_proposal",
    "require_confirmed_fact_review",
]
