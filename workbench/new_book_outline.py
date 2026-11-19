"""A first or next chapter outline, explicitly submitted by the author under the book lock."""
from core.engine.persistence import atomic_create_text
from core.engine.safe_paths import assert_safe_local_tree
from core.story_project.paths import infer_next_chapter, resolve_outline
from core.story_project.reviewed_identity import load_verified_reviewed_identity
from core.story_project.reviewed_state_document import read_project_bytes, verify_current_sources
from workbench.outlines import OutlineBatchConflict, render_canonical_outline, validate_outline_items


def save_next_outline(registration, chapter, payload):
    root = registration["root"]
    verified = load_verified_reviewed_identity(root)
    value = verified["history"]["value"]
    if ("genesis" not in verified["identity"]["reviewed_state"] or chapter != infer_next_chapter(root)
            or chapter != value["committed_through"] + 1):
        raise OutlineBatchConflict("只能填写这本新作品当前待写章节的章纲。")
    verify_current_sources(root=root, value=value, next_chapter=chapter)
    item, = validate_outline_items([{**payload, "chapter_index": chapter}])
    target = resolve_outline(root, chapter)
    text = render_canonical_outline(item)
    if len(text) > 24000:
        raise OutlineBatchConflict("本章章纲过长，请保留关键事件和节拍。")
    assert_safe_local_tree(target.canonical_path.parent)
    if target.candidates:
        if not target.conflict and target.path == target.canonical_path and read_project_bytes(root, target.path.relative_to(root).as_posix()) == text.encode("utf-8"):
            return {"saved": True, "chapter": chapter}
        raise OutlineBatchConflict("本章已有章纲，请刷新查看；不会覆盖已经保存的内容。")
    atomic_create_text(target.canonical_path, text)
    return {"saved": True, "chapter": chapter}
