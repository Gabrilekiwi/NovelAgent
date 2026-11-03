"""Preserve actual post-archive spending after durable source revocation.

This evidence lives with the machine handoff, outside the restored book. It is
never a budget reset or write authorization. A missing source remains unknown;
completed evidence is reconstructed from immutable bytes without that source.
"""
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path, PurePosixPath

from core.engine.persistence import (atomic_create_json, _atomic_create_from_bytes,
    held_persistence_lock_readers, persistence_run_lock, persistence_state_lock)
from core.engine.safe_paths import RootBinding, SafePathResolver
from core.story_project.reviewed_state_document import digest
from workbench.backup_archive import _member
from workbench.backup_archive_usage import _StoryEvidence, _usage
from workbench.backup_auxiliary import Evidence
from workbench.backup_files import inventory_tree, _stream_member, _ordinary
from workbench.backup_restore_roots import _read
from workbench.book_ownership import _record, _require_witness, _lineage, _file_identity, _sha256
from workbench.database import WorkbenchDatabase
from workbench.restore_handoff import RestoreHandoff, RestoreHandoffError, _database_fence, _signed
from workbench.writer_identity import load_verified_writer_identity


_ROOTS = ('.novelagent/runtime/budgets', '.novelagent/runtime/runs/executions',
    '.novelagent/runtime/model_calls')
_IDENTITY = '.novelagent/project.json'
_WITNESS = '.novelagent/write-registration.json'
_MAX_FILES = 10000
_MAX_BYTES = 256 * 1024**2
_MAX_FILE_BYTES = 32 * 1024**2


class _BoundedBytes(io.BytesIO):
    def __init__(self, limit):
        super().__init__()
        self.limit = limit

    def write(self, content):
        if self.tell()+len(content) > self.limit:
            raise RestoreHandoffError('用量文件读取超过保存清单大小。')
        return super().write(content)


def _resolver(root):
    return SafePathResolver({'external:backup_source':RootBinding('external:backup_source',
        '00000000-0000-4000-8000-000000000000', root)})


def _included(name):
    return name in (_IDENTITY, _WITNESS) or any(name == root or name.startswith(root+'/') for root in _ROOTS)


def _financial_tree(root):
    # The enclosing machine, Provider and persistence locks exclude supported
    # writers; the second full inventory also detects changes outside this view.
    original = inventory_tree(root, locked_readers=held_persistence_lock_readers())
    if any(_included(item['path']) for item in original['excluded']):
        raise RestoreHandoffError('用量范围存在未复制文件，不能确认消费记录完整。')
    files = {name:record for name, record in original['files'].items() if _included(name)}
    directories = {name for name in original['directories'] if _included(name)}
    for name in (*files, *directories):
        directories.update(str(parent) for parent in PurePosixPath(name).parents if str(parent) != '.')
    tree = {'source_root':str(root), 'files':files, 'directories':sorted(directories),
        'source_inventory_sha256':original['inventory_sha256']}
    _validate_tree(tree)
    return tree


def _validate_tree(tree):
    if (not isinstance(tree, dict) or set(tree) != {'source_root','files','directories','source_inventory_sha256'}
            or not isinstance(tree['files'], dict) or not isinstance(tree['directories'], list)
            or not _sha256(tree['source_inventory_sha256'])):
        raise RestoreHandoffError('用量保存清单无效。')
    files, directories = tree['files'], tree['directories']
    names = [*files, *directories]
    if (len(names) > _MAX_FILES or any(not isinstance(name, str) for name in names)
            or len({name.casefold() for name in names}) != len(names)
            or any(name not in files for name in (_IDENTITY, _WITNESS))):
        raise RestoreHandoffError('用量保存清单缺少身份、路径重复或超过支持数量。')
    total = 0
    parents = {str(parent) for name in names for parent in PurePosixPath(name).parents if str(parent) != '.'}
    if not parents.issubset(directories):
        raise RestoreHandoffError('用量保存清单目录不完整。')
    for name in names:
        _member(name)
        if not _included(name) and name not in parents:
            raise RestoreHandoffError('用量保存清单包含范围外的文件。')
    for name, record in files.items():
        if (not _included(name) or not isinstance(record, dict)
                or set(record) != {'size','sha256','source_identity'}
                or type(record['size']) is not int or not 0 <= record['size'] <= _MAX_FILE_BYTES
                or not _sha256(record['sha256'])):
            raise RestoreHandoffError('用量保存文件记录无效或超过支持大小。')
        total += record['size']
    if total > _MAX_BYTES:
        raise RestoreHandoffError('用量保存范围超过支持容量。')


class _SavedEvidence(_StoryEvidence):
    """Reuse the actual offline verifier, with a separate byte-store contract."""
    def __init__(self, directory, tree):
        self.directory = directory
        Evidence.__init__(self, tree['source_root'], tree, _read_bytes=self.raw)

    def raw(self, root, name):
        relative = self.relative(Path(root)/name)
        record = self.tree['files'].get(relative)
        if record is None:
            raise RestoreHandoffError('已保存的用量文件缺失。')
        path = self.directory/('usage-'+record['sha256']+'.bin')
        if _ordinary(path).st_size != record['size']:
            raise RestoreHandoffError('已保存的用量文件大小变化。')
        output = _BoundedBytes(record['size'])
        _stream_member(_resolver(self.directory), path.name, output=output)
        raw = output.getvalue()
        if len(raw) != record['size'] or digest(raw) != record['sha256']:
            raise RestoreHandoffError('已保存的用量文件内容变化。')
        return raw


class RestoreUsage(RestoreHandoff):
    def _pending(self, handoff_id):
        value = self._load(handoff_id)
        if (_record(self.owner.directory, self.owner.book_id) != value['restoring_record']
                or os.path.lexists(self._path(handoff_id, 'rollback'))):
            raise RestoreHandoffError('用量保存需要尚未回退的原恢复交接。')
        expected = _signed({'schema_version':'machine_restore_revocation_v1','handoff_id':handoff_id,
            'intent_sha256':value['intent_sha256'],'record_sha256':value['restoring_record']['record_sha256'],
            'source_revoked':True,'target_writable':False,'handoff_completed':False}, 'receipt_sha256')
        if _read(self.owner.directory, self._path(handoff_id, 'revoked').name) != expected:
            raise RestoreHandoffError('保存用量前必须完成原副本资格撤销。')
        return value

    @contextmanager
    def _source(self, value):
        source = value['source_record']; old = source['binding']; root = Path(old['root'])
        path = Path(old['database'])
        def physical():
            if (_lineage(root) != old['root_lineage'] or _lineage(path.parent) != old['database_parent_lineage']
                    or _file_identity(path) != old['database_file_identity']):
                raise RestoreHandoffError('原作品或数据库物理身份变化，不能保存为原消费证据。')
        physical()  # Never recreate a missing source or its Provider lock.
        with persistence_state_lock((root,)), persistence_run_lock(
                root/'.novelagent/runtime/runs/.provider-boundary-lock', require_existing_lock=True):
            with _database_fence(path) as connection:
                from workbench.book_ownership import OWNERSHIP_V3
                from workbench.restore_handoff import verify_qualified_source
                if source['version']==OWNERSHIP_V3:
                    verify_qualified_source(value,connection,self.owner)
                else:
                    database = WorkbenchDatabase(path)
                    if (database.restore_scope is not None or self.owner._binding(root=root, database=database,
                            project_id=old['project_id'], connection=connection) != old):
                        raise RestoreHandoffError('原用量来源与实际数据库登记不同。')
                load_verified_writer_identity(root)
                _require_witness(root, self.owner.book_id, source)
                yield root
                physical()
                _require_witness(root, self.owner.book_id, source)
                if source['version']==OWNERSHIP_V3:
                    verify_qualified_source(value,connection,self.owner)
                if self._pending(value['handoff_id']) != value:
                    raise RestoreHandoffError('保存用量期间交接变化。')

    def _binding_fields(self, value):
        return {'handoff_id':value['handoff_id'], 'handoff_intent_sha256':value['intent_sha256'],
            'restoring_record_sha256':value['restoring_record']['record_sha256']}

    def _unknown_result(self, value):
        return _signed({'schema_version':'restore_usage_unknown_v1', **self._binding_fields(value),
            'usage_state':'unknown','usage':None,'post_archive_usage_verified':False,
            'blockers':[{'code':'post_archive_source_unavailable','reference':'source'}],
            'budget_reconciliation_completed':False,
            'write_activation_allowed':False}, 'receipt_sha256')

    def _unknown(self, value):
        result = self._unknown_result(value)
        path = self._path(value['handoff_id'], 'usage-unknown')
        if os.path.lexists(path):
            if _read(path.parent, path.name) != result:
                raise RestoreHandoffError('原消费未知状态凭据变化。')
        else:
            atomic_create_json(path, result)
        return result

    def _usage_intent(self, value, tree):
        return _signed({'schema_version':'restore_usage_intent_v1', **self._binding_fields(value),
            'tree':tree}, 'intent_sha256')

    def _load_intent(self, value):
        path = self._path(value['handoff_id'], 'usage')
        intent = _read(path.parent, path.name)
        tree = intent.get('tree')
        _validate_tree(tree)
        if intent != self._usage_intent(value, tree) or tree['source_root'] != value['source_record']['binding']['root']:
            raise RestoreHandoffError('用量保存意图与恢复交接不同。')
        return intent

    def _projection(self, value, intent):
        evidence = _SavedEvidence(self.owner.directory, intent['tree'])
        # Validate every preserved byte, including provenance not projected by
        # the budget reader. Summary documents cannot replace missing evidence.
        for name in evidence.tree['files']:
            evidence.raw(evidence.root, name)
        identity = evidence.raw(evidence.root, _IDENTITY)
        witness = evidence.document(evidence.root, _WITNESS)
        if (digest(identity) != value['source_record']['binding']['identity_sha256']
                or witness.get('witness_sha256') != value['source_record']['witness_sha256']
                or witness != _signed(witness, 'witness_sha256')):
            raise RestoreHandoffError('用量保存作品身份或登记凭证不同。')
        blockers = []
        def block(code, reference):
            item = {'code':code,'reference':reference}
            if item not in blockers: blockers.append(item)
        # Budget reports contain tuple keys in unresolved-attempt lists. Use the
        # durable JSON shape for in-process and fresh-process equality alike.
        usage = json.loads(json.dumps(_usage(evidence, self.owner.book_id, block), ensure_ascii=False))
        return _signed({'schema_version':'restore_usage_preserved_v1', **self._binding_fields(value),
            'usage_intent_sha256':intent['intent_sha256'], 'usage_state':'preserved', 'usage':usage,
            'post_archive_usage_verified':True, 'settled':not blockers, 'blockers':blockers,
            'budget_reconciliation_completed':False,
            'write_activation_allowed':False}, 'receipt_sha256')

    def _copy(self, value, intent, root):
        tree = intent['tree']
        resolver = _resolver(root)
        for name, record in sorted(tree['files'].items()):
            buffer = _BoundedBytes(record['size'])
            if _stream_member(resolver, name, output=buffer,
                    locked_readers=held_persistence_lock_readers()) != record:
                raise RestoreHandoffError('原消费证据在保存期间变化。')
            raw = buffer.getvalue(); path = self.owner.directory/('usage-'+record['sha256']+'.bin')
            self.owner._held()
            if os.path.lexists(path):
                _SavedEvidence(self.owner.directory, tree).raw(root, name)
            else:
                _atomic_create_from_bytes(path, raw)
                self._fault('after_usage_blob', value)
        if _financial_tree(root) != tree:
            raise RestoreHandoffError('保存后原消费来源变化，未签发完成凭据。')
        result = self._projection(value, intent)
        self._fault('before_usage_completion', value)
        if self._pending(value['handoff_id']) != value or self._load_intent(value) != intent:
            raise RestoreHandoffError('完成用量保存前交接或意图变化。')
        if _financial_tree(root) != tree or self._projection(value, intent) != result:
            raise RestoreHandoffError('完成用量保存前原始证据变化。')
        atomic_create_json(self._path(value['handoff_id'], 'usage-done'), result)
        self._fault('after_usage_completion', value)
        return result

    def capture(self, *, handoff_id):
        with self.owner.locked():
            value = self._pending(handoff_id)
            if os.path.lexists(self._path(handoff_id, 'usage-done')):
                return self._read_completed(value)
            if os.path.lexists(self._path(handoff_id, 'usage')):
                raise RestoreHandoffError('原用量保存意图已存在，请显式续接。')
            old = value['source_record']['binding']
            if not os.path.lexists(old['root']) or not os.path.lexists(old['database']):
                return self._unknown(value)
            with self._source(value) as root:
                intent = self._usage_intent(value, _financial_tree(root))
                atomic_create_json(self._path(handoff_id, 'usage'), intent)
                self._fault('after_usage_intent', value)
                return self._copy(value, intent, root)

    def resume_usage(self, *, handoff_id):
        with self.owner.locked():
            value = self._pending(handoff_id)
            if os.path.lexists(self._path(handoff_id, 'usage-done')):
                return self._read_completed(value)
            intent = self._load_intent(value)
            with self._source(value) as root:
                if _financial_tree(root) != intent['tree']:
                    raise RestoreHandoffError('原消费证据已偏离保存意图，拒绝重新取样覆盖。')
                return self._copy(value, intent, root)

    def _read_completed(self, value):
        intent = self._load_intent(value)
        result = self._projection(value, intent)
        if _read(self.owner.directory, self._path(value['handoff_id'], 'usage-done').name) != result:
            raise RestoreHandoffError('用量保存完成凭据无法由原始证据重建。')
        return result

    def read_usage(self, *, handoff_id):
        with self.owner.locked():
            value = self._pending(handoff_id)
            if (not os.path.lexists(self._path(handoff_id, 'usage'))
                    and not os.path.lexists(self._path(handoff_id, 'usage-done'))):
                expected = self._unknown_result(value)
                path = self._path(handoff_id, 'usage-unknown')
                if _read(path.parent, path.name) != expected:
                    raise RestoreHandoffError('消费未知状态凭据变化。')
                return expected
            return self._read_completed(value)
