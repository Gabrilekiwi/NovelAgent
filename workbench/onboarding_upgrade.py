"""Author-confirmed, certified-source admission to the existing upgrade.

This service is not installed as a public route until its continuation and
browser flow have been verified. It never accepts baseline state, calibration,
ownership or recovery booleans from a caller. They come from actual evidence.
"""
import json
import os
from pathlib import Path
import re
import sqlite3
from contextlib import closing,contextmanager

from core.engine.chapter_candidate_context import ChapterCandidateContextBuilder
from core.engine.persistence import atomic_create_json,atomic_write_json
from core.engine.reviewed_upgrade import prepare_reviewed_upgrade,prepare_reviewed_upgrade_transaction
from core.story_project.paths import infer_next_chapter
from core.story_project.reviewed_state_document import baseline_request,digest,read_project_bytes
from core.story_project.reviewed_identity import load_verified_reviewed_identity
from workbench.author_baselines import AuthorBaselineStore,assert_baseline_current
from workbench.backup_archive_view import verified_archive_view
from workbench.backup_certification import RecoveryCertification,verify_recovery_certificate
from workbench.backup_files import _ordinary
from workbench.backup_source import locked_backup_source
from workbench.jobs import TERMINAL_JOB_STATES


class OnboardingUpgradeError(ValueError):
    code='onboarding_upgrade_invalid'


def _sha(value):
    return isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value) is not None


def _intent_name(preview_sha256):
    if not _sha(preview_sha256):raise OnboardingUpgradeError('升级预览摘要无效。')
    return 'oug-'+preview_sha256[:16]+'.json'


class OnboardingUpgradeService:
    def __init__(self,backups,*,temporary_root=None,fault_injector=None,transaction_fault=None):
        self.backups=backups
        self.store=AuthorBaselineStore(backups.projects.database)
        self.temporary_root=temporary_root
        self.fault_injector=fault_injector
        self.transaction_fault=transaction_fault

    def _fault(self,event):
        if self.fault_injector:self.fault_injector(event)

    def _request(self,project_id,operation_id,archive_id,baseline_id,expected_revision,expected_sha256):
        if (not isinstance(project_id,str) or not project_id or not isinstance(operation_id,str)
                or re.fullmatch('[0-9a-f]{32}',operation_id) is None
                or not isinstance(baseline_id,str) or re.fullmatch('baseline_[0-9a-f]{32}',baseline_id) is None
                or type(expected_revision) is not int or expected_revision<1 or not _sha(expected_sha256)):
            raise OnboardingUpgradeError('请选择当前已确认的作者基线和维护操作。')
        RecoveryCertification(self.backups)._package(archive_id)
        return dict(project_id=project_id,operation_id=operation_id,archive_id=archive_id,
            baseline_id=baseline_id,expected_revision=expected_revision,expected_sha256=expected_sha256)

    def _derive(self,request,snapshot,scope,permit):
        backups=self.backups;project_id=request['project_id'];root=Path(scope['source_root'])
        if backups.maintenance.status()['purpose']!='backup':raise OnboardingUpgradeError('格式升级需要备份维护状态。')
        backups.projects.database.require_project_access(project_id)
        backups.projects.database.require_write_access()
        if not scope['scope_checks_passed'] or scope['machine_ownership']['state']!='verified':
            raise OnboardingUpgradeError('当前任务、来源或写入登记未通过检查。')
        terminal=tuple(sorted(TERMINAL_JOB_STATES))
        placeholders=','.join('?' for _ in terminal)
        pending=snapshot.connection.execute(
            f'SELECT id FROM jobs WHERE project_id=? AND state NOT IN ({placeholders}) LIMIT 1',
            (project_id,*terminal)).fetchone()
        if pending is not None:
            raise OnboardingUpgradeError('仍有未结束的旧任务，请先取消或结束任务，再重新归档和核对升级。')
        ownership=scope['machine_ownership']['record']
        if ownership is None or ownership['state']!='active':raise OnboardingUpgradeError('原作品尚无有效写入登记。')
        current=backups._preview(scope,snapshot,permit,request['operation_id'])
        package=RecoveryCertification(backups)._package(request['archive_id'])
        certificate=verify_recovery_certificate(package,project_id=project_id,book_id=scope['book_id'],temporary_root=self.temporary_root)
        with verified_archive_view(package) as view:
            archived=view.manifest['preview']
            if view.verified['receipt_sha256']!=certificate['archive_receipt_sha256']:
                raise OnboardingUpgradeError('认证与原归档完成凭证不同。')
            if any(archived[field]!=current[field] for field in ('scope','database','runtime_config','software')):
                raise OnboardingUpgradeError('备份已偏离当前来源或整库，请在基线确认后生成新备份和认证。')
        history=self.store._history(snapshot.connection,request['baseline_id'],project_id)
        if not history:raise OnboardingUpgradeError('作者基线不存在。')
        reviewed=history[-1]
        if (reviewed['revision']!=request['expected_revision'] or reviewed['record_sha256']!=request['expected_sha256']
                or reviewed['status']!='confirmed' or reviewed['author_confirmed'] is not True):
            raise OnboardingUpgradeError('基线尚未确认或修订已变化，请重新核对。')
        context=ChapterCandidateContextBuilder(root).build(infer_next_chapter(root))
        assert_baseline_current(reviewed,trusted_context=context)
        confirmation=baseline_request(book_id=scope['book_id'],chapter=reviewed['chapter'],state=reviewed['state'],
            sources=reviewed['sources'],old_identity_sha256=reviewed['binding']['identity_sha256'],static_references=reviewed['static_references'])
        run_id='upg-'+digest({'review':reviewed['record_sha256'],'certificate':certificate['certificate_sha256']})[:20]
        preparation=prepare_reviewed_upgrade(trusted_context=context,baseline_state=reviewed['state'],
            confirmed_baseline=confirmation,static_references=reviewed['static_references'],
            calibration_report=reviewed['calibration_report'],run_id=run_id)
        binding=preparation['binding']
        value={'schema_version':'workbench_onboarding_upgrade_preview_v1',**request,'book_id':scope['book_id'],
            'source_preview_sha256':current['preview_sha256'],'source_record_sha256':ownership['record_sha256'],
            'archive_receipt_sha256':certificate['archive_receipt_sha256'],'certificate_sha256':certificate['certificate_sha256'],
            'recovery_policy':certificate['policy'],'recovery_policy_sha256':digest(certificate['policy']),
            'run_id':run_id,'committed_through':reviewed['chapter'],'preparation_sha256':preparation['preparation_sha256'],
            'old_identity_sha256':binding['old_identity_sha256'],'new_identity_sha256':binding['new_identity_sha256'],
            'baseline_document_sha256':binding['baseline_document_sha256'],
            'fact_counts':{key:len(reviewed['state'][key]) for key in ('characters','relationships','roster','locations','inventory','numeric_counters','events')},
            'changes':['保存已确认的七类事实基线','将作品身份升级为审核格式','保留 book_id、原正文、草稿和原登记见证','迁移同一作品的写入登记'],
            'can_upgrade':True,'automatic_provider_calls_allowed':False,'publication_authorized':False}
        value['preview_sha256']=digest(value)
        return value,preparation,ownership

    def _source(self,project_id,operation_id,snapshot_permit,final_action=None):
        b=self.backups;b._software()
        if b.maintenance.status()['purpose']!='backup':raise OnboardingUpgradeError('格式升级需要备份维护状态。')
        return locked_backup_source(b.projects,project_id,maintenance=b.maintenance,operation_id=operation_id,
            task_runtime_root=b.task_runtime_root,machine_store=b.machine_store,snapshot_permit=snapshot_permit,
            _on_verified_source=final_action)

    def preview(self,project_id,*,operation_id,archive_id,baseline_id,expected_revision,expected_sha256,snapshot_permit=None):
        request=self._request(project_id,operation_id,archive_id,baseline_id,expected_revision,expected_sha256)
        with self._source(project_id,operation_id,snapshot_permit) as (snapshot,scope,_,permit):
            value,_,__=self._derive(request,snapshot,scope,permit)
        return value

    def commit(self,project_id,*,operation_id,archive_id,baseline_id,expected_revision,expected_sha256,
               expected_preview_sha256,acknowledged_policy_sha256,confirmed=False,snapshot_permit=None):
        request=self._request(project_id,operation_id,archive_id,baseline_id,expected_revision,expected_sha256)
        _intent_name(expected_preview_sha256)
        if confirmed is not True or not _sha(acknowledged_policy_sha256):raise OnboardingUpgradeError('请核对升级差异及恢复前提后明确确认。')
        previous=self.completed_result(project_id,preview_sha256=expected_preview_sha256)
        if previous is not None:
            original=previous['preview']
            if any(original[key]!=value for key,value in request.items()) or original['recovery_policy_sha256']!=acknowledged_policy_sha256:
                raise OnboardingUpgradeError('重复请求与原升级确认不同。')
            return previous['result']
        result={}
        def apply(snapshot,scope,owner,permit):
            # Rederive while all original source locks and the real SQL fence
            # remain held, AFTER the enclosing source verifier succeeds.
            value,preparation,source=self._derive(request,snapshot,scope,permit)
            if value['preview_sha256']!=expected_preview_sha256 or value['recovery_policy_sha256']!=acknowledged_policy_sha256:
                raise OnboardingUpgradeError('升级依据或恢复前提已偏离预览。')
            path=owner.directory/_intent_name(expected_preview_sha256)
            intent={'schema_version':'workbench_onboarding_upgrade_intent_v1','preview':value,
                'preparation':preparation,'source_record':source}
            intent['intent_sha256']=digest(intent)
            if len((json.dumps(intent,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+'\n').encode('utf-8'))>16*1024**2:
                raise OnboardingUpgradeError('本次升级意图超过支持大小，未开始升级。')
            if os.path.lexists(path):
                if self._read_intent(owner,expected_preview_sha256)!=intent:
                    raise OnboardingUpgradeError('原升级意图不同，不能覆盖。')
            else:atomic_create_json(path,intent)
            progress_path=owner.directory/('oup-'+expected_preview_sha256[:16]+'.json')
            atomic_create_json(progress_path,self._signed_progress(intent,[]))
            self._fault('after_onboarding_intent')
            tx=prepare_reviewed_upgrade_transaction(preparation=preparation,fault_injector=self._prepare_observer(owner,intent))
            self._fault('after_onboarding_prepare')
            self.backups._software();permit.require()
            outcome=tx.commit()
            if not outcome['committed']:raise OnboardingUpgradeError('升级未提交，请保留现场并核对原事务。')
            verified=load_verified_reviewed_identity(Path(scope['source_root']))
            if verified['identity']!=preparation['new_identity']:raise OnboardingUpgradeError('实际升级身份与原意图不同。')
            current=owner.require(root=Path(scope['source_root']),database=self.backups.projects.database,
                project_id=project_id,connection=snapshot.connection)
            if current['binding']['identity_sha256']!=value['new_identity_sha256'] or current['registration_id']!=source['registration_id']:
                raise OnboardingUpgradeError('升级后的登记迁移无法核验。')
            result.update(self._result(intent,current['generation']))
        with self._source(project_id,operation_id,snapshot_permit,apply) as (snapshot,scope,_,permit):
            value,_,__=self._derive(request,snapshot,scope,permit)
            if value['preview_sha256']!=expected_preview_sha256 or value['recovery_policy_sha256']!=acknowledged_policy_sha256:
                raise OnboardingUpgradeError('升级依据或恢复前提已偏离预览。')
            self._fault('before_source_verification')
        return result

    @staticmethod
    def _result(intent,generation):
        value=intent['preview']
        return dict(schema_version='workbench_onboarding_upgrade_result_v1',project_id=value['project_id'],
            book_id=value['book_id'],run_id=value['run_id'],preview_sha256=value['preview_sha256'],
            intent_sha256=intent['intent_sha256'],committed=True,identity_version='3.0',
            registration_generation=generation,automatic_provider_calls_allowed=False,
            publication_authorized=False,fresh_chapter_preparation_required=True)

    @contextmanager
    def _original(self,project_id,preview_sha256):
        from workbench.book_ownership import BookOwnership,_file_identity,_lineage,_require_witness
        _intent_name(preview_sha256)
        database=self.backups.projects.database
        database.require_project_access(project_id)
        with closing(sqlite3.connect(database.path.as_uri()+'?mode=ro',uri=True)) as connection:
            connection.row_factory=sqlite3.Row
            connection.execute('PRAGMA query_only=ON');connection.execute('BEGIN')
            row=connection.execute('SELECT book_id,root FROM projects WHERE id=?',(project_id,)).fetchone()
            if row is None:raise OnboardingUpgradeError('原作品登记不存在。')
            book_id,raw_root=row
            root=self.backups.projects.validate_project_root(raw_root)
            owner=BookOwnership(book_id,machine_store=self.backups.machine_store)
            if not os.path.lexists(owner.directory/_intent_name(preview_sha256)):
                yield None;return
            intent=self._read_intent(owner,preview_sha256);value=intent['preview'];source=intent['source_record']
            binding=source['binding']
            if (value['project_id']!=project_id or binding['database']!=str(database.path)
                    or binding['root']!=str(root) or raw_root!=str(root)
                    or binding['database_file_identity']!=_file_identity(database.path)
                    or binding['database_parent_lineage']!=_lineage(database.path.parent)
                    or binding['root_lineage']!=_lineage(root)):
                raise OnboardingUpgradeError('原升级的物理作品或数据库登记已变化。')
            _require_witness(root,book_id,source)
            self._progress(owner,intent)
            history=self.store._history(connection,value['baseline_id'],project_id)
            revision=value['expected_revision']
            if (len(history)<revision or history[revision-1]['record_sha256']!=value['expected_sha256']
                    or history[revision-1]['status']!='confirmed'):
                raise OnboardingUpgradeError('原作者确认修订无法核验。')
            yield intent,owner,root,connection

    def completed_result(self,project_id,*,preview_sha256):
        """Verify historical completion without creating, locking or recovering."""
        from workbench.registered_identity_upgrade import _read_intent as read_upgrade_intent,_completed,_name
        from workbench.backup_restore_roots import _read
        with self._original(project_id,preview_sha256) as resolved:
            if resolved is None:return None
            intent,owner,root,connection=resolved
            value=intent['preview'];source=intent['source_record']
            original=read_upgrade_intent(owner.directory,value['new_identity_sha256'])
            if (original['source_record']!=source or original['immutable']['run_id']!=value['run_id']
                    or original['immutable']['story_project_read_set']!=intent['preparation']['read_set']):
                raise OnboardingUpgradeError('原登记升级事务与作者确认意图不同。')
            completion=_completed(original)
            if _read(owner.directory,_name(value['new_identity_sha256'],'done'))!=completion:
                raise OnboardingUpgradeError('原登记升级完成凭证不同。')
            runtime=root/'.novelagent/runtime'
            if _read(runtime,'reviewed/upgrades/'+value['run_id']+'-preparation.json')!=intent['preparation']:
                raise OnboardingUpgradeError('原发布的升级准备资料不同。')
            # Re-read immutable control after verification; a concurrent change
            # cannot make this request approve a different original operation.
            if self._read_intent(owner,preview_sha256)!=intent:
                raise OnboardingUpgradeError('原升级意图在查询期间变化。')
            return {'preview':value,'result':self._result(intent,original['active_record']['generation'])}

    def operation(self,project_id,*,preview_sha256):
        """Inspect only the original operation; absence is never repaired."""
        from workbench.registered_identity_upgrade import _read_intent as read_upgrade_intent,_manifest,_name
        from core.engine.persistence_v2 import load_persistence_manifest_v2
        with self._original(project_id,preview_sha256) as resolved:
            if resolved is None:return {'state':'not_started','can_continue':False}
            intent,owner,root,_=resolved;value=intent['preview']
            txroot=root/'.novelagent/runtime/reviewed/p';run=value['run_id']
            journal=txroot/'journals'/run;staging=txroot/'staging'/run
            registered=owner.directory/_name(value['new_identity_sha256'],'intent')
            if os.path.lexists(registered):
                original=read_upgrade_intent(owner.directory,value['new_identity_sha256'])
                self._match_transaction(intent,original)
                manifest=_manifest(original)
                if manifest['state']=='completed':
                    # Publication may have finished just before process exit,
                    # with the original owner fence still pending.
                    done=owner.directory/_name(value['new_identity_sha256'],'done')
                    from workbench.book_ownership import _record
                    # A later restore handoff can advance or move ownership.
                    # The original publication remains historical completion;
                    # only its own pending fence still needs upgrade recovery.
                    if os.path.lexists(done) and _record(owner.directory,owner.book_id)!=original['pending_record']:
                        completed=self.completed_result(project_id,preview_sha256=preview_sha256)
                        return {'state':'completed','can_continue':False,**completed}
                state=('rolled_back' if manifest['state']=='rolled_back' else
                    'publication_pending' if os.path.lexists(journal/'commit.marker') else 'rollback_pending')
            elif os.path.lexists(journal):
                manifest=load_persistence_manifest_v2(journal/'manifest.json')
                self._match_preparation(intent,manifest)
                state='prepared'
            elif os.path.lexists(staging):state='preparation_interrupted'
            else:state='intent_saved'
            return {'state':state,'can_continue':state in {'publication_pending','rollback_pending','intent_saved','prepared','preparation_interrupted'},'preview':value,
                'intent_sha256':intent['intent_sha256'],'automatic_provider_calls_allowed':False}

    @staticmethod
    def _match_preparation(intent,manifest):
        value=intent['preview'];immutable=manifest['immutable'];p=intent['preparation']
        targets={t['target_id']:t for t in immutable['targets']}
        if (immutable['run_id']!=value['run_id'] or immutable['book_id']!=value['book_id']
                or immutable['story_project_read_set']!=p['read_set']
                or targets['reviewed_identity']['before_sha256']!=value['old_identity_sha256']
                or targets['reviewed_identity']['after_sha256']!=value['new_identity_sha256']
                or targets['reviewed_state']['after_sha256']!=value['baseline_document_sha256']):
            raise OnboardingUpgradeError('原事务与作者升级准备资料不同。')

    def _match_transaction(self,intent,original):
        if original['source_record']!=intent['source_record']:
            raise OnboardingUpgradeError('原升级登记不同。')
        self._match_preparation(intent,{'immutable':original['immutable']})

    def continue_operation(self,project_id,*,operation_id,preview_sha256,acknowledged_policy_sha256,
                           confirmed=False,snapshot_permit=None):
        """Explicit recovery under real maintenance, owner, book and SQL fences.

        The original transaction decides rollback before its commit marker or
        publication after it. No automatic discovery or new upgrade is allowed.
        """
        from workbench.book_ownership import _record,_require_witness
        from workbench.backup_database import owned_database_snapshot
        from core.engine.persistence import persistence_state_lock,_borrow_held_run_locks
        from core.engine.provider_safety import provider_execution_lock
        from workbench.registered_identity_upgrade import (_read_intent as read_upgrade_intent,
            resume_registered_identity_upgrade,_name)
        if confirmed is not True:raise OnboardingUpgradeError('请明确确认继续原升级操作。')
        with self._original(project_id,preview_sha256) as resolved:
            if resolved is None:raise OnboardingUpgradeError('原升级意图不存在。')
            intent,owner,root,_=resolved;value=intent['preview']
            if operation_id!=value['operation_id'] or acknowledged_policy_sha256!=value['recovery_policy_sha256']:
                raise OnboardingUpgradeError('维护操作或恢复前提与原确认不同。')
            self._require_original_root(root)
            # Check original controls before any lock can create a directory.
            registered=owner.directory/_name(value['new_identity_sha256'],'intent')
            original=read_upgrade_intent(owner.directory,value['new_identity_sha256']) if os.path.lexists(registered) else None
            if original is not None:self._match_transaction(intent,original)
            b=self.backups;b._software();b.projects.database.require_write_access()
            if b.maintenance.status()['purpose']!='backup':raise OnboardingUpgradeError('需要原备份维护状态。')
            capture=(b.maintenance.capture(operation_id) if snapshot_permit is None
                else b.maintenance.borrow_capture(snapshot_permit,operation_id))
            with capture as permit,owner.locked(),persistence_state_lock((root,)):
                with provider_execution_lock(root/'.novelagent/runtime/runs'):
                    with owned_database_snapshot(b.projects.database,snapshot_permit=permit) as snapshot:
                        current=_record(owner.directory,owner.book_id)
                        allowed=([original[k] for k in ('source_record','pending_record','active_record')]
                            if original is not None else [intent['source_record']])
                        if current not in allowed:
                            raise OnboardingUpgradeError('原事务不再拥有当前机器登记。')
                        _require_witness(root,owner.book_id,current)
                        if self._read_intent(owner,preview_sha256)!=intent:
                            raise OnboardingUpgradeError('原升级意图已变化。')
                        row=snapshot.connection.execute('SELECT book_id,root FROM projects WHERE id=?',(project_id,)).fetchone()
                        if tuple(row or ())!=(owner.book_id,str(root)):raise OnboardingUpgradeError('作品登记已变化。')
                        certificate=verify_recovery_certificate(RecoveryCertification(b)._package(value['archive_id']),
                            project_id=project_id,book_id=owner.book_id,temporary_root=self.temporary_root)
                        if certificate['certificate_sha256']!=value['certificate_sha256']:
                            raise OnboardingUpgradeError('原恢复认证已变化。')
                        b._software();permit.require()
                        with _borrow_held_run_locks():
                            if original is None:
                                self._continue_preparation(intent,owner,root,snapshot,permit)
                            else:
                                resume_registered_identity_upgrade(Path(original['transaction_root']),value['run_id'],
                                    fault_injector=self.transaction_fault)
                        snapshot.require(b.projects.database,permit)
            return self.operation(project_id,preview_sha256=preview_sha256)

    @staticmethod
    def _require_original_root(root):
        """Reject lost/unbound transaction roots before acquiring write locks."""
        from core.engine.root_registry import load_root_registry
        txroot=root/'.novelagent/runtime/reviewed/p'
        registry=txroot/'root_registry.json'
        if os.path.lexists(registry):
            load_root_registry(registry)
            return
        for name in ('staging','journals','registry','abandoned'):
            directory=txroot/name
            if os.path.lexists(directory) and any(directory.iterdir()):
                raise OnboardingUpgradeError('原事务根登记缺失或存在未登记事务，不能创建登记或续办。')

    def _continue_preparation(self,intent,owner,root,snapshot,permit):
        """Continue only this explicitly confirmed, not-yet-applying run.

        An incomplete temporary bundle is retained under abandoned/, then the
        same original preparation is staged again. No other run is discovered.
        """
        from core.engine.persistence import persistence_run_lock,_borrow_held_run_locks
        from core.engine.persistence_v2 import (PersistenceV2Transaction,_validate_complete_staged_journal,
            _atomic_publish_journal,_root_map_from_manifest,_load_staged_content)
        from core.story_project.read_set import verify_story_project_read_set
        from workbench.backup_archive import _validate_config
        b=self.backups;value=intent['preview'];p=intent['preparation'];run=value['run_id']
        txroot=root/'.novelagent/runtime/reviewed/p';journal=txroot/'journals'/run;staging=txroot/'staging'/run
        # A new staging attempt needs the original committed DB and config,
        # plus the exact original book read set and physical registration.
        with verified_archive_view(RecoveryCertification(b)._package(value['archive_id'])) as view:
            archived=view.manifest['preview']
            if (archived['database']!=snapshot.fingerprint(b.projects.database,permit)
                    or archived['runtime_config']!=_validate_config(b.runtime_config)):
                raise OnboardingUpgradeError('准备续办的原数据库或运行配置已变化。')
        verify_story_project_read_set(p['read_set'],phase='prepare')
        with persistence_run_lock(txroot,require_existing_lock=os.path.lexists(journal) or os.path.lexists(staging)):
            if os.path.lexists(journal) and os.path.lexists(staging):
                raise OnboardingUpgradeError('原准备事务同时存在两份，不能选择或覆盖。')
            if os.path.lexists(staging):
                # No manifest means a genuinely incomplete preparation. A
                # present but invalid manifest is damaged evidence, not a
                # reason to silently rebuild it.
                if os.path.lexists(staging/'manifest.json'):
                    manifest=_validate_complete_staged_journal(staging)
                    self._match_preparation(intent,manifest)
                    _atomic_publish_journal(staging,journal,txroot)
                else:
                    if os.path.lexists(txroot/'registry/pending'/(run+'.json')):
                        raise OnboardingUpgradeError('不完整准备已有待提交登记，不能重建。')
                    _atomic_publish_journal(staging,txroot/'abandoned'/run,txroot)
            if os.path.lexists(journal):
                manifest=_validate_complete_staged_journal(journal)
                self._match_preparation(intent,manifest)
                if manifest['state']!='prepared' or os.path.lexists(journal/'commit.marker'):
                    raise OnboardingUpgradeError('原事务已进入提交但缺失原登记意图，不能修复。')
                targets={t['target_id']:t for t in manifest['immutable']['targets']}
                if json.loads(_load_staged_content(journal,targets['reviewed_upgrade_preparation']))!=p:
                    raise OnboardingUpgradeError('原事务暂存的准备资料不同。')
                tx=PersistenceV2Transaction(transaction_root=txroot,run_id=run,book_id=value['book_id'],
                    root_map=_root_map_from_manifest(manifest),fault_injector=self.transaction_fault)
            else:
                # Preserve the original author-confirmed preparation and run.
                with _borrow_held_run_locks():
                    tx=prepare_reviewed_upgrade_transaction(preparation=p,fault_injector=self._prepare_observer(owner,intent))
            b._software();permit.require()
            with _borrow_held_run_locks():outcome=tx.commit()
            if not outcome['committed']:raise OnboardingUpgradeError('原准备事务未完成，请保留现场。')

    @staticmethod
    def _signed_progress(intent,directories,complete=None):
        value={'schema_version':'onboarding_upgrade_preparation_progress_v1',
            'intent_sha256':intent['intent_sha256'],'directories':directories,'complete':[] if complete is None else complete}
        return {**value,'progress_sha256':digest(value)}

    def _progress(self,owner,intent):
        """Missing original preparation evidence must never become a new run."""
        from core.engine.root_registry import directory_identity
        from workbench.backup_restore_roots import _read
        from core.engine.persistence_v2 import _validate_complete_staged_journal
        value=intent['preview'];run=value['run_id']
        progress=_read(owner.directory,'oup-'+value['preview_sha256'][:16]+'.json')
        directories=progress.get('directories');complete=progress.get('complete')
        if (not isinstance(directories,list) or len(directories)>16
                or not isinstance(complete,list) or len(complete)>16
                or progress!=self._signed_progress(intent,directories,complete)):
            raise OnboardingUpgradeError('原准备进度凭证不同。')
        txroot=Path(intent['source_record']['binding']['root'])/'.novelagent/runtime/reviewed/p'
        candidates=[txroot/place/run for place in ('staging','journals','abandoned')]
        actual=[directory_identity(path) for path in candidates if os.path.lexists(path)]
        if any(identity not in actual for identity in directories):
            raise OnboardingUpgradeError('原准备目录已丢失或被替换，不能重新创建。')
        for bundle in complete:
            if (not isinstance(bundle,dict) or set(bundle)!={'directory','manifest_digest'}
                    or bundle['directory'] not in directories or not _sha(bundle['manifest_digest'])):
                raise OnboardingUpgradeError('原完整准备凭证不同。')
            path=next(path for path in candidates if os.path.lexists(path) and directory_identity(path)==bundle['directory'])
            manifest=_validate_complete_staged_journal(path)
            self._match_preparation(intent,manifest)
            if manifest['manifest_digest']!=bundle['manifest_digest']:
                raise OnboardingUpgradeError('原完整准备清单已变化，不能重建。')
        return progress

    def _prepare_observer(self,owner,intent):
        from core.engine.root_registry import directory_identity
        def observe(event,index,path):
            if event=='after_temporary_journal_created':
                progress=self._progress(owner,intent)
                directories=progress['directories']+[directory_identity(path)]
                if len(directories)>16:raise OnboardingUpgradeError('准备尝试超过支持次数。')
                atomic_write_json(owner.directory/('oup-'+intent['preview']['preview_sha256'][:16]+'.json'),
                    self._signed_progress(intent,directories,progress['complete']))
            elif event=='before_journal_publish':
                from core.engine.persistence_v2 import _validate_complete_staged_journal
                progress=self._progress(owner,intent)
                stage=Path(intent['source_record']['binding']['root'])/'.novelagent/runtime/reviewed/p/staging'/intent['preview']['run_id']
                manifest=_validate_complete_staged_journal(stage)
                self._match_preparation(intent,manifest)
                bundle={'directory':directory_identity(stage),'manifest_digest':manifest['manifest_digest']}
                complete=progress['complete'] if bundle in progress['complete'] else [*progress['complete'],bundle]
                atomic_write_json(owner.directory/('oup-'+intent['preview']['preview_sha256'][:16]+'.json'),
                    self._signed_progress(intent,progress['directories'],complete))
            if self.transaction_fault:self.transaction_fault(event,index,path)
        return observe

    def _read_intent(self,owner,preview_sha256):
        name=_intent_name(preview_sha256);path=owner.directory/name
        if _ordinary(path).st_size>16*1024**2:raise OnboardingUpgradeError('原升级意图超过支持大小。')
        try:
            value=json.loads(read_project_bytes(owner.directory,name))
            if (set(value)!={'schema_version','preview','preparation','source_record','intent_sha256'}
                    or value['schema_version']!='workbench_onboarding_upgrade_intent_v1'
                    or value['intent_sha256']!=digest({k:v for k,v in value.items() if k!='intent_sha256'})
                    or value['preview']['preview_sha256']!=preview_sha256
                    or preview_sha256!=digest({k:v for k,v in value['preview'].items() if k!='preview_sha256'})
                    or value['preview']['book_id']!=owner.book_id
                    or value['preparation']['preparation_sha256']!=value['preview']['preparation_sha256']
                    or value['preparation']['preparation_sha256']!=digest({k:v for k,v in value['preparation'].items() if k!='preparation_sha256'})
                    or value['preparation']['binding']['run_id']!=value['preview']['run_id']
                    or value['source_record']['record_sha256']!=value['preview']['source_record_sha256']
                    or value['source_record']['record_sha256']!=digest({k:v for k,v in value['source_record'].items() if k!='record_sha256'})
                    or value['source_record']['binding']['project_id']!=value['preview']['project_id']
                    or value['source_record']['binding']['book_id']!=owner.book_id
                    or value['source_record']['binding']['root']!=value['preparation']['read_set']['root_identity']['resolved_path']):
                raise ValueError('intent differs')
        except (ValueError,TypeError,KeyError) as exc:raise OnboardingUpgradeError('原升级意图无法核验，不能修复或覆盖。') from exc
        return value
