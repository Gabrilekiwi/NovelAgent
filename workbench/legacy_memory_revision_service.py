"""Maintenance entry point to acknowledge, without rewriting, old memory edits."""
from pathlib import Path
import json
import re

from core.engine.persistence import atomic_create_json, held_persistence_lock_readers
from core.engine.provider_safety import list_unresolved_provider_calls
from core.story_project.reviewed_state_document import digest, read_project_bytes
from workbench.backup_files import inventory_tree
from workbench.backup_history import BackupHistoryError, verify_persistence_history
from workbench.legacy_history_revisions import DIRECTORY, prepare_observed_revision, confirm_observed_revision
from workbench.legacy_registration import LegacyRegistration
from workbench.jobs import TERMINAL_JOB_STATES


class LegacyMemoryRevisionService:
    def __init__(self,backups):
        self.registration=LegacyRegistration(backups)
        self.backups=backups

    def _prepare(self,connection,binding,*,from_journal,to_journal,reason,reviewer,supersedes=None):
        root=Path(binding['root']);book_id=binding['book_id']
        identity=json.loads(read_project_bytes(root,'.novelagent/project.json'))
        if identity.get('schema_version') not in {'1.0','2.0'}:
            raise ValueError('此入口只核对旧格式作品的历史记忆修订。')
        if any(row['state'] not in TERMINAL_JOB_STATES for row in connection.execute(
                'SELECT state FROM jobs WHERE project_id=?',(binding['project_id'],))):
            raise ValueError('请先结束本书的旧任务，再记录历史记忆修订。')
        if list_unresolved_provider_calls(root/'.novelagent/runtime/runs'):
            raise ValueError('仍有未处理的模型执行，不能确认当前记忆。')
        tree=inventory_tree(root,locked_readers=held_persistence_lock_readers())
        try:
            verify_persistence_history(root,tree,book_id=book_id)
        except BackupHistoryError as exc:
            if exc.gap is None:raise
            transactions=exc.gap['transactions']
        else:
            raise ValueError('当前历史已经连贯，不需要补记外部记忆修订。')
        preview=prepare_observed_revision(root=root,tree=tree,book_id=book_id,transactions=transactions,
            from_journal=from_journal,to_journal=to_journal,read_bytes=read_project_bytes,
            reason=reason,reviewer=reviewer,supersedes=supersedes)
        # Validate the proposed edge before exposing a confirmable preview.
        proposed=confirm_observed_revision(preview,expected_sha256=preview['preview_sha256'],book_id=book_id,
            transactions=transactions)
        # Check the corrected graph record alongside all retained observations
        # before publishing an immutable correction (old bytes stay intact).
        if supersedes:
            from workbench.legacy_history_revisions import read_revisions
            virtual=dict(tree,files=dict(tree['files']))
            name=DIRECTORY+'/'+proposed['id']+'.json'
            virtual['files'][name]={'sha256':digest(proposed)}
            read_revisions(tree=virtual,book_id=book_id,transactions=transactions,
                record=lambda n:proposed if n==name else json.loads(read_project_bytes(root,n)))
        return preview,transactions

    def preview(self,project_id,*,operation_id,from_journal,to_journal=None,reason,reviewer='author',supersedes=None,snapshot_permit=None):
        with self.registration._locked(project_id,operation_id,snapshot_permit) as (_,connection,binding,permit):
            value,_=self._prepare(connection,binding,from_journal=from_journal,to_journal=to_journal,
                reason=reason,reviewer=reviewer,supersedes=supersedes)
            permit.require()
            return value

    def confirm(self,project_id,*,operation_id,from_journal,to_journal=None,reason,reviewer='author',
                expected_sha256,confirmed=False,supersedes=None,snapshot_permit=None):
        if confirmed is not True:raise ValueError('请确认所展示的具体历史记忆修改。')
        if not isinstance(expected_sha256,str) or re.fullmatch('[0-9a-f]{64}',expected_sha256) is None:
            raise ValueError('历史修订预览摘要无效。')
        with self.registration._locked(project_id,operation_id,snapshot_permit) as (_,connection,binding,permit):
            root=Path(binding['root']);path=root/DIRECTORY/('revision_'+expected_sha256[:32]+'.json')
            # Lost responses return the same record, never create a second edge.
            if path.exists():
                saved=json.loads(read_project_bytes(root,path.relative_to(root).as_posix()))
                original={k:v for k,v in saved.items() if k not in {'id','recorded_at','author_confirmed','record_sha256'}}
                if (digest(original)!=expected_sha256 or saved['record_sha256']!=digest({k:v for k,v in saved.items() if k!='record_sha256'})
                        or saved.get('id')!='revision_'+expected_sha256[:32] or saved.get('author_confirmed') is not True
                        or any(saved.get(k)!=v for k,v in {'book_id':binding['book_id'],'from_journal':from_journal,
                            'to_journal':to_journal,'reason':reason,'reviewer':reviewer}.items())
                        or saved.get('supersedes',[])!=(supersedes or [])):
                    raise ValueError('原修订记录与重试请求不一致。')
                return saved
            value,transactions=self._prepare(connection,binding,from_journal=from_journal,to_journal=to_journal,
                reason=reason,reviewer=reviewer,supersedes=supersedes)
            record=confirm_observed_revision(value,expected_sha256=expected_sha256,book_id=binding['book_id'],
                transactions=transactions)
            permit.require()
            path.parent.mkdir(parents=True,exist_ok=True)
            atomic_create_json(path,record)
            if json.loads(read_project_bytes(root,path.relative_to(root).as_posix()))!=record:
                raise ValueError('历史修订保存后读回不一致。')
            return record
