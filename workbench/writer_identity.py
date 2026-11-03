"""Read supported Strict writer evidence without upgrading or enrolling a book."""
import json
from pathlib import Path

from core.engine.persistence import held_persistence_lock_readers
from core.story_project.identity import load_project_identity, validate_project_identity
from core.story_project.reviewed_identity import load_verified_reviewed_identity, validate_reviewed_identity
from core.story_project.reviewed_state_document import read_project_bytes


class WriterIdentityError(ValueError):
    code='book_writer_identity_invalid'


def validate_writer_identity(value):
    identity=(validate_reviewed_identity(value) if isinstance(value,dict) and value.get('schema_version')=='3.0'
        else validate_project_identity(value))
    if (identity.ephemeral or identity.story_state_mode!='strict'
            or identity.authority['mode']!='legacy_markdown_v1'
            or identity.authority['minimum_writer_contract']>2):
        raise WriterIdentityError('作品不符合当前受支持的 Strict 写入契约。')
    return identity


def load_verified_writer_identity(root):
    """Keep reviewed cold proofs; legacy uses the exact archive semantic rules.

    The caller owns the book/source fences. This reader cannot create identity,
    calibration, registration or qualification evidence and makes no model call.
    """
    from workbench.backup_archive_legacy_state import inspect_legacy_state
    from workbench.backup_files import inventory_tree
    root=Path(root).absolute()
    raw=read_project_bytes(root,'.novelagent/project.json')
    value=json.loads(raw)
    identity=validate_writer_identity(value)
    if value['schema_version']=='3.0':
        return load_verified_reviewed_identity(root)
    current=load_project_identity(root)
    if current is None or current.to_dict()!=identity.to_dict():
        raise WriterIdentityError('旧作品的持久身份已变化。')
    tree=inventory_tree(root,locked_readers=held_persistence_lock_readers())
    evidence=inspect_legacy_state(identity,tree,lambda name:read_project_bytes(root,name))
    activation=evidence['activation']
    if (not evidence['legacy_state_verified'] or activation is None
            or activation.get('effective_mode')!='strict' or activation.get('authoritative') is not True
            or activation.get('ready_for_next_step') is not True
            or any(item['code']!='legacy_next_outline_required' for item in evidence['continuation_blockers'])):
        raise WriterIdentityError('旧作品的当前语义、章节序列或 Strict 校准未通过核验。')
    if (read_project_bytes(root,'.novelagent/project.json')!=raw
            or inventory_tree(root,locked_readers=held_persistence_lock_readers())!=tree):
        raise WriterIdentityError('核验期间旧作品的身份或来源变化。')
    # Preserve the existing root-binding receipt shape for legacy books.
    return {'identity':identity.to_dict()}
