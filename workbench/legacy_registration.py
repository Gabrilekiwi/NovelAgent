"""Explicit first legacy writer registration before recoverable upgrade backup.

This changes only ownership controls. A matching, data-verified diagnostic
archive preserves the pre-registration book; it is not an upgrade permit or a
recoverability certificate. The ordinary reviewed enrollment contract remains.
"""
from contextlib import contextmanager
import json
import re
from pathlib import Path

from core.engine.persistence import atomic_create_json, atomic_write_json, held_persistence_lock_readers, persistence_state_lock
from core.engine.provider_safety import provider_execution_lock
from core.story_project.reviewed_state_document import digest, read_project_bytes
from workbench.backup_archive import BackupArchiveError
from workbench.backup_archive_view import verified_archive_view
from workbench.backup_archive_verification import verify_archived_target
from workbench.backup_database import _logical_digest, owned_database_snapshot
from workbench.backup_files import inventory_tree
from workbench.backup_version import fingerprint_software
from workbench.book_ownership import BookOwnership, BookOwnershipError, OWNERSHIP_V2, WITNESS_PATH, _record, _witness, _require_witness, _new_witness
from workbench.maintenance import MaintenanceError
from workbench.writer_identity import load_verified_writer_identity


PROTOCOL = 'legacy_writer_registration_v1'
ADMISSION = 'legacy-registration.json'
OBSERVED = 'legacy-registration-observed.json'


class LegacyRegistration:
    def __init__(self, backups, *, fault_injector=None):
        self.backups = backups
        self.fault_injector = fault_injector

    def _archive(self, archive_id):
        if not isinstance(archive_id, str) or re.fullmatch('backup_[0-9a-f]{32}', archive_id) is None:
            raise BackupArchiveError('请选择当前作品已完成的本机归档。')
        return self.backups.archive_root / archive_id

    def _template(self, project_id, archive_id):
        package = self._archive(archive_id)
        with verified_archive_view(package) as view:
            saved = view.manifest['preview']
            scope = saved['scope']
            if (saved['project_id'] != project_id or scope['machine_ownership']['state'] != 'legacy_not_enrolled'
                    or scope['machine_ownership']['record'] is not None or scope.get('restored_provenance') is not None):
                raise BackupArchiveError('此入口仅用于尚未登记的原始旧书，不能修复丢失登记或重新登记恢复副本。')
            proof = verify_archived_target(package, project_id=project_id, book_id=saved['book_id'])
            if (not proof['offline_data_verified'] or proof['identity']['schema_version'] not in {'1.0', '2.0'}
                    or proof['archive_receipt_sha256'] != view.verified['receipt_sha256']):
                raise BackupArchiveError('旧书归档的 Strict 身份、数据或历史尚未核验，请先处理接入问题。')
            if fingerprint_software() != saved['software'] or self.backups.runtime_config != {
                    k:saved['runtime_config'][k] for k in self.backups.runtime_config}:
                raise BackupArchiveError('归档版本或当前运行配置已经变化，请重新核对。')
            return {'schema_version': PROTOCOL, 'project_id': project_id, 'book_id': saved['book_id'],
                    'archive_id': archive_id, 'archive_receipt_sha256': view.verified['receipt_sha256'],
                    'source_preview': saved, 'verification_sha256': proof['verification_sha256']}

    @contextmanager
    def _locked(self, project_id, operation_id, snapshot_permit):
        backups = self.backups
        if backups.projects.database.restore_scope is not None:
            raise BackupArchiveError('恢复实例不能使用原始旧书首次登记入口。')
        if backups.maintenance.status()['purpose'] != 'backup':
            raise MaintenanceError('旧书登记需要备份维护状态。')
        context = (backups.maintenance.capture(operation_id) if snapshot_permit is None else
                   backups.maintenance.borrow_capture(snapshot_permit, operation_id))
        with context as permit:
            project = backups.projects.get(project_id)
            root = Path(project.root)
            with BookOwnership(project.book_id, machine_store=backups.machine_store).locked() as owner:
                with persistence_state_lock((root,)), provider_execution_lock(root/'.novelagent/runtime/runs'):
                    # Registration changes ownership files, never application SQL.
                    # Keep the same writer fence/read snapshot as backup, including
                    # restart while maintenance has deferred schema initialization.
                    with owned_database_snapshot(backups.projects.database, snapshot_permit=permit) as snapshot:
                        connection = snapshot.connection
                        binding = owner._binding(root=root, database=backups.projects.database,
                            project_id=project_id, connection=connection)
                        yield owner, connection, binding, permit
                        snapshot.require(backups.projects.database, permit)

    def _current(self, template, owner, connection, binding):
        saved = template['source_preview']
        scope = saved['scope']
        if (binding['book_id'] != template['book_id'] or binding['root'] != scope['source_root']
                or binding['identity_sha256'] != scope['inventories']['story_project']['files']['.novelagent/project.json']['sha256']
                or [binding['database_file_identity'][k] for k in ('device','inode')] != saved['database']['file_identity']
                or _logical_digest(connection) != saved['database']['logical_sha256']):
            raise BackupArchiveError('旧书或整库登记已偏离原归档，请保留现场并重新核对。')
        record = _record(owner.directory, owner.book_id)
        witness = _witness(Path(binding['root']), owner.book_id)
        observed_path = owner.directory / OBSERVED
        if record is None:
            if witness is not None or observed_path.exists():
                raise BookOwnershipError('旧书登记证明已丢失，不能当作首次登记重建。')
        else:
            if (record['version'] != OWNERSHIP_V2 or record['generation'] != 1
                    or record['state'] not in {'enrolling', 'active'} or record['binding'] != binding):
                raise BookOwnershipError('旧书登记代际或物理绑定已变化。')
            expected = _new_witness(owner.book_id, record['registration_id'], binding)
            if record['witness_sha256'] != expected['witness_sha256'] or (witness is not None and witness != expected):
                raise BookOwnershipError('旧书登记凭证与原意图不同。')
            if record['state'] == 'active':
                _require_witness(Path(binding['root']), owner.book_id, record)
        for key, original in scope['inventories'].items():
            tree = inventory_tree(original['source_root'], locked_readers=held_persistence_lock_readers())
            if key == 'story_project' and witness is not None:
                item = tree['files'].pop(WITNESS_PATH)
                tree['total_bytes'] -= item['size']
                tree['inventory_sha256'] = digest({k:v for k,v in tree.items() if k != 'inventory_sha256'})
            if tree != original:
                raise BackupArchiveError('原书或任务来源已变化，不能沿用旧登记预览。')
        proof = load_verified_writer_identity(binding['root'])
        if proof['identity']['schema_version'] not in {'1.0', '2.0'}:
            raise BackupArchiveError('原作品格式已改变，不能沿用旧书登记。')
        return record

    def _admission(self, template, owner, binding):
        path = owner.directory / ADMISSION
        if not path.exists():
            if _record(owner.directory, owner.book_id) is not None:
                raise BookOwnershipError('已有机器登记缺少本流程原意图，不能重新登记。')
            return None
        try:
            value = json.loads(read_project_bytes(owner.directory, ADMISSION))
        except (OSError, ValueError) as exc:
            raise BookOwnershipError('旧书原登记意图无法读取，请保留现场。') from exc
        expected = {**template, 'binding': binding}
        if (not isinstance(value, dict) or set(value) != set(expected)|{'initial_preview_sha256','admission_sha256'}
                or any(value[k] != v for k,v in expected.items())
                or re.fullmatch('[0-9a-f]{64}', str(value['initial_preview_sha256'])) is None
                or value['admission_sha256'] != digest({k:v for k,v in value.items() if k!='admission_sha256'})):
            raise BookOwnershipError('旧书原登记意图无法核验，请保留现场。')
        return value

    def _preview(self, template, owner, connection, binding, operation_id):
        admission = self._admission(template, owner, binding)
        record = self._current(template, owner, connection, binding)
        state = 'registered' if record and record['state']=='active' else 'resumable' if admission else 'ready'
        result = {'schema_version':'legacy_writer_registration_preview_v1',
                  'project_id':template['project_id'], 'book_id':template['book_id'],
                  'operation_id':operation_id, 'archive_id':template['archive_id'],
                  'archive_receipt_sha256':template['archive_receipt_sha256'], 'state':state,
                  'can_register':state in {'ready','resumable'}, 'changes_format':False,
                  'archive_recoverable':False, 'automatic_provider_calls_allowed':False}
        result['preview_sha256'] = digest(result)
        return result, admission

    def preview(self, project_id, *, operation_id, archive_id, snapshot_permit=None):
        # Initial admission uses the complete supported backup source boundary.
        project = self.backups.projects.get(project_id)
        owner = BookOwnership(project.book_id, machine_store=self.backups.machine_store)
        if not (owner.directory/ADMISSION).exists():
            from workbench.onboarding import OnboardingPreviewService
            initial = OnboardingPreviewService(self.backups).preview(project_id, operation_id=operation_id,
                archive_id=archive_id, snapshot_permit=snapshot_permit)
            archive = initial['archive']
            if not archive or not archive['source_matches'] or archive['offline_data_verified'] is not True:
                raise BackupArchiveError('请先保存并核对当前旧书归档，再明确登记写入保护。')
        template = self._template(project_id, archive_id)
        with self._locked(project_id, operation_id, snapshot_permit) as (owner, connection, binding, permit):
            return self._preview(template, owner, connection, binding, operation_id)[0]

    def register(self, project_id, *, operation_id, archive_id, expected_preview_sha256,
                 confirmed, snapshot_permit=None):
        if confirmed is not True or re.fullmatch('[0-9a-f]{64}', str(expected_preview_sha256)) is None:
            raise BackupArchiveError('请核对原书和归档，再明确确认登记写入保护。')
        self.preview(project_id, operation_id=operation_id, archive_id=archive_id, snapshot_permit=snapshot_permit)
        template = self._template(project_id, archive_id)
        with self._locked(project_id, operation_id, snapshot_permit) as (owner, connection, binding, permit):
            preview, admission = self._preview(template, owner, connection, binding, operation_id)
            if expected_preview_sha256 not in {preview['preview_sha256'],
                    admission['initial_preview_sha256'] if admission else None}:
                raise BackupArchiveError('旧书登记预览已经变化，请重新核对。')
            if admission is None:
                admission = {**template, 'binding':binding, 'initial_preview_sha256':preview['preview_sha256']}
                admission['admission_sha256'] = digest(admission)
                atomic_create_json(owner.directory/ADMISSION, admission)
                if self.fault_injector: self.fault_injector('after_legacy_admission', admission)
            def observed(phase, record):
                atomic_write_json(owner.directory/OBSERVED, {'admission_sha256':admission['admission_sha256'],
                    'phase':phase, 'record_sha256':record['record_sha256']})
                if self.fault_injector: self.fault_injector(phase, record)
            owner.fault_injector = observed
            permit.require()
            record = owner._enroll_verified_legacy(root=binding['root'], database=self.backups.projects.database,
                project_id=project_id, connection=connection)
            self._current(template, owner, connection, binding)
            permit.require()
            return {'schema_version':PROTOCOL, 'project_id':project_id, 'book_id':owner.book_id,
                    'state':record['state'], 'registration_id':record['registration_id'],
                    'generation':record['generation'], 'changes_format':False, 'archive_recoverable':False,
                    'automatic_provider_calls_allowed':False, 'requires_new_backup':True}
