from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
import hashlib
import json
import re
import stat
import uuid

from api.openai_client import chat_completion
from core.model_call_runtime import ModelCallRuntimeContext
from core.model_calls import ModelCallStore
from core.story_project.paths import infer_next_chapter, prose_chapter_index
from workbench.database import WorkbenchDatabase
from workbench.outlines import (
    OutlineBatchRecord,
    OutlineBatchService,
    compute_source_fingerprint,
    validate_outline_items,
)


class OutlinePlanningError(RuntimeError):
    """Raised when a proposal cannot be produced without an unsafe model replay."""


@dataclass(frozen=True)
class PlanningOperation:
    id: str
    project_id: str
    state: str
    start_chapter: int
    chapter_count: int
    request_sha256: str
    context_sha256: str
    batch_id: str | None
    error: dict[str, Any] | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "state": self.state,
            "start_chapter": self.start_chapter,
            "chapter_count": self.chapter_count,
            "request_sha256": self.request_sha256,
            "context_sha256": self.context_sha256,
            "batch_id": self.batch_id,
            "error": json.loads(json.dumps(self.error, ensure_ascii=False)) if self.error else None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


Provider = Callable[[list[dict[str, str]], str, Path], str]


class OutlinePlanner:
    """Proposes ten-chapter batches with durable, idempotent provider evidence."""

    def __init__(
        self,
        *,
        database: WorkbenchDatabase,
        outlines: OutlineBatchService,
        provider: Provider | None = None,
    ) -> None:
        self.database = database
        self.outlines = outlines
        self.provider = provider or _durable_openai_provider
        self._initialize()

    def propose(
        self,
        project_id: str,
        *,
        direction: str,
        chapter_count: int = 10,
        idempotency_key: str | None = None,
        recover: bool = False,
    ) -> tuple[PlanningOperation, OutlineBatchRecord]:
        clean_direction = _required_direction(direction)
        if isinstance(chapter_count, bool) or not isinstance(chapter_count, int):
            raise OutlinePlanningError("chapter_count must be an integer")
        if chapter_count < 1 or chapter_count > 20:
            raise OutlinePlanningError("chapter_count must be between 1 and 20")
        project = self.outlines.projects.get(project_id)
        start_chapter = infer_next_chapter(project.root)
        source_fingerprint_before = compute_source_fingerprint(project)
        context = collect_planning_context(project.root, start_chapter=start_chapter)
        if compute_source_fingerprint(project) != source_fingerprint_before:
            raise OutlinePlanningError(
                "StoryProject changed while planning context was being collected; retry from the new state"
            )
        context_json = _canonical_json(context)
        request = {
            "project_id": str(project_id),
            "book_id": str(project.book_id),
            "start_chapter": start_chapter,
            "chapter_count": chapter_count,
            "direction": clean_direction,
        }
        request_sha = _sha256(_canonical_json(request))
        context_sha = _sha256(context_json)
        operation_id = _operation_id(
            project_id,
            idempotency_key
            or "derived:"
            + _sha256(f"{request_sha}:{context_sha}:{source_fingerprint_before}"),
        )
        operation = self._claim_operation(
            operation_id=operation_id,
            project_id=project_id,
            start_chapter=start_chapter,
            chapter_count=chapter_count,
            request_sha256=request_sha,
            context_sha256=context_sha,
            recover=recover,
        )
        if operation.state == "completed":
            if not operation.batch_id:
                raise OutlinePlanningError("completed planning operation has no outline batch")
            return operation, self.outlines.get_batch(operation.batch_id)

        evidence_root = (
            Path(project.root)
            / ".novelagent"
            / "runtime"
            / "model_calls"
            / "workbench_outline_batches"
            / operation.id
        )
        messages = build_outline_messages(
            context=context,
            direction=clean_direction,
            start_chapter=start_chapter,
            chapter_count=chapter_count,
        )
        try:
            raw = self.provider(messages, f"outline-plan-{operation.id}", evidence_root)
            try:
                proposal = parse_outline_proposal(
                    raw,
                    start_chapter=start_chapter,
                    chapter_count=chapter_count,
                )
            except OutlinePlanningError as first_error:
                repair_messages = [
                    {
                        "role": "system",
                        "content": (
                            "你是 JSON 契约修复器。只输出修复后的 JSON 对象，不解释，不使用 Markdown。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": _repair_prompt(
                            raw,
                            error=str(first_error),
                            start_chapter=start_chapter,
                            chapter_count=chapter_count,
                        ),
                    },
                ]
                repaired = self.provider(
                    repair_messages,
                    f"outline-repair-{operation.id}",
                    evidence_root,
                )
                proposal = parse_outline_proposal(
                    repaired,
                    start_chapter=start_chapter,
                    chapter_count=chapter_count,
                )
            self._save_proposal(operation.id, proposal)
            current_context_sha = _sha256(
                _canonical_json(
                    collect_planning_context(project.root, start_chapter=start_chapter)
                )
            )
            if (
                current_context_sha != context_sha
                or compute_source_fingerprint(project) != source_fingerprint_before
                or infer_next_chapter(project.root) != start_chapter
            ):
                raise OutlinePlanningError(
                    "StoryProject changed while the model was planning; output was preserved but cannot be approved"
                )
            batch = self.outlines.create_batch(
                project_id,
                proposal,
                expected_source_fingerprint=source_fingerprint_before,
            )
            completed = self._complete(operation.id, batch.id)
            return completed, batch
        except Exception as exc:
            uncertain = _provider_state_uncertain(exc)
            self._fail(operation.id, exc, uncertain=uncertain)
            if uncertain:
                raise OutlinePlanningError(
                    "章纲模型调用状态不确定；工作台已禁止自动重试。请显式执行恢复。"
                ) from exc
            raise

    def get_operation(self, operation_id: str) -> PlanningOperation:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM planning_operations WHERE id = ?", (str(operation_id),)
            ).fetchone()
        if row is None:
            raise OutlinePlanningError(f"unknown planning operation: {operation_id}")
        return _operation_from_row(row)

    def recover_after_restart(self) -> int:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE planning_operations
                SET state = 'needs_recovery', error_json = ?, updated_at = ?
                WHERE state = 'running'
                """,
                (
                    _canonical_json(
                        {
                            "code": "workbench_restarted_during_outline_call",
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
                CREATE TABLE IF NOT EXISTS planning_operations (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    start_chapter INTEGER NOT NULL,
                    chapter_count INTEGER NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    context_sha256 TEXT NOT NULL,
                    batch_id TEXT,
                    proposal_json TEXT,
                    error_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
                    FOREIGN KEY (batch_id) REFERENCES outline_batches(id) ON DELETE SET NULL
                );
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(planning_operations)"
                ).fetchall()
            }
            if "proposal_json" not in columns:
                connection.execute(
                    "ALTER TABLE planning_operations ADD COLUMN proposal_json TEXT"
                )

    def _claim_operation(
        self,
        *,
        operation_id: str,
        project_id: str,
        start_chapter: int,
        chapter_count: int,
        request_sha256: str,
        context_sha256: str,
        recover: bool,
    ) -> PlanningOperation:
        timestamp = _utc_now()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM planning_operations WHERE id = ?", (operation_id,)
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO planning_operations(
                        id, project_id, state, start_chapter, chapter_count,
                        request_sha256, context_sha256, created_at, updated_at
                    ) VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        operation_id,
                        str(project_id),
                        start_chapter,
                        chapter_count,
                        request_sha256,
                        context_sha256,
                        timestamp,
                        timestamp,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM planning_operations WHERE id = ?", (operation_id,)
                ).fetchone()
            else:
                operation = _operation_from_row(row)
                if (
                    operation.project_id != str(project_id)
                    or operation.start_chapter != start_chapter
                    or operation.chapter_count != chapter_count
                    or operation.request_sha256 != request_sha256
                    or operation.context_sha256 != context_sha256
                ):
                    raise OutlinePlanningError(
                        "idempotency key was already used with different project inputs"
                    )
                if operation.state == "completed":
                    return operation
                if not recover:
                    raise OutlinePlanningError(
                        "planning operation already exists and will not be replayed automatically"
                    )
                if operation.state not in {"needs_recovery", "failed"}:
                    raise OutlinePlanningError(
                        f"planning operation cannot be recovered from state {operation.state}"
                    )
                connection.execute(
                    """
                    UPDATE planning_operations SET state = 'running', error_json = NULL,
                        updated_at = ? WHERE id = ?
                    """,
                    (timestamp, operation_id),
                )
                row = connection.execute(
                    "SELECT * FROM planning_operations WHERE id = ?", (operation_id,)
                ).fetchone()
        return _operation_from_row(row)

    def _complete(self, operation_id: str, batch_id: str) -> PlanningOperation:
        timestamp = _utc_now()
        with self.database.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE planning_operations SET state = 'completed', batch_id = ?,
                    error_json = NULL, updated_at = ? WHERE id = ? AND state = 'running'
                """,
                (str(batch_id), timestamp, str(operation_id)),
            )
            if updated.rowcount != 1:
                raise OutlinePlanningError("planning operation completion CAS failed")
        return self.get_operation(operation_id)

    def _fail(self, operation_id: str, exc: Exception, *, uncertain: bool) -> None:
        timestamp = _utc_now()
        error = {
            "type": type(exc).__name__,
            "message": str(exc)[:2000],
            "automatic_retry": False,
        }
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE planning_operations SET state = ?, error_json = ?, updated_at = ?
                WHERE id = ? AND state = 'running'
                """,
                (
                    "needs_recovery" if uncertain else "failed",
                    _canonical_json(error),
                    timestamp,
                    str(operation_id),
                ),
            )

    def _save_proposal(
        self, operation_id: str, proposal: Sequence[Mapping[str, Any]]
    ) -> None:
        with self.database.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE planning_operations SET proposal_json = ?, updated_at = ?
                WHERE id = ? AND state = 'running'
                """,
                (_canonical_json(list(proposal)), _utc_now(), str(operation_id)),
            )
            if updated.rowcount != 1:
                raise OutlinePlanningError(
                    "planning operation changed before proposal preservation"
                )


def collect_planning_context(
    project_root: str | Path,
    *,
    start_chapter: int,
    recent_chapters: int = 3,
) -> dict[str, Any]:
    root = Path(project_root).resolve(strict=True)
    settings = _read_markdown_group(root, root / "设定", max_files=40, max_chars=70_000)
    tracking = _read_markdown_group(root, root / "追踪", max_files=40, max_chars=50_000)
    outline_paths = _markdown_paths(root, root / "大纲")
    global_outlines: list[Path] = []
    recent_outlines: list[Path] = []
    for path in outline_paths:
        match = re.search(r"第\s*0*([0-9]+)\s*章", path.name)
        if match is None:
            global_outlines.append(path)
        elif int(match.group(1)) < start_chapter:
            recent_outlines.append(path)
    recent_outlines = recent_outlines[-max(1, recent_chapters):]
    prose_candidates: list[tuple[int, Path]] = []
    for path in _markdown_paths(root, root / "正文"):
        index = prose_chapter_index(path)
        if index is not None and index < start_chapter:
            prose_candidates.append((index, path))
    prose_candidates.sort(key=lambda item: item[0])
    recent_prose = [path for _, path in prose_candidates[-max(1, recent_chapters):]]
    return {
        "start_chapter": start_chapter,
        "settings": settings,
        "master_outline": _read_paths(root, global_outlines, max_chars=70_000),
        "recent_outlines": _read_paths(root, recent_outlines, max_chars=30_000),
        "tracking": tracking,
        "recent_prose": _read_paths(root, recent_prose, max_chars=60_000),
    }


def build_outline_messages(
    *,
    context: Mapping[str, Any],
    direction: str,
    start_chapter: int,
    chapter_count: int,
) -> list[dict[str, str]]:
    end_chapter = start_chapter + chapter_count - 1
    schema_example = {
        "chapters": [
            {
                "chapter_index": start_chapter,
                "title": "章节标题",
                "core_event": "本章唯一主事件及其结果",
                "required_beats": ["必须发生的节拍一", "必须发生的节拍二"],
                "ending_pressure": "迫使读者继续阅读的具体压力",
            }
        ]
    }
    return [
        {
            "role": "system",
            "content": (
                "你是长篇网文的章节规划编辑。必须维护人物、资源、地点、时间线与既有事实连续性。"
                "只输出一个合法 JSON 对象，不输出 Markdown、注释或额外文字。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"请规划第 {start_chapter}—{end_chapter} 章，共 {chapter_count} 章。\n"
                "每章必须有 title、core_event、required_beats、ending_pressure；"
                "章节号必须连续且不得改写已经发生的事实。\n"
                f"作者阶段方向：\n{direction}\n\n"
                f"输出契约示例：\n{_canonical_json(schema_example)}\n\n"
                f"权威创作上下文：\n{_canonical_json(context)}"
            ),
        },
    ]


def parse_outline_proposal(
    raw: str,
    *,
    start_chapter: int,
    chapter_count: int,
) -> list[dict[str, Any]]:
    if not isinstance(raw, str) or not raw.strip():
        raise OutlinePlanningError("outline proposal is empty")
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OutlinePlanningError(f"outline proposal is not valid JSON: {exc}") from exc
    chapters = value.get("chapters") if isinstance(value, dict) else value
    if not isinstance(chapters, list):
        raise OutlinePlanningError("outline proposal must contain a chapters array")
    if len(chapters) != chapter_count:
        raise OutlinePlanningError(
            f"outline proposal must contain exactly {chapter_count} chapters"
        )
    try:
        validated = validate_outline_items(chapters)
    except ValueError as exc:
        raise OutlinePlanningError(str(exc)) from exc
    if validated[0].chapter_index != start_chapter:
        raise OutlinePlanningError(
            f"outline proposal must start at chapter {start_chapter}"
        )
    expected = list(range(start_chapter, start_chapter + chapter_count))
    actual = [item.chapter_index for item in validated]
    if actual != expected:
        raise OutlinePlanningError("outline proposal chapter indexes must be contiguous")
    return [item.to_dict() for item in validated]


def _durable_openai_provider(
    messages: list[dict[str, str]],
    call_id: str,
    evidence_root: Path,
) -> str:
    runtime = ModelCallRuntimeContext(ModelCallStore(evidence_root))
    response = chat_completion(
        messages,
        temperature=0.4,
        stage="outline_planning",
        max_tokens=5000,
        model_call_runtime=runtime,
        call_id=call_id,
    )
    return response.text


def _read_markdown_group(
    root: Path, directory: Path, *, max_files: int, max_chars: int
) -> list[dict[str, str]]:
    return _read_paths(root, _markdown_paths(root, directory)[:max_files], max_chars=max_chars)


def _markdown_paths(root: Path, directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    _assert_safe_project_entry(root, directory, expect_directory=True)
    markdown: list[Path] = []
    pending = [directory]
    while pending:
        current = pending.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise OutlinePlanningError(
                f"cannot inspect planning source directory: {current}"
            ) from exc
        for path in entries:
            info = _assert_safe_project_entry(root, path)
            if stat.S_ISDIR(info.st_mode):
                pending.append(path)
            elif stat.S_ISREG(info.st_mode) and path.suffix.casefold() == ".md":
                markdown.append(path)
    return sorted(markdown, key=lambda path: path.relative_to(root).as_posix())


def _read_paths(root: Path, paths: Sequence[Path], *, max_chars: int) -> list[dict[str, str]]:
    remaining = max(0, int(max_chars))
    result: list[dict[str, str]] = []
    for path in paths:
        if remaining <= 0:
            break
        try:
            _assert_safe_project_entry(root, path, expect_directory=False)
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeError as exc:
            raise OutlinePlanningError(
                f"planning source is not valid UTF-8: {path}"
            ) from exc
        clipped = text[:remaining]
        result.append({"file": path.relative_to(root).as_posix(), "content": clipped})
        remaining -= len(clipped)
    return result


def _assert_safe_project_entry(
    root: Path, path: Path, *, expect_directory: bool | None = None
) -> Any:
    try:
        info = path.lstat()
        path.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise OutlinePlanningError(
            f"planning source escapes the StoryProject: {path}"
        ) from exc
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    attributes = int(getattr(info, "st_file_attributes", 0))
    if stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_flag):
        raise OutlinePlanningError(
            f"planning sources cannot contain links or reparse points: {path}"
        )
    if expect_directory is True and not stat.S_ISDIR(info.st_mode):
        raise OutlinePlanningError(
            f"planning source directory is not a directory: {path}"
        )
    if expect_directory is False and not stat.S_ISREG(info.st_mode):
        raise OutlinePlanningError(f"planning source is not a regular file: {path}")
    if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
        raise OutlinePlanningError(f"unsupported planning source entry: {path}")
    return info


def _repair_prompt(raw: str, *, error: str, start_chapter: int, chapter_count: int) -> str:
    return (
        f"以下输出不符合契约：{error}\n"
        f"请修复为 chapters 数组，包含从 {start_chapter} 开始连续的 {chapter_count} 章。"
        "每项只保留 chapter_index、title、core_event、required_beats、ending_pressure。\n"
        f"待修复内容：\n{raw[:30_000]}"
    )


def _claim_safe_idempotency_key(value: str | None) -> str:
    if value is None:
        return uuid.uuid4().hex
    text = str(value).strip()
    if not text or len(text) > 200:
        raise OutlinePlanningError("Idempotency-Key must contain 1 to 200 characters")
    return text


def _operation_id(project_id: str, idempotency_key: str | None) -> str:
    key = _claim_safe_idempotency_key(idempotency_key)
    digest = hashlib.sha256(f"{project_id}\0{key}".encode("utf-8")).hexdigest()[:32]
    return f"plan_{digest}"


def _required_direction(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OutlinePlanningError("direction must be non-empty text")
    text = value.strip()
    if len(text) > 20_000:
        raise OutlinePlanningError("direction is too long")
    return text


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


def _operation_from_row(row: Any) -> PlanningOperation:
    return PlanningOperation(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        state=str(row["state"]),
        start_chapter=int(row["start_chapter"]),
        chapter_count=int(row["chapter_count"]),
        request_sha256=str(row["request_sha256"]),
        context_sha256=str(row["context_sha256"]),
        batch_id=str(row["batch_id"]) if row["batch_id"] else None,
        error=json.loads(str(row["error_json"])) if row["error_json"] else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "OutlinePlanner",
    "OutlinePlanningError",
    "PlanningOperation",
    "build_outline_messages",
    "collect_planning_context",
    "parse_outline_proposal",
]
