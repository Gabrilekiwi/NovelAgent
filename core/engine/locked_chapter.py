from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from api.contracts import CHAPTER_CONTRACT, ModelOutputError, validate_language_output
from core.engine.locked_chapter_state import (
    LOCKED_CHAPTER_RESOLUTION_VERSION,
    active_locked_chapter_checkpoint,
    discarded_run_ids,
    resolved_execution_ids,
    validate_locked_chapter_resolution,
)
from core.engine.persistence import atomic_create_json, persistence_run_lock
from core.engine.run_record import validate_run_result
from core.memory_v2.canonical import canonical_json_hash
from core.model_calls import ModelCallStore, model_response_artifact_hash
from core.state.snapshot import load_snapshot
from core.story_project.paths import scan_prose_chapters


class LockedChapterRecoveryError(RuntimeError):
    """The locked chapter cannot be classified safely without user intervention."""


def recover_locked_chapter(
    *,
    story_project_root: str | Path,
    run_dir: str | Path,
    snapshot_path: str | Path,
    expected_book_id: str,
    language: str | None = None,
    manual_draft_path: str | Path | None = None,
    force_reset: bool = False,
    clock: Callable[[], datetime] | None = None,
    id_factory: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Classify one locked chapter and write an append-only recovery checkpoint.

    This operation never calls a model and never changes the chapter snapshot or
    formal prose.  It only marks unresolved executions as handled and preserves
    hash-verified output that the next normal run may reuse.
    """

    story_root = Path(story_project_root).resolve(strict=True)
    runtime_root = Path(run_dir).resolve()
    snapshot_target = Path(snapshot_path).resolve(strict=True)
    now = clock or (lambda: datetime.now(timezone.utc))
    make_id = id_factory or (lambda: uuid.uuid4().hex[:12])
    if force_reset and manual_draft_path is not None:
        raise LockedChapterRecoveryError(
            "forced reset cannot be combined with a manual recovery draft"
        )

    with persistence_run_lock(
        runtime_root,
        state_paths=(snapshot_target,),
        require_existing_root=True,
    ):
        snapshot = load_snapshot(snapshot_target)
        chapter_index = int(snapshot.get("chapter_index") or 1)
        committed_paths = scan_prose_chapters(story_root).get(chapter_index, ())
        if committed_paths:
            raise LockedChapterRecoveryError(
                f"chapter {chapter_index} already has formal prose; locked-chapter recovery refuses to modify it"
            )

        unresolved = _unresolved_executions(runtime_root)
        runs = _load_runs(runtime_root, expected_book_id=expected_book_id)
        active_checkpoint = active_locked_chapter_checkpoint(
            runtime_root,
            chapter_index=chapter_index,
            expected_book_id=expected_book_id,
        )
        if unresolved:
            owners = _bind_unresolved_to_runs(
                unresolved,
                runs,
                chapter_index=chapter_index,
                expected_book_id=expected_book_id,
            )
            source = max(owners, key=lambda item: _run_sort_key(item["payload"], item["path"]))
            source_payload = source["payload"]
            complete_draft = _usable_complete_draft(source_payload, language=language)
        elif force_reset and active_checkpoint is not None:
            source = next(
                (
                    item
                    for item in runs
                    if str(item["payload"]["run"].get("id"))
                    == str(active_checkpoint["source_run_id"])
                ),
                None,
            )
            if source is None:
                raise LockedChapterRecoveryError(
                    "the active recovery checkpoint source run is unavailable; refusing an unbound reset"
                )
            source_payload = source["payload"]
            complete_draft = None
        else:
            terminal_source = _newer_complete_failed_run(
                runs,
                chapter_index=chapter_index,
                active_checkpoint=active_checkpoint,
                language=language,
            )
            if terminal_source is None:
                return _already_recovered_result(chapter_index, active_checkpoint)
            source = terminal_source
            source_payload = source["payload"]
            complete_draft = source["complete_draft"]

        if not force_reset:
            execution_dir = _source_execution_dir(runtime_root, source)
            durable_transform = _latest_usable_complete_transform(
                execution_dir,
                language=language,
            )
            if durable_transform is not None:
                complete_draft = durable_transform
            else:
                complete_draft = _inherit_active_draft_provenance(
                    complete_draft,
                    active_checkpoint,
                )
        manual_draft = None
        if manual_draft_path is not None:
            manual_draft = _load_manual_recovery_draft(
                manual_draft_path,
                story_root=story_root,
                source_payload=source_payload,
                language=language,
            )
            complete_draft = manual_draft

        source_run = source_payload["run"]
        expected_scene_count = _expected_scene_count(source_run)
        if (
            not force_reset
            and active_checkpoint is not None
            and int(active_checkpoint["expected_scene_count"]) != expected_scene_count
        ):
            raise LockedChapterRecoveryError(
                "the existing recovery checkpoint no longer matches the chapter outline; reset it before reusing output"
            )

        recovered_scenes: list[dict[str, Any]] = []
        if force_reset:
            complete_draft = None
            action = "reset"
            reason = "operator_requested_reset"
        elif complete_draft is not None:
            action = "repair_draft"
            if manual_draft is not None:
                reason = "manual_repaired_draft_provided"
            elif complete_draft.get("source_stage") == "claude_polish":
                reason = "durable_polished_draft_available"
            else:
                reason = (
                    "complete_draft_available"
                    if unresolved
                    else "complete_failed_draft_available"
                )
        else:
            recovered_scenes = _recover_scene_prefix(
                source["execution_dir"],
                active_checkpoint=active_checkpoint,
                expected_scene_count=expected_scene_count,
                language=language,
            )
            if len(recovered_scenes) >= expected_scene_count:
                complete_draft = {
                    "text": "\n\n".join(scene["text"] for scene in recovered_scenes),
                    "problem_codes": [],
                    "source_stage": "chapter_generation",
                }
                complete_draft["sha256"] = _content_sha256(complete_draft["text"])
                recovered_scenes = []
                action = "repair_draft"
                reason = "complete_scene_prefix_available"
            elif recovered_scenes:
                action = "resume_scenes"
                reason = "contiguous_scene_prefix_available"
            else:
                action = "reset"
                reason = "no_trustworthy_content"

        all_uncommitted_current_runs = [
            str(item["payload"]["run"]["id"])
            for item in runs
            if item["payload"]["run"].get("committed") is not True
            and int(item["payload"]["run"].get("chapter_index") or 0) == chapter_index
        ]
        created_at = now().astimezone(timezone.utc)
        marker_id = f"resolution_{created_at.strftime('%Y%m%dT%H%M%S%fZ')}_{make_id()}"
        marker = {
            "schema_version": LOCKED_CHAPTER_RESOLUTION_VERSION,
            "id": marker_id,
            "created_at": created_at.isoformat(),
            "book_id": expected_book_id,
            "chapter_index": chapter_index,
            "action": action,
            "source_run_id": str(source_run["id"]),
            "resolved_execution_ids": sorted(item["execution_id"] for item in unresolved),
            "resolved_attempt_ids": sorted(
                call["attempt_id"] for item in unresolved for call in item["calls"]
            ),
            "discarded_run_ids": sorted(set(all_uncommitted_current_runs)) if action == "reset" else [],
            "expected_scene_count": expected_scene_count,
            "complete_draft": complete_draft,
            "scenes": recovered_scenes,
            "reason": reason,
        }
        marker["resolution_hash"] = canonical_json_hash(
            marker,
            exclude_fields=("resolution_hash",),
            exclude_environment_fields=False,
        )
        marker = validate_locked_chapter_resolution(marker)
        marker_path = runtime_root / "locked_chapter_resolutions" / f"{marker_id}.json"
        atomic_create_json(marker_path, marker)
        return _public_result(marker, marker_path)


def _load_manual_recovery_draft(
    path: str | Path,
    *,
    story_root: Path,
    source_payload: dict[str, Any],
    language: str | None,
) -> dict[str, Any]:
    candidate = Path(path).resolve(strict=True)
    if not candidate.is_file():
        raise LockedChapterRecoveryError("manual recovery draft must be a file")
    try:
        candidate.relative_to(story_root)
    except ValueError as exc:
        raise LockedChapterRecoveryError(
            "manual recovery draft must stay inside the StoryProject root"
        ) from exc
    try:
        raw = candidate.read_text(encoding="utf-8")
        text = validate_language_output(raw, CHAPTER_CONTRACT, language=language)
    except (OSError, UnicodeError, ModelOutputError) as exc:
        raise LockedChapterRecoveryError(
            f"manual recovery draft is not valid chapter prose: {exc}"
        ) from exc
    if len(text.strip()) < 500:
        raise LockedChapterRecoveryError(
            "manual recovery draft is too short to be treated as a complete chapter"
        )
    validation = source_payload.get("validation")
    problems = validation.get("problems") if isinstance(validation, dict) else []
    problem_codes = [
        str(item.get("code") or "")
        for item in problems or []
        if isinstance(item, dict) and str(item.get("code") or "").strip()
    ]
    return {
        "text": text,
        "sha256": _content_sha256(text),
        "problem_codes": list(dict.fromkeys(problem_codes)),
        # Preserve repaired-draft workflow semantics: skip generation/polish and
        # run a focused validation of the previously reported problem codes.
        "source_stage": "scene_repair",
    }


def _unresolved_executions(run_dir: Path) -> list[dict[str, Any]]:
    handled = resolved_execution_ids(run_dir)
    executions_root = run_dir / "executions"
    unresolved: list[dict[str, Any]] = []
    if not executions_root.is_dir():
        return unresolved
    for execution_dir in sorted(executions_root.glob("execution_*")):
        execution_id = execution_dir.name
        if execution_id in handled:
            continue
        model_root = execution_dir / "model_calls"
        if not model_root.is_dir():
            continue
        calls = ModelCallStore(model_root).list_uncertain_calls()
        if calls:
            unresolved.append(
                {
                    "execution_id": execution_id,
                    "execution_dir": execution_dir,
                    "calls": calls,
                }
            )
    return unresolved


def _load_runs(run_dir: Path, *, expected_book_id: str) -> list[dict[str, Any]]:
    ignored = discarded_run_ids(run_dir)
    results: list[dict[str, Any]] = []
    for path in sorted(run_dir.glob("chapter_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            validate_run_result(payload)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        run = payload["run"]
        if str(run.get("id") or "") in ignored:
            continue
        book_id = _run_book_id(run)
        if book_id is not None and book_id != expected_book_id:
            continue
        results.append({"path": path, "payload": payload})
    return results


def _newer_complete_failed_run(
    runs: list[dict[str, Any]],
    *,
    chapter_index: int,
    active_checkpoint: dict[str, Any] | None,
    language: str | None,
) -> dict[str, Any] | None:
    checkpoint_created_at = _parse_timestamp(
        active_checkpoint.get("created_at") if isinstance(active_checkpoint, dict) else None
    )
    checkpoint_source_run_id = (
        str(active_checkpoint.get("source_run_id") or "")
        if isinstance(active_checkpoint, dict)
        else ""
    )
    candidates = sorted(
        (
            item
            for item in runs
            if item["payload"]["run"].get("committed") is not True
            and item["payload"]["run"].get("status") == "failed"
            and int(item["payload"]["run"].get("chapter_index") or 0) == chapter_index
        ),
        key=lambda item: _run_sort_key(item["payload"], item["path"]),
        reverse=True,
    )
    for item in candidates:
        run = item["payload"]["run"]
        if str(run.get("id") or "") == checkpoint_source_run_id:
            continue
        run_finished_at = _parse_timestamp(run.get("finished_at") or run.get("started_at"))
        if (
            checkpoint_created_at is not None
            and run_finished_at is not None
            and run_finished_at <= checkpoint_created_at
        ):
            continue
        complete_draft = _usable_complete_draft(item["payload"], language=language)
        if complete_draft is not None:
            return {**item, "complete_draft": complete_draft}
    return None


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _bind_unresolved_to_runs(
    unresolved: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    *,
    chapter_index: int,
    expected_book_id: str,
) -> list[dict[str, Any]]:
    by_execution: dict[str, dict[str, Any]] = {}
    for item in runs:
        run = item["payload"]["run"]
        evidence = run.get("execution_evidence")
        execution_id = evidence.get("execution_id") if isinstance(evidence, dict) else None
        if isinstance(execution_id, str):
            by_execution[execution_id] = item

    owners: list[dict[str, Any]] = []
    for execution in unresolved:
        owner = by_execution.get(execution["execution_id"])
        if owner is None:
            raise LockedChapterRecoveryError(
                "an unresolved model execution has no durable chapter run; automatic recovery stopped safely"
            )
        run = owner["payload"]["run"]
        if run.get("committed") is True or int(run.get("chapter_index") or 0) != chapter_index:
            raise LockedChapterRecoveryError(
                "the lock does not belong exclusively to the current uncommitted chapter"
            )
        book_id = _run_book_id(run)
        if book_id is not None and book_id != expected_book_id:
            raise LockedChapterRecoveryError("the locked execution belongs to a different book")
        owners.append({**owner, **execution})
    return owners


def _usable_complete_draft(payload: dict[str, Any], *, language: str | None) -> dict[str, Any] | None:
    value = payload.get("chapter")
    if not isinstance(value, str) or len(value.strip()) < 500:
        return None
    try:
        text = validate_language_output(value, CHAPTER_CONTRACT, language=language)
    except ModelOutputError:
        return None
    validation = payload.get("validation")
    problem_codes = validation.get("problem_codes") if isinstance(validation, dict) else []
    return {
        "text": text,
        "sha256": _content_sha256(text),
        "problem_codes": [str(code) for code in problem_codes or [] if str(code).strip()],
        "source_stage": "chapter",
    }


def _source_execution_dir(run_dir: Path, source: dict[str, Any]) -> Path | None:
    value = source.get("execution_dir")
    if isinstance(value, Path) and value.is_dir():
        return value
    run = source.get("payload", {}).get("run", {})
    evidence = run.get("execution_evidence") if isinstance(run, dict) else None
    execution_id = evidence.get("execution_id") if isinstance(evidence, dict) else None
    if not isinstance(execution_id, str) or not execution_id.startswith("execution_"):
        return None
    candidate = run_dir / "executions" / execution_id
    return candidate if candidate.is_dir() else None


def _latest_usable_complete_transform(
    execution_dir: Path | None,
    *,
    language: str | None,
) -> dict[str, Any] | None:
    if execution_dir is None:
        return None
    store = ModelCallStore(execution_dir / "model_calls")
    if not store.intents_dir.is_dir():
        return None
    candidates: list[dict[str, Any]] = []
    for path in sorted(store.intents_dir.glob("*.json")):
        intent = store.load_intent(path.stem)
        if intent.get("stage") not in {"claude_polish", "scene_repair"}:
            continue
        attempt_id = str(intent["attempt_id"])
        if not store.has_receipt(attempt_id):
            continue
        receipt = store.load_receipt(attempt_id)
        if receipt.get("status") != "succeeded" or not receipt.get("response_artifact_ref"):
            continue
        candidates.append({"intent": intent, "receipt": receipt})
    candidates.sort(
        key=lambda item: (
            str(item["intent"].get("created_at") or ""),
            str(item["intent"].get("attempt_id") or ""),
        ),
        reverse=True,
    )
    for item in candidates:
        try:
            text = _read_verified_response(store, item["receipt"])
            text = validate_language_output(text, CHAPTER_CONTRACT, language=language)
        except (LockedChapterRecoveryError, ModelOutputError, OSError):
            continue
        if len(text.strip()) < 500:
            continue
        intent = item["intent"]
        return {
            "text": text,
            "sha256": _content_sha256(text),
            "problem_codes": [],
            "source_stage": str(intent["stage"]),
            "source_attempt_id": str(intent["attempt_id"]),
        }
    return None


def _inherit_active_draft_provenance(
    complete_draft: dict[str, Any] | None,
    active_checkpoint: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(complete_draft, dict) or not isinstance(active_checkpoint, dict):
        return complete_draft
    active_draft = active_checkpoint.get("complete_draft")
    if not isinstance(active_draft, dict):
        return complete_draft
    if active_draft.get("sha256") != complete_draft.get("sha256"):
        return complete_draft
    if active_draft.get("source_stage") not in {"claude_polish", "scene_repair"}:
        return complete_draft
    return dict(active_draft)


def _recover_scene_prefix(
    execution_dir: Path,
    *,
    active_checkpoint: dict[str, Any] | None,
    expected_scene_count: int,
    language: str | None,
) -> list[dict[str, Any]]:
    scenes = [dict(scene) for scene in (active_checkpoint or {}).get("scenes", [])]
    seen_attempts = {str(scene["source_attempt_id"]) for scene in scenes}
    store = ModelCallStore(execution_dir / "model_calls")
    intents: list[dict[str, Any]] = []
    if store.intents_dir.is_dir():
        for path in sorted(store.intents_dir.glob("*.json")):
            intent = store.load_intent(path.stem)
            if intent["stage"] == "chapter_generation":
                intents.append(intent)
    intents.sort(key=lambda item: (str(item["created_at"]), str(item["attempt_id"])))

    for intent in intents:
        attempt_id = str(intent["attempt_id"])
        if attempt_id in seen_attempts or not store.has_receipt(attempt_id):
            continue
        receipt = store.load_receipt(attempt_id)
        if receipt["status"] != "succeeded" or not receipt.get("response_artifact_ref"):
            continue
        try:
            text = _read_verified_response(store, receipt)
        except (LockedChapterRecoveryError, OSError):
            break
        try:
            text = validate_language_output(text, CHAPTER_CONTRACT, language=language)
        except ModelOutputError:
            break
        if len(text) < 100:
            break
        scene_index = len(scenes) + 1
        if scene_index > expected_scene_count:
            break
        scenes.append(
            {
                "index": scene_index,
                "text": text,
                "sha256": _content_sha256(text),
                "source_attempt_id": attempt_id,
            }
        )
        seen_attempts.add(attempt_id)
    return scenes


def _read_verified_response(store: ModelCallStore, receipt: dict[str, Any]) -> str:
    relative = Path(str(receipt["response_artifact_ref"]))
    path = (store.root / relative).resolve(strict=True)
    try:
        path.relative_to(store.root)
    except ValueError as exc:
        raise LockedChapterRecoveryError("model response artifact escaped its evidence directory") from exc
    raw = path.read_bytes()
    if model_response_artifact_hash(raw) != receipt["response_artifact_hash"]:
        raise LockedChapterRecoveryError("model response artifact hash mismatch")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LockedChapterRecoveryError("model response artifact is not UTF-8 text") from exc


def _expected_scene_count(run: dict[str, Any]) -> int:
    story_metadata = ((run.get("input_pack") or {}).get("metadata") or {}).get("story_project")
    count = story_metadata.get("required_beat_count") if isinstance(story_metadata, dict) else None
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise LockedChapterRecoveryError("locked run does not record a valid StoryProject scene count")
    return count


def _run_book_id(run: dict[str, Any]) -> str | None:
    direct = run.get("book_id")
    if isinstance(direct, str) and direct:
        return direct
    story = run.get("story_project")
    value = story.get("book_id") if isinstance(story, dict) else None
    return str(value) if isinstance(value, str) and value else None


def _run_sort_key(payload: dict[str, Any], path: Path) -> tuple[str, int]:
    run = payload["run"]
    return str(run.get("finished_at") or run.get("started_at") or ""), path.stat().st_mtime_ns


def _public_result(marker: dict[str, Any], marker_path: Path) -> dict[str, Any]:
    scene_count = len(marker["scenes"])
    return {
        "ok": True,
        "status": "recovered",
        "chapter_index": marker["chapter_index"],
        "action": marker["action"],
        "reason": marker["reason"],
        "reusable_scene_count": scene_count,
        "expected_scene_count": marker["expected_scene_count"],
        "next_scene_index": scene_count + 1 if marker["action"] == "resume_scenes" else None,
        "resolved_execution_count": len(marker["resolved_execution_ids"]),
        "source_run_id": marker["source_run_id"],
        "draft_stage": (
            marker["complete_draft"].get("source_stage")
            if isinstance(marker.get("complete_draft"), dict)
            else None
        ),
        "checkpoint_path": str(marker_path.resolve()),
        "provider_calls": 0,
    }


def _already_recovered_result(
    chapter_index: int,
    checkpoint: dict[str, Any] | None,
) -> dict[str, Any]:
    if checkpoint is None:
        return {
            "ok": True,
            "status": "not_locked",
            "chapter_index": chapter_index,
            "action": "none",
            "reusable_scene_count": 0,
            "expected_scene_count": 0,
            "next_scene_index": None,
            "resolved_execution_count": 0,
            "checkpoint_path": None,
            "provider_calls": 0,
        }
    result = _public_result(checkpoint, Path(checkpoint["_path"]))
    result["status"] = "already_recovered"
    result["resolved_execution_count"] = 0
    return result


def _content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "LockedChapterRecoveryError",
    "recover_locked_chapter",
]
