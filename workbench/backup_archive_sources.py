"""Capture current author sources from archive while retaining required review."""
from pathlib import PurePosixPath

from core.story_project.reviewed_state_document import (RELATIVE_PATH,_manual_source_hash,
    _verify_reference_content,decode_document,digest,source_conflict,ReviewedStateDocumentError)
from workbench.backup_archive import BackupArchiveError
from workbench.backup_archive_reviewed import verify_archived_reviewed_state
from workbench.backup_archive_view import verified_archive_view


def inspect_archived_sources(package,*,project_id,book_id):
    history=verify_archived_reviewed_state(package,project_id=project_id,book_id=book_id)
    with verified_archive_view(package) as view:
        if view.verified['receipt_sha256']!=history['archive_receipt_sha256']:
            raise BackupArchiveError('归档在历史与当前来源核验之间变化。')
        tree=view.manifest['preview']['scope']['inventories']['story_project']
        document=view.read_root('story_project',RELATIVE_PATH)
        value=decode_document(document,book_id=book_id)
        entries={};blockers=[];continuation=[];references=[];current=None;conflict=None
        try:
            if any(name in tree['files'] for name in ('追踪','设定')):
                raise BackupArchiveError('归档人工来源根不是目录。')
            for name in sorted(tree['files']):
                path=PurePosixPath(name)
                if (path.parts[0] in {'追踪','设定'} and path.suffix.lower()=='.md'
                        and name.casefold()!=RELATIVE_PATH.casefold()):
                    entries[name]=_manual_source_hash(view.read_root('story_project',name))
            current={'entries':entries,'sha256':digest(entries)}
            if current!=value['source_revision']:
                conflict=source_conflict(document=document,value=value,book_id=book_id,current_sources=current)
                continuation.append({'code':'reviewed_manual_source_changed','reference':RELATIVE_PATH})
            for ref in value['static_references']:
                raw=view.read_root('story_project',ref['relative_path'])
                matches=True
                try:_verify_reference_content(ref,raw)
                except ReviewedStateDocumentError as error:
                    if error.code!='reviewed_static_reference_changed':raise
                    matches=False
                    continuation.append({'code':error.code,'reference':ref['relative_path']})
                references.append({'path':ref['relative_path'],'current_sha256':digest(raw),'reviewed_sha256':ref['sha256'],
                                   'matches_reviewed_reference':matches})
        except (OSError,ValueError,RuntimeError):
            blockers.append({'code':'reviewed_current_source_evidence_unverified','reference':'story_project'})
        result={'schema_version':'archived_reviewed_sources_v1','archive_id':view.verified['archive_id'],
            'archive_receipt_sha256':view.verified['receipt_sha256'],'project_id':project_id,'book_id':book_id,
            'reviewed_history_verification_sha256':history['verification_sha256'],'current_sources':current,
            'reviewed_sources':value['source_revision'],'source_conflict':conflict,'static_references':references,
            'source_snapshot_verified':not blockers,'current_sources_match':not blockers and not continuation,
            'blockers':blockers,'continuation_blockers':continuation,'author_review_required':bool(continuation),
            'restore_plan_ready':False,'recoverable':False,'write_activation_allowed':False}
        result['verification_sha256']=digest(result)
    return result
