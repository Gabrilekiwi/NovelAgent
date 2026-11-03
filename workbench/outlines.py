from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
import hashlib
import json
import sqlite3
import stat
import uuid

from core.engine.persistence import (
    LocalPersistenceTransaction,
    PersistenceTarget,
    persistence_run_lock,
)
from core.story_project.identity import load_project_identity
from core.story_project.paths import canonical_outline_path, infer_next_chapter, resolve_outline
from core.story_project.read_set import (
    capture_story_project_read_set,
    declared_read_set_writes,
)
from workbench.book_writes import registered_book_write, registered_record_write
from workbench.database import WorkbenchDatabase
from workbench.projects import ProjectRecord, ProjectRegistry


class OutlineBatchError(ValueError):
    """Raised when an outline batch is invalid, stale, or unsafe to publish."""


class OutlineBatchConflict(OutlineBatchError):
    """Raised when a revision, hash, source, or filesystem CAS fails."""


@dataclass(frozen=True)
class OutlineItem:
    chapter_index: int
    title: str
    core_event: str
    required_beats: tuple[str, ...]
    ending_pressure: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "chapter_index": self.chapter_index,
            "title": self.title,
            "core_event": self.core_event,
            "required_beats": list(self.required_beats),
            "ending_pressure": self.ending_pressure,
        }


@dataclass(frozen=True)
class OutlineBatchRecord:
    id: str
    project_id: str
    start_chapter: int
    end_chapter: int
    status: str
    revision: int
    items: tuple[OutlineItem, ...]
    proposal_sha256: str
    source_fingerprint: str
    created_at: str
    updated_at: str
    approved_at: str | None = None
    approval_manifest_path: str | None = None
    approval_receipt_sha256: str | None = None

    @property
    def source_state_sha(self) -> str:
        """Public CAS alias used by the browser editing contract."""

        return self.proposal_sha256

    def to_dict(self) -> dict[str, Any]:
        items = [item.to_dict() for item in self.items]
        return {
            "id": self.id,
            "project_id": self.project_id,
            "start_chapter": self.start_chapter,
            "end_chapter": self.end_chapter,
            "status": self.status,
            "revision": self.revision,
            "items": items,
            "proposal": items,
            "proposal_sha256": self.proposal_sha256,
            "source_state_sha": self.source_state_sha,
            "source_fingerprint": self.source_fingerprint,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "approved_at": self.approved_at,
            "approval_manifest_path": self.approval_manifest_path,
            "approval_receipt_sha256": self.approval_receipt_sha256,
        }


class OutlineBatchService:
    def __init__(
        self,
        database: WorkbenchDatabase,
        projects: ProjectRegistry,
    ) -> None:
        self.database = database
        self.projects = projects
        self.database.initialize()

    def create_batch(
        self,
        project_id: str,
        proposal: Sequence[Mapping[str, Any]],
        *,
        expected_source_fingerprint: str | None = None,
    ) -> OutlineBatchRecord:
        with registered_book_write(self.projects, project_id, database=self.database) as write:
            return self._create_batch_owned(write, project_id, proposal, expected_source_fingerprint=expected_source_fingerprint)

    def _create_batch_owned(
        self, write,
        project_id: str,
        proposal: Sequence[Mapping[str, Any]],
        *,
        expected_source_fingerprint: str | None = None,
    ) -> OutlineBatchRecord:
        project = write.project
        items = validate_outline_items(proposal)
        next_chapter = infer_next_chapter(project.root)
        if items[0].chapter_index != next_chapter:
            raise OutlineBatchError(
                "outline batch must start at the canonical next chapter: "
                f"expected {next_chapter}, got {items[0].chapter_index}"
            )
        source_fingerprint = compute_source_fingerprint(project)
        if (
            expected_source_fingerprint is not None
            and source_fingerprint != str(expected_source_fingerprint)
        ):
            raise OutlineBatchConflict(
                "StoryProject sources changed while the outline proposal was being generated"
            )
        proposal_json = _proposal_json(items)
        proposal_sha256 = _sha256(proposal_json.encode("utf-8"))
        timestamp = _utc_now()
        batch_id = f"ob_{uuid.uuid4().hex[:24]}"
        with write.transaction() as connection:
            connection.execute(
                """
                INSERT INTO outline_batches(
                    id, project_id, start_chapter, end_chapter, status, revision,
                    proposal_json, proposal_sha256, source_fingerprint,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'draft', 1, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    project.id,
                    items[0].chapter_index,
                    items[-1].chapter_index,
                    proposal_json,
                    proposal_sha256,
                    source_fingerprint,
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM outline_batches WHERE id = ?", (batch_id,)
            ).fetchone()
        return _record_from_row(row)

    def update_batch(
        self,
        batch_id: str,
        proposal: Sequence[Mapping[str, Any]],
        *,
        expected_revision: int,
        expected_sha256: str,
    ) -> OutlineBatchRecord:
        with registered_record_write(self.projects, 'outline_batches', batch_id, database=self.database) as write:
            return self._update_batch_owned(write, batch_id, proposal, expected_revision=expected_revision, expected_sha256=expected_sha256)

    def _update_batch_owned(
        self, write,
        batch_id: str,
        proposal: Sequence[Mapping[str, Any]],
        *,
        expected_revision: int,
        expected_sha256: str,
    ) -> OutlineBatchRecord:
        _validate_cas(expected_revision, expected_sha256)
        items = validate_outline_items(proposal)
        proposal_json = _proposal_json(items)
        proposal_sha256 = _sha256(proposal_json.encode("utf-8"))
        timestamp = _utc_now()
        with write.transaction() as connection:
            current = connection.execute(
                "SELECT * FROM outline_batches WHERE id = ?", (str(batch_id),)
            ).fetchone()
            if current is None:
                raise OutlineBatchError(f"unknown outline batch: {batch_id}")
            if current["status"] != "draft":
                raise OutlineBatchConflict("approved outline batches are immutable")
            project = self.projects.get(str(current["project_id"]))
            next_chapter = infer_next_chapter(project.root)
            if items[0].chapter_index != next_chapter:
                raise OutlineBatchConflict(
                    "outline batch no longer starts at the canonical next chapter"
                )
            cursor = connection.execute(
                """
                UPDATE outline_batches
                SET start_chapter = ?, end_chapter = ?, revision = revision + 1,
                    proposal_json = ?, proposal_sha256 = ?, updated_at = ?
                WHERE id = ? AND status = 'draft' AND revision = ? AND proposal_sha256 = ?
                """,
                (
                    items[0].chapter_index,
                    items[-1].chapter_index,
                    proposal_json,
                    proposal_sha256,
                    timestamp,
                    str(batch_id),
                    expected_revision,
                    expected_sha256,
                ),
            )
            if cursor.rowcount != 1:
                raise OutlineBatchConflict(
                    "outline batch changed; reload before saving your edits"
                )
            row = connection.execute(
                "SELECT * FROM outline_batches WHERE id = ?", (str(batch_id),)
            ).fetchone()
        return _record_from_row(row)

    def approve_batch(
        self,
        batch_id: str,
        *,
        expected_revision: int,
        expected_sha256: str,
        adopt_existing: bool = False,
    ) -> OutlineBatchRecord:
        with registered_record_write(self.projects, 'outline_batches', batch_id, database=self.database) as write:
            return self._approve_batch_owned(write, batch_id, expected_revision=expected_revision, expected_sha256=expected_sha256, adopt_existing=adopt_existing)

    def _approve_batch_owned(
        self, write,
        batch_id: str,
        *,
        expected_revision: int,
        expected_sha256: str,
        adopt_existing: bool = False,
    ) -> OutlineBatchRecord:
        _validate_cas(expected_revision, expected_sha256)
        with write.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM outline_batches WHERE id = ?", (str(batch_id),)
            ).fetchone()
            if row is None:
                raise OutlineBatchError(f"unknown outline batch: {batch_id}")
            batch = _record_from_row(row)
            if batch.status == "approved":
                if (
                    batch.revision == expected_revision
                    and batch.proposal_sha256 == expected_sha256
                ):
                    return batch
                raise OutlineBatchConflict("outline batch is already approved at another revision")
            if batch.revision != expected_revision or batch.proposal_sha256 != expected_sha256:
                raise OutlineBatchConflict(
                    "outline batch changed; reload before approving it"
                )

            project = self.projects.get(batch.project_id)
            runtime = _approval_runtime(project.root, batch.id)
            recovered = _load_committed_approval(runtime, batch, project)
            if recovered is not None:
                return self._mark_approved(connection, batch, recovered)

            collisions = _existing_outline_paths(project.root, batch)
            if collisions and not adopt_existing:
                raise OutlineBatchConflict(
                    "outline publication refused because chapter files already exist: "
                    + ", ".join(str(path) for path in collisions)
                )
            existing_chapters = (
                _matching_existing_outline_chapters(project.root, batch)
                if adopt_existing
                else frozenset()
            )
            actual_source = compute_source_fingerprint(project)
            if actual_source != batch.source_fingerprint:
                raise OutlineBatchConflict(
                    "StoryProject sources changed after this batch was drafted; regenerate the batch"
                )
            if infer_next_chapter(project.root) != batch.start_chapter:
                raise OutlineBatchConflict(
                    "canonical next chapter changed after this batch was drafted"
                )

            write.require(connection)
            runtime.mkdir(parents=True, exist_ok=True)
            publication = _build_publication(
                batch,
                project,
                runtime,
                adopted_existing_chapters=existing_chapters,
            )
            lock_root = project.root / ".novelagent" / "runtime" / "wb" / "outline.lock"
            with persistence_run_lock(lock_root):
                if compute_source_fingerprint(project) != batch.source_fingerprint:
                    raise OutlineBatchConflict(
                        "StoryProject sources changed while approval was starting"
                    )
                if existing_chapters != _matching_existing_outline_chapters(project.root, batch):
                    raise OutlineBatchConflict(
                        "existing outline files changed while approval was starting"
                    )
                result = _publish_batch(batch, project, runtime, publication)
            if not result["committed"]:
                raise OutlineBatchConflict(
                    "outline publication was rolled back: " + result["state"]
                )
            return self._mark_approved(connection, batch, publication)

    def get_batch(self, batch_id: str) -> OutlineBatchRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM outline_batches WHERE id = ?", (str(batch_id),)
            ).fetchone()
        if row is None:
            raise OutlineBatchError(f"unknown outline batch: {batch_id}")
        return _record_from_row(row)

    def adopt_existing_and_approve_batch(
        self,
        batch_id: str,
        *,
        expected_revision: int,
        expected_sha256: str,
    ) -> OutlineBatchRecord:
        with registered_record_write(self.projects, 'outline_batches', batch_id, database=self.database) as write:
            return self._adopt_existing_and_approve_batch_owned(write, batch_id, expected_revision=expected_revision, expected_sha256=expected_sha256)

    def _adopt_existing_and_approve_batch_owned(
        self, write,
        batch_id: str,
        *,
        expected_revision: int,
        expected_sha256: str,
    ) -> OutlineBatchRecord:
        """Preserve canonical existing outlines while publishing the missing batch files.

        An author may already have written part of the next batch outside the
        workbench.  Replacing those files would be unsafe, so this path first
        imports only byte-for-byte canonical files into the draft, then records
        them in the same immutable approval manifest as newly published files.
        """

        _validate_cas(expected_revision, expected_sha256)
        batch = self.get_batch(batch_id)
        if batch.status != "draft":
            raise OutlineBatchConflict("only a draft outline batch can adopt existing files")
        if batch.revision != expected_revision or batch.proposal_sha256 != expected_sha256:
            raise OutlineBatchConflict("outline batch changed; reload before adopting existing files")
        project = self.projects.get(batch.project_id)
        adopted_items, adopted_chapters = _adopt_existing_outline_items(project.root, batch)
        if not adopted_chapters:
            raise OutlineBatchConflict("there are no existing canonical outline files to adopt")
        if adopted_items != batch.items:
            batch = self.update_batch(
                batch.id,
                [item.to_dict() for item in adopted_items],
                expected_revision=batch.revision,
                expected_sha256=batch.proposal_sha256,
            )
        return self.approve_batch(
            batch.id,
            expected_revision=batch.revision,
            expected_sha256=batch.proposal_sha256,
            adopt_existing=True,
        )

    def list_batches(self, project_id: str) -> tuple[OutlineBatchRecord, ...]:
        self.projects.get(project_id)
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM outline_batches WHERE project_id = ? ORDER BY created_at DESC",
                (str(project_id),),
            ).fetchall()
        return tuple(_record_from_row(row) for row in rows)

    def _mark_approved(
        self,
        connection: sqlite3.Connection,
        batch: OutlineBatchRecord,
        publication: Mapping[str, Any],
    ) -> OutlineBatchRecord:
        receipt = publication["receipt"]
        manifest_path = str(publication["manifest_path"])
        cursor = connection.execute(
            """
            UPDATE outline_batches
            SET status = 'approved', approved_at = ?, updated_at = ?,
                approval_manifest_path = ?, approval_receipt_sha256 = ?
            WHERE id = ? AND status = 'draft' AND revision = ? AND proposal_sha256 = ?
            """,
            (
                receipt["approved_at"],
                receipt["approved_at"],
                manifest_path,
                receipt["receipt_sha256"],
                batch.id,
                batch.revision,
                batch.proposal_sha256,
            ),
        )
        if cursor.rowcount != 1:
            current = connection.execute(
                "SELECT * FROM outline_batches WHERE id = ?", (batch.id,)
            ).fetchone()
            if current is not None and current["status"] == "approved":
                return _record_from_row(current)
            raise OutlineBatchConflict(
                "outline files were published but the database approval requires reconciliation"
            )
        row = connection.execute(
            "SELECT * FROM outline_batches WHERE id = ?", (batch.id,)
        ).fetchone()
        return _record_from_row(row)


def validate_outline_items(value: Sequence[Mapping[str, Any]]) -> tuple[OutlineItem, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not value:
        raise OutlineBatchError("outline proposal must contain at least one chapter")
    items: list[OutlineItem] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise OutlineBatchError(f"outline proposal item {index} must be an object")
        chapter = raw.get("chapter_index")
        if not isinstance(chapter, int) or isinstance(chapter, bool) or chapter < 1:
            raise OutlineBatchError(f"outline proposal item {index} has an invalid chapter_index")
        beats = raw.get("required_beats")
        if isinstance(beats, (str, bytes)) or not isinstance(beats, Sequence) or not beats:
            raise OutlineBatchError(
                f"outline proposal chapter {chapter} requires at least one required beat"
            )
        normalized_beats = tuple(
            _required_text(f"chapter {chapter} required_beats[{beat_index}]", beat)
            for beat_index, beat in enumerate(beats)
        )
        items.append(
            OutlineItem(
                chapter_index=chapter,
                title=_required_text(f"chapter {chapter} title", raw.get("title")),
                core_event=_required_text(
                    f"chapter {chapter} core_event", raw.get("core_event")
                ),
                required_beats=normalized_beats,
                ending_pressure=_required_text(
                    f"chapter {chapter} ending_pressure", raw.get("ending_pressure")
                ),
            )
        )
    chapters = [item.chapter_index for item in items]
    expected = list(range(chapters[0], chapters[0] + len(chapters)))
    if chapters != expected:
        raise OutlineBatchError(
            "outline proposal chapters must be strictly ordered and contiguous"
        )
    return tuple(items)


def _existing_outline_paths(project_root: Path, batch: OutlineBatchRecord) -> tuple[Path, ...]:
    paths: list[Path] = []
    for item in batch.items:
        paths.extend(resolve_outline(project_root, item.chapter_index).candidates)
    return tuple(paths)


def _adopt_existing_outline_items(
    project_root: Path,
    batch: OutlineBatchRecord,
) -> tuple[tuple[OutlineItem, ...], frozenset[int]]:
    items: list[OutlineItem] = []
    adopted_chapters: set[int] = set()
    for item in batch.items:
        resolution = resolve_outline(project_root, item.chapter_index)
        if not resolution.candidates:
            items.append(item)
            continue
        if len(resolution.candidates) != 1 or resolution.path != resolution.canonical_path:
            raise OutlineBatchConflict(
                f"existing outline for chapter {item.chapter_index} is ambiguous or non-canonical"
            )
        items.append(_parse_existing_canonical_outline(resolution.path, item.chapter_index))
        adopted_chapters.add(item.chapter_index)
    return tuple(items), frozenset(adopted_chapters)


def _matching_existing_outline_chapters(
    project_root: Path,
    batch: OutlineBatchRecord,
) -> frozenset[int]:
    existing_items, chapters = _adopt_existing_outline_items(project_root, batch)
    for expected, existing in zip(batch.items, existing_items):
        if expected.chapter_index in chapters and expected != existing:
            raise OutlineBatchConflict(
                f"existing outline for chapter {expected.chapter_index} does not match the adopted draft"
            )
    return chapters


def _parse_existing_canonical_outline(path: Path, chapter_index: int) -> OutlineItem:
    """Parse only the exact canonical outline form that can be safely adopted."""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OutlineBatchConflict(f"cannot read existing outline: {path}") from exc
    lines = text.splitlines()
    heading = f"# 第{chapter_index}章 "
    if (
        not text.endswith("\n")
        or "\r" in text
        or len(lines) < 9
        or not lines[0].startswith(heading)
        or lines[1] != ""
        or not lines[2].startswith("核心事件：")
        or lines[3] != ""
        or lines[4] != "## 必写节拍"
        or lines[5] != ""
        or lines[-2] != ""
        or not lines[-1].startswith("结尾压力：")
        or any(not line.startswith("- ") for line in lines[6:-2])
    ):
        raise OutlineBatchConflict(
            f"existing outline for chapter {chapter_index} is not in canonical workbench format"
        )
    item = OutlineItem(
        chapter_index=chapter_index,
        title=lines[0][len(heading) :],
        core_event=lines[2][len("核心事件：") :],
        required_beats=tuple(line[2:] for line in lines[6:-2]),
        ending_pressure=lines[-1][len("结尾压力：") :],
    )
    checked = validate_outline_items([item.to_dict()])[0]
    if render_canonical_outline(checked) != text:
        raise OutlineBatchConflict(
            f"existing outline for chapter {chapter_index} is not byte-for-byte canonical"
        )
    return checked


def render_canonical_outline(item: OutlineItem) -> str:
    beats = "\n".join(f"- {beat}" for beat in item.required_beats)
    return (
        f"# 第{item.chapter_index}章 {item.title}\n\n"
        f"核心事件：{item.core_event}\n\n"
        "## 必写节拍\n\n"
        f"{beats}\n\n"
        f"结尾压力：{item.ending_pressure}\n"
    )


def compute_source_fingerprint(project: ProjectRecord) -> str:
    root = project.root.resolve(strict=True)
    relative_files: list[Path] = [Path(".novelagent/project.json")]
    for directory_name in ("设定", "大纲", "正文", "追踪"):
        directory = root / directory_name
        relative_files.extend(_safe_source_files(root, directory))

    digest = hashlib.sha256()
    digest.update(b"NovelAgent-workbench-source-v1\0")
    digest.update(project.book_id.encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(infer_next_chapter(root)).encode("ascii"))
    digest.update(b"\0")
    for relative in sorted(set(relative_files), key=lambda path: path.as_posix()):
        path = root / relative
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise OutlineBatchConflict(f"cannot read StoryProject source: {path}") from exc
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(content).digest())
        digest.update(b"\0")
    return digest.hexdigest()


def _safe_source_files(root: Path, directory: Path) -> list[Path]:
    try:
        directory_info = directory.lstat()
        directory.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise OutlineBatchConflict(
            f"StoryProject source directory is missing or unsafe: {directory}"
        ) from exc
    if not stat.S_ISDIR(directory_info.st_mode) or _is_link_or_reparse(directory_info):
        raise OutlineBatchConflict(
            f"links and reparse points are not allowed in StoryProject sources: {directory}"
        )
    files: list[Path] = []
    pending = [directory]
    while pending:
        current = pending.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise OutlineBatchConflict(f"cannot inspect StoryProject source: {current}") from exc
        for candidate in entries:
            try:
                info = candidate.lstat()
                candidate.resolve(strict=True).relative_to(root)
            except (OSError, ValueError) as exc:
                raise OutlineBatchConflict(f"cannot safely inspect StoryProject source: {candidate}") from exc
            if _is_link_or_reparse(info):
                raise OutlineBatchConflict(
                    f"links and reparse points are not allowed in StoryProject sources: {candidate}"
                )
            if stat.S_ISREG(info.st_mode):
                files.append(candidate.relative_to(root))
            elif stat.S_ISDIR(info.st_mode):
                pending.append(candidate)
            else:
                raise OutlineBatchConflict(
                    f"unsupported StoryProject source entry: {candidate}"
                )
    return files


def _is_link_or_reparse(info: Any) -> bool:
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    attributes = int(getattr(info, "st_file_attributes", 0))
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_flag)


def _build_publication(
    batch: OutlineBatchRecord,
    project: ProjectRecord,
    runtime: Path,
    *,
    adopted_existing_chapters: frozenset[int] = frozenset(),
) -> dict[str, Any]:
    approved_at = _utc_now()
    files = []
    for item in batch.items:
        path = canonical_outline_path(project.root, item.chapter_index).resolve(strict=False)
        text = render_canonical_outline(item)
        files.append(
            {
                "chapter_index": item.chapter_index,
                "relative_path": path.relative_to(project.root).as_posix(),
                "sha256": _sha256(text.encode("utf-8")),
                "content": text,
                "path": path,
                "adopted_existing": item.chapter_index in adopted_existing_chapters,
            }
        )
    manifest = {
        "schema_version": "1.0",
        "kind": "workbench_outline_approval_manifest",
        "batch_id": batch.id,
        "project_id": batch.project_id,
        "book_id": project.book_id,
        "revision": batch.revision,
        "proposal_sha256": batch.proposal_sha256,
        "source_fingerprint": batch.source_fingerprint,
        "approved_at": approved_at,
        "files": [
            {key: entry[key] for key in ("chapter_index", "relative_path", "sha256")}
            for entry in files
        ],
    }
    if adopted_existing_chapters:
        manifest["adopted_existing_chapters"] = sorted(adopted_existing_chapters)
    manifest["manifest_sha256"] = _canonical_hash(manifest)
    receipt = {
        "schema_version": "1.0",
        "kind": "workbench_outline_approval_receipt",
        "batch_id": batch.id,
        "project_id": batch.project_id,
        "book_id": project.book_id,
        "revision": batch.revision,
        "proposal_sha256": batch.proposal_sha256,
        "source_fingerprint": batch.source_fingerprint,
        "manifest_sha256": manifest["manifest_sha256"],
        "approved_at": approved_at,
    }
    receipt["receipt_sha256"] = _canonical_hash(receipt)
    return {
        "files": files,
        "manifest": manifest,
        "receipt": receipt,
        "manifest_path": runtime / "approval_manifest.json",
        "receipt_path": runtime / "approval_receipt.json",
    }


def _publish_batch(
    batch: OutlineBatchRecord,
    project: ProjectRecord,
    runtime: Path,
    publication: Mapping[str, Any],
) -> dict[str, Any]:
    targets = [
        PersistenceTarget(
            "outline",
            entry["path"],
            entry["content"],
            metadata={"batch_id": batch.id, "chapter_index": entry["chapter_index"]},
            expected_before_exists=False,
        )
        for entry in publication["files"]
        if not entry["adopted_existing"]
    ]
    targets.extend(
        (
            PersistenceTarget(
                "outline_approval_manifest",
                publication["manifest_path"],
                _pretty_json(publication["manifest"]),
                expected_before_exists=False,
            ),
            PersistenceTarget(
                "outline_approval_receipt",
                publication["receipt_path"],
                _pretty_json(publication["receipt"]),
                expected_before_exists=False,
            ),
        )
    )
    identity = load_project_identity(project.root)
    if identity is None or identity.book_id != project.book_id:
        raise OutlineBatchConflict("ProjectIdentity changed before outline publication")
    read_set = capture_story_project_read_set(
        project.root,
        batch.start_chapter,
        project_identity=identity,
    )
    declared_writes = declared_read_set_writes(
        read_set,
        (
            (
                target.path,
                _sha256(target.content_bytes()),
                len(target.content_bytes()),
            )
            for target in targets
        ),
    )
    transaction = LocalPersistenceTransaction(
        run_dir=runtime,
        transactions_dir=runtime / "tx",
        run_id=f"a{batch.revision}_{uuid.uuid4().hex[:8]}",
        allowed_roots=[project.root],
        book_id=project.book_id,
        story_project_read_set=read_set,
        read_set_declared_writes=declared_writes,
    )
    transaction.prepare(targets)
    committed = transaction.commit()
    if committed.committed:
        committed = transaction.complete_publication()
    return {
        "committed": committed.committed,
        "state": committed.state,
        "journal_path": committed.journal_path,
    }


def _load_committed_approval(
    runtime: Path,
    batch: OutlineBatchRecord,
    project: ProjectRecord,
) -> dict[str, Any] | None:
    receipt_path = runtime / "approval_receipt.json"
    manifest_path = runtime / "approval_manifest.json"
    if not receipt_path.exists() and not manifest_path.exists():
        return None
    if not receipt_path.is_file() or not manifest_path.is_file():
        raise OutlineBatchConflict("outline approval evidence is incomplete")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OutlineBatchConflict("outline approval evidence is unreadable") from exc
    expected = {
        "batch_id": batch.id,
        "project_id": batch.project_id,
        "book_id": project.book_id,
        "revision": batch.revision,
        "proposal_sha256": batch.proposal_sha256,
        "source_fingerprint": batch.source_fingerprint,
    }
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise OutlineBatchConflict("outline approval receipt does not match the draft")
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise OutlineBatchConflict("outline approval manifest does not match the draft")
    if receipt.get("receipt_sha256") != _canonical_hash(
        receipt, exclude="receipt_sha256"
    ):
        raise OutlineBatchConflict("outline approval receipt hash is invalid")
    if manifest.get("manifest_sha256") != _canonical_hash(
        manifest, exclude="manifest_sha256"
    ):
        raise OutlineBatchConflict("outline approval manifest hash is invalid")
    if receipt.get("manifest_sha256") != manifest.get("manifest_sha256"):
        raise OutlineBatchConflict("outline approval evidence hashes disagree")
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) != len(batch.items):
        raise OutlineBatchConflict("outline approval manifest has an invalid file set")
    for entry, item in zip(files, batch.items):
        if not isinstance(entry, dict):
            raise OutlineBatchConflict("outline approval manifest has an invalid file entry")
        expected_path = canonical_outline_path(project.root, item.chapter_index).relative_to(
            project.root
        ).as_posix()
        expected_hash = _sha256(render_canonical_outline(item).encode("utf-8"))
        if (
            entry.get("chapter_index") != item.chapter_index
            or entry.get("relative_path") != expected_path
            or entry.get("sha256") != expected_hash
        ):
            raise OutlineBatchConflict("outline approval manifest differs from the draft")
        relative = Path(str(entry.get("relative_path", "")))
        path = (project.root / relative).resolve(strict=False)
        if not _is_relative_to(path, project.root) or not path.is_file():
            raise OutlineBatchConflict("published outline evidence points to an unsafe file")
        if _sha256(path.read_bytes()) != entry.get("sha256"):
            raise OutlineBatchConflict("published outline differs from its approval manifest")
    return {
        "receipt": receipt,
        "manifest": manifest,
        "receipt_path": receipt_path,
        "manifest_path": manifest_path,
        "files": files,
    }


def _record_from_row(row: sqlite3.Row | None) -> OutlineBatchRecord:
    if row is None:
        raise OutlineBatchError("outline batch record is missing")
    try:
        raw_items = json.loads(str(row["proposal_json"]))
    except (TypeError, ValueError) as exc:
        raise OutlineBatchError("stored outline proposal is invalid JSON") from exc
    items = validate_outline_items(raw_items)
    if _sha256(_proposal_json(items).encode("utf-8")) != row["proposal_sha256"]:
        raise OutlineBatchError("stored outline proposal hash is invalid")
    return OutlineBatchRecord(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        start_chapter=int(row["start_chapter"]),
        end_chapter=int(row["end_chapter"]),
        status=str(row["status"]),
        revision=int(row["revision"]),
        items=items,
        proposal_sha256=str(row["proposal_sha256"]),
        source_fingerprint=str(row["source_fingerprint"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        approved_at=str(row["approved_at"]) if row["approved_at"] is not None else None,
        approval_manifest_path=(
            str(row["approval_manifest_path"])
            if row["approval_manifest_path"] is not None
            else None
        ),
        approval_receipt_sha256=(
            str(row["approval_receipt_sha256"])
            if row["approval_receipt_sha256"] is not None
            else None
        ),
    )


def _approval_runtime(root: Path, batch_id: str) -> Path:
    return root / ".novelagent" / "runtime" / "wb" / "ob" / batch_id


def _proposal_json(items: Sequence[OutlineItem]) -> str:
    return json.dumps(
        [item.to_dict() for item in items],
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _pretty_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _canonical_hash(value: Mapping[str, Any], *, exclude: str | None = None) -> str:
    payload = {key: item for key, item in value.items() if key != exclude}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256(encoded)


def _validate_cas(revision: int, sha256: str) -> None:
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise OutlineBatchError("expected_revision must be a positive integer")
    if (
        not isinstance(sha256, str)
        or len(sha256) != 64
        or any(character not in "0123456789abcdef" for character in sha256)
    ):
        raise OutlineBatchError("expected_sha256 must be a lowercase SHA-256")


def _required_text(field: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OutlineBatchError(f"{field} must be non-empty text")
    return value.strip()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_relative_to(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "OutlineBatchConflict",
    "OutlineBatchError",
    "OutlineBatchRecord",
    "OutlineBatchService",
    "OutlineItem",
    "compute_source_fingerprint",
    "render_canonical_outline",
    "validate_outline_items",
]
