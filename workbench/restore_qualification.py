"""Stable, mandatory write qualification for an activated restore.

The original database scope stays intact. One completion receipt activates its
effective state only when the independent machine record matches exactly.
"""
from contextlib import contextmanager
import os
from pathlib import Path, PurePosixPath

from workbench.backup_restore_roots import _read
from workbench.restore_handoff import _signed

ACTIVATION_FILE = 'write-qualification.json'
ACTIVATION_TEMP = '.write-qualification.tmp'


def machine_store_for(book_id, explicit=None):
    from workbench import book_ownership as owner
    if explicit is not None: return Path(explicit).absolute()
    selected = {store for known,store in owner._machine_scopes.get() if known==book_id}
    if len(selected)>1: raise owner.BookOwnershipError('conflicting restore machine stores')
    return next(iter(selected)) if selected else owner.default_machine_store()


def activation_receipt(intent):
    return _signed({'schema_version':'restored_write_activation_receipt_v1',
        'activation_intent_sha256':intent['intent_sha256'],
        'qualification_sha256':intent['qualification']['qualification_sha256'],
        'active_record_sha256':intent['active_record']['record_sha256'],
        'handoff_id':intent['qualification']['handoff_id'],
        'target_writable':True,'automatic_provider_calls_allowed':False},'receipt_sha256')


def archived_recovery_history(copy_):
    """Freeze already verified terminal journals and their terminal registration."""
    import json
    from workbench.restore_handoff import RestoreHandoffError
    history = {}
    for name in copy_.tree['files']:
        path = PurePosixPath(name)
        if path.name != 'manifest.json' or path.parent.parent.name != 'journals':
            continue
        manifest = json.loads(copy_.view.read_root('story_project', name))
        if manifest.get('schema_version') not in {'2.0','2.1'}:
            continue
        state = manifest['state']; run = manifest['immutable']['run_id']
        if state not in {'completed','rolled_back'} or path.parent.name != run:
            raise RestoreHandoffError('归档事务未完整结束，不能作为只读恢复历史。')
        transaction_root = path.parent.parent.parent
        registration = transaction_root/'registry'/state/(run+'.json')
        if str(registration) not in copy_.tree['files']:
            raise RestoreHandoffError('归档终态事务缺少原终态登记。')
        def record(relative):
            return {k:copy_.tree['files'][str(relative)][k] for k in ('size','sha256')}
        history.setdefault(str(transaction_root),{})[run] = {
            'manifest':record(path),'state':state,'registration':record(registration)}
    return history


def restored_recovery_history(transaction_root):
    """Validate frozen history before recovery discovers or mutates new work."""
    from workbench import book_ownership as owner
    from workbench.restore_scope import load_restore_scope, RestoreScopeError
    from workbench.backup_files import _ordinary
    transaction_root = Path(transaction_root).absolute()
    for root, book_id in owner._output_books((transaction_root,)).items():
        container = root.parent
        if not any(os.path.lexists(container/name) for name in
                ('restore-intent.json','execution-policy.json','workbench.sqlite.restore-scope.json')):
            continue
        scope = load_restore_scope(container/'workbench.sqlite')
        if scope is None or not scope.active or scope.book_root != root:
            raise RestoreScopeError('恢复副本尚未启用，不能扫描事务恢复。')
        history = scope.activation.get('archived_recovery_history')
        if not isinstance(history,dict):
            raise RestoreScopeError('恢复资格缺少原事务历史绑定。')
        retained = history.get(transaction_root.relative_to(root).as_posix(),{})
        for run, item in retained.items():
            if '/' in run or '\\' in run or run in {'.','..'}:
                raise RestoreScopeError('恢复事务标识无效。')
            expected = [(transaction_root/'journals'/run/'manifest.json',item['manifest']),
                (transaction_root/'registry'/item['state']/(run+'.json'),item['registration'])]
            for path, record in expected:
                try:
                    valid = (_ordinary(path).st_size == record['size']
                        and owner.digest(owner.read_project_bytes(root,path.relative_to(root).as_posix())) == record['sha256'])
                except (OSError,ValueError,RuntimeError) as exc:
                    raise RestoreScopeError('原恢复事务历史或终态登记缺失，禁止自动重建。') from exc
                if not valid:
                    raise RestoreScopeError('原恢复事务历史或终态登记变化，禁止自动重建。')
            if os.path.lexists(transaction_root/'staging'/run) or any(
                    os.path.lexists(transaction_root/'registry'/state/(run+'.json'))
                    for state in ('pending','completed','rolled_back','recovery_required') if state != item['state']):
                raise RestoreScopeError('原只读事务出现新的暂存或登记，禁止重新执行。')
        return retained
    return {}


def require_no_lost_activation(connection, database, *, machine_store=None):
    """Use the independent machine record when local restore markers are lost.

    Do not load a qualified owner recursively or grant any write authority here.
    A source workbench with a different registered root remains distinguishable
    from the target database and from a copy still pointing at the target root.
    """
    from workbench import book_ownership as owner
    from workbench.restore_scope import RestoreScopeError
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='projects'").fetchone()
    if exists is None:
        return
    for book_id, root in connection.execute('SELECT book_id,root FROM projects'):
        store = machine_store_for(book_id, machine_store)
        directory = store/owner.digest(book_id.encode('utf-8'))
        record = owner._record(directory, book_id)
        if record is not None and record['version'] == owner.OWNERSHIP_V3:
            binding = record['binding']
            if binding['database'] == str(database) or binding['root'] == root:
                raise RestoreScopeError('机器记录表明此数据库属于恢复副本，但本地恢复范围已丢失，拒绝普通启动。')
        # Revoking a qualified source changes the current record to a target's
        # V2 restoring record. Durable handoff history still identifies both
        # restore copies even if every local marker and history table is lost.
        from workbench.restore_handoff import validate_handoff_intent
        paths=sorted(directory.glob('handoff-*-intent.json'))
        if len(paths)>512 or sum(p.stat().st_size for p in paths)>32*1024**2:
            raise RestoreScopeError('机器交接历史超过支持大小，不能判断普通启动。')
        for path in paths:
            handoff=_read(directory,path.name)
            candidates=[handoff.get('restoring_record')]
            source=handoff.get('source_record')
            if isinstance(source,dict) and source.get('version')==owner.OWNERSHIP_V3:
                candidates.append(source)
            for candidate in candidates:
                binding=candidate.get('binding') if isinstance(candidate,dict) else None
                if isinstance(binding,dict) and (binding.get('database')==str(database) or binding.get('root')==root):
                    validate_handoff_intent(handoff,book_id)
                    raise RestoreScopeError('原机器交接证明此数据库属于恢复副本，但本地恢复范围已丢失。')


def load_activation(scope, *, machine_store=None):
    from workbench import book_ownership as owner
    from workbench.restore_scope import RestoreScopeError
    from workbench.restore_executions import execution_receipt
    book_id=scope['book_id']; root=Path(scope['book_root']); container=Path(scope['database']).parent
    store=machine_store_for(book_id,machine_store)
    directory=store/owner.digest(book_id.encode('utf-8'))
    with owner._machine_gate(book_id,store,wait_timeout=2.0):
        record=owner._record(directory,book_id)
        qualified=record is not None and record['version']==owner.OWNERSHIP_V3
        path=container/ACTIVATION_FILE
        if not os.path.lexists(path):
            bound_here=qualified and (record['binding']['root']==str(root)
                or record['binding']['database']==scope['database'])
            if bound_here: raise RestoreScopeError('活动恢复副本的写入资格凭据丢失。')
            return None
        value=_read(container,ACTIVATION_FILE)
        if (value != _signed({k:v for k,v in value.items() if k!='qualification_sha256'},'qualification_sha256')
                or value['schema_version']!='restored_write_qualification_v1'
                or value['scope_sha256']!=scope['record_sha256'] or value['book_id']!=book_id
                or value['book_root']!=str(root) or value['database']!=scope['database']):
            raise RestoreScopeError('恢复写入资格与实际目标范围不同。')
        prefix='handoff-'+value['handoff_id']
        intent=_read(directory,prefix+'-activation.json')
        if (intent!=_signed({k:v for k,v in intent.items() if k!='intent_sha256'},'intent_sha256')
                or intent['qualification']!=value):
            raise RestoreScopeError('本地恢复资格与机器启用意图不同。')
        execution_intent=_read(directory,prefix+'-executions.json')
        execution_done=_read(directory,prefix+'-executions-done.json')
        if (execution_intent!=_signed({'schema_version':'restore_execution_preparation_v1',
                'policy':execution_intent['policy']},'intent_sha256')
                or execution_done!=execution_receipt(execution_intent)
                or execution_done['receipt_sha256']!=value['execution_receipt_sha256']
                or _read(container,'execution-policy.json')!=execution_intent['policy']
                or execution_intent['policy']['policy_sha256']!=value['execution_policy_sha256']):
            raise RestoreScopeError('恢复后的执行隔离或新任务目录资格变化。')
        for suffix,field in (('jobs-done','jobs_receipt_sha256'),('budget-done','budget_receipt_sha256')):
            receipt=_read(directory,prefix+'-'+suffix+'.json')
            if (receipt!=_signed({k:v for k,v in receipt.items() if k!='receipt_sha256'},'receipt_sha256')
                    or receipt['receipt_sha256']!=value[field]):
                raise RestoreScopeError('恢复启用所依赖的任务或预算完成凭据变化。')
        done=directory/(prefix+'-activation-done.json')
        if not os.path.lexists(done): return None
        if (not qualified or record['state']!='active'
                or record['restore_qualification_sha256']!=value['qualification_sha256']
                or _read(directory,done.name)!=activation_receipt(intent)):
            raise RestoreScopeError('恢复活动资格、机器登记或完成凭据不一致。')
        from workbench.restore_reinstatement import load_reinstatements
        load_reinstatements(directory,intent['active_record'],value,record)
        # Basic physical binding checks are independent of recursive scope loading.
        binding=record['binding']; owner._require_witness(root,book_id,record)
        if (binding['root']!=str(root) or binding['database']!=scope['database']
                or binding['root_lineage']!=owner._lineage(root)
                or binding['database_file_identity']!=owner._file_identity(Path(scope['database']))
                or binding['identity_sha256']!=owner.digest(owner.read_project_bytes(root,'.novelagent/project.json'))):
            raise RestoreScopeError('恢复活动资格的物理作品或数据库不同。')
        return value


def require_qualified_record(record, *, machine_store=None):
    from workbench.book_ownership import OWNERSHIP_V3, BookOwnershipError
    if record['version']!=OWNERSHIP_V3: return
    from workbench.restore_scope import load_restore_scope
    scope=load_restore_scope(record['binding']['database'],machine_store=machine_store)
    if scope is None or not scope.active or scope.activation['qualification_sha256']!=record['restore_qualification_sha256']:
        raise BookOwnershipError('restored write activation is incomplete or missing')


@contextmanager
def restored_database_write_lock(scope):
    from workbench import book_ownership as owner
    store=machine_store_for(scope.document['book_id'],scope.machine_store)
    with owner._machine_gate(scope.document['book_id'],store):
        scope.require_unchanged(Path(scope.document['database']))
        directory=store/owner.digest(scope.document['book_id'].encode('utf-8'))
        owner._require_output_owner(scope.book_root,scope.document['book_id'],owner._record(directory,scope.document['book_id']))
        yield
        scope.require_unchanged(Path(scope.document['database']))
