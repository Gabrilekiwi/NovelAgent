"""Materialize a verified archive in a new, startup-blocked container.

This preserves original bytes, including the whole database and historical
physical guards. It neither rebinds those guards nor grants write ownership.
Partial output is retained behind restore-intent.json. Explicit resume verifies
the same physical container, original plan and every retained byte first.
"""
import json
import os
from pathlib import Path
import stat

from core.engine.persistence import atomic_create_json, persistence_state_lock
from core.story_project.reviewed_state_document import digest, read_project_bytes
from workbench.backup_archive import BackupArchiveError, _file_hash, _lineage, _member
from workbench.backup_archive_verification import _implementation
from workbench.backup_archive_view import verified_archive_view
from workbench.backup_files import _identity, _ordinary, inventory_tree
from workbench.backup_restore_plan import _plan_archived_restore


class RestoreMaterializationError(BackupArchiveError):
    code = 'restore_materialization_invalid'


def _json_copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


class _NewContainer:
    """Exclusive writes to a bound directory; never delete partial evidence."""

    def __init__(self, layout, *, resume=False):
        self.root = Path(layout['container'])
        parent = _json_copy(_lineage(self.root.parent))
        if parent != layout['parent_binding'] or os.path.lexists(self.root) != resume:
            raise RestoreMaterializationError('恢复目标或父目录已变化，请重新规划。')
        if not resume:
            self.root.mkdir()
        self.bindings = _json_copy(_lineage(self.root))
        if any(self.bindings.get(name) != value for name, value in parent.items()):
            raise RestoreMaterializationError('创建恢复容器期间父目录变化。')
        self.directories = set()
        self.files = {}
        self.resuming = resume
        self.existing = {}

    def adopt(self, view, layout):
        """Verify all retained prefixes before adding any byte or directory."""
        members = {'workbench.sqlite':'workbench.sqlite'}
        directories = set()
        for mapping in layout['roots']:
            prefix = Path(mapping['destination']).relative_to(self.root)
            tree = view.manifest['preview']['scope']['inventories'][mapping['root']]
            for name in ['.', *tree['directories']]:
                path = prefix if name == '.' else prefix/name
                while path != Path('.'):
                    directories.add(path.as_posix())
                    path = path.parent
            for name in tree['files']:
                members[(prefix/name).as_posix()] = mapping['archive_path']+'/'+name
        current = inventory_tree(self.root)
        controls = {'restore-intent.json','restore-plan.json'}
        if (current['excluded'] or not set(current['files']) <= set(members)|controls
                or not set(current['directories']) <= directories):
            raise RestoreMaterializationError('中断副本包含原复制计划之外的文件或目录。')
        for name in current['directories']:
            path = self.root/name
            self.bindings[str(path)] = _json_copy(_lineage(path))[str(path)]
            self.directories.add(name)
        for name, member in members.items():
            if name not in current['files']:
                continue
            path = self.root/name
            before = _identity(_ordinary(path))
            if before['size'] > view.manifest['members'][member]['size']:
                raise RestoreMaterializationError('中断文件超过原归档大小：'+name)
            with path.open('rb') as source:
                if _identity(os.fstat(source.fileno())) != before:
                    raise RestoreMaterializationError('中断文件在打开时被替换。')
                view.copy_member(member, _PrefixVerifier(source, before['size']))
            if _identity(_ordinary(path)) != before:
                raise RestoreMaterializationError('核对中断文件期间内容或身份变化。')
            self.existing[name] = before
        if inventory_tree(self.root) != current:
            raise RestoreMaterializationError('核对期间中断副本发生变化。')
        self.require()

    def require(self, directory=None):
        current = _json_copy(_lineage(self.root if directory is None else directory))
        if any(self.bindings.get(name) != value for name, value in current.items()):
            raise RestoreMaterializationError('恢复目录的物理绑定变化，已停止写入。')

    def relative(self, path):
        path = Path(path)
        if path == self.root or not path.is_relative_to(self.root):
            raise RestoreMaterializationError('恢复写入超出新容器。')
        name = path.relative_to(self.root).as_posix()
        _member(name)
        return name

    def mkdir(self, path):
        name = self.relative(path)
        path = self.root / name
        if name in self.directories:
            self.require(path)
            return
        if path.parent != self.root:
            self.mkdir(path.parent)
        self.require(path.parent)
        path.mkdir()
        info = _ordinary(path)
        if not stat.S_ISDIR(info.st_mode):
            raise RestoreMaterializationError('恢复目录不是普通目录。')
        self.bindings[str(path)] = [info.st_dev, info.st_ino]
        self.directories.add(name)
        self.require(path)

    def json(self, name, value):
        path = self.root / name
        self.relative(path)
        self.require(path.parent)
        if self.resuming and os.path.lexists(path):
            if json.loads(read_project_bytes(path.parent, path.name)) != value:
                raise RestoreMaterializationError('中断复制的原控制文件已经变化。')
        else:
            atomic_create_json(path, value)
        self.require(path.parent)
        written = _file_hash(path)
        if (json.loads(read_project_bytes(path.parent, path.name)) != value
                or _file_hash(path) != written):
            raise RestoreMaterializationError('恢复控制文件偏离待发布的内容。')
        self.files[name] = written

    def member(self, view, member, path):
        name = self.relative(path)
        self.require(path.parent)
        retained = self.existing.get(name)
        if retained is not None and _identity(_ordinary(path)) != retained:
            raise RestoreMaterializationError('已核对的中断文件在续写前变化。')
        with path.open('r+b' if retained is not None else 'xb') as output:
            if retained is not None and _identity(os.fstat(output.fileno())) != retained:
                raise RestoreMaterializationError('中断文件在续写打开时被替换。')
            output.seek(retained['size'] if retained is not None else 0)
            view.copy_member(member, _AppendTail(output, retained['size']) if retained is not None else output)
            output.flush()
            os.fsync(output.fileno())
            opened = _identity(os.fstat(output.fileno()))
        self.require(path.parent)
        if _identity(_ordinary(path)) != opened:
            raise RestoreMaterializationError('恢复文件在复制期间被替换。')
        expected = {key:view.manifest['members'][member][key] for key in ('size', 'sha256')}
        if _file_hash(path) != expected:
            raise RestoreMaterializationError('恢复文件未通过写后校验。')
        self.files[name] = expected

    def verify(self):
        self.require()
        for name in self.directories:
            self.require(self.root / name)
        current = inventory_tree(self.root)
        files = {name:{key:value[key] for key in ('size', 'sha256')}
                 for name,value in current['files'].items()}
        if (files != self.files or set(current['directories']) != self.directories
                or current['excluded']):
            raise RestoreMaterializationError('恢复副本清单或内容变化，不能登记复制完成。')
        self.require()
        return {'files':files, 'directories':sorted(self.directories)}


class _PrefixVerifier:
    def __init__(self, source, remaining):
        self.source, self.remaining = source, remaining

    def write(self, data):
        length = min(len(data), self.remaining)
        if self.source.read(length) != data[:length]:
            raise RestoreMaterializationError('中断文件的已有内容偏离原归档，已保留现场。')
        self.remaining -= length
        return len(data)


class _AppendTail:
    def __init__(self, output, remaining):
        self.output, self.remaining = output, remaining

    def write(self, data):
        skip = min(len(data), self.remaining)
        self.remaining -= skip
        if skip < len(data):
            self.output.write(data[skip:])
        return len(data)

    def __getattr__(self, name):
        return getattr(self.output, name)


def _validate_plan(plan):
    try:
        saved = _json_copy(plan)
        if (not isinstance(saved, dict) or saved.get('schema_version') != 'target_restore_plan_v1'
                or saved.get('plan_ready') is not True or saved.get('offline_data_verified') is not True
                or saved.get('blockers') != []
                or saved.get('plan_sha256') != digest({k:v for k,v in saved.items() if k != 'plan_sha256'})):
            raise ValueError('unverified plan')
        for name in ('project_id', 'book_id'):
            if not isinstance(saved[name], str) or not saved[name]:
                raise ValueError('missing target')
        if not isinstance(saved['root_plan']['container'], str):
            raise ValueError('missing destination')
        return saved
    except (ValueError, TypeError, KeyError) as exc:
        raise RestoreMaterializationError('恢复计划缺失、未通过或已修改。') from exc


def materialize_archived_restore(package, *, plan, temporary_root=None, resume=False):
    saved = _validate_plan(plan)
    # Serialize explicit retries without adding files to the copied container.
    with persistence_state_lock((Path(saved['root_plan']['container'])/'restore-intent.json',)):
        return _materialize(package, plan=saved, temporary_root=temporary_root, resume=resume)


def _materialize(package, *, plan, temporary_root, resume):
    """Reverify the full saved plan, then copy bytes without activating a book.

    No caller-provided success flag skips archive checks. The durable intent is
    published before any DB bytes, so both incomplete and complete materialized
    containers fail ordinary WorkbenchDatabase startup until later binding.
    """
    saved = _validate_plan(plan)
    if type(resume) is not bool:
        raise RestoreMaterializationError('复制续接必须是明确的布尔选择。')
    fresh = _plan_archived_restore(package, project_id=saved['project_id'], book_id=saved['book_id'],
        destination=saved['root_plan']['container'], temporary_root=temporary_root, _existing=resume)
    if _json_copy(fresh) != saved:
        raise RestoreMaterializationError('归档、实现版本或目标绑定已偏离保存的计划。')
    layout = saved['root_plan']
    with verified_archive_view(package) as view:
        if (view.verified['receipt_sha256'] != saved['archive_receipt_sha256']
                or _file_hash(Path(package).absolute() / 'archive.zip') != saved['archive_binding']['archive']):
            raise RestoreMaterializationError('执行复制前归档完成凭据或内容变化。')
        writer = _NewContainer(layout, resume=resume)
        intent = {'schema_version':'restore_materialization_intent_v1', 'state':'restoring',
            'plan_sha256':saved['plan_sha256'], 'archive_receipt_sha256':saved['archive_receipt_sha256'],
            'project_id':saved['project_id'], 'book_id':saved['book_id'],
            'container':layout['container'], 'container_binding':writer.bindings.copy(),
            'database':layout['database'], 'write_activation_allowed':False}
        intent['intent_sha256'] = digest(intent)
        if resume:
            if json.loads(read_project_bytes(writer.root,'restore-intent.json')) != intent:
                raise RestoreMaterializationError('中断复制意图或原容器物理身份已经变化。')
            plan_path = writer.root/'restore-plan.json'
            if os.path.lexists(plan_path) and json.loads(read_project_bytes(writer.root,plan_path.name)) != saved:
                raise RestoreMaterializationError('中断复制的原计划已经变化。')
            if os.path.lexists(writer.root/'restore-materialization.json'):
                from workbench.backup_restore_roots import _MaterializedCopy
                copy_ = _MaterializedCopy(writer.root,saved,view)
                copy_.verify({},())
                if _implementation()['fingerprint_sha256'] != saved['verifier']['implementation_sha256']:
                    raise RestoreMaterializationError('核对已完成复制期间验证器磁盘版本变化。')
                return copy_.receipt
            writer.adopt(view,layout)
        writer.json('restore-intent.json', intent)
        writer.json('restore-plan.json', saved)
        # Retain the complete original database, including disabled projects.
        # Do not open it with a schema-initializing WorkbenchDatabase here.
        writer.member(view, 'workbench.sqlite', Path(layout['database']))
        inventories = view.manifest['preview']['scope']['inventories']
        for mapping in layout['roots']:
            tree = inventories[mapping['root']]
            root = Path(mapping['destination'])
            writer.mkdir(root)
            for name in sorted(tree['directories'], key=lambda s:(len(Path(s).parts), s)):
                writer.mkdir(root / name)
            for name in sorted(tree['files']):
                writer.member(view, mapping['archive_path'] + '/' + name, root / name)
        copied = writer.verify()
    # The view's exit checks must finish BEFORE publication of a completion.
    if _implementation()['fingerprint_sha256'] != saved['verifier']['implementation_sha256']:
        raise RestoreMaterializationError('复制期间验证器磁盘版本变化。')
    writer.verify()
    result = {'schema_version':'restore_materialization_receipt_v1', 'state':'materialized',
        'plan_sha256':saved['plan_sha256'], 'archive_receipt_sha256':saved['archive_receipt_sha256'],
        'intent_sha256':intent['intent_sha256'], 'project_id':saved['project_id'], 'book_id':saved['book_id'],
        'container':layout['container'], 'container_binding':_json_copy(_lineage(writer.root)),
        'copied':copied, 'copied_sha256':digest(copied), 'historical_bytes_preserved':True,
        'database_rebound':False, 'new_physical_controls_installed':False,
        'read_only_app_ready':False, 'restored':False, 'recoverable':False,
        'write_activation_allowed':False, 'ownership_handoff_completed':False,
        'start_worker':False, 'provider_calls_allowed':False}
    result['receipt_sha256'] = digest(result)
    writer.json('restore-materialization.json', result)
    writer.verify()
    return result
