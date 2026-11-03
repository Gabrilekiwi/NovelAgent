"""Actual maintenance/ownership boundary for author review SQL mutations.

Review first, then create a fresh backup/certificate, then explicitly upgrade.
This service never publishes a baseline, writes story files or calls a Provider.
"""
from contextlib import contextmanager
from pathlib import Path
import json
import re

from core.engine.chapter_candidate_context import ChapterCandidateContextBuilder
from core.engine.persistence import persistence_state_lock
from core.engine.provider_safety import provider_execution_lock
from core.story_project.paths import infer_next_chapter
from core.story_project.reviewed_state_document import digest
from core.story_project.reviewed_state_document import capture_sources,read_project_bytes
from core.state.authoritative import seed_authoritative_state_from_snapshot,validate_authoritative_state
from workbench.author_baselines import AuthorBaselineStore,AuthorBaselineError,build_baseline_draft,assert_baseline_current
from workbench.author_baselines import validate_baseline_revision
from workbench.book_ownership import BookOwnership,_record,_require_witness
from workbench.maintenance import MaintenanceError


class AuthorBaselineReviewService:
    def __init__(self,backups,*,fault_injector=None):
        self.backups=backups
        self.store=AuthorBaselineStore(backups.projects.database)
        self.fault_injector=fault_injector

    @contextmanager
    def _locked(self,project_id,operation_id,snapshot_permit):
        service=self.backups;database=service.projects.database
        service._software()
        database.require_project_access(project_id)
        database.require_write_access()
        if service.maintenance.status()['purpose']!='backup':raise MaintenanceError('基线审核需要备份维护状态。')
        admission=(service.maintenance.capture(operation_id) if snapshot_permit is None else
            service.maintenance.borrow_capture(snapshot_permit,operation_id))
        with admission as permit:
            project=service.projects.get(project_id);root=Path(project.root)
            owner=BookOwnership(project.book_id,machine_store=service.machine_store)
            # Missing/inactive controls must not be recreated by opening a new
            # lock directory. This is advisory; require repeats inside the lock.
            original=_record(owner.directory,project.book_id)
            if original is None or original['state']!='active':
                raise AuthorBaselineError('请先完成当前作品的正常写入登记；不能重建丢失或停用的资格。')
            _require_witness(root,project.book_id,original)
            with owner.locked(),persistence_state_lock((root,)),provider_execution_lock(root/'.novelagent/runtime/runs'):
                with database.transaction() as connection:
                    arguments=dict(root=root,database=database,project_id=project_id,connection=connection)
                    held=owner.require(**arguments)
                    context=ChapterCandidateContextBuilder(root).build(infer_next_chapter(root))
                    yield context,connection,permit
                    if self.fault_injector:self.fault_injector('before_review_commit',root)
                    # The outer transaction commits only after every check.
                    if owner.require(**arguments)!=held:raise AuthorBaselineError('保存期间写入资格发生变化。')
                    current=ChapterCandidateContextBuilder(root).build(context.chapter_index)
                    if current.to_dict()!=context.to_dict():raise AuthorBaselineError('保存期间作品来源发生变化。')
                    service._software();permit.require()

    def _preview(self,project_id,operation_id,draft):
        value={'schema_version':'author_baseline_review_preview_v1','project_id':project_id,
            'book_id':draft['book_id'],'chapter':draft['chapter'],'operation_id':operation_id,
            'binding':draft['binding'],'state_sha256':digest(draft['state']),
            'static_references_sha256':digest(draft['static_references']),
            'calibration_report_sha256':draft['calibration_report']['report_sha256'],
            'publication_authorized':False,'can_upgrade':False}
        value['preview_sha256']=digest(value)
        return value

    def proposal(self,project_id,*,operation_id,snapshot_permit=None):
        with self._locked(project_id,operation_id,snapshot_permit) as (context,_,__):
            # Only the existing deterministic seed is suggested. Empty categories
            # are explicitly unreviewed, never a claim that a book has no facts.
            semantic=context.story_project_context['semantic_state']
            state=validate_authoritative_state(seed_authoritative_state_from_snapshot(semantic))
            return {'schema_version':'author_baseline_proposal_v1','project_id':project_id,
                'book_id':context.story_project_context['project_identity']['book_id'],'chapter':context.chapter_index-1,
                'operation_id':operation_id,'state':state,'source_paths':sorted(capture_sources(Path(context.story_project_context['story_project_root']))['entries']),
                'unreviewed_categories':['event','character_state','relationship','roster','location','inventory','counter'],
                'automatic_seed_covers':['character_state','location','counter'],'publication_authorized':False,'can_upgrade':False}

    def source_text(self,project_id,*,operation_id,relative_path,snapshot_permit=None):
        with self._locked(project_id,operation_id,snapshot_permit) as (context,_,__):
            root=Path(context.story_project_context['story_project_root'])
            if not isinstance(relative_path,str) or relative_path not in capture_sources(root)['entries']:
                raise AuthorBaselineError('请选择当前作品的原始设定或追踪资料。')
            raw=read_project_bytes(root,relative_path)
            if len(raw)>1024**2:raise AuthorBaselineError('此资料超过网页核对上限，请保留原件并在本机编辑器核对。')
            return {'relative_path':relative_path,'sha256':digest(raw),'text':raw.decode('utf-8')}

    def list(self,project_id,*,after=''):
        if after and re.fullmatch('baseline_[0-9a-f]{32}',after) is None:raise AuthorBaselineError('审核列表位置无效。')
        database=self.store.database;database.require_project_access(project_id)
        result=[];total=0;more=False
        with database.connection() as connection:
            project=connection.execute('SELECT book_id FROM projects WHERE id=?',(project_id,)).fetchone()
            if project is None:raise AuthorBaselineError('实际作品登记不存在。')
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='author_baseline_revisions'").fetchone():
                for row in connection.execute('''SELECT * FROM author_baseline_revisions r WHERE r.project_id=? AND r.id>?
                        AND r.revision=(SELECT MAX(h.revision) FROM author_baseline_revisions h WHERE h.id=r.id)
                        ORDER BY r.id LIMIT 101''',(project_id,after)):
                    if not isinstance(row['payload_json'],str):raise AuthorBaselineError('审核列表中的记录不是文本。')
                    size=len(row['payload_json'].encode('utf-8'))
                    if size>2*1024**2:raise AuthorBaselineError('审核列表中的记录超过支持大小。')
                    if len(result)>=100 or total+size>8*1024**2:more=True;break
                    try:value=validate_baseline_revision(json.loads(row['payload_json']))
                    except (ValueError,TypeError,KeyError) as exc:raise AuthorBaselineError('审核列表记录损坏，请保留原记录并核对。') from exc
                    if any(row[k]!=value[k] for k in ('id','revision','project_id','book_id','record_sha256')) or value['book_id']!=project['book_id']:
                        raise AuthorBaselineError('审核列表记录与实际作品不同。')
                    total+=size
                    result.append({k:value[k] for k in ('id','revision','record_sha256','chapter','status','created_at')})
        return {'items':result,'next_cursor':result[-1]['id'] if more and result else None,
            'source_checked':False,'history_verified':False,'publication_authorized':False,'can_upgrade':False}

    def preview(self,project_id,*,operation_id,state,calibration_report,static_references=(),snapshot_permit=None):
        with self._locked(project_id,operation_id,snapshot_permit) as (context,_,__):
            draft=build_baseline_draft(project_id=project_id,trusted_context=context,state=state,
                calibration_report=calibration_report,static_references=static_references)
            return self._preview(project_id,operation_id,draft)

    def create(self,project_id,*,operation_id,state,calibration_report,expected_preview_sha256,
               static_references=(),snapshot_permit=None):
        with self._locked(project_id,operation_id,snapshot_permit) as (context,connection,_):
            draft=build_baseline_draft(project_id=project_id,trusted_context=context,state=state,
                calibration_report=calibration_report,static_references=static_references)
            preview=self._preview(project_id,operation_id,draft)
            if preview['preview_sha256']!=expected_preview_sha256:
                raise AuthorBaselineError('审核内容或来源已偏离预览，请重新核对。')
            saved=self.store.create(draft,trusted_context=context,_connection=connection)
            return self._result(saved)

    def revise(self,project_id,identifier,*,operation_id,expected_revision,expected_sha256,state=None,
               confirmed_categories=(),author_confirmed=False,snapshot_permit=None):
        with self._locked(project_id,operation_id,snapshot_permit) as (context,connection,_):
            saved=self.store.revise(identifier,project_id=project_id,expected_revision=expected_revision,
                expected_sha256=expected_sha256,trusted_context=context,state=state,
                confirmed_categories=confirmed_categories,author_confirmed=author_confirmed,_connection=connection)
            return self._result(saved)

    def inspect(self,project_id,identifier,*,operation_id,snapshot_permit=None):
        with self._locked(project_id,operation_id,snapshot_permit) as (context,connection,_):
            history=self.store._history(connection,identifier,project_id)
            if not history:raise AuthorBaselineError('作者基线不存在。')
            saved=history[-1]
            try:assert_baseline_current(saved,trusted_context=context)
            except ValueError as exc:
                return {**self._result(saved),'source_current':False,'can_confirm':False,'reason':str(exc)}
            return {**self._result(saved),'source_current':True,'can_confirm':saved['status']=='draft'}

    def history(self,project_id,identifier):
        # Pure history reads still work when source/qualification is unavailable;
        # a stored confirmed state is not a current-source assertion.
        return {'items':self.store.history(identifier,project_id=project_id),
            'source_checked':False,'publication_authorized':False,'can_upgrade':False}

    @staticmethod
    def _result(saved):
        return {'record':saved,'publication_authorized':False,'can_upgrade':False,
            'calibration_report_json':json.dumps(saved['calibration_report'],ensure_ascii=False,allow_nan=False,sort_keys=True),
            'fresh_backup_required':True}
