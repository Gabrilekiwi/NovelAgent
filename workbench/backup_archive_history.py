"""Recheck retained file transactions using only a completed archive.

Old absolute paths are logical evidence coordinates. They never become local
file reads or writable PathGuards. This is one check for a future restore plan,
not a reviewed-state verifier, database restore or write authorization.
"""
import json
from pathlib import Path

from core.story_project.reviewed_state_document import digest
from workbench.backup_archive import BackupArchiveError
from workbench.backup_archive_view import verified_archive_view
from workbench.backup_history import verify_persistence_history


def verify_archived_persistence_history(package, *, project_id, book_id):
    """No source access, extraction, runtime construction or repair is allowed."""
    with verified_archive_view(package) as view:
        verified = view.verified
        preview = view.manifest['preview']
        if preview['project_id'] != project_id or preview['book_id'] != book_id:
            raise BackupArchiveError('离线历史目标与归档作品不同。')
        tree = preview['scope']['inventories']['story_project']
        root = Path(tree['source_root'])
        if not root.is_absolute() or '..' in root.parts:
            raise BackupArchiveError('归档作品来源坐标无效。')
        def read_bytes(source_root, name):
            if Path(source_root) != root:
                raise BackupArchiveError('离线历史读取越过已绑定作品根。')
            return view.read_root('story_project', name)
        identity = json.loads(read_bytes(root, '.novelagent/project.json'))
        if not isinstance(identity, dict) or identity.get('book_id') != book_id:
            raise BackupArchiveError('归档作品身份与历史目标不同。')
        history = verify_persistence_history(root, tree, book_id=book_id, read_bytes=read_bytes,
            provenance=preview['scope'].get('restored_provenance'))
    result = {'schema_version': 'archived_persistence_history_v1',
              'archive_id': verified['archive_id'], 'archive_receipt_sha256': verified['receipt_sha256'],
              'project_id': project_id, 'book_id': book_id,
              'source_inventory_sha256': tree['inventory_sha256'],
              'coverage': history['coverage'], 'history': history, 'history_verified': True,
              'archive_purpose': verified['archive_purpose'], 'recoverable': False,
              'restore_plan_ready': False, 'write_activation_allowed': False}
    result['verification_sha256'] = digest(result)
    return result
