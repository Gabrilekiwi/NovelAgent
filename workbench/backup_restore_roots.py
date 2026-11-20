"""Proof-bound physical registries for an actual, still-disabled restore copy.

Only mutable root registries change. Original registry bytes are retained in a
separate intent; old manifests, guards and publication evidence stay untouched.
This service neither rebinds SQLite nor grants book ownership or worker access.
"""
import base64
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat

from core.engine.persistence import atomic_create_json, atomic_write_json, _json_bytes
from core.engine.root_registry import (directory_identity, _path_digest, _registry_digest,
    _plain_file_identity, validate_root_registry)
from core.story_project.identity import load_project_identity
from core.story_project.reviewed_identity import load_verified_reviewed_identity
from core.story_project.reviewed_state_document import digest, read_project_bytes
from workbench.backup_archive import BackupArchiveError, _file_hash, _lineage, _member
from workbench.backup_archive_verification import _implementation
from workbench.backup_archive_view import verified_archive_view
from workbench.backup_files import _ordinary
from workbench.backup_history import verify_persistence_history
from workbench.backup_restore_materialization import _json_copy, _validate_plan
from workbench.backup_restore_plan import _plan_archived_restore
from workbench.instance import InstanceLease


class RestoreRootBindingError(BackupArchiveError):
    code = 'restore_root_binding_invalid'


def _read(root, name):
    if _ordinary(root/name).st_size > 32*1024**2:
        raise RestoreRootBindingError('恢复控制资料超过支持的大小。')
    value = json.loads(read_project_bytes(root, name))
    if not isinstance(value, dict):
        raise RestoreRootBindingError('恢复控制资料必须是对象。')
    return value


def _signed(value, field):
    return {**value, field:digest(value)}


def _file_record(raw):
    return {'size':len(raw), 'sha256':digest(raw)}


def _walk_error(error):
    raise error


class _MaterializedCopy:
    def __init__(self, root, plan, view, *, _usage_overlay=None):
        self.root, self.plan, self.view = root, plan, view
        self.usage_overlay = _usage_overlay
        self.layout = plan['root_plan']
        self.binding = _json_copy(_lineage(root))
        self.book = Path(self.layout['book_root'])
        self.tree = view.manifest['preview']['scope']['inventories']['story_project']
        self.source = Path(self.tree['source_root'])
        intent = _signed({'schema_version':'restore_materialization_intent_v1', 'state':'restoring',
            'plan_sha256':plan['plan_sha256'], 'archive_receipt_sha256':plan['archive_receipt_sha256'],
            'project_id':plan['project_id'], 'book_id':plan['book_id'],
            'container':str(root), 'container_binding':self.binding,
            'database':self.layout['database'], 'write_activation_allowed':False}, 'intent_sha256')
        if _read(root, 'restore-intent.json') != intent:
            raise RestoreRootBindingError('恢复意图或新容器物理绑定不一致。')
        self.files = {'workbench.sqlite':view.manifest['members']['workbench.sqlite']}
        self.directories = set()
        for mapping in self.layout['roots']:
            prefix = Path(mapping['destination']).relative_to(root)
            tree = view.manifest['preview']['scope']['inventories'][mapping['root']]
            for name in ['.', *tree['directories']]:
                path = prefix if name == '.' else prefix/name
                while path != Path('.'):
                    self.directories.add(path.as_posix())
                    path = path.parent
            for name in tree['files']:
                self.files[(prefix/name).as_posix()] = {k:tree['files'][name][k] for k in ('size','sha256')}
        for name in ('restore-intent.json','restore-plan.json'):
            self.files[name] = _file_hash(root/name)
        copied = {'files':self.files.copy(), 'directories':sorted(self.directories)}
        receipt = _signed({'schema_version':'restore_materialization_receipt_v1', 'state':'materialized',
            'plan_sha256':plan['plan_sha256'], 'archive_receipt_sha256':plan['archive_receipt_sha256'],
            'intent_sha256':intent['intent_sha256'], 'project_id':plan['project_id'], 'book_id':plan['book_id'],
            'container':str(root), 'container_binding':self.binding, 'copied':copied, 'copied_sha256':digest(copied),
            'historical_bytes_preserved':True, 'database_rebound':False, 'new_physical_controls_installed':False,
            'read_only_app_ready':False, 'restored':False, 'recoverable':False, 'write_activation_allowed':False,
            'ownership_handoff_completed':False, 'start_worker':False, 'provider_calls_allowed':False}, 'receipt_sha256')
        if _read(root, 'restore-materialization.json') != receipt:
            raise RestoreRootBindingError('复制完成凭据与实际归档、计划或控制资料不一致。')
        self.receipt = receipt
        self.files['restore-materialization.json'] = _file_hash(root/'restore-materialization.json')

    def require_binding(self):
        if _json_copy(_lineage(self.root)) != self.binding:
            raise RestoreRootBindingError('恢复容器的物理绑定变化。')

    def verify(self, replacements, extra):
        """Check every original byte except explicitly proved mutable registries."""
        self.require_binding()
        if self.usage_overlay is not None:
            self.usage_overlay.verify(self, replacements, extra)
            self.require_binding()
            return
        actual_files, actual_directories = set(), set()
        allowed = {'workbench.lock','workbench-instance.json', *extra}
        count = 0
        for parent, directories, files in os.walk(self.root, followlinks=False, onerror=_walk_error):
            for name in directories + files:
                count += 1
                if count > 100000:
                    raise RestoreRootBindingError('恢复副本超过路径数量上限。')
                path = Path(parent)/name
                info = _ordinary(path)
                relative = path.relative_to(self.root).as_posix()
                if stat.S_ISDIR(info.st_mode):
                    actual_directories.add(relative)
                elif relative not in allowed:
                    actual_files.add(relative)
        if actual_files != set(self.files) or actual_directories != self.directories:
            raise RestoreRootBindingError('恢复副本的文件或目录范围变化。')
        for name, expected in self.files.items():
            actual = _file_hash(self.root/name)
            if actual != expected and actual != replacements.get(name):
                raise RestoreRootBindingError('恢复资料偏离复制凭据：' + name)
        self.require_binding()


def _registry_plan(copy_, created_at):
    entries = []
    for item in copy_.plan['historical_registries']:
        name = item['path']
        raw = copy_.view.read_root('story_project', name)
        if digest(raw) != item['sha256']:
            raise RestoreRootBindingError('原根登记与恢复计划不一致。')
        original = validate_root_registry(json.loads(raw))
        updated = copy.deepcopy(original)
        bindings = {}
        for spec in item['roots']:
            key = spec['root_id']
            path = Path(spec['destination'])
            if (spec['historical_binding'] != original['roots'][key]
                    or path != copy_.book / Path(spec['source_path']).relative_to(copy_.source)):
                raise RestoreRootBindingError('根登记的来源映射不一致。')
            binding = updated['roots'][key]
            binding.update(path=str(path), path_identity_sha256=_path_digest(path),
                directory_identity=directory_identity(path), updated_at=created_at)
            bindings[str(path)] = _json_copy(_lineage(path))
            fence = binding.get('project_remap_fence')
            if fence is not None:
                # A copied, pre-armed fence must already exist. Do not create an
                # absent fence or transplant the source lock's physical identity.
                fence_path = path/fence['relative_path']
                binding['project_remap_fence'] = {'relative_path':fence['relative_path'],
                    'directory_identity':directory_identity(fence_path),
                    'lock_identity':_plain_file_identity(fence_path/'.persistence.lock')}
                bindings[str(fence_path)] = _json_copy(_lineage(fence_path))
        updated.update(revision=original['revision']+1, updated_at=created_at)
        updated['registry_digest'] = _registry_digest(updated)
        updated = validate_root_registry(updated)
        entries.append({'path':name, 'before':_file_record(raw),
            'original_base64':base64.b64encode(raw).decode('ascii'), 'after':_file_record(_json_bytes(updated)),
            'registry':updated, 'physical_bindings':bindings})
    return entries


def _root_intent(copy_, created_at):
    try:
        instant = datetime.fromisoformat(created_at)
        if instant.utcoffset() != timezone.utc.utcoffset(instant):
            raise ValueError('UTC required')
    except (TypeError, ValueError) as exc:
        raise RestoreRootBindingError('根绑定意图缺少明确时间。') from exc
    return _signed({'schema_version':'restore_root_binding_intent_v1', 'created_at':created_at,
        'project_id':copy_.plan['project_id'], 'book_id':copy_.plan['book_id'],
        'plan_sha256':copy_.plan['plan_sha256'], 'archive_receipt_sha256':copy_.plan['archive_receipt_sha256'],
        'materialization_receipt_sha256':copy_.receipt['receipt_sha256'], 'container_binding':copy_.binding,
        'registries':_registry_plan(copy_, created_at), 'replay_old_plans':False,
        'write_activation_allowed':False}, 'intent_sha256')


def _require_registry_bindings(intent):
    for item in intent['registries']:
        for name, expected in item['physical_bindings'].items():
            if _json_copy(_lineage(Path(name))) != expected:
                raise RestoreRootBindingError('恢复逻辑根的物理身份变化。')
        for binding in item['registry']['roots'].values():
            fence = binding.get('project_remap_fence')
            if fence is not None and _plain_file_identity(Path(binding['path'])/fence['relative_path']/'.persistence.lock') != fence['lock_identity']:
                raise RestoreRootBindingError('恢复运行锁的物理身份变化。')


def _verify_history(copy_, intent):
    originals = {item['path']:base64.b64decode(item['original_base64'], validate=True) for item in intent['registries']}
    def read(source_root, name):
        if Path(source_root) != copy_.source:
            raise RestoreRootBindingError('历史读取越过原作品的证据坐标。')
        _member(name)
        return originals[name] if name in originals else read_project_bytes(copy_.book, name)
    # Map only old coordinate reads. Every real artifact comes from the new
    # copy; immutable PathGuards are never passed to an execution/replay API.
    history = verify_persistence_history(copy_.source, copy_.tree,
        book_id=copy_.plan['book_id'], read_bytes=read,
        provenance=copy_.view.manifest['preview']['scope'].get('restored_provenance'))
    identity = json.loads(read_project_bytes(copy_.book, '.novelagent/project.json'))
    if identity['schema_version'] == '3.0':
        current = load_verified_reviewed_identity(copy_.book)
    else:
        current = {'identity':load_project_identity(copy_.book).to_dict()}
    if current['identity'] != identity:
        raise RestoreRootBindingError('新目录的身份冷读结果与原身份不同。')
    return {'history_sha256':digest(history), 'transaction_count':len(history['transactions']),
        'current':current, 'current_sha256':digest(current)}


def _root_completion(copy_, intent, verified):
    return _signed({'schema_version':'restore_root_binding_receipt_v1',
        'intent_sha256':intent['intent_sha256'], 'plan_sha256':copy_.plan['plan_sha256'],
        'materialization_receipt_sha256':copy_.receipt['receipt_sha256'],
        'registry_count':len(intent['registries']), 'verified':verified,
        'new_v2_physical_registries_bound':True, 'old_guards_replay_allowed':False,
        'database_rebound':False, 'read_only_app_ready':False, 'ownership_handoff_completed':False,
        'restored':False, 'write_activation_allowed':False, 'recoverable':False}, 'receipt_sha256')


def _verify_bound_copy(copy_, *, database_record=None, extra_files=()):
    """Recheck a root-bound copy; a DB caller must first prove its SQL delta."""
    loaded = _read(copy_.root, 'root-binding.json')
    intent = _root_intent(copy_, loaded.get('created_at'))
    if loaded != intent:
        raise RestoreRootBindingError('原根绑定意图或新物理根发生变化。')
    replacements = {(Path('book')/item['path']).as_posix():item['after'] for item in intent['registries']}
    if database_record is not None:
        replacements['workbench.sqlite'] = database_record
    copy_.verify(replacements, {'root-binding.json','root-binding-done.json', *extra_files})
    _require_registry_bindings(intent)
    verified = _verify_history(copy_, intent)
    receipt = _root_completion(copy_, intent, verified)
    if _read(copy_.root, 'root-binding-done.json') != receipt:
        raise RestoreRootBindingError('根绑定完成凭据与当前副本不一致。')
    return receipt


def bind_restored_roots(package, *, destination, temporary_root=None):
    """Explicit, repeatable physical binding while normal DB startup is blocked."""
    root = Path(destination).absolute()
    _lineage(root)
    saved = _validate_plan(_read(root, 'restore-plan.json'))
    if str(root) != saved['root_plan']['container']:
        raise RestoreRootBindingError('恢复计划属于其他容器。')
    fresh = _plan_archived_restore(package, project_id=saved['project_id'], book_id=saved['book_id'],
        destination=root, temporary_root=temporary_root, _existing=True)
    if _json_copy(fresh) != saved:
        raise RestoreRootBindingError('原归档、实现版本或恢复位置已变化。')
    lease = InstanceLease(root).acquire()
    try:
        with verified_archive_view(package) as view:
            if (view.verified['receipt_sha256'] != saved['archive_receipt_sha256']
                    or _file_hash(Path(package).absolute()/'archive.zip') != saved['archive_binding']['archive']):
                raise RestoreRootBindingError('根绑定前归档变化。')
            copy_ = _MaterializedCopy(root, saved, view)
            intent_path = root/'root-binding.json'
            existed = os.path.lexists(intent_path)
            if existed:
                loaded = _read(root, intent_path.name)
                intent = _root_intent(copy_, loaded.get('created_at'))
                if loaded != intent:
                    raise RestoreRootBindingError('原根绑定意图或新物理根发生变化。')
            else:
                intent = _root_intent(copy_, datetime.now(timezone.utc).isoformat())
            replacements = {(Path('book')/item['path']).as_posix():item['after'] for item in intent['registries']}
            extras = {'root-binding.json','root-binding-done.json'} if existed else set()
            copy_.verify(replacements if existed else {}, extras)
            if not existed:
                atomic_create_json(intent_path, intent)
            if _read(root, intent_path.name) != intent:
                raise RestoreRootBindingError('根绑定意图发布后变化。')
            for item in intent['registries']:
                copy_.require_binding()
                _require_registry_bindings(intent)
                path = copy_.book/item['path']
                actual = _file_hash(path)
                if actual == item['before']:
                    atomic_write_json(path, item['registry'])
                elif actual != item['after']:
                    raise RestoreRootBindingError('待绑定根登记既非原版本也非本次计划版本。')
                if _file_hash(path) != item['after']:
                    raise RestoreRootBindingError('新物理根登记未通过写后核对。')
            verified = _verify_history(copy_, intent)
            copy_.verify(replacements, {'root-binding.json','root-binding-done.json'})
            _require_registry_bindings(intent)
        # Publish completion only after the archive view's final verification.
        if _implementation()['fingerprint_sha256'] != saved['verifier']['implementation_sha256']:
            raise RestoreRootBindingError('根绑定期间实现版本变化。')
        result = _root_completion(copy_, intent, verified)
        done = root/'root-binding-done.json'
        if os.path.lexists(done):
            if _read(root, done.name) != result:
                raise RestoreRootBindingError('已存在的根绑定完成凭据不一致。')
        else:
            atomic_create_json(done, result)
        if _read(root, done.name) != result or _read(root, intent_path.name) != intent:
            raise RestoreRootBindingError('根绑定凭据发布后变化。')
        copy_.verify(replacements, {'root-binding.json','root-binding-done.json'})
        _require_registry_bindings(intent)
        return result
    finally:
        lease.close()
