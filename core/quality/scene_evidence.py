from __future__ import annotations

import copy
import hashlib
from typing import Any

from core.quality.repair_patch import (
    apply_repair_patch,
    coerce_repair_patch,
    validate_repair_patch,
)


class SceneEvidenceAlignmentError(ValueError):
    code = "stale_scene_evidence"

    def __init__(self, message: str, *, evidence: Any = None) -> None:
        self.evidence = copy.deepcopy(evidence)
        super().__init__(f"{self.code}: {message}")


def canonicalize_scene_local_repair_patch(
    pipeline: dict[str, Any] | None,
    *,
    before_chapter: str,
    patch: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Preserve the canonical scene separator for a narrowly safe deletion.

    A full-text repair can remove the duplicate tail of one scene together with
    the two newlines immediately before the next scene.  That does not rewrite
    the next scene, but it makes the otherwise local operation appear to cross
    a scene boundary.  Canonicalize only that exact case; all other cross-scene
    operations remain for ``realign_scene_evidence`` to reject.
    """
    base = str(before_chapter)
    validated_patch = validate_repair_patch(patch, base_chapter=base)
    spans = _canonicalizable_spans(pipeline, before_chapter=base)
    if spans is None:
        return validated_patch, None

    adjusted_operations: list[dict[str, Any]] = []
    normalized: list[dict[str, int]] = []
    for operation_index, operation in enumerate(validated_patch["operations"]):
        start = int(operation["start_char"])
        end = int(operation["end_char"])
        candidates = [
            span
            for span in spans
            if span["start_char"] <= start and end <= span["end_char"]
        ]
        if len(candidates) == 1:
            adjusted_operations.append(copy.deepcopy(operation))
            continue

        preceding = next(
            (
                span
                for position, span in enumerate(spans[:-1])
                if span["start_char"] < start < span["end_char"]
                and end == spans[position + 1]["start_char"]
                and span["end_char"] + 2 == spans[position + 1]["start_char"]
                and base[span["end_char"] : spans[position + 1]["start_char"]] == "\n\n"
                and str(operation["replacement"]) == ""
            ),
            None,
        )
        if preceding is None:
            # Do not normalize a patch which contains an actual cross-scene
            # rewrite.  The existing fail-closed evidence check will report it.
            return validated_patch, None
        adjusted = copy.deepcopy(operation)
        adjusted["end_char"] = preceding["end_char"]
        adjusted_operations.append(adjusted)
        normalized.append(
            {
                "operation_index": operation_index,
                "scene_index": preceding["index"],
                "separator_chars": 2,
            }
        )

    if not normalized:
        return validated_patch, None

    model_output, _ = apply_repair_patch(base, validated_patch)
    canonical_output = base
    for operation in reversed(adjusted_operations):
        start = int(operation["start_char"])
        end = int(operation["end_char"])
        canonical_output = (
            canonical_output[:start]
            + str(operation["replacement"])
            + canonical_output[end:]
        )
    canonical_operations = []
    for operation in adjusted_operations:
        normalized_operation = copy.deepcopy(operation)
        start = int(normalized_operation["start_char"])
        end = int(normalized_operation["end_char"])
        normalized_operation["expected_text_sha256"] = _sha256(base[start:end])
        canonical_operations.append(normalized_operation)
    canonical_patch = coerce_repair_patch(
        base,
        {
            "base_chapter_sha256": validated_patch["base_chapter_sha256"],
            "mode": "stage_text_diff",
            "operations": canonical_operations,
        },
    )
    if not _operations_are_scene_local(canonical_patch, spans):
        return validated_patch, None
    return canonical_patch, {
        "policy": "scene_separator_preservation_v1",
        "model_patch_sha256": validated_patch["patch_sha256"],
        "model_output_sha256": _sha256(model_output),
        "canonical_output_sha256": _sha256(canonical_output),
        "normalized_operations": normalized,
    }


def realign_scene_evidence(
    pipeline: dict[str, Any],
    *,
    before_chapter: str,
    after_chapter: str,
    patch: dict[str, Any],
    stage: str,
) -> dict[str, Any]:
    updated = copy.deepcopy(pipeline)
    drafts = [
        item
        for item in updated.get("scene_drafts") or []
        if isinstance(item, dict)
    ]
    spans = [
        item
        for item in updated.get("scene_spans") or []
        if isinstance(item, dict)
    ]
    if str(updated.get("merged_chapter") or "") != str(before_chapter):
        raise SceneEvidenceAlignmentError(
            "pipeline merged_chapter does not match the patch base",
            evidence={
                "pipeline_sha256": _sha256(str(updated.get("merged_chapter") or "")),
                "base_sha256": _sha256(str(before_chapter)),
            },
        )
    validated_patch = validate_repair_patch(patch, base_chapter=str(before_chapter))
    if len(drafts) != len(spans) or not drafts:
        raise SceneEvidenceAlignmentError(
            "scene drafts and spans are incomplete before alignment",
            evidence={"draft_count": len(drafts), "span_count": len(spans)},
        )
    span_by_index = {
        int(item.get("index") or 0): item
        for item in spans
        if int(item.get("index") or 0) > 0
    }
    operations_by_scene: dict[int, list[dict[str, Any]]] = {
        int(item.get("index") or position): []
        for position, item in enumerate(drafts, start=1)
    }
    for operation_index, operation in enumerate(validated_patch["operations"]):
        start = int(operation["start_char"])
        end = int(operation["end_char"])
        candidates = [
            index
            for index, span in span_by_index.items()
            if int(span.get("start_char") or 0) <= start
            and end <= int(span.get("end_char") or 0)
        ]
        if len(candidates) != 1:
            raise SceneEvidenceAlignmentError(
                "repair operation crosses a Scene boundary or separator",
                evidence={
                    "operation_index": operation_index,
                    "start_char": start,
                    "end_char": end,
                    "candidate_scene_indexes": candidates,
                },
            )
        operations_by_scene[candidates[0]].append(copy.deepcopy(operation))

    revised_drafts: list[dict[str, Any]] = []
    for position, draft in enumerate(drafts, start=1):
        index = int(draft.get("index") or position)
        span = span_by_index.get(index)
        if not isinstance(span, dict):
            raise SceneEvidenceAlignmentError(
                "scene draft has no matching span",
                evidence={"scene_index": index},
            )
        original = str(draft.get("text") or "")
        span_start = int(span.get("start_char") or 0)
        span_end = int(span.get("end_char") or 0)
        if str(before_chapter)[span_start:span_end] != original:
            raise SceneEvidenceAlignmentError(
                "scene evidence was already stale before repair",
                evidence={"scene_index": index, "start_char": span_start, "end_char": span_end},
            )
        revised = original
        local_operations = operations_by_scene.get(index, [])
        for operation in reversed(local_operations):
            local_start = int(operation["start_char"]) - span_start
            local_end = int(operation["end_char"]) - span_start
            revised = (
                revised[:local_start]
                + str(operation["replacement"])
                + revised[local_end:]
            )
        revised_draft = copy.deepcopy(draft)
        revised_draft["text"] = revised
        revised_draft["evidence_revision"] = int(draft.get("evidence_revision") or 0) + 1
        revised_draft["evidence_alignment"] = {
            "stage": stage,
            "modified": revised != original,
            "before_sha256": _sha256(original),
            "after_sha256": _sha256(revised),
            "patch_sha256": validated_patch["patch_sha256"],
            "operation_count": len(local_operations),
        }
        revised_drafts.append(revised_draft)

    rebuilt, rebuilt_spans = _merge_exact(revised_drafts)
    if rebuilt != str(after_chapter):
        raise SceneEvidenceAlignmentError(
            "Scene-local patch reconstruction differs from the repaired chapter",
            evidence={
                "rebuilt_sha256": _sha256(rebuilt),
                "repaired_sha256": _sha256(str(after_chapter)),
                "rebuilt_chars": len(rebuilt),
                "repaired_chars": len(str(after_chapter)),
            },
        )
    updated["scene_drafts"] = revised_drafts
    updated["scene_spans"] = rebuilt_spans
    updated["merged_chapter"] = rebuilt
    history = list(updated.get("scene_evidence_history") or [])
    history.append(
        {
            "stage": stage,
            "before_sha256": _sha256(str(before_chapter)),
            "after_sha256": _sha256(rebuilt),
            "patch_sha256": validated_patch["patch_sha256"],
            "operation_count": len(validated_patch["operations"]),
            "modified_scene_indexes": [
                int(item.get("index") or position)
                for position, item in enumerate(revised_drafts, start=1)
                if bool((item.get("evidence_alignment") or {}).get("modified"))
            ],
        }
    )
    updated["scene_evidence_history"] = history
    return updated


def _merge_exact(scene_drafts: list[dict[str, Any]]) -> tuple[str, list[dict[str, int]]]:
    texts = [str(item.get("text") or "") for item in scene_drafts]
    spans: list[dict[str, int]] = []
    cursor = 0
    for position, (draft, text) in enumerate(zip(scene_drafts, texts), start=1):
        if position > 1:
            cursor += 2
        start = cursor
        end = start + len(text)
        spans.append(
            {
                "index": int(draft.get("index") or position),
                "start_char": start,
                "end_char": end,
                "chars": len(text),
            }
        )
        cursor = end
    return "\n\n".join(texts), spans


def _canonicalizable_spans(
    pipeline: dict[str, Any] | None,
    *,
    before_chapter: str,
) -> list[dict[str, int]] | None:
    if not isinstance(pipeline, dict) or str(pipeline.get("merged_chapter") or "") != before_chapter:
        return None
    raw_spans = [item for item in pipeline.get("scene_spans") or [] if isinstance(item, dict)]
    if len(raw_spans) < 2:
        return None
    spans: list[dict[str, int]] = []
    seen_indexes: set[int] = set()
    for item in raw_spans:
        index = int(item.get("index") or 0)
        start = int(item.get("start_char") or 0)
        end = int(item.get("end_char") or 0)
        if index <= 0 or index in seen_indexes or start < 0 or end < start:
            return None
        seen_indexes.add(index)
        spans.append({"index": index, "start_char": start, "end_char": end})
    return sorted(spans, key=lambda item: item["start_char"])


def _operations_are_scene_local(
    patch: dict[str, Any],
    spans: list[dict[str, int]],
) -> bool:
    return all(
        sum(
            1
            for span in spans
            if span["start_char"] <= int(operation["start_char"])
            and int(operation["end_char"]) <= span["end_char"]
        )
        == 1
        for operation in patch["operations"]
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "SceneEvidenceAlignmentError",
    "canonicalize_scene_local_repair_patch",
    "realign_scene_evidence",
]
