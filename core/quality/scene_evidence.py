from __future__ import annotations

import copy
import hashlib
from typing import Any

from core.quality.repair_patch import validate_repair_patch


class SceneEvidenceAlignmentError(ValueError):
    code = "stale_scene_evidence"

    def __init__(self, message: str, *, evidence: Any = None) -> None:
        self.evidence = copy.deepcopy(evidence)
        super().__init__(f"{self.code}: {message}")


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


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "SceneEvidenceAlignmentError",
    "realign_scene_evidence",
]
