"""Own a target book's known write boundaries across DB and file capture.

This is the archive source boundary. Certification still belongs to the final
archive/history verifier, not the presence of locks or a successful file copy.
"""
from contextlib import ExitStack,contextmanager
import json
import os
from pathlib import Path

from core.engine.persistence import persistence_run_lock,persistence_state_lock,held_persistence_lock_readers,_borrow_held_run_locks
from core.engine.provider_safety import provider_execution_lock
from core.engine.safe_paths import assert_safe_local_tree
from core.story_project.reviewed_state_document import digest,read_project_bytes
from workbench.backup_database import owned_database_snapshot
from workbench.backup_files import inventory_tree
from workbench.backup_scope import inspect_backup_scope,BackupScopeError
from workbench.book_ownership import BookOwnership, _record, _require_witness
from workbench.backup_restore_provenance import capture_restore_provenance


class BackupSourceError(BackupScopeError):
    code='backup_source_invalid'


def _registration(projects,project_id,connection):
    row=connection.execute('SELECT id,book_id,root FROM projects WHERE id=?',(project_id,)).fetchone()
    if row is None:raise BackupSourceError('备份作品登记已丢失。')
    root=projects.validate_project_root(assert_safe_local_tree(Path(row['root'])))
    return {'project_id':row['id'],'book_id':row['book_id'],'root':str(root)}


def _registry_roots(tree):
    return {str(Path(relative).parent) for relative in tree['files']
            if Path(relative).name=='root_registry.json'}


@contextmanager
def locked_backup_source(projects,project_id,*,maintenance,operation_id,task_runtime_root,machine_store=None,
                         snapshot_permit=None,_on_verified_source=None):
    """Yield (snapshot, scope, lock readers) until every source read is done.

    Acquire the auxiliary book state lock before creating runtime lock roots;
    a migration may already own a book whose runtime does not exist yet.
    Keep machine -> Provider -> DB order used by publication, then EA global
    -> project dependency fence -> local persistence roots used by remapping.
    SQLite and OS file locks prevent independent supported writers from entering
    their original boundaries. Unknown evidence/roots remain preflight blockers.
    """
    context=(maintenance.capture(operation_id) if snapshot_permit is None
             else maintenance.borrow_capture(snapshot_permit,operation_id))
    with context as permit:
        with projects.database.connection() as connection:
            initial=_registration(projects,project_id,connection)
        root=Path(initial['root']);runtime=assert_safe_local_tree(root/'.novelagent/runtime')
        with BookOwnership(initial['book_id'],machine_store=machine_store).locked() as owner, persistence_state_lock((root,)):
            with provider_execution_lock(runtime/'runs'):
                with owned_database_snapshot(projects.database,snapshot_permit=permit) as snapshot:
                    current=_registration(projects,project_id,snapshot.connection)
                    if current!=initial:raise BackupSourceError('取得写锁前作品登记发生变化。')
                    identity=json.loads(read_project_bytes(root,'.novelagent/project.json'))
                    if not isinstance(identity,dict):raise BackupSourceError('作品身份不可读。')
                    owner_args=dict(root=root,database=projects.database,project_id=project_id,connection=snapshot.connection)
                    original_record=_record(owner.directory,initial['book_id'])
                    original_witness=_require_witness(root,initial['book_id'],original_record)
                    ownership=(owner.require(**owner_args)
                        if identity.get('schema_version')=='3.0' or original_record is not None else None)
                    provenance=capture_restore_provenance(projects.database,ownership,machine_store=machine_store)
                    before=inventory_tree(root,locked_readers=held_persistence_lock_readers())
                    registry_roots=_registry_roots(before)
                    with ExitStack() as locks:
                        ea=runtime/'ea'
                        locks.enter_context(persistence_run_lock(ea,require_existing_lock=os.path.lexists(ea)))
                        locks.enter_context(persistence_run_lock(runtime/'.root-remap-fence'))
                        local={root/name for name in registry_roots}
                        local.update(path for path in (runtime/'runs',runtime/'persistence',runtime/'reviewed/p') if path.is_dir())
                        identity_lock=root/'.novelagent/authority/cas'
                        local.add(identity_lock)
                        local.discard(ea)
                        for path in sorted(local,key=lambda p:os.path.normcase(str(p))):
                            # Existing registered control roots must already have
                            # their original armed identity; never silently repair.
                            state_paths=()
                            if path==identity_lock:state_paths=(root/'.novelagent/project.json',)
                            elif path==runtime/'runs':state_paths=(runtime/'snapshot.json',
                                runtime/'memory/notion_memory.json',runtime/'notion_memory.json',runtime/'chapters')
                            locks.enter_context(persistence_run_lock(path,
                                state_paths=state_paths,
                                require_existing_lock=str(path.relative_to(root)) in registry_roots))
                        if json.loads(read_project_bytes(root,'.novelagent/project.json'))!=identity:
                            raise BackupSourceError('取得身份写锁期间作品身份发生变化。')
                        readers=held_persistence_lock_readers()
                        scope=inspect_backup_scope(projects,project_id,connection=snapshot.connection,
                            task_runtime_root=task_runtime_root,locked_readers=readers,restored_provenance=provenance)
                        tree=scope['inventories'].get('story_project')
                        if tree is None or _registry_roots(tree)!=registry_roots:
                            raise BackupSourceError('取得写锁期间持久化登记范围发生变化。')
                        scope['machine_ownership']={'state':'verified' if ownership is not None else 'legacy_not_enrolled',
                            'record':ownership,'restore_handoff_required':True}
                        if provenance is not None:scope['restored_provenance']=provenance
                        scope['scope_sha256']=digest({k:v for k,v in scope.items() if k!='scope_sha256'})
                        yield snapshot,scope,readers,permit
                        permit.require();snapshot.require(projects.database,permit)
                        for inventory in scope['inventories'].values():
                            if inventory_tree(inventory['source_root'],locked_readers=readers)!=inventory:
                                raise BackupSourceError('快照期间作品或外部任务来源发生变化。')
                        if ownership is not None and owner.require(**owner_args)!=ownership:
                            raise BackupSourceError('快照期间作品所有权发生变化。')
                        if (_record(owner.directory,initial['book_id'])!=original_record
                                or _require_witness(root,initial['book_id'],original_record)!=original_witness):
                            raise BackupSourceError('快照期间机器登记或作品凭证发生变化。')
                        if capture_restore_provenance(projects.database,ownership,machine_store=machine_store)!=provenance:
                            raise BackupSourceError('快照期间恢复来源证据发生变化。')
                        # Product transitions may write only AFTER the complete
                        # unchanged-source proof, while every original fence is
                        # still held. Ordinary archives have no final action.
                        # Never expose this Python callback as request data.
                        if _on_verified_source is not None:
                            with _borrow_held_run_locks():
                                _on_verified_source(snapshot,scope,owner,permit)
                            snapshot.require(projects.database,permit)
