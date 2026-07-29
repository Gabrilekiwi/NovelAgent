from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
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
from core.execution_provenance import (
    ExecutionProvenanceError,
    validate_execution_provenance,
)
from core.model_calls import (
    ModelCallEvidenceError,
    ModelCallStore,
    build_scene_generation_call_id,
    model_call_receipt_hash,
    model_response_artifact_hash,
)
from core.prompt_compiler import compile_prompt_contexts
from core.state.authoritative import (
    AuthoritativeStateError,
    validate_authoritative_state,
)
from core.state.authoritative_context import authoritative_state_from_markdown
from core.state.snapshot import SnapshotError, validate_snapshot


REPLAY_SCHEMA_VERSION = "1.0"
REPLAY_PROVIDER = "openai"
REPLAY_MODEL = "gpt-5.5"
REPLAY_ENDPOINT_TYPE = "openai_compatible"
HISTORICAL_SOURCE_COMMIT = "92573dfbf1ffb68884d4d917d32da32b15a520ae"
HISTORICAL_COVERAGE_PATH = "core/story_project/coverage.py"
HISTORICAL_COVERAGE_BLOB_ID = "039deb20f19836af1dbee54ff2a42a6f4b1f581a"
HISTORICAL_COVERAGE_SHA256 = (
    "7e1bacc6315d536a4e4452c22e8421b940b4a6092602add5a9df20031beb5bfc"
)
SYNTHETIC_PREVIOUS_SCENE_TAIL_CHARS = 600
SYNTHETIC_PRIOR_SUMMARY_TAIL_CHARS = 280
_RECORDED_HEADROOM_ERROR = re.compile(
    r"^(?P<code>story_project_context_headroom_exceeded): "
    r"scene input requires (?P<required>\d+) tokens; "
    r"safe target is (?P<safe>\d+); hard limit is (?P<hard>\d+)$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ReplaySceneBudgetError(RuntimeError):
    """The persisted evidence is incomplete, inconsistent, or not replayable."""


class _RecordingBudget:
    """Delegate production admission while retaining the last candidate text."""

    def __init__(self, budget: ContextBudget) -> None:
        self.budget = budget
        self.last_text: str | None = None
        self.measurements: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.budget, name)

    def measure(self, text: str, **kwargs: Any) -> dict[str, Any]:
        self.last_text = str(text)
        report = self.budget.measure(text, **kwargs)
        self.measurements.append(
            {
                "text": self.last_text,
                "report": dict(report),
            }
        )
        return report

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


def _current_snapshot_overlay(
    input_pack: str,
    *,
    snapshot_path: str | Path,
    chapter_index: int,
    run_book_id: Any = None,
) -> tuple[str, dict[str, Any]]:
    resolved = Path(snapshot_path).expanduser().resolve()
    if not resolved.is_file():
        raise ReplaySceneBudgetError(
            f"current snapshot does not exist: {resolved}"
        )
    try:
        raw_bytes = resolved.read_bytes()
        snapshot = json.loads(raw_bytes.decode("utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReplaySceneBudgetError(
            f"current snapshot is not readable UTF-8 JSON: {resolved}"
        ) from exc
    if not isinstance(snapshot, dict):
        raise ReplaySceneBudgetError("current snapshot must contain one JSON object")
    try:
        validate_snapshot(snapshot)
    except SnapshotError as exc:
        raise ReplaySceneBudgetError(
            f"current snapshot failed schema validation: {exc}"
        ) from exc
    snapshot_chapter = snapshot.get("chapter_index")
    if (
        isinstance(snapshot_chapter, bool)
        or not isinstance(snapshot_chapter, int)
        or snapshot_chapter != chapter_index
    ):
        raise ReplaySceneBudgetError(
            "current snapshot chapter_index does not match the run chapter index"
        )
    snapshot_book_id = snapshot.get("book_id")
    if not isinstance(run_book_id, str) or not run_book_id.strip():
        raise ReplaySceneBudgetError(
            "run StoryProject book_id is required for current snapshot overlay"
        )
    if not isinstance(snapshot_book_id, str) or not snapshot_book_id.strip():
        raise ReplaySceneBudgetError(
            "current snapshot book_id is required for identity verification"
        )
    if snapshot_book_id != run_book_id:
        raise ReplaySceneBudgetError(
            "current snapshot book_id does not match the run book_id"
        )
    authority = snapshot.get("authoritative_state")
    if not isinstance(authority, dict):
        raise ReplaySceneBudgetError(
            "current snapshot has no authoritative_state object"
        )
    try:
        validated_authority = validate_authoritative_state(authority)
    except AuthoritativeStateError as exc:
        raise ReplaySceneBudgetError(
            f"current snapshot authoritative_state is invalid: {exc}"
        ) from exc
    if validated_authority != authority:
        raise ReplaySceneBudgetError(
            "current snapshot authoritative_state is not canonical"
        )
    match = re.search(
        r"(?ms)^# Authoritative State[ \t]*\r?\n.*?(?=^# |\Z)",
        str(input_pack or ""),
    )
    if match is None:
        raise ReplaySceneBudgetError(
            "persisted input pack has no Authoritative State section to overlay"
        )
    canonical_authority = json.dumps(
        authority,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    replacement = "# Authoritative State\n" + canonical_authority
    suffix = str(input_pack or "")[match.end() :]
    overlaid = (
        str(input_pack or "")[: match.start()]
        + replacement
        + ("\n\n" if suffix else "")
        + suffix
    )
    roster_counts = {
        str(roster_id): record.get("computed_count")
        for roster_id, record in (authority.get("roster") or {}).items()
        if isinstance(record, dict)
    }
    return overlaid, {
        "source_kind": "current_snapshot_authoritative_state",
        "recorded": False,
        "snapshot_path": str(resolved),
        "snapshot_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "snapshot_chapter_index": snapshot_chapter,
        "snapshot_book_id": snapshot_book_id,
        "authoritative_state_sha256": hashlib.sha256(
            canonical_authority.encode("utf-8")
        ).hexdigest(),
        "snapshot_schema_valid": True,
        "authoritative_state_valid": True,
        "overlay_scope": "Authoritative State section only",
        "roster_counts": roster_counts,
    }


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReplaySceneBudgetError(f"{label} is not readable JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ReplaySceneBudgetError(f"{label} must contain one JSON object")
    return value


def _load_bound_run_json(
    path: Path,
    *,
    expected_sha256: str | None,
    recorded_sha256: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Verify the run artifact bytes before decoding or consulting its fields."""

    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise ReplaySceneBudgetError(f"run JSON is not readable: {path}") from exc
    actual_sha256 = hashlib.sha256(raw_bytes).hexdigest()

    caller_sha256 = str(expected_sha256 or "").strip().lower()
    trusted_recorded_sha256 = str(recorded_sha256 or "").strip().lower()
    if trusted_recorded_sha256:
        if _SHA256.fullmatch(trusted_recorded_sha256) is None:
            raise ReplaySceneBudgetError(
                "recorded run JSON sha256 must be a lowercase SHA-256 digest"
            )
        if caller_sha256:
            if _SHA256.fullmatch(caller_sha256) is None:
                raise ReplaySceneBudgetError(
                    "caller-pinned run JSON sha256 must be a lowercase SHA-256 digest"
                )
            if caller_sha256 != trusted_recorded_sha256:
                raise ReplaySceneBudgetError(
                    "caller-pinned run JSON sha256 conflicts with the recorded digest"
                )
        bound_sha256 = trusted_recorded_sha256
        source = "recorded_run_json_sha256"
        recorded = True
    else:
        if _SHA256.fullmatch(caller_sha256) is None:
            raise ReplaySceneBudgetError(
                "legacy run JSON has no trusted recorded sha256; provide "
                "--run-json-sha256 with an explicit caller-pinned digest"
            )
        bound_sha256 = caller_sha256
        source = "caller_pinned_legacy_run_json_sha256"
        recorded = False

    if actual_sha256 != bound_sha256:
        raise ReplaySceneBudgetError(
            "run JSON sha256 does not match its evidence binding"
        )

    try:
        payload = json.loads(raw_bytes.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReplaySceneBudgetError(
            f"run JSON is not readable UTF-8 JSON: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise ReplaySceneBudgetError("run JSON must contain one JSON object")
    return payload, {
        "expected_sha256": bound_sha256,
        "actual_sha256": actual_sha256,
        "source": source,
        "recorded": recorded,
        "verified": True,
    }


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
    run_id = _required_text(run.get("id"), "run.id")
    chapter_index = _required_integer(run.get("chapter_index"), "run.chapter_index")
    return _safe_run_artifact_path(
        run_dir,
        artifact.get("path"),
        label="run.input_pack.artifact.path",
        fallback=(
            run_dir
            / "input_packs"
            / f"input_pack_{chapter_index:04d}_{run_id}.md"
        ),
    )


def _safe_run_artifact_path(
    run_dir: Path,
    artifact_ref: Any,
    *,
    label: str,
    fallback: Path | None = None,
) -> Path:
    try:
        run_root = run_dir.resolve(strict=True)
    except OSError as exc:
        raise ReplaySceneBudgetError(
            f"{label} run directory is unavailable"
        ) from exc
    recorded = _required_text(artifact_ref, label)
    recorded_path = Path(recorded).expanduser()
    candidates = [
        (
            recorded_path.resolve()
            if recorded_path.is_absolute()
            else (run_root / recorded_path).resolve()
        )
    ]
    if fallback is not None:
        candidates.append(fallback.resolve())
    for path in dict.fromkeys(candidates):
        try:
            path.relative_to(run_root)
        except ValueError as exc:
            raise ReplaySceneBudgetError(
                f"{label} escapes the run directory"
            ) from exc
        if path.is_file():
            return path
    raise ReplaySceneBudgetError(f"{label} cannot be resolved")


def _sha256_text(value: Any, label: str) -> str:
    digest = _required_text(value, label).lower()
    if len(digest) != 64 or any(
        character not in "0123456789abcdef"
        for character in digest
    ):
        raise ReplaySceneBudgetError(f"{label} must be a lowercase SHA-256 digest")
    return digest


def _recorded_context_failure(run: dict[str, Any]) -> dict[str, Any]:
    if run.get("status") != "failed" or run.get("committed") is not False:
        raise ReplaySceneBudgetError(
            "run must record an uncommitted failed context-budget execution"
        )
    error = run.get("error")
    if not isinstance(error, dict):
        raise ReplaySceneBudgetError("run.error is missing")
    error_type = _required_text(error.get("type"), "run.error.type")
    if error_type != "ContextBudgetError":
        raise ReplaySceneBudgetError(
            "run.error.type is not ContextBudgetError"
        )
    message = _required_text(error.get("message"), "run.error.message")
    match = _RECORDED_HEADROOM_ERROR.fullmatch(message)
    if match is None:
        raise ReplaySceneBudgetError(
            "run.error.message is not the expected Scene headroom failure"
        )
    required = int(match.group("required"))
    safe = int(match.group("safe"))
    hard = int(match.group("hard"))
    if not (safe < required <= hard):
        raise ReplaySceneBudgetError(
            "recorded Scene headroom failure has inconsistent token limits"
        )
    return {
        "evidence_kind": "run_recorded_error",
        "recorded": True,
        "error_type": error_type,
        "error_code": match.group("code"),
        "message": message,
        "required_input_tokens": required,
        "safe_input_limit": safe,
        "hard_input_limit": hard,
        "safe_limit_excess_tokens": required - safe,
        "hard_limit_headroom_tokens": hard - required,
        "integrity_scope": (
            "the persisted run record; the rejected Scene request payload "
            "was not persisted"
        ),
    }


def _persisted_plan_metadata(run: dict[str, Any]) -> dict[str, Any] | None:
    chapter = run.get("chapter")
    if not isinstance(chapter, dict):
        return None
    pipeline = chapter.get("pipeline")
    if not isinstance(pipeline, dict):
        return None
    artifacts = pipeline.get("artifacts")
    if not isinstance(artifacts, dict):
        return None
    plan = artifacts.get("plan")
    if plan is None:
        return None
    if not isinstance(plan, dict):
        raise ReplaySceneBudgetError(
            "run.chapter.pipeline.artifacts.plan must be an artifact object"
        )
    return plan


def _validate_recorded_plan(
    value: dict[str, Any],
    *,
    chapter_index: int,
) -> dict[str, Any]:
    scenes = value.get("scenes")
    if not isinstance(scenes, list) or len(scenes) < 2:
        raise ReplaySceneBudgetError(
            "persisted pipeline plan must contain at least two Scenes"
        )
    normalized = chapter_pipeline._validate_plan(
        value,
        chapter_index=chapter_index,
    )
    if normalized != value:
        raise ReplaySceneBudgetError(
            "persisted pipeline plan is not a complete normalized production plan"
        )
    expected_indexes = list(range(1, len(scenes) + 1))
    actual_indexes = [
        int(scene.get("index") or 0)
        for scene in scenes
        if isinstance(scene, dict)
    ]
    if actual_indexes != expected_indexes:
        raise ReplaySceneBudgetError(
            "persisted pipeline plan Scene indexes are not contiguous"
        )
    return normalized


def _load_persisted_plan(
    run_dir: Path,
    run: dict[str, Any],
    *,
    chapter_index: int,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    metadata = _persisted_plan_metadata(run)
    if metadata is None:
        return None
    expected_hash = _sha256_text(
        metadata.get("sha256"),
        "run.chapter.pipeline.artifacts.plan.sha256",
    )
    run_id = _required_text(run.get("id"), "run.id")
    path = _safe_run_artifact_path(
        run_dir,
        metadata.get("path"),
        label="run.chapter.pipeline.artifacts.plan.path",
        fallback=(
            run_dir
            / "chapter_pipeline"
            / f"chapter_plan_{chapter_index:04d}_{run_id}.json"
        ),
    )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ReplaySceneBudgetError(
            f"persisted pipeline plan is not readable: {path}"
        ) from exc
    actual_hash = hashlib.sha256(raw).hexdigest()
    if actual_hash != expected_hash:
        raise ReplaySceneBudgetError(
            "persisted pipeline plan artifact hash does not match run metadata"
        )
    try:
        text = raw.decode("utf-8-sig")
        value = json.loads(text)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReplaySceneBudgetError(
            "persisted pipeline plan artifact is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(value, dict):
        raise ReplaySceneBudgetError(
            "persisted pipeline plan artifact must contain one JSON object"
        )
    normalized_chars = len(text.replace("\r\n", "\n").replace("\r", "\n"))
    recorded_chars = _required_integer(
        metadata.get("chars"),
        "run.chapter.pipeline.artifacts.plan.chars",
    )
    if normalized_chars != recorded_chars:
        raise ReplaySceneBudgetError(
            "persisted pipeline plan artifact chars do not match run metadata"
        )
    plan = _validate_recorded_plan(value, chapter_index=chapter_index)
    return plan, {
        "evidence_kind": "persisted_plan_artifact",
        "recorded": True,
        "artifact_path": str(path),
        "artifact_sha256": actual_hash,
        "artifact_chars": normalized_chars,
        "integrity_verified": True,
    }


def _git_bytes(arguments: list[str]) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=ROOT,
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReplaySceneBudgetError(
            "git is unavailable; source revision reconstruction cannot be verified"
        ) from exc
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ReplaySceneBudgetError(
            "git could not verify the historical source revision"
            + (f": {message}" if message else "")
        )
    return completed.stdout


def _load_source_revision_evidence(
    run_dir: Path,
    run: dict[str, Any],
) -> dict[str, Any]:
    evidence = run.get("execution_evidence")
    if not isinstance(evidence, dict):
        raise ReplaySceneBudgetError("run.execution_evidence is missing")
    provenance_path = _safe_run_artifact_path(
        run_dir,
        evidence.get("provenance_artifact_ref"),
        label="run.execution_evidence.provenance_artifact_ref",
    )
    provenance_value = _load_json_object(
        provenance_path,
        label="execution provenance artifact",
    )
    try:
        provenance = validate_execution_provenance(provenance_value)
    except ExecutionProvenanceError as exc:
        raise ReplaySceneBudgetError(
            "execution provenance failed canonical integrity validation"
        ) from exc
    expected_provenance_hash = _sha256_text(
        evidence.get("provenance_hash"),
        "run.execution_evidence.provenance_hash",
    )
    if provenance["provenance_hash"] != expected_provenance_hash:
        raise ReplaySceneBudgetError(
            "execution provenance hash does not match the run record"
        )
    git_evidence = provenance["code"]["git"]
    if git_evidence["dirty"]:
        raise ReplaySceneBudgetError(
            "dirty source provenance cannot authorize plan reconstruction"
        )
    if git_evidence["commit"] != HISTORICAL_SOURCE_COMMIT:
        raise ReplaySceneBudgetError(
            "source revision does not match the audited historical plan revision"
        )

    source_bytes = _git_bytes(
        ["show", f"{HISTORICAL_SOURCE_COMMIT}:{HISTORICAL_COVERAGE_PATH}"]
    )
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    if source_sha256 != HISTORICAL_COVERAGE_SHA256:
        raise ReplaySceneBudgetError(
            "historical coverage.py source hash does not match the audited revision"
        )
    blob_id = _git_bytes(
        [
            "rev-parse",
            f"{HISTORICAL_SOURCE_COMMIT}:{HISTORICAL_COVERAGE_PATH}",
        ]
    ).decode("ascii", errors="strict").strip()
    if blob_id != HISTORICAL_COVERAGE_BLOB_ID:
        raise ReplaySceneBudgetError(
            "historical coverage.py Git blob id does not match the audited revision"
        )
    return {
        "evidence_kind": "source_revision_reconstruction",
        "recorded": False,
        "reconstruction_label": "reconstructed_not_recorded",
        "provenance_artifact_path": str(provenance_path),
        "provenance_hash": provenance["provenance_hash"],
        "source_commit": git_evidence["commit"],
        "source_dirty": git_evidence["dirty"],
        "source_path": HISTORICAL_COVERAGE_PATH,
        "source_blob_id": blob_id,
        "source_sha256": source_sha256,
        "integrity_verified": True,
    }


def _normalized_blueprint_beats(blueprint: dict[str, Any]) -> list[dict[str, Any]]:
    beats: list[dict[str, Any]] = []
    for position, raw in enumerate(blueprint.get("required_beats") or [], start=1):
        if isinstance(raw, dict):
            text = str(raw.get("text") or "").strip()
            raw_index = raw.get("index")
            index = (
                raw_index
                if isinstance(raw_index, int) and not isinstance(raw_index, bool)
                else position
            )
        else:
            text = str(raw).strip()
            index = position
        if text:
            beats.append({"index": int(index), "text": text})
    if not beats:
        raise ReplaySceneBudgetError(
            "chapter blueprint has no beats for source revision reconstruction"
        )
    return beats


def _reconstruct_historical_plan(
    blueprint: dict[str, Any],
    *,
    chapter_index: int,
) -> dict[str, Any]:
    beats = _normalized_blueprint_beats(blueprint)
    title = str(blueprint.get("title") or "").strip()
    prefix = f"{title}: " if title else ""
    raw_plan = {
        "goal": str(
            blueprint.get("core_event")
            or blueprint.get("title")
            or "Follow StoryProject blueprint."
        ),
        "scenes": [
            {
                "index": position,
                "type": "story_project_blueprint",
                "goal": (
                    f"{prefix}Cover StoryProject required beat group {position}: "
                    + str(beat["text"])
                ),
                "required_beats": [str(beat["text"])],
                "required_beat_indexes": [int(beat["index"])],
            }
            for position, beat in enumerate(beats, start=1)
        ],
    }
    return chapter_pipeline._validate_plan(
        raw_plan,
        chapter_index=chapter_index,
    )


def _historical_plan(
    run_dir: Path,
    run: dict[str, Any],
    blueprint: dict[str, Any],
    *,
    chapter_index: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    persisted = _load_persisted_plan(
        run_dir,
        run,
        chapter_index=chapter_index,
    )
    if persisted is not None:
        return persisted
    source_evidence = _load_source_revision_evidence(run_dir, run)
    return (
        _reconstruct_historical_plan(
            blueprint,
            chapter_index=chapter_index,
        ),
        {
            **source_evidence,
            "reconstruction_rule": "scene_count=len(beats); one ordered beat per Scene",
        },
    )


def _plan_summary(plan: dict[str, Any]) -> dict[str, Any]:
    scenes = [
        scene
        for scene in plan.get("scenes") or []
        if isinstance(scene, dict)
    ]
    groups = [
        [int(value) for value in scene.get("required_beat_indexes") or []]
        for scene in scenes
    ]
    flattened = [value for group in groups for value in group]
    return {
        "scene_count": len(scenes),
        "beat_index_groups": groups,
        "flattened_beat_indexes": flattened,
        "beat_indexes_unique": len(flattened) == len(set(flattened)),
        "scene_indexes": [int(scene.get("index") or 0) for scene in scenes],
    }


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


def _scene_required_beats(
    scene: dict[str, Any],
    blueprint: dict[str, Any],
) -> list[dict[str, Any]]:
    indexes = set(chapter_pipeline._scene_beat_indexes(scene))
    return [
        beat
        for beat in blueprint.get("required_beats") or []
        if isinstance(beat, dict)
        and int(beat.get("index") or 0) in indexes
    ]


def _build_scene_request_replay(
    *,
    budget: ContextBudget,
    input_pack: str,
    plan: dict[str, Any],
    scene: dict[str, Any],
    blueprint: dict[str, Any],
    previous_scene_tail: str,
    prior_scene_summaries: list[dict[str, Any]],
    scene_state: dict[str, Any],
    authoritative_state_source: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
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
                input_pack=input_pack,
                plan=plan,
                scene=scene,
                scene_required_beats=_scene_required_beats(scene, blueprint),
                blueprint=blueprint,
                previous_scene_tail=previous_scene_tail,
                prior_scene_summaries=prior_scene_summaries,
                scene_state=scene_state,
                authoritative_state_source=authoritative_state_source,
            )
    except ContextBudgetError as exc:
        request_payload = recording_budget.last_text
        construction_error = {
            "code": str(exc.code),
            "message": str(exc),
        }
    if request_payload is None:
        raise ReplaySceneBudgetError(
            f"Scene {int(scene.get('index') or 0)} request construction "
            "produced no replayable payload"
        )
    try:
        decoded_request = json.loads(request_payload)
    except json.JSONDecodeError as exc:
        raise ReplaySceneBudgetError(
            f"Scene {int(scene.get('index') or 0)} request is not compact transport JSON"
        ) from exc
    if not isinstance(decoded_request, dict):
        raise ReplaySceneBudgetError("Scene request must be a JSON object")
    pretty_payload = json.dumps(
        decoded_request,
        ensure_ascii=False,
        indent=2,
    )
    if json.loads(pretty_payload) != decoded_request:
        raise ReplaySceneBudgetError(
            "Scene request transport compaction changed JSON semantics"
        )
    protocol_texts = (chapter_pipeline._load_scene_prompt(),)
    budget_report = budget.measure(
        request_payload,
        stage="scene",
        protocol_texts=protocol_texts,
    )
    pretty_budget_report = budget.measure(
        pretty_payload,
        stage="scene",
        protocol_texts=protocol_texts,
    )
    safe_input_limit = chapter_pipeline._scene_safe_input_limit(budget)
    within_safe_limit = chapter_pipeline._scene_report_within_safe_limit(
        budget_report,
        safe_input_limit=safe_input_limit,
    )
    budgeted_tokens = int(budget_report["budgeted_input_tokens"])
    return request_payload, {
        "index": int(scene["index"]),
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
        "safe_headroom_tokens": (
            int(safe_input_limit) - budgeted_tokens
            if isinstance(safe_input_limit, int)
            else None
        ),
        "hard_headroom_tokens": (
            int(budget_report["hard_input_limit"]) - budgeted_tokens
        ),
        "within_safe_limit": bool(within_safe_limit),
        "within_hard_limit": bool(budget_report["within_budget"]),
        "construction_error": construction_error,
        "transport_compaction": {
            "semantic_json_equal": True,
            "compact_chars": len(request_payload),
            "pretty_chars": len(pretty_payload),
            "chars_saved": len(pretty_payload) - len(request_payload),
            "compact_budgeted_input_tokens": budgeted_tokens,
            "pretty_budgeted_input_tokens": int(
                pretty_budget_report["budgeted_input_tokens"]
            ),
            "budgeted_input_tokens_saved": (
                int(pretty_budget_report["budgeted_input_tokens"])
                - budgeted_tokens
            ),
        },
        "budget_report": budget_report,
    }


def _fixed_synthetic_text(label: str, chars: int) -> str:
    marker = chr(0x4E00 + (sum(ord(character) for character in label) % 1_000))
    unit = "\u8fde\u7eed\u573a\u666f" + marker
    return (unit * ((max(0, chars) // len(unit)) + 1))[: max(0, chars)]


def _synthetic_completed_scene_events(
    scene: dict[str, Any],
) -> list[dict[str, Any]]:
    planned_by_id = {
        str(event.get("event_id") or ""): event
        for event in scene.get("planned_events") or []
        if isinstance(event, dict) and str(event.get("event_id") or "")
    }
    events: list[dict[str, Any]] = []
    for event_id in scene.get("required_event_ids") or []:
        normalized_id = str(event_id)
        planned = planned_by_id.get(normalized_id)
        if not isinstance(planned, dict):
            raise ReplaySceneBudgetError(
                "synthetic continuity preflight requires a planned event for "
                f"{normalized_id}"
            )
        events.append(
            {
                "event_id": normalized_id,
                "type": str(planned.get("type") or ""),
                "subjects": [
                    str(value)
                    for value in planned.get("subjects") or []
                    if str(value)
                ],
                "objects": [
                    str(value)
                    for value in planned.get("objects") or []
                    if str(value)
                ],
                "location": str(planned.get("location") or ""),
                "status": "completed",
            }
        )
    return events


def _synthetic_bounded_current_plan_preflight(
    *,
    budget: ContextBudget,
    input_pack: str,
    plan: dict[str, Any],
    blueprint: dict[str, Any],
    initial_state: dict[str, Any],
    authoritative_state_source: dict[str, Any] | None,
) -> dict[str, Any]:
    scenes = [
        scene
        for scene in plan.get("scenes") or []
        if isinstance(scene, dict)
    ]
    if not scenes:
        raise ReplaySceneBudgetError(
            "synthetic continuity preflight requires at least one current Scene"
        )
    scene_state = chapter_pipeline.normalize_scene_state(initial_state)
    prior_summaries: list[dict[str, Any]] = []
    accumulated_required_event_ids: list[str] = []
    scene_budgets: list[dict[str, Any]] = []
    empty_deltas = {
        key: []
        for key in (
            "characters",
            "relationships",
            "rosters",
            "locations",
            "inventory",
            "counters",
        )
    }

    for scene in scenes:
        scene_index = int(scene.get("index") or 0)
        previous_tail = _fixed_synthetic_text(
            f"scene-{scene_index}-previous-tail",
            SYNTHETIC_PREVIOUS_SCENE_TAIL_CHARS,
        )
        summaries_for_request = [
            {
                **summary,
                "event_ids": list(summary.get("event_ids") or []),
            }
            for summary in prior_summaries
        ]
        request_payload, scene_budget = _build_scene_request_replay(
            budget=budget,
            input_pack=input_pack,
            plan=plan,
            scene=scene,
            blueprint=blueprint,
            previous_scene_tail=previous_tail,
            prior_scene_summaries=summaries_for_request,
            scene_state=scene_state,
            authoritative_state_source=authoritative_state_source,
        )
        request = json.loads(request_payload)
        transported_summaries = [
            summary
            for summary in request.get("prior_scene_summaries") or []
            if isinstance(summary, dict)
        ]
        transported_state = request.get("current_scene_state")
        if not isinstance(transported_state, dict):
            raise ReplaySceneBudgetError(
                "synthetic continuity preflight request omitted current_scene_state"
            )
        required_event_ids = [
            str(event_id)
            for event_id in scene.get("required_event_ids") or []
        ]
        synthetic_events = _synthetic_completed_scene_events(scene)
        boundary, state_after = chapter_pipeline.validate_scene_transition(
            scene_index=scene_index,
            state_before=scene_state,
            events=synthetic_events,
            deltas=empty_deltas,
            prose=_fixed_synthetic_text(
                f"scene-{scene_index}-completed-prose",
                SYNTHETIC_PREVIOUS_SCENE_TAIL_CHARS,
            ),
            required_event_ids=required_event_ids,
            forbidden_event_ids=scene.get("forbidden_event_ids") or [],
            planned_events=scene.get("planned_events") or [],
        )
        if not boundary.get("accepted"):
            codes = [
                str(finding.get("code") or "unknown")
                for finding in boundary.get("findings") or []
                if isinstance(finding, dict)
            ]
            raise ReplaySceneBudgetError(
                "synthetic continuity preflight could not advance Scene "
                f"{scene_index}: {', '.join(codes)}"
            )
        state_before_summary = chapter_pipeline.scene_state_summary(scene_state)
        state_after_summary = chapter_pipeline.scene_state_summary(state_after)
        scene_budget["synthetic_continuity"] = {
            "recorded": False,
            "previous_scene_tail_input_chars": len(previous_tail),
            "previous_scene_tail_transport_chars": len(
                str(request.get("previous_scene_tail") or "")
            ),
            "prior_scene_summary_count_input": len(summaries_for_request),
            "prior_scene_summary_count_transport": len(transported_summaries),
            "prior_scene_summary_tail_input_chars": [
                len(str(summary.get("tail") or ""))
                for summary in summaries_for_request
            ],
            "prior_scene_summary_tail_transport_chars": [
                len(str(summary.get("tail") or ""))
                for summary in transported_summaries
            ],
            "accumulated_required_event_ids_before": list(
                accumulated_required_event_ids
            ),
            "current_required_event_ids": required_event_ids,
            "completed_event_ids_count_before": int(
                state_before_summary["completed_event_ids_count"]
            ),
            "completed_event_ids_count_transport": int(
                transported_state.get("completed_event_ids_count") or 0
            ),
            "completed_event_ids_count_after": int(
                state_after_summary["completed_event_ids_count"]
            ),
            "completed_events_count_before": len(
                state_before_summary.get("completed_events") or []
            ),
            "completed_events_count_transport": len(
                transported_state.get("completed_events") or []
            ),
            "completed_events_count_after": len(
                state_after_summary.get("completed_events") or []
            ),
            "boundary_accepted": True,
            "state_before_sha256": str(boundary["state_before_sha256"]),
            "state_after_sha256": str(boundary["state_after_sha256"]),
        }
        scene_budgets.append(scene_budget)

        accumulated_required_event_ids.extend(required_event_ids)
        prior_summaries.append(
            {
                "index": scene_index,
                "goal": str(scene.get("goal") or ""),
                "tail": _fixed_synthetic_text(
                    f"scene-{scene_index}-summary-tail",
                    SYNTHETIC_PRIOR_SUMMARY_TAIL_CHARS,
                ),
                "event_ids": required_event_ids,
            }
        )
        scene_state = state_after

    safe = all(
        bool(scene["within_safe_limit"] and scene["within_hard_limit"])
        for scene in scene_budgets
    )
    return {
        "mode": "synthetic_bounded_continuity_preflight",
        "evidence_kind": "synthetic_bounded_continuity_preflight",
        "recorded": False,
        "model_calls_performed": 0,
        "assumptions": {
            "provider_request_recorded": False,
            "previous_scene_tail_input_chars_per_scene": (
                SYNTHETIC_PREVIOUS_SCENE_TAIL_CHARS
            ),
            "prior_scene_summary_tail_input_chars": (
                SYNTHETIC_PRIOR_SUMMARY_TAIL_CHARS
            ),
            "prior_scene_summaries": (
                "one bounded summary per preceding current-plan Scene"
            ),
            "event_progression": (
                "after each measured request, every required event for that "
                "Scene is synthetically marked completed"
            ),
            "state_deltas": "all synthetic state-delta collections are empty",
            "synthetic_text_character_class": (
                "CJK filler is used so UTF-8/token calibration reflects Chinese prose"
            ),
            "transport_policy": (
                "production request compaction may shorten continuity fields; "
                "each Scene reports both input and transported sizes"
            ),
        },
        "scenes": scene_budgets,
        "result": {
            "safe": safe,
            "max_budgeted_input_tokens": max(
                int(scene["budgeted_input_tokens"])
                for scene in scene_budgets
            ),
            "minimum_safe_headroom_tokens": min(
                int(scene["safe_headroom_tokens"])
                for scene in scene_budgets
                if scene.get("safe_headroom_tokens") is not None
            ),
            "reason": (
                "all_synthetic_current_scene_requests_within_safe_and_hard_limits"
                if safe
                else "one_or_more_synthetic_current_scene_requests_exceed_budget"
            ),
        },
    }


def replay_scene_budget(
    run_json_path: str | Path,
    *,
    current_snapshot_path: str | Path | None = None,
    expected_input_pack_sha256: str | None = None,
    expected_run_json_sha256: str | None = None,
    recorded_run_json_sha256: str | None = None,
) -> dict[str, Any]:
    run_path = Path(run_json_path).expanduser().resolve()
    payload, run_json_integrity = _load_bound_run_json(
        run_path,
        expected_sha256=expected_run_json_sha256,
        recorded_sha256=recorded_run_json_sha256,
    )
    run = _run_record(payload)
    run_id = _required_text(run.get("id"), "run.id")
    chapter_index = _required_integer(run.get("chapter_index"), "run.chapter_index")
    run_dir = run_path.parent.resolve()
    recorded_failure = _recorded_context_failure(run)

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
        artifact_bytes = input_artifact_path.read_bytes()
        artifact_text = artifact_bytes.decode("utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ReplaySceneBudgetError(
            f"input-pack artifact is not readable UTF-8: {input_artifact_path}"
        ) from exc
    actual_input_artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
    recorded_input_artifact_sha256 = artifact_summary.get("sha256")
    if recorded_input_artifact_sha256 is not None:
        expected_input_artifact_sha256 = str(
            recorded_input_artifact_sha256
        ).strip().lower()
        if _SHA256.fullmatch(expected_input_artifact_sha256) is None:
            raise ReplaySceneBudgetError(
                "run.input_pack.artifact.sha256 is invalid"
            )
        input_artifact_binding = {
            "source": "run_record_artifact_sha256",
            "recorded": True,
        }
    else:
        expected_input_artifact_sha256 = str(
            expected_input_pack_sha256 or ""
        ).strip().lower()
        if _SHA256.fullmatch(expected_input_artifact_sha256) is None:
            raise ReplaySceneBudgetError(
                "legacy input-pack artifact has no recorded sha256; provide "
                "--input-pack-sha256 with an explicit caller-pinned digest"
            )
        input_artifact_binding = {
            "source": "caller_pinned_legacy_artifact_sha256",
            "recorded": False,
        }
    if actual_input_artifact_sha256 != expected_input_artifact_sha256:
        raise ReplaySceneBudgetError(
            "input-pack artifact sha256 does not match its evidence binding"
        )
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
    input_pack_report["artifact_integrity"] = {
        **input_artifact_binding,
        "verified": True,
        "expected_sha256": expected_input_artifact_sha256,
        "actual_sha256": actual_input_artifact_sha256,
    }
    current_input_pack = input_pack
    current_authority_source: dict[str, Any] = {
        "source_kind": "persisted_input_pack_authoritative_state",
        "recorded": True,
        "snapshot_path": None,
        "overlay_scope": "none",
    }
    if current_snapshot_path is not None:
        current_input_pack, current_authority_source = _current_snapshot_overlay(
            input_pack,
            snapshot_path=current_snapshot_path,
            chapter_index=chapter_index,
            run_book_id=(
                (run.get("story_project") or {}).get("book_id")
                if isinstance(run.get("story_project"), dict)
                else None
            ),
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
    response_text, scene_one_evidence = load_scene_one_evidence(
        run_dir=run_dir,
        run=run,
    )

    with patch.object(
        chapter_pipeline,
        "chat_completion",
        side_effect=_forbid_chat_completion,
    ) as completion_guard:
        historical_contexts = compile_prompt_contexts(input_pack, budget=budget)
        current_contexts = (
            historical_contexts
            if current_input_pack == input_pack
            else compile_prompt_contexts(current_input_pack, budget=budget)
        )
        current_plan = chapter_pipeline.plan_scenes(
            current_contexts.plan.text,
            chapter_index=chapter_index,
            chapter_blueprint=blueprint,
        )
        current_scenes = current_plan.get("scenes")
        if not isinstance(current_scenes, list) or not current_scenes:
            raise ReplaySceneBudgetError(
                "current chapter blueprint produced no Scenes"
            )
        current_scene_one = current_scenes[0]
        if not isinstance(current_scene_one, dict):
            raise ReplaySceneBudgetError("current Scene 1 plan must be an object")
        current_initial_state = chapter_pipeline._initial_scene_state(
            current_input_pack
        )
        current_authoritative_state = authoritative_state_from_markdown(
            current_input_pack
        )
        _current_request, current_scene_one_budget = _build_scene_request_replay(
            budget=budget,
            input_pack=current_contexts.scene.text,
            plan=current_plan,
            scene=current_scene_one,
            blueprint=blueprint,
            previous_scene_tail="",
            prior_scene_summaries=[],
            scene_state=current_initial_state,
            authoritative_state_source=current_authoritative_state,
        )
        synthetic_current_preflight = (
            _synthetic_bounded_current_plan_preflight(
                budget=budget,
                input_pack=current_contexts.scene.text,
                plan=current_plan,
                blueprint=blueprint,
                initial_state=current_initial_state,
                authoritative_state_source=current_authoritative_state,
            )
        )

        historical_plan, historical_plan_evidence = _historical_plan(
            run_dir,
            run,
            blueprint,
            chapter_index=chapter_index,
        )
        historical_scenes = historical_plan.get("scenes")
        if not isinstance(historical_scenes, list) or len(historical_scenes) < 2:
            raise ReplaySceneBudgetError(
                "historical plan must contain at least two Scenes"
            )
        historical_scene_one = historical_scenes[0]
        historical_scene_two = historical_scenes[1]
        if not isinstance(historical_scene_one, dict) or not isinstance(
            historical_scene_two,
            dict,
        ):
            raise ReplaySceneBudgetError("historical plan Scenes must be objects")
        scene_one_result = chapter_pipeline._load_scene_response(response_text)
        historical_initial_state = chapter_pipeline._initial_scene_state(
            input_pack
        )
        historical_authoritative_state = authoritative_state_from_markdown(
            input_pack
        )
        project_profile = _extract_section_object(input_pack, "Project Profile")
        language = str(project_profile.get("language") or "").strip() or None
        scene_one_prose = chapter_pipeline.validate_language_output(
            scene_one_result["prose"],
            chapter_pipeline.CHAPTER_CONTRACT,
            language=language,
        )
        boundary, state_after = chapter_pipeline.validate_scene_transition(
            scene_index=int(historical_scene_one["index"]),
            state_before=historical_initial_state,
            events=scene_one_result["events"],
            deltas=scene_one_result["deltas"],
            prose=scene_one_prose,
            required_event_ids=historical_scene_one.get("required_event_ids")
            or [],
            forbidden_event_ids=historical_scene_one.get("forbidden_event_ids")
            or [],
            planned_events=historical_scene_one.get("planned_events") or [],
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

        prior_scene_summaries = [
            {
                "index": int(historical_scene_one["index"]),
                "goal": str(historical_scene_one["goal"]),
                "tail": scene_one_prose[-280:],
                "event_ids": [
                    str(event["event_id"])
                    for event in scene_one_result["events"]
                ],
            }
        ]
        _historical_request, historical_scene_two_budget = (
            _build_scene_request_replay(
                budget=budget,
                input_pack=historical_contexts.scene.text,
                plan=historical_plan,
                scene=historical_scene_two,
                blueprint=blueprint,
                previous_scene_tail=scene_one_prose[-600:],
                prior_scene_summaries=prior_scene_summaries,
                scene_state=state_after,
                authoritative_state_source=historical_authoritative_state,
            )
        )
        historical_scene_two_budget.update(
            {
                "evidence_kind": "current_compact_transport_counterfactual",
                "recorded": False,
                "comparison_to_recorded_failure": {
                    "recorded_required_input_tokens": recorded_failure[
                        "required_input_tokens"
                    ],
                    "recorded_safe_limit_excess_tokens": recorded_failure[
                        "safe_limit_excess_tokens"
                    ],
                    "counterfactual_compact_input_tokens": (
                        historical_scene_two_budget["budgeted_input_tokens"]
                    ),
                    "counterfactual_compact_tokens_below_recorded": (
                        recorded_failure["required_input_tokens"]
                        - historical_scene_two_budget["budgeted_input_tokens"]
                    ),
                },
            }
        )
        if completion_guard.call_count:
            raise ReplaySceneBudgetError(
                "offline replay performed an unexpected chat_completion"
            )

    historical_safe = bool(
        historical_scene_two_budget["within_safe_limit"]
        and historical_scene_two_budget["within_hard_limit"]
    )
    current_safe = bool(
        current_scene_one_budget["within_safe_limit"]
        and current_scene_one_budget["within_hard_limit"]
        and synthetic_current_preflight["result"]["safe"]
    )
    replay_safe = historical_safe and current_safe
    historical_summary = _plan_summary(historical_plan)
    current_summary = _plan_summary(current_plan)
    return {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "mode": "offline_scene_budget_replay",
        "model_calls_performed": 0,
        "run": {
            "id": run_id,
            "chapter_index": chapter_index,
            "run_json": str(run_path),
            "run_dir": str(run_dir),
            "artifact_integrity": run_json_integrity,
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
            "count_mode": historical_scene_two_budget["budget_report"][
                "count_mode"
            ],
            "counter_version": historical_scene_two_budget["budget_report"][
                "counter_version"
            ],
        },
        "compile_context": {
            "context_digest": historical_contexts.context_digest,
            "scene_chars": len(historical_contexts.scene.text),
            "scene_selected_sections": list(
                historical_contexts.scene.selected_sections
            ),
            "scene_budget_report": historical_contexts.scene.report,
        },
        "current_compile_context": {
            "context_digest": current_contexts.context_digest,
            "scene_chars": len(current_contexts.scene.text),
            "scene_selected_sections": list(
                current_contexts.scene.selected_sections
            ),
            "scene_budget_report": current_contexts.scene.report,
        },
        "historical_plan_replay": {
            "mode": "historical_plan_replay",
            "model_calls_performed": 0,
            "recorded_failure": recorded_failure,
            "plan_evidence": historical_plan_evidence,
            "chapter_plan": historical_summary,
            "scene_one": {
                **scene_one_evidence,
                "boundary_accepted": True,
                "state_before_sha256": str(boundary["state_before_sha256"]),
                "state_after_sha256": str(boundary["state_after_sha256"]),
                "event_count": len(scene_one_result["events"]),
            },
            "scene_two": historical_scene_two_budget,
            "result": {
                "safe": historical_safe,
                "recorded_failure_confirmed": True,
                "counterfactual_compact_transport_safe": historical_safe,
                "reason": (
                    "recorded_failure_confirmed_and_compact_counterfactual_within_limits"
                    if historical_safe
                    else "recorded_failure_confirmed_but_compact_counterfactual_exceeds_budget"
                ),
            },
            "limitations": [
                (
                    "The exact 30902-token value is authoritative recorded "
                    "run.error evidence; the rejected Scene 2 request payload "
                    "was not persisted."
                ),
                (
                    "The nine-Scene plan is reconstructed from hash-verified "
                    "source revision evidence and is recorded=false."
                ),
                (
                    "Scene 2 compact and pretty counts are counterfactual "
                    "current-request measurements over the persisted input and "
                    "reconstructed plan, not historical recorded request counts."
                ),
            ],
        },
        "current_plan_preflight": {
            "mode": "current_plan_preflight",
            "model_calls_performed": 0,
            "plan_evidence": {
                "evidence_kind": "current_source_plan",
                "recorded": False,
            },
            "authority_source": current_authority_source,
            "chapter_plan": current_summary,
            "scene_one": current_scene_one_budget,
            "synthetic_bounded_continuity_preflight": (
                synthetic_current_preflight
            ),
            "result": {
                "safe": current_safe,
                "reason": (
                    "current_baseline_and_synthetic_scene_requests_within_limits"
                    if current_safe
                    else "one_or_more_current_preflight_requests_exceed_budget"
                ),
            },
        },
        "result": {
            "safe": replay_safe,
            "historical_recorded_failure_confirmed": True,
            "historical_plan_replay_safe": historical_safe,
            "historical_counterfactual_compact_safe": historical_safe,
            "current_plan_preflight_safe": current_safe,
            "interpretation": (
                "safe refers to the compact counterfactual and current "
                "four-Scene preflight; the historical run itself is recorded "
                "as failed at 30902 tokens"
            ),
            "reason": (
                "recorded_historical_failure_confirmed_and_compact_preflights_within_budget"
                if replay_safe
                else "one_or_more_offline_budget_checks_failed"
            ),
        },
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit a recorded Scene budget failure, rebuild the Scene 2 "
            "counterfactual, and preflight the current Scene plan without "
            "making any model call."
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
        "--current-snapshot",
        help=(
            "Optional current snapshot JSON. Only its exact Authoritative State "
            "overlays the persisted input for current-plan preflight; historical "
            "replay remains bound to the recorded input pack."
        ),
    )
    parser.add_argument(
        "--run-json-sha256",
        help=(
            "Caller-pinned SHA-256 of the exact persisted run JSON bytes. "
            "Required while the selected legacy run has no trusted external "
            "record of its artifact hash."
        ),
    )
    parser.add_argument(
        "--input-pack-sha256",
        help=(
            "Caller-pinned SHA-256 for a legacy input-pack artifact whose run "
            "record predates recorded artifact hashes."
        ),
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
        report = replay_scene_budget(
            run_path,
            current_snapshot_path=args.current_snapshot,
            expected_input_pack_sha256=args.input_pack_sha256,
            expected_run_json_sha256=args.run_json_sha256,
        )
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
