from __future__ import annotations

import copy
from difflib import SequenceMatcher
import hashlib
import json
from typing import Any, Iterable

from core.schema import SchemaValidationError, validate_schema


REPAIR_PATCH_SCHEMA_VERSION = "1.0"


class RepairPatchError(ValueError):
    def __init__(self, code: str, message: str, *, evidence: Any = None) -> None:
        self.code = code
        self.evidence = copy.deepcopy(evidence)
        super().__init__(f"{code}: {message}")


def build_repair_patch_from_texts(
    base_chapter: str,
    repaired_chapter: str,
    *,
    problem_codes: Iterable[str] = (),
    mode: str = "controlled_full_text_fallback",
) -> dict[str, Any]:
    base = str(base_chapter)
    output = str(repaired_chapter)
    codes = _unique_strings(problem_codes)
    operations: list[dict[str, Any]] = []
    matcher = SequenceMatcher(None, base, output, autojunk=False)
    for tag, start, end, output_start, output_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        expected = base[start:end]
        operations.append(
            {
                "operation": "replace",
                "start_char": start,
                "end_char": end,
                "expected_text_sha256": _sha256(expected),
                "replacement": output[output_start:output_end],
                "problem_codes": codes,
            }
        )
    patch = {
        "schema_version": REPAIR_PATCH_SCHEMA_VERSION,
        "base_chapter_sha256": _sha256(base),
        "output_chapter_sha256": _sha256(output),
        "mode": mode,
        "operations": operations,
    }
    patch["patch_sha256"] = _patch_hash(patch)
    validate_repair_patch(patch, base_chapter=base)
    return patch


def coerce_repair_patch(
    base_chapter: str,
    repair_result: Any,
    *,
    problem_codes: Iterable[str] = (),
) -> dict[str, Any]:
    if isinstance(repair_result, dict):
        return _finalize_patch_candidate(base_chapter, repair_result)
    text = str(repair_result)
    candidate = _json_patch_candidate(text)
    if candidate is not None:
        return _finalize_patch_candidate(base_chapter, candidate)
    return build_repair_patch_from_texts(
        base_chapter,
        text,
        problem_codes=problem_codes,
        mode="controlled_full_text_fallback",
    )


def validate_repair_patch(
    patch: Any,
    *,
    base_chapter: str,
) -> dict[str, Any]:
    try:
        validated = validate_schema(copy.deepcopy(patch), "repair_patch.schema.json")
    except SchemaValidationError as exc:
        raise RepairPatchError("repair_patch_invalid", str(exc)) from exc
    base = str(base_chapter)
    if validated["base_chapter_sha256"] != _sha256(base):
        raise RepairPatchError(
            "repair_patch_base_hash_mismatch",
            "base_chapter_sha256 does not match the current chapter",
        )
    if validated["patch_sha256"] != _patch_hash(validated):
        raise RepairPatchError(
            "repair_patch_hash_mismatch",
            "patch_sha256 does not match the canonical patch payload",
        )
    previous_end = 0
    for index, operation in enumerate(validated["operations"]):
        start = int(operation["start_char"])
        end = int(operation["end_char"])
        if start < previous_end or end < start or end > len(base):
            raise RepairPatchError(
                "repair_patch_range_invalid",
                "repair operations must be ordered, non-overlapping, and within the base chapter",
                evidence={"operation_index": index, "start_char": start, "end_char": end},
            )
        expected = base[start:end]
        if operation["expected_text_sha256"] != _sha256(expected):
            raise RepairPatchError(
                "repair_patch_expected_text_mismatch",
                "expected_text_sha256 does not match the selected base range",
                evidence={"operation_index": index, "start_char": start, "end_char": end},
            )
        previous_end = end
    output = _apply_validated_patch(base, validated)
    if validated["output_chapter_sha256"] != _sha256(output):
        raise RepairPatchError(
            "repair_patch_output_hash_mismatch",
            "output_chapter_sha256 does not match the applied patch",
        )
    return validated


def apply_repair_patch(
    base_chapter: str,
    patch: Any,
) -> tuple[str, dict[str, Any]]:
    validated = validate_repair_patch(patch, base_chapter=base_chapter)
    output = _apply_validated_patch(str(base_chapter), validated)
    audit = {
        "schema_version": REPAIR_PATCH_SCHEMA_VERSION,
        "patch_sha256": validated["patch_sha256"],
        "mode": validated["mode"],
        "base_chapter_sha256": validated["base_chapter_sha256"],
        "output_chapter_sha256": validated["output_chapter_sha256"],
        "base_chars": len(str(base_chapter)),
        "output_chars": len(output),
        "operation_count": len(validated["operations"]),
        "changed_ranges": [
            {
                "operation_index": index,
                "start_char": int(operation["start_char"]),
                "end_char": int(operation["end_char"]),
                "source_chars": int(operation["end_char"]) - int(operation["start_char"]),
                "replacement_chars": len(str(operation["replacement"])),
                "problem_codes": list(operation.get("problem_codes") or []),
            }
            for index, operation in enumerate(validated["operations"])
        ],
        "patch": copy.deepcopy(validated),
    }
    return output, audit


def _apply_validated_patch(base: str, patch: dict[str, Any]) -> str:
    output = base
    for operation in reversed(patch["operations"]):
        start = int(operation["start_char"])
        end = int(operation["end_char"])
        output = output[:start] + str(operation["replacement"]) + output[end:]
    return output


def _finalize_patch_candidate(
    base_chapter: str,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    patch = copy.deepcopy(candidate)
    patch.setdefault("schema_version", REPAIR_PATCH_SCHEMA_VERSION)
    patch.setdefault("mode", "patch")
    operations = patch.get("operations")
    if not isinstance(operations, list):
        raise RepairPatchError(
            "repair_patch_invalid",
            "RepairPatch proposal requires an operations array",
        )
    base = str(base_chapter)
    if patch.get("base_chapter_sha256") != _sha256(base):
        raise RepairPatchError(
            "repair_patch_base_hash_mismatch",
            "base_chapter_sha256 does not match the current chapter",
        )
    previous_end = 0
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            raise RepairPatchError(
                "repair_patch_invalid",
                "RepairPatch operations must be objects",
                evidence={"operation_index": index},
            )
        operation.setdefault("operation", "replace")
        operation.setdefault("problem_codes", [])
        start = operation.get("start_char")
        end = operation.get("end_char")
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or start < previous_end
            or end < start
            or end > len(base)
        ):
            raise RepairPatchError(
                "repair_patch_range_invalid",
                "repair operations must be ordered, non-overlapping, and within the base chapter",
                evidence={"operation_index": index, "start_char": start, "end_char": end},
            )
        expected_hash = _sha256(base[start:end])
        if operation.get("expected_text_sha256") != expected_hash:
            raise RepairPatchError(
                "repair_patch_expected_text_mismatch",
                "expected_text_sha256 does not match the selected base range",
                evidence={"operation_index": index, "start_char": start, "end_char": end},
            )
        if not isinstance(operation.get("replacement"), str):
            raise RepairPatchError(
                "repair_patch_invalid",
                "repair operation replacement must be a string",
                evidence={"operation_index": index},
            )
        previous_end = end
    output = _apply_validated_patch(base, {"operations": operations})
    computed_output_hash = _sha256(output)
    if (
        patch.get("output_chapter_sha256") is not None
        and patch["output_chapter_sha256"] != computed_output_hash
    ):
        raise RepairPatchError(
            "repair_patch_output_hash_mismatch",
            "provided output_chapter_sha256 does not match the applied proposal",
        )
    patch["output_chapter_sha256"] = computed_output_hash
    computed_patch_hash = _patch_hash(patch)
    if patch.get("patch_sha256") is not None and patch["patch_sha256"] != computed_patch_hash:
        raise RepairPatchError(
            "repair_patch_hash_mismatch",
            "provided patch_sha256 does not match the canonical proposal",
        )
    patch["patch_sha256"] = computed_patch_hash
    return validate_repair_patch(patch, base_chapter=base)


def _json_patch_candidate(value: str) -> dict[str, Any] | None:
    text = value.strip()
    if not text.startswith("{") or not text.endswith("}"):
        return None
    try:
        candidate = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(candidate, dict) or "base_chapter_sha256" not in candidate:
        return None
    return candidate


def _patch_hash(patch: dict[str, Any]) -> str:
    payload = {
        key: value
        for key, value in patch.items()
        if key != "patch_sha256"
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256(encoded)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _unique_strings(value: Iterable[str]) -> list[str]:
    result: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


__all__ = [
    "REPAIR_PATCH_SCHEMA_VERSION",
    "RepairPatchError",
    "apply_repair_patch",
    "build_repair_patch_from_texts",
    "coerce_repair_patch",
    "validate_repair_patch",
]
