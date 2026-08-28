from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
import hashlib
import json
import re
import stat
import uuid

from api.openai_client import chat_completion
from core.engine.persistence import (
    atomic_create_json,
    atomic_create_text,
    atomic_write_json,
    persistence_run_lock,
)
from core.model_call_runtime import ModelCallRuntimeContext
from core.model_calls import ModelCallStore
from core.story_project.amendments import (
    AMENDMENT_ROOT,
    AMENDMENT_SCHEMA_VERSION,
    active_amendment_pointer_path,
    amendment_chapter_root,
    read_current_chapter_text,
    resolve_active_amendment,
)
from core.story_project.paths import resolve_prose
from workbench.database import WorkbenchDatabase
from workbench.projects import ProjectRegistry


class AmendmentError(RuntimeError):
    """A committed chapter could not be safely amended."""


@dataclass(frozen=True)
class AmendmentRecord:
    id: str
    project_id: str
    chapter: int
    status: str
    base_sha256: str
    content_sha256: str
    content_path: str
    deterministic_check: dict[str, Any]
    semantic_check: dict[str, Any] | None
    error: dict[str, Any] | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "chapter": self.chapter,
            "status": self.status,
            "base_sha256": self.base_sha256,
            "content_sha256": self.content_sha256,
            "content_path": self.content_path,
            "deterministic_check": _json_copy(self.deterministic_check),
            "semantic_check": _json_copy(self.semantic_check),
            "error": _json_copy(self.error),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


SemanticProvider = Callable[[list[dict[str, str]], str, Path], str]


class AmendmentService:
    """Immutable copy-edit versions with fail-closed semantic activation."""

    def __init__(
        self,
        *,
        database: WorkbenchDatabase,
        projects: ProjectRegistry,
        provider: SemanticProvider | None = None,
    ) -> None:
        self.database = database
        self.projects = projects
        self.provider = provider or _durable_semantic_provider
        self._initialize()

    def submit(
        self,
        project_id: str,
        chapter: int,
        *,
        text: str,
        expected_base_sha256: str,
        idempotency_key: str | None = None,
        recover: bool = False,
    ) -> AmendmentRecord:
        if isinstance(chapter, bool) or not isinstance(chapter, int) or chapter < 1:
            raise AmendmentError("chapter must be a positive integer")
        revised = _validated_text(text)
        if not _is_sha256(expected_base_sha256):
            raise AmendmentError("expected_base_sha256 must be a lowercase SHA-256")
        project = self.projects.get(project_id)
        root = Path(project.root).resolve(strict=True)
        content_sha = _sha256_bytes(revised.encode("utf-8"))
        amendment_id = _amendment_id(
            project_id,
            chapter,
            idempotency_key
            or f"derived:{expected_base_sha256}:{content_sha}",
        )
        with self.database.connection() as connection:
            prior = connection.execute(
                "SELECT * FROM amendments WHERE id = ?", (amendment_id,)
            ).fetchone()
        if prior is not None:
            prior_record = _record_from_row(prior)
            if (
                prior_record.project_id != str(project_id)
                or prior_record.chapter != chapter
                or prior_record.base_sha256 != expected_base_sha256
                or prior_record.content_sha256 != content_sha
            ):
                raise AmendmentError(
                    "idempotency key was already used for different amendment content"
                )
            if prior_record.status in {"active", "rejected", "superseded"}:
                return prior_record
            try:
                active = resolve_active_amendment(root, chapter)
            except Exception:
                active = None
            if (
                active is not None
                and active.amendment_id == amendment_id
                and active.sha256 == content_sha
            ):
                return self._set_status(amendment_id, "active")
        original, original_path, actual_base_sha, _ = read_current_chapter_text(root, chapter)
        _assert_safe_project_file(root, original_path)
        formal = resolve_prose(root, chapter)
        if formal.conflict or formal.path is None:
            raise AmendmentError("chapter must have one unique formal prose file")
        _assert_safe_project_file(root, formal.path)
        formal_prose_sha = _sha256_bytes(formal.path.read_bytes())
        if actual_base_sha != expected_base_sha256:
            raise AmendmentError(
                "chapter version changed; reload before submitting this amendment"
            )
        chapter_root = amendment_chapter_root(root, chapter)
        version_root = chapter_root / "versions"
        version_path = version_root / f"{amendment_id}.md"
        manifest_path = version_path.with_suffix(".json")
        relative_path = version_path.relative_to(root).as_posix()
        deterministic = deterministic_fact_check(original, revised)
        timestamp = _utc_now()

        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM amendments WHERE id = ?", (amendment_id,)
            ).fetchone()
            if existing is not None:
                record = _record_from_row(existing)
                if (
                    record.project_id != str(project_id)
                    or record.chapter != chapter
                    or record.base_sha256 != expected_base_sha256
                    or record.content_sha256 != content_sha
                ):
                    raise AmendmentError(
                        "idempotency key was already used for different amendment content"
                    )
                if record.status in {"active", "rejected", "superseded"}:
                    return record
                if not recover:
                    raise AmendmentError(
                        "amendment validation already started and will not be replayed automatically"
                    )
                connection.execute(
                    """
                    UPDATE amendments SET status = 'validating', error_json = NULL,
                        updated_at = ? WHERE id = ?
                    """,
                    (timestamp, amendment_id),
                )
            else:
                version_root.mkdir(parents=True, exist_ok=True)
                atomic_create_text(version_path, revised)
                connection.execute(
                    """
                    INSERT INTO amendments(
                        id, project_id, chapter, status, base_sha256,
                        content_sha256, content_path, deterministic_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 'validating', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        amendment_id,
                        str(project_id),
                        chapter,
                        expected_base_sha256,
                        content_sha,
                        relative_path,
                        _canonical_json(deterministic),
                        timestamp,
                        timestamp,
                    ),
                )

        if not deterministic["ok"]:
            semantic = {
                "equivalent": False,
                "confidence": 1.0,
                "critical_changes": list(deterministic["differences"]),
                "uncertain": False,
                "summary": "确定性事实检查发现改动，未调用模型。",
                "provider_calls": 0,
            }
            self._write_manifest(
                manifest_path,
                amendment_id=amendment_id,
                chapter=chapter,
                base_sha256=expected_base_sha256,
                formal_prose_sha256=formal_prose_sha,
                content_sha256=content_sha,
                semantic=semantic,
                created_at=timestamp,
            )
            return self._set_status(amendment_id, "rejected", semantic=semantic)

        evidence_root = (
            root
            / ".novelagent"
            / "runtime"
            / "model_calls"
            / "workbench_amendments"
            / amendment_id
        )
        messages = build_semantic_equivalence_messages(original, revised)
        try:
            raw = self.provider(
                messages,
                f"amendment-check-{amendment_id}",
                evidence_root,
            )
            semantic = parse_semantic_check(raw)
        except Exception as exc:
            uncertain = _provider_state_uncertain(exc)
            self._set_status(
                amendment_id,
                "needs_recovery" if uncertain else "pending_validation",
                error={
                    "type": type(exc).__name__,
                    "message": str(exc)[:2000],
                    "automatic_retry": False,
                },
            )
            if uncertain:
                raise AmendmentError(
                    "润色审核调用状态不确定；版本已保存，但禁止自动重试或激活。"
                ) from exc
            raise

        equivalent = (
            semantic["equivalent"] is True
            and semantic["uncertain"] is False
            and float(semantic["confidence"]) >= 0.9
            and not semantic["critical_changes"]
        )
        self._write_manifest(
            manifest_path,
            amendment_id=amendment_id,
            chapter=chapter,
            base_sha256=expected_base_sha256,
            formal_prose_sha256=formal_prose_sha,
            content_sha256=content_sha,
            semantic=semantic,
            created_at=timestamp,
        )
        if not equivalent:
            return self._set_status(amendment_id, "rejected", semantic=semantic)

        pointer = {
            "schema_version": AMENDMENT_SCHEMA_VERSION,
            "amendment_id": amendment_id,
            "chapter_index": chapter,
            "base_sha256": expected_base_sha256,
            "formal_prose_sha256": formal_prose_sha,
            "content_sha256": content_sha,
            "content_path": relative_path,
            "activated_at": _utc_now(),
        }
        pointer_path = active_amendment_pointer_path(root, chapter)
        lock_root = root / AMENDMENT_ROOT / ".locks"
        with persistence_run_lock(lock_root, state_paths=(pointer_path,)):
            _, _, locked_base_sha, _ = read_current_chapter_text(root, chapter)
            if locked_base_sha != expected_base_sha256:
                return self._set_status(
                    amendment_id,
                    "rejected",
                    semantic=semantic,
                    error={
                        "code": "amendment_base_changed_before_activation",
                        "expected": expected_base_sha256,
                        "actual": locked_base_sha,
                    },
                )
            atomic_write_json(pointer_path, pointer)
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE amendments SET status = 'superseded', updated_at = ?
                    WHERE project_id = ? AND chapter = ? AND status = 'active' AND id <> ?
                    """,
                    (_utc_now(), str(project_id), chapter, amendment_id),
                )
                connection.execute(
                    """
                    UPDATE amendments SET status = 'active', semantic_json = ?,
                        error_json = NULL, updated_at = ? WHERE id = ?
                    """,
                    (_canonical_json(semantic), _utc_now(), amendment_id),
                )
        return self.get(amendment_id)

    def get(self, amendment_id: str) -> AmendmentRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM amendments WHERE id = ?", (str(amendment_id),)
            ).fetchone()
        if row is None:
            raise AmendmentError(f"unknown amendment: {amendment_id}")
        return _record_from_row(row)

    def list_for_chapter(self, project_id: str, chapter: int) -> tuple[AmendmentRecord, ...]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM amendments WHERE project_id = ? AND chapter = ?
                ORDER BY created_at DESC, id DESC
                """,
                (str(project_id), int(chapter)),
            ).fetchall()
        return tuple(_record_from_row(row) for row in rows)

    def recover_after_restart(self) -> int:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE amendments SET status = 'needs_recovery', error_json = ?, updated_at = ?
                WHERE status = 'validating'
                """,
                (
                    _canonical_json(
                        {
                            "code": "workbench_restarted_during_amendment_call",
                            "automatic_retry": False,
                        }
                    ),
                    _utc_now(),
                ),
            )
        return int(cursor.rowcount)

    def _initialize(self) -> None:
        with self.database.transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS amendments (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    chapter INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    base_sha256 TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    content_path TEXT NOT NULL,
                    deterministic_json TEXT NOT NULL,
                    semantic_json TEXT,
                    error_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_amendments_project_chapter
                ON amendments(project_id, chapter, created_at DESC);
                """
            )

    def _write_manifest(
        self,
        path: Path,
        *,
        amendment_id: str,
        chapter: int,
        base_sha256: str,
        formal_prose_sha256: str,
        content_sha256: str,
        semantic: Mapping[str, Any],
        created_at: str,
    ) -> None:
        payload = {
            "schema_version": AMENDMENT_SCHEMA_VERSION,
            "id": amendment_id,
            "chapter_index": chapter,
            "base_sha256": base_sha256,
            "formal_prose_sha256": formal_prose_sha256,
            "content_sha256": content_sha256,
            "semantic_equivalent": bool(semantic.get("equivalent")),
            "semantic_check": dict(semantic),
            "created_at": created_at,
        }
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if _canonical_json(existing) != _canonical_json(payload):
                raise AmendmentError("immutable amendment manifest conflict")
            return
        atomic_create_json(path, payload)

    def _set_status(
        self,
        amendment_id: str,
        status: str,
        *,
        semantic: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
    ) -> AmendmentRecord:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE amendments SET status = ?, semantic_json = COALESCE(?, semantic_json),
                    error_json = ?, updated_at = ? WHERE id = ?
                """,
                (
                    status,
                    _canonical_json(semantic) if semantic is not None else None,
                    _canonical_json(error) if error is not None else None,
                    _utc_now(),
                    str(amendment_id),
                ),
            )
        return self.get(amendment_id)


def deterministic_fact_check(original: str, revised: str) -> dict[str, Any]:
    original_facts = _critical_tokens(original)
    revised_facts = _critical_tokens(revised)
    differences: list[dict[str, Any]] = []
    for category in sorted(set(original_facts) | set(revised_facts)):
        before = original_facts.get(category, [])
        after = revised_facts.get(category, [])
        if before != after:
            differences.append(
                {"category": category, "before": before[:100], "after": after[:100]}
            )
    original_paragraphs = [item for item in re.split(r"\n\s*\n", original.strip()) if item.strip()]
    revised_paragraphs = [item for item in re.split(r"\n\s*\n", revised.strip()) if item.strip()]
    if original_paragraphs and abs(len(original_paragraphs) - len(revised_paragraphs)) > max(
        2, len(original_paragraphs) // 5
    ):
        differences.append(
            {
                "category": "paragraph_structure",
                "before": len(original_paragraphs),
                "after": len(revised_paragraphs),
            }
        )
    length_ratio = len(revised) / max(1, len(original))
    if length_ratio < 0.8 or length_ratio > 1.2:
        differences.append(
            {"category": "length_ratio", "before": len(original), "after": len(revised)}
        )
    return {
        "ok": not differences,
        "differences": differences,
        "length_ratio": round(length_ratio, 4),
    }


def build_semantic_equivalence_messages(original: str, revised: str) -> list[dict[str, str]]:
    if max(len(original), len(revised)) > 160_000:
        raise AmendmentError("chapter is too large for safe semantic equivalence review")
    contract = {
        "equivalent": True,
        "confidence": 0.98,
        "critical_changes": [],
        "uncertain": False,
        "summary": "仅措辞、标点和节奏润色，事实与剧情等价。",
    }
    return [
        {
            "role": "system",
            "content": (
                "你是小说版本安全审核器。只有纯措辞、语法、节奏和标点润色才可判等价。"
                "人物身份/关系、数量、地点、时间线、资源、能力、伤亡、承诺、因果或关键情节"
                "发生任何变化，或你无法确定时，equivalent 必须为 false。只输出 JSON。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"输出契约：{_canonical_json(contract)}\n\n"
                f"原版本：\n{original}\n\n候选润色版本：\n{revised}"
            ),
        },
    ]


def parse_semantic_check(raw: str) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw.strip():
        raise AmendmentError("semantic check returned empty output")
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines.pop()
        text = "\n".join(lines)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AmendmentError(f"semantic check is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise AmendmentError("semantic check must be a JSON object")
    if type(value.get("equivalent")) is not bool or type(value.get("uncertain")) is not bool:
        raise AmendmentError("semantic check boolean fields are invalid")
    confidence = value.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise AmendmentError("semantic check confidence is invalid")
    if not 0 <= float(confidence) <= 1:
        raise AmendmentError("semantic check confidence must be between 0 and 1")
    changes = value.get("critical_changes")
    if not isinstance(changes, list) or any(not isinstance(item, (str, dict)) for item in changes):
        raise AmendmentError("semantic check critical_changes is invalid")
    summary = value.get("summary")
    if not isinstance(summary, str):
        raise AmendmentError("semantic check summary is invalid")
    return {
        "equivalent": value["equivalent"],
        "confidence": float(confidence),
        "critical_changes": changes[:100],
        "uncertain": value["uncertain"],
        "summary": summary[:2000],
        "provider_calls": 1,
    }


def _critical_tokens(text: str) -> dict[str, list[str]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return {
        "arabic_numbers": re.findall(r"(?<![A-Za-z0-9])[-+]?\d+(?:\.\d+)?%?", normalized),
        "measured_quantities": re.findall(
            r"[零〇一二两三四五六七八九十百千万亿\d]+(?:个|名|人|只|头|枚|颗|支|把|"
            r"件|份|次|天|日|年|月|小时|分钟|秒|米|公里|层|级|阶|点|岁|吨|斤)",
            normalized,
        ),
        "clock_dates": re.findall(
            r"(?:\d{1,4}[-/.年]){1,2}\d{1,2}(?:日)?|\d{1,2}:\d{2}", normalized
        ),
        "coordinates": re.findall(r"[-+]?\d+(?:\.\d+)?\s*[,，]\s*[-+]?\d+(?:\.\d+)?", normalized),
    }


def _durable_semantic_provider(
    messages: list[dict[str, str]],
    call_id: str,
    evidence_root: Path,
) -> str:
    runtime = ModelCallRuntimeContext(ModelCallStore(evidence_root))
    response = chat_completion(
        messages,
        temperature=0.0,
        stage="amendment_semantic_equivalence",
        max_tokens=2000,
        model_call_runtime=runtime,
        call_id=call_id,
    )
    return response.text


def _record_from_row(row: Any) -> AmendmentRecord:
    return AmendmentRecord(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        chapter=int(row["chapter"]),
        status=str(row["status"]),
        base_sha256=str(row["base_sha256"]),
        content_sha256=str(row["content_sha256"]),
        content_path=str(row["content_path"]),
        deterministic_check=json.loads(str(row["deterministic_json"])),
        semantic_check=json.loads(str(row["semantic_json"])) if row["semantic_json"] else None,
        error=json.loads(str(row["error_json"])) if row["error_json"] else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _amendment_id(project_id: str, chapter: int, idempotency_key: str | None) -> str:
    if idempotency_key is None:
        key = uuid.uuid4().hex
    else:
        key = str(idempotency_key).strip()
        if not key or len(key) > 200:
            raise AmendmentError("Idempotency-Key must contain 1 to 200 characters")
    digest = hashlib.sha256(f"{project_id}\0{chapter}\0{key}".encode("utf-8")).hexdigest()[:32]
    return f"am_{digest}"


def _validated_text(value: Any) -> str:
    if not isinstance(value, str):
        raise AmendmentError("amendment text must be a string")
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) < 500:
        raise AmendmentError("amendment text is too short to be a complete chapter")
    if len(text.encode("utf-8")) > 4 * 1024 * 1024:
        raise AmendmentError("amendment text exceeds 4 MiB")
    return text + "\n"


def _provider_state_uncertain(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ == "ProviderCallUncertainError":
            return True
        if getattr(current, "failure_category", None) == "provider_call_uncertain":
            return True
        nested = getattr(current, "cause", None)
        current = nested if isinstance(nested, BaseException) else current.__cause__
    return False


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _assert_safe_project_file(root: Path, path: Path) -> None:
    try:
        info = path.lstat()
        path.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise AmendmentError(f"amendment source escapes the StoryProject: {path}") from exc
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    attributes = int(getattr(info, "st_file_attributes", 0))
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or bool(attributes & reparse_flag)
    ):
        raise AmendmentError(
            f"amendment source must be a regular non-link file: {path}"
        )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _json_copy(value: Any) -> Any:
    if value is None:
        return None
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "AmendmentError",
    "AmendmentRecord",
    "AmendmentService",
    "build_semantic_equivalence_messages",
    "deterministic_fact_check",
    "parse_semantic_check",
]
