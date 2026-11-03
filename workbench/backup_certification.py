"""Explicit, immutable, exact-build recovery certification of completed archives.

The diagnostic ZIP/manifest/completion are never rewritten. Issuance owns the
original maintenance/source fences; offline verification cannot grant writers.
Uncovered auxiliary history remains a blocker, rather than an assumed pass.
"""
import json
import os
from pathlib import Path
import re
from contextlib import contextmanager

from core.engine.persistence import atomic_create_json
from core.story_project.reviewed_state_document import digest,read_project_bytes
from workbench.backup_archive import BackupArchiveError,_file_hash,_lineage
from workbench.backup_archive_verification import verify_archived_target,_implementation
from workbench.backup_archive_view import verified_archive_view
from workbench.backup_version import validate_source_session
from workbench.book_ownership import OWNERSHIP_V2,OWNERSHIP_V3,WITNESS_PATH,_sha256
from workbench.backup_auxiliary_history import inspect_archived_auxiliary_history


PROTOCOL='target_recovery_certificate_v1'
PREVIEW='target_recovery_certification_preview_v1'
CERTIFICATE='recovery-certificate.json'
POLICY='windows_same_host_single_book_v1'

# These are supported current entry families, not a claim about arbitrary
# scripts, older installed writers, external machines, or unknown backends.
# The exact source hashes come from the independently measured implementation.
_WRITERS={
    'certified_onboarding_upgrade':('workbench/onboarding_upgrade.py','workbench/onboarding_upgrade_api.py'),
    'author_baseline_review':('workbench/author_baseline_service.py','workbench/author_baselines.py','workbench/author_baseline_api.py'),
    'workbench_admission':('workbench/maintenance.py','workbench/maintenance_api.py',
        'workbench/jobs.py','workbench/database.py','workbench/instance.py'),
    'generation_and_publication':('core/engine/executor.py','core/runtime_paths.py',
        'core/engine/persistence.py','core/engine/persistence_v2.py','core/engine/provider_safety.py',
        'core/engine/reviewed_chapter_commit.py','workbench/chapter_publication.py'),
    'identity_and_recovery':('core/story_project/activation.py','core/story_project/authority_persistence.py',
        'core/engine/recovery.py','core/engine/reviewed_upgrade.py','workbench/book_ownership.py',
        'workbench/registered_identity_upgrade.py','workbench/restored_identity_upgrade.py'),
    'review_and_migration':('core/review/dashboard.py','core/review/index.py',
        'core/story_project/migration.py','core/story_project/migration_execution.py'),
    'delivery_and_autonomy':('core/delivery.py','core/autonomy/common.py',
        'core/autonomy/session.py','core/autonomy/arc.py','core/autonomy/receipts.py'),
    'restore_protocol':('workbench/backup_source.py','workbench/backup_restore_materialization.py',
        'workbench/backup_restore_roots.py','workbench/backup_restore_database.py',
        'workbench/restore_handoff.py','workbench/restore_usage.py','workbench/restore_budget_preparation.py',
        'workbench/restore_jobs.py','workbench/restore_executions.py','workbench/restore_activation.py'),
}


def _policy(implementation):
    files=implementation['files']
    coverage={name:{path:files[path] for path in paths} for name,paths in _WRITERS.items()}
    return {'schema_version':POLICY,'host_platform':'Windows',
        'target_scope':'single_registered_strict_legacy_book',
        'identity_versions':['1.0','2.0','3.0'],'minimum_writer_contract_max':2,
        'database_scope':'whole_database_other_projects_disabled',
        'writer_coverage':coverage,
        'restore':{'copy_to_new_empty_container':True,'immutable_history_preserved':True,
            'current_machine_handoff_required':True,'original_machine_control_files_required':True,
            'latest_source_usage_verification_required':True,
            'source_unavailable_write_activation_supported':False,
            'unknown_usage_write_activation_allowed':False,
            'automatic_provider_calls_allowed':False,'fresh_execution_preparation_required':True},
        'compatibility':'exact_issuer_and_verifier_source_build',
        'arbitrary_external_writers_covered':False,'loaded_modules_match_proven':False}


def _certificate(evidence,preview,implementation,auxiliary):
    return {'schema_version':PROTOCOL,'archive_id':evidence['archive_id'],
        'archive_receipt_sha256':evidence['archive_receipt_sha256'],
        'archive_binding':evidence['archive_binding'],
        'project_id':evidence['project_id'],'book_id':evidence['book_id'],
        'identity_sha256':evidence['identity_sha256'],
        'data_verification_sha256':evidence['verification_sha256'],
        'auxiliary_history_sha256':auxiliary['verification_sha256'],
        'source_session':preview['source_session'],
        'verifier':evidence['verifier'],'policy':_policy(implementation),
        'recoverable':True,'write_activation_allowed':False,
        'automatic_provider_calls_allowed':False}


def _registered_archive(view,project_id,book_id):
    """Check archived registration/witness relationships without live paths."""
    preview=view.manifest['preview'];scope=preview['scope']
    try:
        ownership=scope.get('machine_ownership',{});record=ownership.get('record')
        version=record['version']
        fields={'version','generation','state','binding','record_sha256','registration_id','witness_sha256'}
        if version==OWNERSHIP_V3:fields.add('restore_qualification_sha256')
        binding=record['binding'];identity=view.read_root('story_project','.novelagent/project.json')
        witness=json.loads(view.read_root('story_project',WITNESS_PATH))
        if (ownership.get('state')!='verified' or set(record)!=fields
                or version not in {OWNERSHIP_V2,OWNERSHIP_V3} or record['state']!='active'
                or type(record['generation']) is not int or record['generation']<1
                or record['record_sha256']!=digest({k:v for k,v in record.items() if k!='record_sha256'})
                or not isinstance(record['registration_id'],str)
                or re.fullmatch('[0-9a-f]{32}',record['registration_id']) is None
                or set(binding)!={'book_id','project_id','root','root_lineage','database',
                    'database_parent_lineage','database_file_identity','identity_sha256'}
                or binding['book_id']!=book_id or binding['project_id']!=project_id
                or binding['root']!=scope['inventories']['story_project']['source_root']
                or binding['identity_sha256']!=digest(identity)
                or binding['database_file_identity']!={'device':preview['database']['file_identity'][0],
                    'inode':preview['database']['file_identity'][1]}
                or set(witness)!={'version','book_id','registration_id','initial_binding_sha256','witness_sha256'}
                or witness['version']!='machine_book_witness_v1' or witness['book_id']!=book_id
                or witness['registration_id']!=record['registration_id']
                or not _sha256(witness['initial_binding_sha256'])
                or witness['witness_sha256']!=record['witness_sha256']
                or witness['witness_sha256']!=digest({k:v for k,v in witness.items() if k!='witness_sha256'})
                or version==OWNERSHIP_V3 and not _sha256(record['restore_qualification_sha256'])):
            raise ValueError('archive registration differs')
    except (OSError,ValueError,KeyError,TypeError,AttributeError,IndexError) as exc:
        raise BackupArchiveError('归档的原活动登记、作品凭证或物理数据库证据不同。') from exc


def _assessment(package,*,project_id,book_id,temporary_root=None):
    """Always derive decisions from the real package, never supplied booleans."""
    implementation=_implementation()
    evidence=verify_archived_target(package,project_id=project_id,book_id=book_id,
        temporary_root=temporary_root)
    blockers=[*evidence['blockers'],*evidence['continuation_blockers']]
    def block(code,reference='story_project'):
        item={'check':'certification','code':code,'reference':reference}
        if item not in blockers:blockers.append(item)
    with verified_archive_view(package) as view:
        preview=view.manifest['preview'];scope=preview['scope']
        if view.verified['receipt_sha256']!=evidence['archive_receipt_sha256']:
            raise BackupArchiveError('认证期间归档完成凭据发生变化。')
        if os.name!='nt':block('unsupported_certification_host','host')
        try:validate_source_session(preview.get('source_session'),preview['software'])
        except (ValueError,KeyError,TypeError):block('startup_source_version_evidence_missing','software')
        if preview['software']!=implementation:
            block('source_and_certifier_build_differ','software')
        try:_registered_archive(view,project_id,book_id)
        except BackupArchiveError:block('registered_active_source_required','ownership')
        auxiliary=inspect_archived_auxiliary_history(package,project_id=project_id,book_id=book_id)
        if auxiliary['archive_receipt_sha256']!=evidence['archive_receipt_sha256']:
            raise BackupArchiveError('辅助历史与归档绑定不同。')
        if auxiliary['complete_history_certified'] is not True:
            for issue in auxiliary['blockers']:block(issue['code'],issue['reference'])
        candidate=None
        if not blockers:
            candidate=_certificate(evidence,preview,implementation,auxiliary)
            candidate['certificate_sha256']=digest(candidate)
        if _implementation()!=implementation:
            raise BackupArchiveError('认证期间验证器版本发生变化，请重启后重新核对。')
        result={'schema_version':PREVIEW,'archive_id':evidence['archive_id'],
            'project_id':project_id,'book_id':book_id,
            'archive_receipt_sha256':evidence['archive_receipt_sha256'],
            'verifier_sha256':implementation['fingerprint_sha256'],
            'auxiliary_history':auxiliary,
            'blockers':sorted(blockers,key=lambda item:(item['check'],item['code'],item['reference'])),
            'can_certify':not blockers,'certificate':candidate,
            'write_activation_allowed':False,'automatic_provider_calls_allowed':False}
        result['assessment_sha256']=digest(result)
        return result,preview


def verify_recovery_certificate(package,*,project_id,book_id,temporary_root=None):
    """Read-only certificate + data re-verification; no source or owner access."""
    try:return _verify_recovery_certificate(package,project_id=project_id,book_id=book_id,temporary_root=temporary_root)
    except BackupArchiveError:raise
    except (OSError,ValueError,KeyError,TypeError,AttributeError,RuntimeError) as exc:
        raise BackupArchiveError('恢复认证凭据缺失或实际内容无法核验。') from exc


def _verify_recovery_certificate(package,*,project_id,book_id,temporary_root=None):
    package=Path(package).absolute();lineage=_lineage(package)
    before=_file_hash(package/CERTIFICATE)
    if before['size']>4*1024**2:raise BackupArchiveError('恢复认证凭据超过支持大小。')
    try:value=json.loads(read_project_bytes(package,CERTIFICATE))
    except (OSError,ValueError) as exc:raise BackupArchiveError('恢复认证凭据不可读。') from exc
    assessment,_=_assessment(package,project_id=project_id,book_id=book_id,temporary_root=temporary_root)
    if (not assessment['can_certify'] or value!=assessment['certificate']
            or _file_hash(package/CERTIFICATE)!=before or _lineage(package)!=lineage):
        raise BackupArchiveError('恢复认证凭据、当前验证器或实际归档内容不一致。')
    return {'schema_version':'target_recovery_certificate_verification_v1',
        'archive_id':value['archive_id'],'project_id':project_id,'book_id':book_id,
        'archive_receipt_sha256':value['archive_receipt_sha256'],
        'certificate_sha256':value['certificate_sha256'],'policy':value['policy'],
        'recoverable':True,'write_activation_allowed':False,'automatic_provider_calls_allowed':False}


class RecoveryCertification:
    """Explicit local issuance, under the existing live snapshot boundary."""
    def __init__(self,backups,*,fault_injector=None):
        self.backups=backups;self.fault_injector=fault_injector

    def _package(self,archive_id):
        if not isinstance(archive_id,str) or re.fullmatch('backup_[0-9a-f]{32}',archive_id) is None:
            raise BackupArchiveError('请选择当前作品已完成的本机归档。')
        return self.backups.archive_root/archive_id

    @contextmanager
    def _preview(self,project_id,archive_id,operation_id,temporary_root,snapshot_permit):
        service=self.backups;package=self._package(archive_id)
        service._software()
        if service.maintenance.status()['purpose']!='backup':
            raise BackupArchiveError('恢复认证需要原备份维护状态。')
        with service._source(project_id,operation_id,snapshot_permit) as (snapshot,scope,_,permit):
            result,archived=_assessment(package,project_id=project_id,book_id=scope['book_id'],
                temporary_root=temporary_root)
            current=service._preview(scope,snapshot,permit,operation_id)
            blockers=list(result['blockers'])
            for field in ('scope','database','runtime_config','software'):
                if archived[field]!=current[field]:
                    blockers.append({'check':'source_freshness','code':'source_changed_since_archive','reference':field})
            result.update(operation_id=operation_id,
                current_source_preview_sha256=current['preview_sha256'],blockers=blockers,
                can_certify=not blockers,certificate=result['certificate'] if not blockers else None)
            result['assessment_sha256']=digest({k:v for k,v in result.items() if k!='assessment_sha256'})
            yield result,package,permit

    def preview(self,project_id,*,archive_id,operation_id,temporary_root=None,snapshot_permit=None):
        with self._preview(project_id,archive_id,operation_id,temporary_root,snapshot_permit) as (result,_,__):
            return result

    def issue(self,project_id,*,archive_id,operation_id,expected_assessment_sha256,
              temporary_root=None,snapshot_permit=None):
        if not isinstance(expected_assessment_sha256,str) or re.fullmatch('[0-9a-f]{64}',expected_assessment_sha256) is None:
            raise BackupArchiveError('认证需要已核对的预览摘要。')
        with self._preview(project_id,archive_id,operation_id,temporary_root,snapshot_permit) as (result,package,permit):
            if result['assessment_sha256']!=expected_assessment_sha256:
                raise BackupArchiveError('认证依据已偏离预览，请重新核对。')
            if not result['can_certify']:raise BackupArchiveError('归档仍有未完成的恢复认证条件。')
            value=result['certificate'];path=package/CERTIFICATE
            if self.fault_injector:self.fault_injector('before_certificate',result)
            service_software,_=self.backups._software()
            if service_software['fingerprint_sha256']!=result['verifier_sha256']:
                raise BackupArchiveError('认证发布前版本发生变化。')
            rechecked,_=_assessment(package,project_id=project_id,book_id=result['book_id'],
                temporary_root=temporary_root)
            if not rechecked['can_certify'] or rechecked['certificate']!=value:
                raise BackupArchiveError('认证发布前归档或实际核验依据发生变化。')
            permit.require()
            if os.path.lexists(path):
                if _file_hash(path)['size']>4*1024**2:
                    raise BackupArchiveError('原恢复认证凭据超过支持大小，不能覆盖或自动修复。')
                try:existing=json.loads(read_project_bytes(package,CERTIFICATE))
                except (OSError,ValueError) as exc:
                    raise BackupArchiveError('原恢复认证凭据无法读取，不能覆盖或自动修复。') from exc
                if existing!=value:
                    raise BackupArchiveError('原恢复认证凭据缺损或属于不同依据，不能覆盖。')
            else:atomic_create_json(path,value)
            if self.fault_injector:self.fault_injector('after_certificate',result)
        return verify_recovery_certificate(package,project_id=project_id,book_id=result['book_id'],
            temporary_root=temporary_root)
