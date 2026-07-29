from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path, PurePosixPath
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Import the chapter pipeline before core.model_calls.  The API package exports
# the provider clients and contracts from one package initializer, so this is
# also the import order used by the production chapter workflow.
from modules.chapter_generator import pipeline as chapter_pipeline

from core.context_budget import ContextBudget, ContextBudgetError, default_context_budget
from core.model_calls import (
    ModelCallEvidenceError,
    ModelCallStore,
    build_scene_generation_call_id,
    model_call_receipt_hash,
    model_response_artifact_hash,
)
from core.prompt_compiler import compile_prompt_contexts
from core.state.authoritative_context import authoritative_state_from_markdown


REPLAY_SCHEMA_VERSION = "1.0"
REPLAY_PROVIDER = "openai"
REPLAY_MODEL = "gpt-5.5"
REPLAY_ENDPOINT_TYPE = "openai_compatible"


class ReplaySceneBudgetError(RuntimeError):
    """The persisted evidence is incomplete, inconsistent, or not replayable."""


class _RecordingBudget:
    """Delegate production admission while retaining the last candidate text."""

    def __init__(self, budget: ContextBudget) -> None:
        self.budget = budget
        self.last_text: str | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.budget, name)

    def measure(self, text: str, **kwargs: Any) -> dict[str, Any]:
        self.last_text = str(text)
        return self.budget.measure(text, **kwargs)

    def require_input(self, text: str, **kwargs: Any) -> dict[str, Any]:
        self.last_text = str(text)
        return self.budget.require_input(text, **kwargs)


def resolve_run_json_path(
    *,
    run_json: str | Path | None = None,
    run_dir: str | Path | None = None,
    run_id: str | None = None,
) -> Path:
    if run_json is not None:
        if run_dir is not None or run_id is not None:
            raise ReplaySceneBudgetError(
                "--run-json is mutually exclusive with --run-dir/--run-id"
            )
        path = Path(run_json).expanduser().resolve()
    else:
        if run_dir is None or not str(run_id or "").strip():
            raise ReplaySceneBudgetError(
                "provide either --run-json or both --run-dir and --run-id"
            )
        normalized_run_id = str(run_id).strip()
        if (
            Path(normalized_run_id).name != normalized_run_id
            or "/" in normalized_run_id
            or "\\" in normalized_run_id
        ):
            raise ReplaySceneBudgetError("run_id must be a filename-safe run identifier")
        path = (Path(run_dir).expanduser().resolve() / f"{normalized_run_id}.json")
    if not path.is_file():
        raise ReplaySceneBudgetError(f"run JSON does not exist: {path}")
    return path


def extract_input_pack_artifact(
    artifact_text: str,
    *,
    run_id: str,
    chapter_index: int,
    recorded_chars: int,
    artifact_recorded_chars: int,
) -> tuple[str, dict[str, Any]]:
    normalized = str(artifact_text).replace("\r\n", "\n").replace("\r", "\n")
    marker = "\n---\n\n"
    if marker not in normalized:
        raise ReplaySceneBudgetError("input-pack artifact wrapper separator is missing")
    header, logical_with_newline = normalized.split(marker, 1)
    required_header_lines = {
        f"# Input Pack: Chapter {int(chapter_index)}",
        f"- Run: `{run_id}`",
    }
    actual_header_lines = set(header.splitlines())
    missing = sorted(required_header_lines - actual_header_lines)
    if missing:
        raise ReplaySceneBudgetError(
            "input-pack artifact wrapper does not match the run record: "
            + ", ".join(missing)
        )
    if not logical_with_newline.endswith("\n"):
        raise ReplaySceneBudgetError(
            "input-pack artifact is missing its formatter-owned terminal newline"
        )
    logical = logical_with_newline[:-1]
    logical_chars = len(logical)
    for label, value in (
        ("run.input_pack.chars", recorded_chars),
        ("run.input_pack.artifact.chars", artifact_recorded_chars),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ReplaySceneBudgetError(f"{label} must be an integer")
        if value != logical_chars:
            raise ReplaySceneBudgetError(
                f"{label}={value} does not match extracted logical chars={logical_chars}"
            )
    return logical, {
        "artifact_chars": len(normalized),
        "logical_chars": logical_chars,
        "recorded_chars": recorded_chars,
        "artifact_recorded_chars": artifact_recorded_chars,
        "chars_match": True,
    }


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReplaySceneBudgetError(f"{label} is not readable JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ReplaySceneBudgetError(f"{label} must contain one JSON object")
    return value


def _run_record(payload: dict[str, Any]) -> dict[str, Any]:
    candidate = payload.get("run", payload)
    if not isinstance(candidate, dict):
        raise ReplaySceneBudgetError("run JSON has no run record")
    return candidate


def _required_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ReplaySceneBudgetError(f"{label} must be non-empty")
    return text


def _required_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReplaySceneBudgetError(f"{label} must be an integer")
    return value


def _input_artifact_path(run_dir: Path, run: dict[str, Any]) -> Path:
    input_summary = run.get("input_pack")
    if not isinstance(input_summary, dict):
        raise ReplaySceneBudgetError("run.input_pack is missing")
    artifact = input_summary.get("artifact")
    if not isinstance(artifact, dict):
        raise ReplaySceneBudgetError("run.input_pack.artifact is missing")
    recorded = str(artifact.get("path") or "").strip()
    candidates: list[Path] = []
    if recorded:
        recorded_path = Path(recorded).expanduser()
        candidates.append(
            recorded_path.resolve()
            if recorded_path.is_absolute()
            else (run_dir / recorded_path).resolve()
        )
        candidates.append((run_dir / "input_packs" / recorded_path.name).resolve())
    run_id = _required_text(run.get("id"), "run.id")
    chapter_index = _required_integer(run.get("chapter_index"), "run.chapter_index")
    candidates.append(
        (
            run_dir
            / "input_packs"
            / f"input_pack_{chapter_index:04d}_{run_id}.md"
        ).resolve()
    )
    for path in dict.fromkeys(candidates):
        if path.is_file():
            return path
    raise ReplaySceneBudgetError(
        "input-pack artifact cannot be resolved from the run record"
    )


def _safe_model_calls_root(run_dir: Path, run: dict[str, Any]) -> Path:
    evidence = run.get("execution_evidence")
    if not isinstance(evidence, dict):
        raise ReplaySceneBudgetError("run.execution_evidence is missing")
    ref = _required_text(
        evidence.get("model_calls_ref"),
        "run.execution_evidence.model_calls_ref",
    )
    relative = Path(ref)
    if relative.is_absolute() or ".." in relative.parts:
        raise ReplaySceneBudgetError("model_calls_ref must stay within the run directory")
    root = (run_dir / relative).resolve()
    try:
        root.relative_to(run_dir.resolve())
    except ValueError as exc:
        raise ReplaySceneBudgetError(
            "model_calls_ref resolves outside the run directory"
        ) from exc
    if not root.is_dir():
        raise ReplaySceneBudgetError(f"ModelCallStore does not exist: {root}")
    return root


def _safe_response_path(store: ModelCallStore, artifact_ref: Any) -> Path:
    ref = _required_text(artifact_ref, "scene 1 receipt.response_artifact_ref")
    if "\\" in ref or ":" in ref:
        raise ReplaySceneBudgetError("response_artifact_ref must be a relative POSIX path")
    relative = PurePosixPath(ref)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ReplaySceneBudgetError("response_artifact_ref is not a safe relative path")
    path = store.root.joinpath(*relative.parts).resolve()
    try:
        path.relative_to(store.root)
    except ValueError as exc:
        raise ReplaySceneBudgetError(
            "response_artifact_ref resolves outside ModelCallStore"
        ) from exc
    if not path.is_file():
        raise ReplaySceneBudgetError(f"scene 1 response artifact is missing: {path}")
    return path


def load_scene_one_evidence(
    *,
    run_dir: Path,
    run: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    store = ModelCallStore(_safe_model_calls_root(run_dir, run))
    expected_call_id = build_scene_generation_call_id(1)
    candidates: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
    if not store.intents_dir.is_dir():
        raise ReplaySceneBudgetError("ModelCallStore has no intent directory")
    for path in sorted(store.intents_dir.glob("*.json")):
        try:
            intent = store.load_intent(path.stem)
        except (OSError, ValueError, ModelCallEvidenceError) as exc:
            raise ReplaySceneBudgetError(
                f"model-call intent failed integrity validation: {path}"
            ) from exc
        if intent.get("call_id") != expected_call_id:
            continue
        attempt_id = _required_text(intent.get("attempt_id"), "scene 1 attempt_id")
        if not store.has_receipt(attempt_id):
            continue
        try:
            receipt = store.load_receipt(attempt_id)
        except (OSError, ValueError, ModelCallEvidenceError) as exc:
            raise ReplaySceneBudgetError(
                f"scene 1 receipt failed integrity validation: {attempt_id}"
            ) from exc
        if receipt.get("status") != "succeeded":
            continue
        candidates.append(
            (
                str(receipt.get("received_at") or ""),
                attempt_id,
                intent,
                receipt,
            )
        )
    if not candidates:
        raise ReplaySceneBudgetError(
            f"no succeeded Scene 1 receipt exists for call_id={expected_call_id}"
        )
    _received_at, attempt_id, intent, receipt = max(
        candidates,
        key=lambda item: (item[0], item[1]),
    )
    if intent.get("provider") != REPLAY_PROVIDER:
        raise ReplaySceneBudgetError("Scene 1 intent provider is not openai")
    if intent.get("model") != REPLAY_MODEL:
        raise ReplaySceneBudgetError("Scene 1 intent model is not gpt-5.5")
    if intent.get("stage") != "chapter_generation":
        raise ReplaySceneBudgetError("Scene 1 intent stage is not chapter_generation")
    if receipt.get("endpoint_type") != REPLAY_ENDPOINT_TYPE:
        raise ReplaySceneBudgetError(
            "Scene 1 receipt endpoint_type is not openai_compatible"
        )
    actual_model = str(receipt.get("actual_model") or "").strip()
    if actual_model and actual_model != REPLAY_MODEL:
        raise ReplaySceneBudgetError("Scene 1 receipt actual_model is not gpt-5.5")
    if model_call_receipt_hash(receipt) != receipt.get("receipt_hash"):
        raise ReplaySceneBudgetError("Scene 1 receipt_hash does not match its receipt")

    response_path = _safe_response_path(store, receipt.get("response_artifact_ref"))
    try:
        response_bytes = response_path.read_bytes()
    except OSError as exc:
        raise ReplaySceneBudgetError(
            f"Scene 1 response artifact is not readable: {response_path}"
        ) from exc
    actual_response_hash = model_response_artifact_hash(response_bytes)
    if actual_response_hash != receipt.get("response_artifact_hash"):
        raise ReplaySceneBudgetError(
            "Scene 1 response artifact hash does not match its receipt"
        )
    try:
        response_text = response_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReplaySceneBudgetError("Scene 1 response artifact is not UTF-8") from exc
    return response_text, {
        "call_id": expected_call_id,
        "attempt_id": attempt_id,
        "intent_hash": str(intent["intent_hash"]),
        "receipt_hash": str(receipt["receipt_hash"]),
        "response_artifact_hash": actual_response_hash,
        "response_artifact_ref": str(receipt["response_artifact_ref"]),
        "response_chars": len(response_text),
        "response_utf8_bytes": len(response_bytes),
        "integrity_verified": True,
    }


def _extract_section_object(input_pack: str, section_name: str) -> dict[str, Any]:
    value = chapter_pipeline._input_pack_json_section(input_pack, section_name)
    if not isinstance(value, dict):
        raise ReplaySceneBudgetError(
            f"input pack has no valid {section_name} JSON object"
        )
    return value


def _forbid_chat_completion(*_args: Any, **_kwargs: Any) -> str:
    raise ReplaySceneBudgetError(
        "offline Scene budget replay attempted a chat_completion"
    )


def _production_replay_budget() -> ContextBudget:
    return default_context_budget(
        provider=REPLAY_PROVIDER,
        model=REPLAY_MODEL,
        endpoint_type=REPLAY_ENDPOINT_TYPE,
        enable_model_tokenizer=False,
    )


def replay_scene_budget(run_json_path: str | Path) -> dict[str, Any]:
    run_path = Path(run_json_path).expanduser().resolve()
    payload = _load_json_object(run_path, label="run JSON")
    run = _run_record(payload)
    run_id = _required_text(run.get("id"), "run.id")
    chapter_index = _required_integer(run.get("chapter_index"), "run.chapter_index")
    run_dir = run_path.parent.resolve()

    input_summary = run.get("input_pack")
    artifact_summary = (
        input_summary.get("artifact")
        if isinstance(input_summary, dict)
        else None
    )
    if not isinstance(input_summary, dict) or not isinstance(artifact_summary, dict):
        raise ReplaySceneBudgetError("run.input_pack artifact metadata is incomplete")
    input_artifact_path = _input_artifact_path(run_dir, run)
    try:
        artifact_text = input_artifact_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ReplaySceneBudgetError(
            f"input-pack artifact is not readable UTF-8: {input_artifact_path}"
        ) from exc
    input_pack, input_pack_report = extract_input_pack_artifact(
        artifact_text,
        run_id=run_id,
        chapter_index=chapter_index,
        recorded_chars=_required_integer(
            input_summary.get("chars"),
            "run.input_pack.chars",
        ),
        artifact_recorded_chars=_required_integer(
            artifact_summary.get("chars"),
            "run.input_pack.artifact.chars",
        ),
    )

    response_text, scene_one_evidence = load_scene_one_evidence(
        run_dir=run_dir,
        run=run,
    )
    budget = _production_replay_budget()
    blueprint_section = _extract_section_object(
        input_pack,
        "StoryProject Chapter Blueprint",
    )
    blueprint = blueprint_section.get("chapter_blueprint")
    if not isinstance(blueprint, dict):
        raise ReplaySceneBudgetError(
            "StoryProject Chapter Blueprint has no chapter_blueprint object"
        )
    if blueprint.get("chapter_index") != chapter_index:
        raise ReplaySceneBudgetError(
            "chapter blueprint index does not match the run chapter index"
        )

    with patch.object(
        chapter_pipeline,
        "chat_completion",
        side_effect=_forbid_chat_completion,
    ) as completion_guard:
        contexts = compile_prompt_contexts(input_pack, budget=budget)
        plan = chapter_pipeline.plan_scenes(
            contexts.plan.text,
            chapter_index=chapter_index,
            chapter_blueprint=blueprint,
        )
        scenes = plan.get("scenes")
        if not isinstance(scenes, list) or len(scenes) < 2:
            raise ReplaySceneBudgetError(
                "chapter blueprint must produce at least two Scenes for this replay"
            )
        scene_one = scenes[0]
        scene_two = scenes[1]
        if not isinstance(scene_one, dict) or not isinstance(scene_two, dict):
            raise ReplaySceneBudgetError("chapter plan Scenes must be objects")

        scene_one_result = chapter_pipeline._load_scene_response(response_text)
        project_profile = _extract_section_object(input_pack, "Project Profile")
        language = str(project_profile.get("language") or "").strip() or None
        scene_one_prose = chapter_pipeline.validate_language_output(
            scene_one_result["prose"],
            chapter_pipeline.CHAPTER_CONTRACT,
            language=language,
        )
        state_before = chapter_pipeline._initial_scene_state(input_pack)
        boundary, state_after = chapter_pipeline.validate_scene_transition(
            scene_index=int(scene_one["index"]),
            state_before=state_before,
            events=scene_one_result["events"],
            deltas=scene_one_result["deltas"],
            prose=scene_one_prose,
            required_event_ids=scene_one.get("required_event_ids") or [],
            forbidden_event_ids=scene_one.get("forbidden_event_ids") or [],
            planned_events=scene_one.get("planned_events") or [],
        )
        if not boundary.get("accepted"):
            codes = [
                str(item.get("code") or "unknown")
                for item in boundary.get("findings") or []
                if isinstance(item, dict)
            ]
            raise ReplaySceneBudgetError(
                "persisted Scene 1 response cannot advance the boundary: "
                + ", ".join(codes)
            )

        required_beat_indexes = chapter_pipeline._scene_beat_indexes(scene_two)
        required_beats = [
            beat
            for beat in blueprint.get("required_beats") or []
            if isinstance(beat, dict)
            and int(beat.get("index") or 0) in required_beat_indexes
        ]
        prior_scene_summaries = [
            {
                "index": int(scene_one["index"]),
                "goal": str(scene_one["goal"]),
                "tail": scene_one_prose[-280:],
                "event_ids": [
                    str(event["event_id"])
                    for event in scene_one_result["events"]
                ],
            }
        ]
        authoritative_state = authoritative_state_from_markdown(input_pack)
        recording_budget = _RecordingBudget(budget)
        construction_error: dict[str, str] | None = None
        request_payload: str | None = None
        try:
            with patch.object(
                chapter_pipeline,
                "default_context_budget",
                return_value=recording_budget,
            ):
                request_payload = chapter_pipeline._scene_request_payload(
                    input_pack=contexts.scene.text,
                    plan=plan,
                    scene=scene_two,
                    scene_required_beats=required_beats,
                    blueprint=blueprint,
                    previous_scene_tail=scene_one_prose[-600:],
                    prior_scene_summaries=prior_scene_summaries,
                    scene_state=state_after,
                    authoritative_state_source=authoritative_state,
                )
        except ContextBudgetError as exc:
            request_payload = recording_budget.last_text
            construction_error = {
                "code": str(exc.code),
                "message": str(exc),
            }
        if request_payload is None:
            raise ReplaySceneBudgetError(
                "Scene 2 request construction produced no replayable payload"
            )
        try:
            decoded_request = json.loads(request_payload)
        except json.JSONDecodeError as exc:
            raise ReplaySceneBudgetError(
                "Scene 2 request is not compact transport JSON"
            ) from exc
        if not isinstance(decoded_request, dict):
            raise ReplaySceneBudgetError("Scene 2 request must be a JSON object")
        protocol_texts = (chapter_pipeline._load_scene_prompt(),)
        budget_report = budget.measure(
            request_payload,
            stage="scene",
            protocol_texts=protocol_texts,
        )
        safe_input_limit = chapter_pipeline._scene_safe_input_limit(budget)
        within_safe_limit = chapter_pipeline._scene_report_within_safe_limit(
            budget_report,
            safe_input_limit=safe_input_limit,
        )
        if completion_guard.call_count:
            raise ReplaySceneBudgetError(
                "offline replay performed an unexpected chat_completion"
            )

    budgeted_tokens = int(budget_report["budgeted_input_tokens"])
    safe_headroom = (
        int(safe_input_limit) - budgeted_tokens
        if isinstance(safe_input_limit, int)
        else None
    )
    hard_headroom = int(budget_report["hard_input_limit"]) - budgeted_tokens
    replay_safe = bool(budget_report["within_budget"] and within_safe_limit)
    return {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "mode": "offline_scene_budget_replay",
        "model_calls_performed": 0,
        "run": {
            "id": run_id,
            "chapter_index": chapter_index,
            "run_json": str(run_path),
            "run_dir": str(run_dir),
        },
        "input_pack": {
            "artifact_path": str(input_artifact_path),
            **input_pack_report,
        },
        "model_binding": {
            "provider": budget.provider,
            "model": budget.model,
            "endpoint_type": budget.endpoint_type,
            "enable_model_tokenizer": False,
            "count_mode": budget_report["count_mode"],
            "counter_version": budget_report["counter_version"],
        },
        "compile_context": {
            "context_digest": contexts.context_digest,
            "scene_chars": len(contexts.scene.text),
            "scene_selected_sections": list(contexts.scene.selected_sections),
            "scene_budget_report": contexts.scene.report,
        },
        "chapter_plan": {
            "scene_count": len(scenes),
            "scene_one_index": int(scene_one["index"]),
            "scene_two_index": int(scene_two["index"]),
        },
        "scene_one": {
            **scene_one_evidence,
            "boundary_accepted": True,
            "state_before_sha256": str(boundary["state_before_sha256"]),
            "state_after_sha256": str(boundary["state_after_sha256"]),
            "event_count": len(scene_one_result["events"]),
        },
        "scene_two": {
            "index": int(scene_two["index"]),
            "transport": "compact_json",
            "compact_request_chars": len(request_payload),
            "compact_request_utf8_bytes": len(request_payload.encode("utf-8")),
            "compact_request_sha256": hashlib.sha256(
                request_payload.encode("utf-8")
            ).hexdigest(),
            "raw_input_tokens": int(budget_report["raw_input_tokens"]),
            "budgeted_input_tokens": budgeted_tokens,
            "safe_input_limit": safe_input_limit,
            "hard_input_limit": int(budget_report["hard_input_limit"]),
            "safe_headroom_tokens": safe_headroom,
            "hard_headroom_tokens": hard_headroom,
            "within_safe_limit": bool(within_safe_limit),
            "within_hard_limit": bool(budget_report["within_budget"]),
            "construction_error": construction_error,
            "budget_report": budget_report,
        },
        "result": {
            "safe": replay_safe,
            "reason": (
                "scene_2_request_within_safe_and_hard_limits"
                if replay_safe
                else "scene_2_request_exceeds_replay_budget"
            ),
        },
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild the Scene 2 request from a persisted run and Scene 1 "
            "receipt without making any model call."
        )
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--run-json", help="Path to a persisted chapter run JSON.")
    source.add_argument(
        "--run-dir",
        help="Runtime runs directory containing <run-id>.json.",
    )
    parser.add_argument(
        "--run-id",
        help="Run identifier used with --run-dir.",
    )
    parser.add_argument(
        "--output-json",
        help="Optional report path. The report is always printed to stdout.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run_path = resolve_run_json_path(
            run_json=args.run_json,
            run_dir=args.run_dir,
            run_id=args.run_id,
        )
        report = replay_scene_budget(run_path)
    except (ReplaySceneBudgetError, ValueError) as exc:
        print(f"Scene budget replay failed: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output_json:
        output = Path(args.output_json).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["result"]["safe"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
