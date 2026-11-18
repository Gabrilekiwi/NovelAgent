"""Read-only app views of v3 projects, including interrupted transactions.

Metadata lookup is not a writer registry. Every authoritative display uses the
receipt-backed reader; damaged/pending state is displayed as a diagnostic.
"""
import json

from core.engine.safe_paths import assert_safe_local_tree
from core.story_project.paths import resolve_outline, resolve_prose, scan_prose_chapters
from core.story_project.mapper import extract_chapter_title
from core.story_project.reviewed_identity import load_verified_reviewed_identity, validate_reviewed_identity
from core.story_project.reviewed_state_document import digest, read_project_bytes, verify_current_sources
from core.story_project.reviewed_state_store import read_verified_commit_requests
from workbench.chapter_commits import ChapterCommitRecoveryRequired
from workbench.projects import ProjectRegistry


def _display_title(root, chapter, outline, prose):
    for source in (outline, prose):
        if source.path is None or source.conflict:
            continue
        text = read_project_bytes(root, source.path.relative_to(root).as_posix()).decode('utf-8')
        title = extract_chapter_title(text, source.path)
        if title and title != '无题':
            return title
    return f'第 {chapter} 章'


class ProjectDisplayRegistry(ProjectRegistry):
    """Only passed to GET view helpers; never to an executor or mutating service."""
    def _load_registered_identity(self, root):
        raw = json.loads(read_project_bytes(root, ".novelagent/project.json"))
        if raw.get("schema_version") == "3.0":
            return validate_reviewed_identity(raw)
        return super()._load_registered_identity(root)


class ReviewedProjectViews:
    def __init__(self, publication):
        self.publication = publication

    def inspect(self, record):
        root = record.root
        verified = None
        try:
            self.publication._require_settled_persistence(assert_safe_local_tree(root / ".novelagent/runtime/reviewed/p"))
            verified = load_verified_reviewed_identity(root)
            value = verified["history"]["value"]
            next_chapter = value["committed_through"] + 1
            verify_current_sources(root=root, value=value, next_chapter=next_chapter)
            return {"state":"ready", "message":"已核验已审核事实与提交历史。", "next_chapter":next_chapter,
                "verified":verified}
        except ChapterCommitRecoveryRequired:
            return {"state":"needs_recovery", "message":"存在尚未恢复的章节事务，请先恢复原事务。", "next_chapter":None}
        except (ValueError, OSError, RuntimeError) as exc:
            code = getattr(exc, "code", "reviewed_evidence_invalid")
            changed = code in {"reviewed_manual_source_changed", "reviewed_static_reference_changed"}
            return {"state":"source_changed" if changed else "evidence_invalid",
                "message":"人工来源已变更，需要重新审核；当前写作已暂停。" if changed else "提交历史未能通过核验，请保留原记录并检查恢复状态。",
                "next_chapter":None, "error_code":code, **({"verified":verified} if changed and verified else {})}

    def summary(self, record):
        inspection = self.inspect(record)
        ready = inspection["state"] == "ready"
        count = inspection["verified"]["history"]["value"]["committed_through"] if "verified" in inspection else None
        return {**record.to_dict(), "title":record.name, "genre":record.metadata.get("genre") or "未设置题材",
            "synopsis":record.metadata.get("synopsis") or "", "summary":record.metadata.get("synopsis") or "",
            "chapter_count":count, "last_chapter":count, "next_chapter":inspection["next_chapter"],
            "story_state_mode":"strict", "reviewed_format":True, "experimental":False,
            "capabilities":{"author_review":True, "automatic_generation":False, "outline_planning":False,
                "prose_amendment":False, "job_history":False},
            "status":"idle" if ready else "needs_recovery", "status_label":"已审核事实" if ready else "需要处理",
            "active_job":None, "pending_count":0 if ready else 1, "has_attention":not ready,
            "progress_label":f"已写至第 {count} 章" if count is not None else "待核验章节进度",
            "reviewed_status":{k:v for k,v in inspection.items() if k != "verified"}}

    def detail(self, record):
        summary = self.summary(record)
        return {"project":summary, "overview":{"chapter_count":summary["chapter_count"],
            "next_chapter":summary["next_chapter"], "word_count_label":"—", "pending_count":summary["pending_count"],
            "current_tasks":[], "recent_history":[]}, "health":self.health(record, summary=summary)}

    def health(self, record, *, summary=None):
        summary = summary or self.summary(record)
        status = summary["reviewed_status"]
        return {"reviewed_format":True, "project_id":record.id, "production_eligible":False,
            "label":"已审核事实" if status["state"] == "ready" else "需要处理",
            "mode_label":"已审核事实", "checks":[{"label":"已审核事实与提交历史", "detail":status["message"],
                "state":"pass" if status["state"] == "ready" else "failed"}],
            "summary":status["message"], "reviewed_status":status,
            "validation":{"ok":status["state"] == "ready", "problems":[]}}

    def export(self, record):
        publications = {r["chapter"]:r for r in self._publications(record)}
        sections = []
        for chapter in sorted(scan_prose_chapters(record.root)):
            prose = resolve_prose(record.root, chapter)
            if prose.path is None or prose.conflict:
                raise ChapterCommitRecoveryRequired("published prose path cannot be resolved for export")
            raw = read_project_bytes(record.root, prose.path.relative_to(record.root).as_posix())
            if chapter in publications and digest(raw) != publications[chapter]["prose_sha256"]:
                raise ChapterCommitRecoveryRequired("published prose changed during export")
            sections.append(f"===== 第 {chapter} 章 =====\n\n{raw.decode('utf-8').rstrip()}")
        if {r["chapter"]:r for r in self._publications(record)} != publications:
            raise ChapterCommitRecoveryRequired("publication history changed during export")
        return sections

    def _publications(self, record, *, inspection=None):
        inspection = inspection or self.inspect(record)
        if "verified" not in inspection:
            raise ChapterCommitRecoveryRequired(inspection["message"])
        verified = inspection["verified"]
        return read_verified_commit_requests(root=record.root, book_id=record.book_id,
            root_map={"story_project":record.root,"runtime":record.root / ".novelagent/runtime"},
            baseline_document_sha256=verified["identity"]["reviewed_state"]["baseline_document_sha256"])

    def chapters(self, record):
        inspection = self.inspect(record)
        if "verified" not in inspection:
            return []
        publications = {r["chapter"]:r for r in self._publications(record, inspection=inspection)}
        next_chapter = record.next_chapter
        indexes = set(scan_prose_chapters(record.root)) | {next_chapter}
        items = []
        for chapter in sorted(indexes):
            prose, outline = resolve_prose(record.root, chapter), resolve_outline(record.root, chapter)
            exists = prose.path is not None and not prose.conflict
            items.append({"chapter":chapter, "title":_display_title(record.root,chapter,outline,prose), "has_prose":exists,
                "has_outline":outline.path is not None and not outline.conflict, "status":"committed" if exists else "draft",
                "publication":publications.get(chapter), "reviewed_format":True})
        return items

    def chapter(self, record, chapter):
        inspection = self.inspect(record)
        if "verified" not in inspection:
            return {"project":{"id":record.id,"title":record.name}, "chapter":chapter, "title":f"第 {chapter} 章",
                "reviewed_format":True, "reviewed_status":inspection, "has_prose":False, "has_outline":False,
                "status":"needs_recovery", "current_prose":None, "versions":[], "review":{}}
        publications = {r["chapter"]:r for r in self._publications(record, inspection=inspection)}
        prose, outline = resolve_prose(record.root, chapter), resolve_outline(record.root, chapter)
        raw = None if prose.path is None or prose.conflict else read_project_bytes(record.root, prose.path.relative_to(record.root).as_posix())
        published = publications.get(chapter)
        if published and (raw is None or digest(raw) != published["prose_sha256"]):
            raise ChapterCommitRecoveryRequired("published prose changed during display")
        return {"project":{"id":record.id,"title":record.name}, "chapter":chapter, "title":_display_title(record.root,chapter,outline,prose),
            "reviewed_format":True, "has_prose":raw is not None, "has_outline":outline.path is not None and not outline.conflict,
            "status":"committed" if raw is not None else "draft", "current_prose":None if raw is None else raw.decode("utf-8"),
            "current_sha256":None if raw is None else digest(raw), "publication":published,
            "outline":None if outline.path is None or outline.conflict else read_project_bytes(record.root, outline.path.relative_to(record.root).as_posix()).decode("utf-8"),
            "versions":[], "review":{}, "run":None}
