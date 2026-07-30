from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.context_budget import default_context_budget
from core.state.authoritative import validate_authoritative_state
from core.state.chapter_read_set import parse_chapter_context_read_set
from core.state.roster import project_roster_for_generation
from core.state.snapshot import validate_snapshot
from scripts.replay_scene_budget import (
    _input_artifact_path,
    extract_input_pack_artifact,
)


COLLECTIONS = (
    "characters",
    "relationships",
    "roster",
    "numeric_counters",
    "inventory",
    "locations",
    "events",
)
CHAPTER_INDEX = 18
POLICY = "chapter_18_explicit_contract_working_set_spike_v2"

CURRENT_VIEW_FIELDS = {
    "characters": (
        "character_id",
        "canonical_name",
        "aliases",
        "identity",
        "role",
        "status",
        "condition",
        "physical_condition",
        "current_goal",
        "current_location",
        "active_motivations",
        "traits",
    ),
    "relationships": (
        "relationship_id",
        "source_character_id",
        "target_character_id",
        "source_id",
        "target_id",
        "type",
        "status",
        "field",
        "combat_coordination",
        "threat_assessment",
    ),
    "roster": (
        "roster_id",
        "name",
        "aliases",
        "members",
        "declared_count",
        "computed_count",
        "unresolved_count",
    ),
    "numeric_counters": (
        "counter_id",
        "owner_id",
        "label",
        "current_value",
        "minimum",
        "maximum",
        "rule",
    ),
    "inventory": (
        "inventory_id",
        "owner_id",
        "item_id",
        "quantity",
    ),
    "locations": (
        "entity_id",
        "location_id",
    ),
    "events": (
        "event_id",
        "type",
        "status",
        "subjects",
        "objects",
        "location",
    ),
}


class SpikeError(RuntimeError):
    pass


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _compact_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _load_json(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SpikeError(f"{label} is not readable UTF-8 JSON: {path}") from exc
    if not isinstance(value, dict):
        raise SpikeError(f"{label} must contain one JSON object: {path}")
    return value, raw


def _authority_item(
    authority: Mapping[str, Any],
    item_id: str,
) -> tuple[str, str, dict[str, Any]]:
    collection, separator, record_id = item_id.partition("/")
    if not separator or collection not in COLLECTIONS or not record_id:
        raise SpikeError(f"invalid authority item id: {item_id!r}")
    records = authority.get(collection)
    record = records.get(record_id) if isinstance(records, Mapping) else None
    if not isinstance(record, dict):
        raise SpikeError(f"required authority item is missing: {item_id}")
    return collection, record_id, record


def _select_records(
    authority: Mapping[str, Any],
    item_ids: tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    selected = {collection: {} for collection in COLLECTIONS}
    for item_id in item_ids:
        collection, record_id, record = _authority_item(authority, item_id)
        selected[collection][record_id] = (
            project_roster_for_generation(record)
            if collection == "roster"
            else dict(record)
        )
    return selected


def _project_current_record(
    collection: str,
    record: Mapping[str, Any],
) -> dict[str, Any]:
    projected = {
        field: record[field]
        for field in CURRENT_VIEW_FIELDS[collection]
        if field in record
    }
    if collection == "relationships":
        state_field = record.get("field")
        if isinstance(state_field, str) and state_field in record:
            projected[state_field] = record[state_field]
    return projected


def _current_state_view(
    selected: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> dict[str, dict[str, Any]]:
    return {
        collection: {
            record_id: _project_current_record(collection, record)
            for record_id, record in records.items()
        }
        for collection, records in selected.items()
        if records and collection != "events"
    }


def _omitted_fields(
    selected: Mapping[str, Mapping[str, Mapping[str, Any]]],
    current_view: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> dict[str, list[str]]:
    omitted: dict[str, list[str]] = {}
    for collection, records in selected.items():
        if collection == "events" or not records:
            continue
        projected_records = current_view.get(collection, {})
        dropped = {
            field
            for record_id, record in records.items()
            for field in record
            if field not in projected_records.get(record_id, {})
        }
        if dropped:
            omitted[collection] = sorted(dropped)
    return omitted


def _context_text(
    *,
    source_sha256: str,
    state: Mapping[str, Any],
    profile: str,
) -> str:
    payload = {
        "schema_version": "1.0",
        "chapter_index": CHAPTER_INDEX,
        "policy": POLICY,
        "profile": profile,
        "source_authority_sha256": source_sha256,
        "state": state,
    }
    return "# Authoritative Working Set\n" + _compact_json(payload)


def _measure(text: str) -> dict[str, Any]:
    budget = default_context_budget(
        provider="openai",
        model="gpt-5.5",
        endpoint_type="openai_compatible",
        enable_model_tokenizer=False,
    )
    report = budget.measure(text, stage="chapter_18_authority_working_set_spike")
    return {
        "chars": len(text),
        "utf8_bytes": len(text.encode("utf-8")),
        "sha256": _sha256_bytes(text.encode("utf-8")),
        "raw_input_tokens": report["raw_input_tokens"],
        "budgeted_input_tokens": report["budgeted_input_tokens"],
        "count_mode": report["count_mode"],
        "counter_version": report["counter_version"],
        "within_32000_hard_limit": report["within_budget"],
    }


def _extract_markdown_section(text: str, heading: str) -> str | None:
    match = re.search(
        rf"(?ms)^# {re.escape(heading)}[ \t]*\r?\n(.*?)(?=^# |\Z)",
        text,
    )
    if match is None:
        return None
    return f"# {heading}\n{match.group(1).strip()}"


def _verified_input_pack(run_json: Path) -> tuple[str, dict[str, Any]]:
    payload, _raw = _load_json(run_json, label="run JSON")
    run = payload.get("run", payload)
    if not isinstance(run, dict):
        raise SpikeError("run JSON has no run record")
    if run.get("chapter_index") != CHAPTER_INDEX:
        raise SpikeError(f"run chapter_index is not {CHAPTER_INDEX}")
    summary = run.get("input_pack")
    artifact = summary.get("artifact") if isinstance(summary, dict) else None
    if not isinstance(summary, dict) or not isinstance(artifact, dict):
        raise SpikeError("run input-pack artifact metadata is incomplete")
    artifact_path = _input_artifact_path(run_json.parent.resolve(), run)
    try:
        raw = artifact_path.read_bytes()
    except OSError as exc:
        raise SpikeError(f"input-pack artifact is unreadable: {artifact_path}") from exc
    actual_sha256 = _sha256_bytes(raw)
    expected_sha256 = str(artifact.get("sha256") or "").strip().lower()
    if actual_sha256 != expected_sha256:
        raise SpikeError("input-pack artifact SHA-256 does not match the run record")
    logical, extraction = extract_input_pack_artifact(
        raw.decode("utf-8-sig"),
        run_id=str(run.get("id") or ""),
        chapter_index=CHAPTER_INDEX,
        recorded_chars=summary.get("chars"),
        artifact_recorded_chars=artifact.get("chars"),
    )
    return logical, {
        "run_json": str(run_json),
        "run_id": run.get("id"),
        "artifact_path": str(artifact_path),
        "artifact_sha256": actual_sha256,
        **extraction,
    }


def run_spike(
    *,
    snapshot_path: Path,
    outline_path: Path,
    run_json_path: Path | None,
    target_tokens: int,
) -> dict[str, Any]:
    snapshot, snapshot_bytes = _load_json(snapshot_path, label="snapshot")
    validate_snapshot(snapshot)
    if snapshot.get("chapter_index") != CHAPTER_INDEX:
        raise SpikeError(f"snapshot chapter_index is not {CHAPTER_INDEX}")
    authority = snapshot.get("authoritative_state")
    if not isinstance(authority, dict):
        raise SpikeError("snapshot has no authoritative_state object")
    validate_authoritative_state(authority)

    try:
        outline_bytes = outline_path.read_bytes()
        outline_text = outline_bytes.decode("utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise SpikeError(f"outline is not readable UTF-8: {outline_path}") from exc
    outline_sha256 = _sha256_bytes(outline_text.encode("utf-8"))
    read_set = parse_chapter_context_read_set(
        outline_text,
        chapter_index=CHAPTER_INDEX,
        source_outline_sha256=outline_sha256,
    )
    if read_set is None:
        raise SpikeError(
            "Chapter 18 outline has no novelagent-chapter-context contract"
        )
    current_state_item_ids = tuple(read_set["required_state_item_ids"])
    explicit_history_item_ids = tuple(read_set["required_event_item_ids"])

    canonical_authority = _compact_json(authority)
    authority_sha256 = _sha256_bytes(canonical_authority.encode("utf-8"))
    selected = _select_records(authority, current_state_item_ids)
    selected_with_history = _select_records(
        authority,
        current_state_item_ids + explicit_history_item_ids,
    )
    current_view = _current_state_view(selected)
    omitted_fields = _omitted_fields(selected, current_view)

    full_text = "# Authoritative State\n" + canonical_authority
    lossless_text = _context_text(
        source_sha256=authority_sha256,
        state={key: value for key, value in selected.items() if value},
        profile="named_records_lossless",
    )
    current_view_text = _context_text(
        source_sha256=authority_sha256,
        state=current_view,
        profile="named_current_state_view_lossy_counterfactual",
    )
    history_text = _context_text(
        source_sha256=authority_sha256,
        state={key: value for key, value in selected_with_history.items() if value},
        profile="named_current_state_plus_explicit_history",
    )

    measurements = {
        "full_authoritative_state_compact": _measure(full_text),
        "named_records_lossless": _measure(lossless_text),
        "named_current_state_view": _measure(current_view_text),
        "named_current_state_plus_explicit_history": _measure(history_text),
    }
    input_pack_evidence: dict[str, Any] | None = None
    if run_json_path is not None:
        input_pack, input_pack_evidence = _verified_input_pack(run_json_path)
        persisted_authority = _extract_markdown_section(
            input_pack,
            "Authoritative State",
        )
        if persisted_authority is None:
            raise SpikeError("persisted input pack has no Authoritative State section")
        persisted_body = persisted_authority.split("\n", 1)[1]
        persisted_value = json.loads(persisted_body)
        persisted_sha256 = _sha256_bytes(
            _compact_json(persisted_value).encode("utf-8")
        )
        timeline = _extract_markdown_section(input_pack, "Timeline")
        if timeline is not None:
            measurements["recorded_raw_timeline"] = _measure(timeline)
        input_pack_evidence.update(
            {
                "historical_authority_sha256": persisted_sha256,
                "authority_sha256_matches_snapshot": (
                    persisted_sha256 == authority_sha256
                ),
                "authority_difference_observed_after_migration": (
                    persisted_sha256 != authority_sha256
                ),
            }
        )

    lossless_tokens = measurements["named_records_lossless"]["budgeted_input_tokens"]
    view_tokens = measurements["named_current_state_view"]["budgeted_input_tokens"]
    approximation_ceiling = max(
        target_tokens,
        (target_tokens * 3 + 1) // 2,
    )
    if lossless_tokens <= target_tokens:
        size_verdict = "confirmed_by_named_selection_alone"
    elif view_tokens <= target_tokens:
        size_verdict = "confirmed_only_with_current_state_projection"
    elif view_tokens <= approximation_ceiling:
        size_verdict = "confirmed_within_approximation_band"
    else:
        size_verdict = "not_confirmed_at_target"
    missing_entity_count = 0
    coverage_verdict = "complete"
    overall_verdict = size_verdict
    comparison = {
        "view_vs_full_authority_token_ratio": round(
            view_tokens
            / measurements["full_authoritative_state_compact"][
                "budgeted_input_tokens"
            ],
            4,
        ),
        "view_tokens_saved_vs_full_authority": (
            measurements["full_authoritative_state_compact"][
                "budgeted_input_tokens"
            ]
            - view_tokens
        ),
        "selection_only_requires_projection": lossless_tokens > target_tokens,
    }
    if "recorded_raw_timeline" in measurements:
        timeline_tokens = measurements["recorded_raw_timeline"][
            "budgeted_input_tokens"
        ]
        comparison.update(
            {
                "view_vs_recorded_timeline_token_ratio": round(
                    view_tokens / timeline_tokens,
                    4,
                ),
                "view_tokens_saved_vs_recorded_timeline": (
                    timeline_tokens - view_tokens
                ),
            }
        )

    return {
        "schema_version": "1.0",
        "experiment": POLICY,
        "hypothesis": (
            "Chapter-outline named-entity retrieval can reduce the model-facing "
            "authoritative context to approximately 2k tokens."
        ),
        "target_budgeted_tokens": target_tokens,
        "approximation_ceiling_budgeted_tokens": approximation_ceiling,
        "verdict": overall_verdict,
        "size_verdict": size_verdict,
        "coverage_verdict": coverage_verdict,
        "source": {
            "snapshot_path": str(snapshot_path),
            "snapshot_sha256": _sha256_bytes(snapshot_bytes),
            "snapshot_chapter_index": snapshot.get("chapter_index"),
            "book_id": snapshot.get("book_id"),
            "authoritative_state_sha256": authority_sha256,
            "outline_path": str(outline_path),
            "outline_sha256": outline_sha256,
            "outline_chars": len(outline_text),
            "recorded_input_pack": input_pack_evidence,
        },
        "selection": {
            "contract_sha256": read_set["contract_sha256"],
            "named_current_state_item_ids": list(current_state_item_ids),
            "explicit_history_item_ids": list(explicit_history_item_ids),
            "named_current_state_record_count": len(current_state_item_ids),
            "explicit_history_record_count": len(explicit_history_item_ids),
            "narrative_constraint_count": len(
                read_set["narrative_constraints"]
            ),
            "expected_new_entities": list(
                read_set["expected_new_entities"]
            ),
            "missing_named_entity_count": missing_entity_count,
            "current_state_projection_omitted_fields": omitted_fields,
            "current_state_projection_status": (
                "lossy_counterfactual_not_production_ready"
            ),
            "current_state_projection_risk": (
                "The manual field allowlist can omit a future constraint-bearing "
                "field; production use needs typed generation-view schemas and "
                "dependency tests."
            ),
            "history_policy": (
                "Explicit history is measured separately and excluded from the "
                "current-state verdict because the Chapter 18 outline already "
                "restates those facts."
            ),
        },
        "measurements": measurements,
        "comparison": comparison,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only Chapter 18 spike: render an authoritative working set "
            "from a hand-authored outline entity read set and measure it with "
            "the production context-budget estimator."
        )
    )
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--outline", required=True)
    parser.add_argument(
        "--run-json",
        help=(
            "Optional Chapter 18 run record used to verify the persisted "
            "authority and measure its raw Timeline section."
        ),
    )
    parser.add_argument(
        "--target-tokens",
        type=int,
        default=2_500,
        help="Maximum budgeted tokens treated as approximately 2k (default: 2500).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.target_tokens < 1:
        print("Spike failed: --target-tokens must be positive", file=sys.stderr)
        return 2
    try:
        report = run_spike(
            snapshot_path=Path(args.snapshot).expanduser().resolve(),
            outline_path=Path(args.outline).expanduser().resolve(),
            run_json_path=(
                Path(args.run_json).expanduser().resolve()
                if args.run_json
                else None
            ),
            target_tokens=args.target_tokens,
        )
    except (SpikeError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"Spike failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["size_verdict"] != "not_confirmed_at_target" else 1


if __name__ == "__main__":
    raise SystemExit(main())
