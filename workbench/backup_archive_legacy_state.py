"""Reconstruct legacy semantic sources from archive members, never source paths."""
import json
from pathlib import Path, PurePosixPath

from core.story_project.activation import evaluate_story_state_activation, StoryStateActivationError
from core.story_project.identity import validate_project_identity
from core.story_project.model import CORE_DIRECTORY_NAMES
from core.story_project.paths import outline_chapter_index, prose_chapter_index
from core.story_project.semantic_contracts import validate_story_project_semantic_state
from core.story_project.semantic_parser import _SemanticStateBuilder, _detect_layout_profile
from core.story_project.reviewed_state_document import digest
from workbench.backup_archive import BackupArchiveError
from workbench.backup_archive_view import verified_archive_view


def inspect_archived_legacy_state(package, *, project_id, book_id):
    with verified_archive_view(package) as view:
        preview = view.manifest["preview"]
        tree = preview["scope"]["inventories"]["story_project"]
        root = Path(tree["source_root"])
        identity = validate_project_identity(json.loads(view.read_root("story_project", ".novelagent/project.json")))
        if (preview["project_id"] != project_id or preview["book_id"] != book_id
                or identity.book_id != book_id or not root.is_absolute() or ".." in root.parts):
            raise BackupArchiveError("归档 legacy 语义状态的目标身份不同。")
        state = inspect_legacy_state(identity, tree, lambda name:view.read_root("story_project", name))
        result = {"schema_version":"archived_legacy_semantic_state_v1",
            "archive_id":view.verified["archive_id"], "archive_receipt_sha256":view.verified["receipt_sha256"],
            "project_id":project_id, "book_id":book_id, "identity":identity.to_dict(),
            **state,
            "restore_plan_ready":False, "recoverable":False, "write_activation_allowed":False}
        result["verification_sha256"] = digest(result)
    return result


def inspect_legacy_state(identity, tree, read_bytes):
    """Pure, shared evidence for archived and physically bound legacy readers."""
    root=Path(tree["source_root"])
    book_id=identity.book_id
    if not root.is_absolute() or ".." in root.parts:
        raise BackupArchiveError("legacy 状态缺少明确的作品根。")
    if (identity.ephemeral or identity.story_state_mode != "strict"
            or identity.authority["mode"] != "legacy_markdown_v1"
            or identity.authority["minimum_writer_contract"] > 2):
        raise BackupArchiveError("归档 legacy 状态不符合受支持的 Strict 契约。")
    receipts = ".novelagent/authority/receipts"
    if receipts in tree["files"] or any(name.startswith(receipts + "/")
            for name in set(tree["files"]) | set(tree["directories"])):
        raise BackupArchiveError("归档 legacy 身份存在待处理的事件权威凭据。")
    blockers, continuation = [], []
    state = activation = None
    sources = {}
    outlines, prose = {}, {}
    for name in sorted(tree["files"]):
        path = PurePosixPath(name)
        directory = str(path.parent)
        if directory not in {"正文", "大纲"}:
            continue
        chapter = (prose_chapter_index(path.name) if directory == "正文" else outline_chapter_index(path.name))
        if chapter is not None:
            (prose if directory == "正文" else outlines).setdefault(chapter, []).append(name)
    next_chapter = 1
    while next_chapter in prose:
        next_chapter += 1
    for chapter, names in prose.items():
        if len(names) > 1:
            blockers.append({"code":"legacy_prose_alias", "reference":str(chapter)})
    for chapter, names in outlines.items():
        if len(names) > 1:
            blockers.append({"code":"legacy_outline_alias", "reference":str(chapter)})
    if any(chapter >= next_chapter for chapter in prose):
        continuation.append({"code":"legacy_prose_sequence_gap", "reference":str(next_chapter)})
    for name in CORE_DIRECTORY_NAMES:
        if name not in tree["directories"]:
            blockers.append({"code":"legacy_core_directory_missing", "reference":name})

    def add(role, name):
        # Identical BOM/universal-newline semantics to loader.load_text.
        text = read_bytes(name).decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        sources[role] = {"path":str(root / name), "relative_path":name, "text":text,
                         "chars":len(text), "sha256":digest(text.encode("utf-8"))}

    try:
        if len(outlines.get(next_chapter, [])) == 1:
            add("outline", outlines[next_chapter][0])
        else:
            # Backups must preserve a finished book or an unfinished outline.
            # The shared pure builder can validate tracking facts without an
            # outline; generation still requires an explicit next chapter.
            continuation.append({"code":"legacy_next_outline_required", "reference":str(next_chapter)})
        if next_chapter > 1 and len(prose.get(next_chapter - 1, [])) == 1:
            add("previous_prose", prose[next_chapter - 1][0])
        for name in sorted(tree["files"]):
            path = PurePosixPath(name)
            if path.parts[0] in {"追踪", "设定"} and path.suffix.lower() == ".md":
                add(name, name)
        builder = _SemanticStateBuilder(root=root, chapter_index=next_chapter, book_id=book_id,
            layout_profile_version=_detect_layout_profile(sources), sources=sources)
        builder.parse()
        state = validate_story_project_semantic_state(builder.state())
        try:
            activation = evaluate_story_state_activation(identity, state)
        except StoryStateActivationError as error:
            continuation.append({"code":error.code, "reference":"legacy_current_sources"})
    except (OSError, ValueError, RuntimeError):
        blockers.append({"code":"legacy_semantic_evidence_unverified", "reference":"story_project"})
    return {
        "next_chapter":next_chapter, "semantic_state":state, "activation":activation,
        "source_bindings":{role:{k:v for k,v in source.items() if k not in {"text", "path"}}
                           for role,source in sorted(sources.items())},
        "blockers":blockers, "continuation_blockers":continuation,
        "legacy_state_verified":not blockers, "ready_for_next_chapter":not blockers and not continuation,
        "coverage":"legacy_current_tracking_settings_previous_prose_and_next_outline"}
