"""Read archived delivery/autonomy evidence without opening their runtime stores."""
import json
from pathlib import Path

from core.story_project.reviewed_state_document import digest
from workbench.backup_archive import BackupArchiveError
from workbench.backup_archive_view import verified_archive_view
from workbench.backup_auxiliary import inspect_auxiliary_settlement


def inspect_archived_auxiliary(package,*,project_id,book_id):
    with verified_archive_view(package) as view:
        preview=view.manifest['preview'];tree=preview['scope']['inventories']['story_project']
        identity=json.loads(view.read_root('story_project','.novelagent/project.json'))
        if (preview['project_id']!=project_id or preview['book_id']!=book_id
                or not isinstance(identity,dict) or identity.get('book_id')!=book_id):
            raise BackupArchiveError('归档辅助状态目标与作品身份不同。')
        root=Path(tree['source_root'])
        def read(asked_root,name):
            if Path(asked_root)!=root:raise BackupArchiveError('归档辅助状态读取超出目标根。')
            return view.read_root('story_project',name)
        evidence=inspect_auxiliary_settlement(root,tree,book_id,_read_bytes=read)
        result={'schema_version':'archived_auxiliary_settlement_v1',
            'archive_id':view.verified['archive_id'],'archive_receipt_sha256':view.verified['receipt_sha256'],
            'project_id':project_id,'book_id':book_id,'evidence':evidence,'blockers':evidence['blockers'],
            'auxiliary_settled':evidence['state']=='settled',
            'restore_plan_ready':False,'recoverable':False,'write_activation_allowed':False}
        result['verification_sha256']=digest(result)
    return result
