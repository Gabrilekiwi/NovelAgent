"""Durable revocation for an actual restored book, before write activation.

The machine ledger is the CAS authority. A completed revocation leaves both
copies blocked; activation additionally needs preserved budgets and fresh plans.
Rollback is explicit and requires the original physical DB and book to match.
"""
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3

from core.engine.persistence import atomic_create_json, atomic_write_json, persistence_state_lock, persistence_run_lock
from core.engine.safe_paths import assert_safe_local_tree
from core.story_project.reviewed_state_document import digest, read_project_bytes
from workbench.backup_archive_view import verified_archive_view
from workbench.backup_database import _read_authorizer
from workbench.backup_restore_database import prepare_restored_workbench
from workbench.backup_restore_roots import _read
from workbench.book_ownership import (BookOwnership, BookOwnershipError, OWNERSHIP_V2, OWNERSHIP_V3,
    _record, _require_witness, _file_identity, _sha256, _lineage)
from workbench.database import WorkbenchDatabase
from workbench.instance import InstanceLease
from workbench.writer_identity import load_verified_writer_identity


class RestoreHandoffError(BookOwnershipError):
    code = 'restore_handoff_invalid'


def _signed(value, field='record_sha256'):
    return {**value, field:digest({k:v for k,v in value.items() if k != field})}


def restoring_record(source, binding):
    # An earlier activation qualifies only its original physical copy. Carry
    # its evidence in the handoff, never its write permission onto the target.
    record = {k:v for k,v in source.items() if k != 'restore_qualification_sha256'}
    return _signed({**record, 'version':OWNERSHIP_V2,
        'generation':source['generation']+1, 'state':'restoring', 'binding':binding})


def validate_handoff_intent(value, book_id, *, depth=0):
    """Validate recorded bindings without opening ancestral files or ledgers."""
    try:
        fields = {'schema_version','handoff_id','source_record','restoring_record','target',
            'plan_sha256','archive_receipt_sha256','database_binding_receipt_sha256','intent_sha256'}
        next_generation = isinstance(value,dict) and value.get('schema_version') in {'machine_restore_handoff_v2','machine_restore_handoff_v4'}
        qualified_ancestor = isinstance(value,dict) and value.get('schema_version') == 'machine_restore_handoff_v4'
        rollback_ancestor = isinstance(value,dict) and value.get('schema_version') == 'machine_restore_handoff_v3'
        registration_upgrade = isinstance(value,dict) and value.get('schema_version') == 'machine_restore_handoff_v5'
        if next_generation: fields.add('source_restore_provenance')
        if qualified_ancestor: fields.add('archived_source_restore_provenance')
        if rollback_ancestor: fields.update({'archived_source_record','source_rollback_ancestry'})
        if registration_upgrade: fields.update({'archived_source_record','source_identity_upgrade'})
        if (depth > 16 or not isinstance(value,dict) or set(value) != fields
                or value['schema_version'] not in {'machine_restore_handoff_v1','machine_restore_handoff_v2','machine_restore_handoff_v3','machine_restore_handoff_v4','machine_restore_handoff_v5'}
                or (qualified_ancestor or registration_upgrade) and len(json.dumps(value,ensure_ascii=False).encode('utf-8'))>32*1024**2
                or value != _signed(value,'intent_sha256')):
            raise ValueError('invalid handoff intent')
        source=value['source_record']; target=value['restoring_record']; root=Path(value['target'])
        if (not root.is_absolute() or '..' in root.parts
                or source['version'] != (OWNERSHIP_V3 if next_generation else OWNERSHIP_V2)
                or source['state'] != 'active' or source['binding']['book_id'] != book_id
                or source != _signed(source) or type(source['generation']) is not int or source['generation'] < 1
                or target != restoring_record(source,target['binding'])
                or target['binding']['book_id'] != book_id
                or target['binding']['root'] != str(root/'book')
                or target['binding']['database'] != str(root/'workbench.sqlite')
                or target['binding']['project_id'] != source['binding']['project_id']
                or target['binding']['root'] == source['binding']['root']
                or target['binding']['database'] == source['binding']['database']
                or not all(_sha256(value[k]) for k in ('plan_sha256','archive_receipt_sha256',
                    'database_binding_receipt_sha256','handoff_id'))
                or value['handoff_id'] != digest({'source_record_sha256':source['record_sha256'],
                    'target_binding':target['binding'],'plan_sha256':value['plan_sha256']})):
            raise ValueError('handoff record bindings differ')
        if next_generation and not qualified_ancestor:
            from workbench.backup_restore_provenance import validate_restore_provenance
            validate_restore_provenance(value['source_restore_provenance'],
                root=Path(source['binding']['root']),book_id=book_id,ownership=source,depth=depth+1)
        if rollback_ancestor:
            from workbench.restore_rollback_ancestry import validate_rollback_ancestry
            validate_rollback_ancestry(value['archived_source_record'], source,
                value['source_rollback_ancestry'], book_id, depth=depth+1)
        if qualified_ancestor:
            from workbench.restore_qualified_archive_ancestry import validate_qualified_archive_ancestry
            validate_qualified_archive_ancestry(value['archived_source_restore_provenance'],
                value['source_restore_provenance'], source, book_id, depth=depth+1)
        if registration_upgrade:
            from workbench.restore_registration_upgrade import validate_registration_upgrade_proof
            validate_registration_upgrade_proof(value['archived_source_record'],source,
                value['source_identity_upgrade'],book_id,depth=depth+1)
        return value
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise RestoreHandoffError('交接意图无法核验。') from exc


def rollback_decision(value):
    source=value['source_record']
    record=_signed({**source,'generation':value['restoring_record']['generation']+1})
    return _signed({'schema_version':'machine_restore_rollback_v1','handoff_id':value['handoff_id'],
        'intent_sha256':value['intent_sha256'],'record':record},'decision_sha256')


def verify_qualified_source(value, connection, owner):
    """Privileged evidence read after revocation; grants no database writes.

    Ordinary WorkbenchDatabase startup intentionally refuses this revoked
    source. Here the original archive-bound scope and static qualification are
    checked under the machine, book, Provider and SQLite fences instead.
    """
    owner._held()
    verify_qualified_source_evidence(value,connection,owner.directory)


def verify_qualified_source_evidence(value, connection, directory):
    """Read the same source proof under either a writer or controller read fence."""
    from workbench.restore_scope import _stored, _sidecar
    from workbench.backup_restore_provenance import validate_restore_provenance
    from workbench.restore_reinstatement import load_reinstatements
    source=value['source_record']; binding=source['binding']; root=Path(binding['root'])
    path=Path(binding['database']); proof=value['source_restore_provenance']
    book_id=binding['book_id']
    validate_restore_provenance(proof,root=root,book_id=book_id,ownership=source)
    row=connection.execute('SELECT id,book_id,root FROM projects WHERE id=?',(binding['project_id'],)).fetchone()
    actual=connection.execute('PRAGMA database_list').fetchone()[2]
    if (not connection.in_transaction or Path(actual).resolve()!=path
            or row is None or tuple(row)!=(binding['project_id'],book_id,str(root))
            or binding['root_lineage']!=_lineage(root)
            or binding['database_parent_lineage']!=_lineage(path.parent)
            or binding['database_file_identity']!=_file_identity(path)
            or binding['identity_sha256']!=digest(read_project_bytes(root,'.novelagent/project.json'))):
        raise RestoreHandoffError('原恢复副本的物理数据库登记变化。')
    if (_stored(connection) != proof['scope'] or _read(path.parent,_sidecar(path).name) != proof['scope']):
        raise RestoreHandoffError('原恢复副本的范围已偏离交接证据。')
    local={'qualification':'write-qualification.json','plan':'restore-plan.json',
        'materialization':'restore-materialization.json','root_intent':'root-binding.json',
        'root_done':'root-binding-done.json','database_intent':'database-binding.json',
        'database_done':'database-binding-done.json','execution_policy':'execution-policy.json'}
    prefix='handoff-'+proof['qualification']['handoff_id']+'-'
    machine={'activation_intent':'activation','activation_done':'activation-done',
        'jobs_intent':'jobs','jobs_done':'jobs-done','budget_done':'budget-done',
        'execution_intent':'executions','execution_done':'executions-done'}
    for key,name in local.items():
        if _read(path.parent,name) != proof[key]:
            raise RestoreHandoffError('原恢复副本的本地启用证据变化。')
    for key,suffix in machine.items():
        if _read(directory,prefix+suffix+'.json') != proof[key]:
            raise RestoreHandoffError('原恢复副本的机器启用证据变化。')
    if load_reinstatements(directory,proof['activation_intent']['active_record'],
            proof['qualification'],source) != proof.get('qualification_reinstatements',[]):
        raise RestoreHandoffError('原恢复副本的资格续接证据变化。')
    _require_witness(root,book_id,source)
    if load_verified_writer_identity(root)['identity']['book_id']!=book_id:
        raise RestoreHandoffError('原恢复副本的作品身份变化。')


@contextmanager
def _database_fence(path):
    path = Path(path).absolute()
    assert_safe_local_tree(path.parent)
    identity = _file_identity(path)
    with closing(sqlite3.connect(path.as_uri()+'?mode=rw', uri=True, isolation_level=None, timeout=1)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('BEGIN IMMEDIATE')
        connection.set_authorizer(_read_authorizer)
        try:
            yield connection
            if _file_identity(path) != identity:
                raise RestoreHandoffError('交接期间数据库物理身份变化。')
        finally:
            connection.set_authorizer(None)
            connection.rollback()


class RestoreHandoff:
    def __init__(self, book_id, *, machine_store=None, fault_injector=None):
        self.owner = BookOwnership(book_id, machine_store=machine_store)
        self.fault_injector = fault_injector

    def _fault(self, phase, value):
        if self.fault_injector is not None:
            self.fault_injector(phase, value)

    def _path(self, handoff_id, suffix='intent'):
        if not _sha256(handoff_id):
            raise RestoreHandoffError('交接标识无效。')
        return self.owner.directory / ('handoff-'+handoff_id+'-'+suffix+'.json')

    def _load(self, handoff_id):
        self.owner._held()
        path = self._path(handoff_id)
        _file_identity(path)
        if path.stat().st_size > 32*1024**2:
            raise RestoreHandoffError('交接意图超过支持大小。')
        try:
            value = json.loads(read_project_bytes(path.parent, path.name))
            if isinstance(value,dict) and value.get('schema_version')=='machine_restore_handoff_v1' and path.stat().st_size>1024**2:
                raise ValueError('original handoff exceeds supported size')
            validate_handoff_intent(value,self.owner.book_id)
            if value['schema_version']=='machine_restore_handoff_v3':
                from workbench.restore_rollback_ancestry import verify_rollback_ancestry_files
                verify_rollback_ancestry_files(self.owner.directory,value['archived_source_record'],
                    value['source_record'],value['source_rollback_ancestry'],self.owner.book_id)
            if value['schema_version']=='machine_restore_handoff_v4':
                from workbench.restore_qualified_archive_ancestry import verify_qualified_archive_ancestry_files
                verify_qualified_archive_ancestry_files(self.owner.directory,
                    value['archived_source_restore_provenance'],value['source_restore_provenance'],
                    value['source_record'],self.owner.book_id)
            if value['schema_version']=='machine_restore_handoff_v5':
                from workbench.restore_registration_upgrade import verify_registration_upgrade_files
                verify_registration_upgrade_files(self.owner.directory,value['archived_source_record'],
                    value['source_record'],value['source_identity_upgrade'],self.owner.book_id)
            if value['handoff_id'] != handoff_id: raise ValueError('handoff identifier differs')
            return value
        except (KeyError, TypeError, ValueError) as exc:
            raise RestoreHandoffError('交接意图无法核验。') from exc

    def _id(self, source, target_binding, plan_sha256):
        return digest({'source_record_sha256':source['record_sha256'],
            'target_binding':target_binding,'plan_sha256':plan_sha256})

    @contextmanager
    def _target(self, package, destination, temporary_root, *, _usage_overlay=None):
        root = assert_safe_local_tree(Path(destination).absolute())
        _lineage(root)
        _file_identity(root/'workbench.sqlite')
        with closing(InstanceLease(root).acquire()) as lease:
            # No repair, scope creation or SQL rebinding is permitted here.
            with persistence_state_lock((root/'book',)), _database_fence(root/'workbench.sqlite') as connection:
                receipt = prepare_restored_workbench(package, destination=root,
                    temporary_root=temporary_root, _lease=lease, _verify_only=True, _usage_overlay=_usage_overlay)
                database = WorkbenchDatabase(root/'workbench.sqlite',machine_store=self.owner.store)
                scope = database.restore_scope
                if scope is None or scope.document['book_id'] != self.owner.book_id:
                    raise RestoreHandoffError('恢复副本不属于当前交接作品。')
                binding = self.owner._binding(root=root/'book', database=database,
                    project_id=scope.project_id, connection=connection)
                load_verified_writer_identity(root/'book')
                with verified_archive_view(package) as view:
                    evidence = view.manifest['preview']['scope'].get('machine_ownership')
                    archived = evidence.get('record') if isinstance(evidence, dict) else None
                    if evidence is None or evidence.get('state') != 'verified' or archived is None:
                        raise RestoreHandoffError('归档没有可核对的原机器登记。')
                    _require_witness(root/'book', self.owner.book_id, archived)
                    yield root, receipt, binding, archived
                    scope.require_unchanged(database.path)
                    # Whole copied history/DB is reverified after any transition;
                    # a failed post-check leaves the durable revocation in place.
                    if prepare_restored_workbench(package, destination=root, temporary_root=temporary_root,
                            _lease=lease, _verify_only=True, _usage_overlay=_usage_overlay) != receipt:
                        raise RestoreHandoffError('交接期间恢复凭据变化。')

    def preview(self, package, *, destination, temporary_root=None):
        with self.owner.locked():
            source = _record(self.owner.directory, self.owner.book_id)
            if source is None or source['version'] not in {OWNERSHIP_V2,OWNERSHIP_V3} or source['state'] != 'active':
                raise RestoreHandoffError('首次交接需要原本机有效登记；已有交接必须显式续接。')
            with self._target(package, destination, temporary_root) as (root, receipt, binding, archived):
                if source['binding'] == binding:
                    raise RestoreHandoffError('恢复交接需要独立的实际恢复副本。')
                value = self._intent(source, root, receipt, binding, archived=archived)
                if _record(self.owner.directory, self.owner.book_id) != source:
                    raise RestoreHandoffError('交接预览期间机器登记变化。')
            return value

    def _intent(self, source, root, receipt, binding, *, archived=None):
        value={'schema_version':'machine_restore_handoff_v1',
            'handoff_id':self._id(source, binding, receipt['plan_sha256']), 'source_record':source,
            'restoring_record':restoring_record(source,binding),
            'target':str(root), 'plan_sha256':receipt['plan_sha256'],
            'archive_receipt_sha256':_read(root, 'restore-plan.json')['archive_receipt_sha256'],
            'database_binding_receipt_sha256':receipt['receipt_sha256']}
        if source['version'] == OWNERSHIP_V3:
            value.update(schema_version='machine_restore_handoff_v2',
                source_restore_provenance=_read(root,'restore-plan.json').get('source_restore_provenance'))
        witness=_require_witness(root/'book',self.owner.book_id,archived) if archived is not None else None
        if (source['version']==OWNERSHIP_V2 and witness is not None
                and witness['initial_binding_sha256']!=digest(source['binding'])):
            from workbench.restore_registration_upgrade import load_registration_upgrade_proof
            value.update(schema_version='machine_restore_handoff_v5',archived_source_record=archived,
                source_identity_upgrade=load_registration_upgrade_proof(self.owner.directory,archived,source,self.owner.book_id))
        elif archived is not None and archived != source:
            if source['version'] == OWNERSHIP_V3:
                from workbench.restore_qualified_archive_ancestry import load_qualified_archive_ancestry
                proof=value['source_restore_provenance']
                if not isinstance(proof,dict) or proof.get('active_record')!=archived:
                    raise RestoreHandoffError('归档登记与原恢复来源证明不同。')
                value.update(schema_version='machine_restore_handoff_v4',
                    archived_source_restore_provenance=proof,
                    source_restore_provenance=load_qualified_archive_ancestry(self.owner.directory,
                        proof,source,self.owner.book_id))
            else:
                from workbench.restore_rollback_ancestry import load_rollback_ancestry
                value.update(schema_version='machine_restore_handoff_v3',archived_source_record=archived,
                    source_rollback_ancestry=load_rollback_ancestry(self.owner.directory,
                        archived,source,self.owner.book_id))
        value=_signed(value,'intent_sha256')
        validate_handoff_intent(value,self.owner.book_id)
        return value

    def begin(self, package, *, destination, expected_preview_sha256, temporary_root=None):
        if not _sha256(expected_preview_sha256):
            raise RestoreHandoffError('必须核对交接预览摘要。')
        with self.owner.locked():
            source = _record(self.owner.directory, self.owner.book_id)
            if source is None or source['version'] not in {OWNERSHIP_V2,OWNERSHIP_V3} or source['state'] != 'active':
                raise RestoreHandoffError('原登记不再活动，请显式续接原交接。')
            with self._target(package, destination, temporary_root) as (root, receipt, binding, archived):
                value = self._intent(source, root, receipt, binding, archived=archived)
                if value['intent_sha256'] != expected_preview_sha256:
                    raise RestoreHandoffError('当前登记或恢复副本已偏离交接预览。')
                path = self._path(value['handoff_id'])
                if os.path.lexists(path):
                    raise RestoreHandoffError('原交接意图已存在，请使用原标识显式续接。')
                atomic_create_json(path, value)
                self._fault('after_handoff_intent', value)
                return self._revoke(value)

    def resume(self, package, *, handoff_id, temporary_root=None):
        with self.owner.locked():
            value = self._load(handoff_id)
            if os.path.lexists(self._path(handoff_id, 'rollback')):
                raise RestoreHandoffError('交接已选择回退，不能重新撤销原副本资格。')
            with self._target(package, value['target'], temporary_root) as (root, receipt, binding, archived):
                if self._intent(value['source_record'], root, receipt, binding, archived=archived) != value:
                    raise RestoreHandoffError('续接的恢复副本或原归档已经变化。')
                return self._revoke(value)

    def _revoke(self, value):
        self.owner._held()
        if self._load(value['handoff_id']) != value:
            raise RestoreHandoffError('撤销前原交接意图变化。')
        current = _record(self.owner.directory, self.owner.book_id)
        if current == value['source_record']:
            atomic_write_json(self.owner.record_path, value['restoring_record'])
            self._fault('after_source_revoked', value)
        elif current != value['restoring_record']:
            raise RestoreHandoffError('当前机器登记不属于原交接，拒绝覆盖。')
        if _record(self.owner.directory, self.owner.book_id) != value['restoring_record']:
            raise RestoreHandoffError('原副本资格撤销后机器登记变化。')
        if self._load(value['handoff_id']) != value:
            raise RestoreHandoffError('撤销后原交接意图变化。')
        result = _signed({'schema_version':'machine_restore_revocation_v1','handoff_id':value['handoff_id'],
            'intent_sha256':value['intent_sha256'],'record_sha256':value['restoring_record']['record_sha256'],
            'source_revoked':True,'target_writable':False,'handoff_completed':False}, 'receipt_sha256')
        path = self._path(value['handoff_id'], 'revoked')
        if os.path.lexists(path):
            if _read(path.parent, path.name) != result:
                raise RestoreHandoffError('撤销完成凭据不同。')
        else:
            atomic_create_json(path, result)
            self._fault('after_revocation_receipt', value)
        return result

    def rollback(self, *, handoff_id, source_lease=None):
        with self.owner.locked():
            value = self._load(handoff_id); source = value['source_record']
            if source['version'] == OWNERSHIP_V3:
                return self._rollback_qualified(value,source_lease=source_lease)
            old = source['binding']; root = Path(old['root']); database = WorkbenchDatabase(old['database'])
            rolled_back = _signed({**source,'generation':value['restoring_record']['generation']+1})
            registration_upgrade = value['schema_version']=='machine_restore_handoff_v5'
            pending = _signed({**rolled_back,'state':'reactivating'}) if registration_upgrade else rolled_back
            allowed = (source,value['restoring_record'],pending,rolled_back)
            decision = _signed({'schema_version':'machine_restore_rollback_v1','handoff_id':handoff_id,
                'intent_sha256':value['intent_sha256'],'record':rolled_back}, 'decision_sha256')
            path = self._path(handoff_id, 'rollback')
            from workbench.restore_rollback_ancestry import rollback_receipt, rollback_receipt_name
            completion = rollback_receipt(value)
            completion_path = self.owner.directory/rollback_receipt_name(rolled_back)
            if os.path.lexists(completion_path) and _read(completion_path.parent,completion_path.name) != completion:
                raise RestoreHandoffError('回退完成凭据变化。')
            # Never reactivate a moved/replaced source or a different DB copy.
            current = _record(self.owner.directory, self.owner.book_id)
            if registration_upgrade and current==rolled_back and not os.path.lexists(completion_path):
                raise RestoreHandoffError('活动来源的原回退完成凭据丢失，不能补造。')
            if current not in allowed:
                raise RestoreHandoffError('机器登记已属于另一操作，不能回退。')
            if (_lineage(root) != old['root_lineage']
                    or _lineage(database.path.parent) != old['database_parent_lineage']
                    or _file_identity(database.path) != old['database_file_identity']):
                raise RestoreHandoffError('原作品或数据库物理身份已变化，不能回退。')
            # Same Provider boundary, but recovery must never recreate a lost
            # original runtime directory or its armed lock file.
            with persistence_state_lock((root,)), persistence_run_lock(
                    root/'.novelagent/runtime/runs/.provider-boundary-lock', require_existing_lock=True):
                with _database_fence(database.path) as connection:
                    if (database.restore_scope is not None or self.owner._binding(root=root, database=database,
                            project_id=old['project_id'], connection=connection) != old):
                        raise RestoreHandoffError('原作品或数据库物理登记已变化，不能回退。')
                    load_verified_writer_identity(root)
                    _require_witness(root, self.owner.book_id, source)
                    current = _record(self.owner.directory, self.owner.book_id)
                    if current not in allowed:
                        raise RestoreHandoffError('机器登记已属于另一操作，不能回退。')
                    if os.path.lexists(path):
                        if _read(path.parent, path.name) != decision:
                            raise RestoreHandoffError('已有回退决议不一致。')
                    else:
                        if current in (pending,rolled_back):
                            raise RestoreHandoffError('缺失原回退意图，不能补造凭据。')
                        self.owner._held()
                        atomic_create_json(path, decision)
                        self._fault('after_rollback_intent', value)
                    self.owner._held()
                    if _read(path.parent, path.name) != decision:
                        raise RestoreHandoffError('恢复原资格前回退决议变化。')
                    if os.path.lexists(completion_path) and _read(completion_path.parent,completion_path.name) != completion:
                        raise RestoreHandoffError('恢复原资格前回退完成凭据变化。')
                    current = _record(self.owner.directory, self.owner.book_id)
                    if current not in allowed:
                        raise RestoreHandoffError('恢复原资格前机器登记变化。')
                    if current not in (pending,rolled_back):
                        atomic_write_json(self.owner.record_path, pending)
                        self._fault('after_registration_rollback_pending' if registration_upgrade else 'after_source_reactivated', value)
                    self.owner._held()
                    if _record(self.owner.directory, self.owner.book_id) not in (pending,rolled_back):
                        raise RestoreHandoffError('回退后机器登记变化。')
                    if self.owner._binding(root=root, database=database, project_id=old['project_id'], connection=connection) != old:
                        raise RestoreHandoffError('回退后原登记物理绑定变化。')
                    _require_witness(root, self.owner.book_id, rolled_back)
                    if (self._load(handoff_id) != value or _read(path.parent,path.name) != decision):
                        raise RestoreHandoffError('回退完成前原交接或决议变化。')
                    created_completion = False
                    if os.path.lexists(completion_path):
                        if _read(completion_path.parent,completion_path.name) != completion:
                            raise RestoreHandoffError('回退完成凭据变化。')
                    else:
                        atomic_create_json(completion_path,completion)
                        created_completion = True
                        self._fault('after_registration_rollback_receipt' if registration_upgrade else 'after_rollback_completion',value)
                    if registration_upgrade:
                        promoted = _record(self.owner.directory,self.owner.book_id)==pending
                        if promoted:
                            if _read(completion_path.parent,completion_path.name)!=completion:
                                raise RestoreHandoffError('恢复活动来源前原回退凭据变化。')
                            atomic_write_json(self.owner.record_path,rolled_back)
                            self._fault('after_source_reactivated',value)
                        if created_completion or promoted:self._fault('after_rollback_completion',value)
                    if (_read(completion_path.parent,completion_path.name) != completion
                            or _record(self.owner.directory,self.owner.book_id) != rolled_back
                            or self.owner._binding(root=root,database=database,
                                project_id=old['project_id'],connection=connection) != old):
                        raise RestoreHandoffError('回退完成后当前资格或物理绑定变化。')
                    _require_witness(root,self.owner.book_id,rolled_back)
                    return {'handoff_id':handoff_id, 'rolled_back':True, 'source_record':rolled_back,
                        'target_writable':False}

    @contextmanager
    def _source_instance(self, database, lease):
        if lease is None:
            with closing(InstanceLease(database.parent).acquire()) as acquired:
                yield acquired
        else:
            lease.require_held()
            if lease.directory!=database.parent:
                raise RestoreHandoffError('原恢复控制实例锁不属于原数据库。')
            lineage=_lineage(database.parent)
            yield lease
            lease.require_held()
            if _lineage(database.parent)!=lineage:
                raise RestoreHandoffError('原恢复控制实例目录在回退期间变化。')

    def _rollback_qualified(self, value, *, source_lease=None):
        from workbench.restore_reinstatement import (reinstatement_intent,
            reinstatement_receipt, reinstatement_name)
        from workbench.restore_scope import load_restore_scope
        source=value['source_record']; old=source['binding']; root=Path(old['root'])
        database=Path(old['database']); handoff_id=value['handoff_id']
        decision=rollback_decision(value); rolled_back=decision['record']
        path=self._path(handoff_id,'rollback')
        renewal=reinstatement_intent(value)
        renewal_path=self.owner.directory/reinstatement_name(rolled_back)
        done_path=self.owner.directory/reinstatement_name(rolled_back,done=True)

        def require_current():
            self.owner._held()
            current=_record(self.owner.directory,self.owner.book_id)
            if current not in (source,value['restoring_record'],rolled_back):
                raise RestoreHandoffError('机器记录已属于其他操作，不能恢复原副本资格。')
            return current

        def physical():
            if (_lineage(root)!=old['root_lineage']
                    or _lineage(database.parent)!=old['database_parent_lineage']
                    or _file_identity(database)!=old['database_file_identity']):
                raise RestoreHandoffError('原恢复副本或数据库的物理身份变化，不能回退。')

        require_current(); physical()
        with self._source_instance(database,source_lease), persistence_state_lock((root,)), persistence_run_lock(
                root/'.novelagent/runtime/runs/.provider-boundary-lock',require_existing_lock=True):
            with _database_fence(database) as connection:
                verify_qualified_source(value,connection,self.owner)
                current=require_current()
                if os.path.lexists(path):
                    if _read(path.parent,path.name)!=decision:
                        raise RestoreHandoffError('原回退决议变化。')
                else:
                    if current==rolled_back:
                        raise RestoreHandoffError('原回退决议丢失，不能补造。')
                    atomic_create_json(path,decision)
                    self._fault('after_rollback_intent',value)
                if os.path.lexists(renewal_path):
                    if _read(renewal_path.parent,renewal_path.name)!=renewal:
                        raise RestoreHandoffError('原资格续接意图变化。')
                else:
                    if current==rolled_back or os.path.lexists(done_path):
                        raise RestoreHandoffError('原资格续接意图丢失，不能补造。')
                    atomic_create_json(renewal_path,renewal)
                    self._fault('after_reinstatement_intent',value)
                verify_qualified_source(value,connection,self.owner)
                physical()
                if (self._load(handoff_id)!=value or _read(path.parent,path.name)!=decision
                        or _read(renewal_path.parent,renewal_path.name)!=renewal):
                    raise RestoreHandoffError('恢复机器资格前原交接、回退或续接意图变化。')
                if require_current()!=rolled_back:
                    atomic_write_json(self.owner.record_path,rolled_back)
                    self._fault('after_source_reactivated',value)
                if require_current()!=rolled_back:
                    raise RestoreHandoffError('恢复资格后机器记录变化。')
                verify_qualified_source(value,connection,self.owner)
                physical()
                result=reinstatement_receipt(renewal)
                if os.path.lexists(done_path):
                    if _read(done_path.parent,done_path.name)!=result:
                        raise RestoreHandoffError('资格续接完成凭据变化。')
                else:
                    atomic_create_json(done_path,result)
                    self._fault('after_reinstatement_completion',value)
                scope=load_restore_scope(database,machine_store=self.owner.store)
                if scope is None or not scope.active:
                    raise RestoreHandoffError('回退后未取得原副本活动资格。')
                return {'handoff_id':handoff_id,'rolled_back':True,'source_record':rolled_back,
                    'target_writable':False,'qualification_reinstatement_receipt_sha256':result['receipt_sha256']}
