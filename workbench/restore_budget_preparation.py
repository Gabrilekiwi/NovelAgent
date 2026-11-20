"""Apply reconciled financial evidence to the actual still-read-only copy.

The original archive/history/DB are reverified with a narrowly qualified set of
budget changes. Every other byte and directory keeps its existing proof. Known
temporary paths make interrupted file publication explicitly resumable.
"""
from contextlib import contextmanager
import os
from pathlib import Path, PurePosixPath
import stat

from core.engine.persistence import atomic_create_json, _fsync_directory, _windows_move_file
from core.engine.safe_paths import assert_safe_local_tree
from core.story_project.reviewed_state_document import digest
from workbench.backup_archive import _file_hash
from workbench.backup_archive_view import verified_archive_view
from workbench.backup_archive_usage import _StoryEvidence, _usage
from workbench.backup_auxiliary import Evidence
from workbench.backup_files import _ordinary, _stream_member
from workbench.backup_restore_roots import _read, _walk_error
from workbench.restore_budget_merge import reconcile_usage_evidence
from workbench.restore_handoff import RestoreHandoffError, _signed
from workbench.restore_usage import RestoreUsage, _SavedEvidence, _resolver, _BoundedBytes


_CAPABILITY = object()


class _TargetEvidence(_StoryEvidence):
    def __init__(self, target, source_tree, files, directories):
        self.target = target
        Evidence.__init__(self, source_tree['source_root'],
            {'files':files,'directories':directories}, _read_bytes=self.raw)

    def raw(self, root, name):
        relative = self.relative(Path(root)/name)
        record = self.tree['files'].get(relative)
        if record is None or _ordinary(self.target/relative).st_size != record['size']:
            raise RestoreHandoffError('恢复副本的预算文件缺失或大小变化。')
        output = _BoundedBytes(record['size'])
        _stream_member(_resolver(self.target), relative, output=output)
        raw = output.getvalue()
        if len(raw) != record['size'] or digest(raw) != record['sha256']:
            raise RestoreHandoffError('恢复副本的预算文件内容不同。')
        return raw


class _BudgetOverlay:
    def __init__(self, service, value, intent, source, *, _capability):
        if _capability is not _CAPABILITY:
            raise RestoreHandoffError('预算变更缺少已核验的交接上下文。')
        self.service, self.value, self.intent, self.source = service, value, intent, source
        self.live = True
        self.complete = os.path.lexists(service._path(value['handoff_id'], 'budget-done'))

    def require(self, copy_):
        if not self.live:
            raise RestoreHandoffError('预算变更核验上下文已结束。')
        service, value, intent = self.service, self.value, self.intent
        service.owner._held()
        if (copy_.root != Path(value['target']) or copy_.plan['plan_sha256'] != value['plan_sha256']
                or copy_.plan['archive_receipt_sha256'] != value['archive_receipt_sha256']
                or service._pending(value['handoff_id']) != value
                or _read(service.owner.directory, service._path(value['handoff_id'], 'budget').name) != intent
                or service._read_completed(value)['receipt_sha256'] != intent['usage_receipt_sha256']):
            raise RestoreHandoffError('预算变更的交接、原凭据或实际恢复副本变化。')

    def verify(self, copy_, replacements, extra):
        self.require(copy_)
        recipe = self.intent['reconciliation']
        additions = {'book/'+name:record for name, record in recipe['files'].items()}
        directories = {'book/'+name for name in recipe['directories']} | self._additional_directories()
        temporaries = {'book/'+name:record for name, record in self.intent['temporaries'].items()}
        if set(temporaries) & (set(copy_.files)|set(additions)):
            raise RestoreHandoffError('预算暂存名与已有资料冲突。')
        ignored = {'workbench.lock','workbench-instance.json', *extra}
        actual_files, actual_directories = set(), set()
        for parent, dirs, files in os.walk(copy_.root, followlinks=False, onerror=_walk_error):
            for name in dirs+files:
                path = Path(parent)/name; info = _ordinary(path)
                relative = path.relative_to(copy_.root).as_posix()
                if stat.S_ISDIR(info.st_mode): actual_directories.add(relative)
                elif relative not in ignored: actual_files.add(relative)
                if len(actual_files)+len(actual_directories) > 100000:
                    raise RestoreHandoffError('预算准备副本超过支持的路径数量。')
        expected_files, expected_dirs = set(copy_.files), copy_.directories
        if (not expected_files.issubset(actual_files)
                or not actual_files.issubset(expected_files|set(additions)|set(temporaries))
                or not expected_dirs.issubset(actual_directories)
                or not actual_directories.issubset(expected_dirs|directories)):
            raise RestoreHandoffError('预算准备期间修改了范围外资料或丢失了原资料。')
        for name in sorted(actual_files):
            if name in temporaries:
                if self.complete or _ordinary(copy_.root/name).st_size > temporaries[name]['size']:
                    raise RestoreHandoffError('预算暂存文件异常或准备尚未完成。')
                continue
            actual = _file_hash(copy_.root/name)
            allowed = [copy_.files.get(name), replacements.get(name), additions.get(name)]
            if actual not in allowed or self.complete and name in additions and actual != additions[name]:
                raise RestoreHandoffError('恢复文件不属于原归档或已核验预算变更：'+name)
        if self.complete and (not set(additions).issubset(actual_files) or not directories.issubset(actual_directories)):
            raise RestoreHandoffError('恢复副本的预算变更尚未完整落盘。')
        self.require(copy_)

    def _additional_directories(self):
        return set()


class RestoreBudgetPreparation(RestoreUsage):
    def _budget_intent(self, value, usage, reconciliation):
        temporaries = {}
        for index, (name, record) in enumerate(reconciliation['files'].items()):
            temp = PurePosixPath(name).with_name('.restore-budget-'+value['handoff_id'][:8]+'-'+str(index)+'.tmp')
            temporaries[str(temp)] = {'target':name, 'size':record['size']}
        return _signed({'schema_version':'restore_budget_preparation_v1', **self._binding_fields(value),
            'plan_sha256':value['plan_sha256'],'archive_receipt_sha256':value['archive_receipt_sha256'],
            'usage_receipt_sha256':usage['receipt_sha256'], 'reconciliation':reconciliation,
            'temporaries':temporaries}, 'intent_sha256')

    def _budget_receipt(self, value, intent):
        recipe = intent['reconciliation']
        return _signed({'schema_version':'restore_budget_prepared_v1', **self._binding_fields(value),
            'budget_intent_sha256':intent['intent_sha256'],'reconciliation_sha256':recipe['reconciliation_sha256'],
            'budget_evidence_reconciled':True,'target_budget_prepared':True,'settled':recipe['settled'],
            'blockers':recipe['blockers'],'usage':recipe['usage'],
            'old_jobs_invalidated':False,'fresh_execution_roots_ready':False,
            'write_activation_allowed':False,'handoff_completed':False}, 'receipt_sha256')

    @contextmanager
    def _budget_context(self, package, value):
        usage = self._read_completed(value)
        source_intent = self._load_intent(value)
        source = _SavedEvidence(self.owner.directory, source_intent['tree'])
        with verified_archive_view(package) as view:
            if view.verified['receipt_sha256'] != value['archive_receipt_sha256']:
                raise RestoreHandoffError('预算准备的归档与原交接不同。')
            reconciliation = reconcile_usage_evidence(_StoryEvidence(view), source, book_id=self.owner.book_id)
            if reconciliation['usage'] != usage['usage']:
                raise RestoreHandoffError('原消费核验结果在预算合并期间变化。')
            intent = self._budget_intent(value, usage, reconciliation)
            yield intent, source
            if self._pending(value['handoff_id']) != value or self._read_completed(value) != usage:
                raise RestoreHandoffError('预算准备期间原交接或消费证据变化。')

    def _require_budget_intent(self, value, intent):
        self.owner._held()
        path = self._path(value['handoff_id'], 'budget')
        if self._pending(value['handoff_id']) != value or _read(path.parent, path.name) != intent:
            raise RestoreHandoffError('发布预算文件前原交接或预算准备意图变化。')

    def _apply_file(self, root, name, record, temporary, raw, value, intent):
        self._require_budget_intent(value, intent)
        resolver = _resolver(root)
        ref = {'root_id':'external:backup_source','relative_path':name}
        resolved = resolver.resolve(ref)
        path = resolved.path
        before = _file_hash(path) if os.path.lexists(path) else None
        temp = root/temporary
        if before == record and not os.path.lexists(temp): return
        assert_safe_local_tree(path.parent).mkdir(parents=True, exist_ok=True)
        guard = resolver.resolve(ref, expected_guard=resolved.guard, allow_guard_extension=True).guard
        if os.path.lexists(temp):
            if _ordinary(temp).st_size > record['size']:
                raise RestoreHandoffError('原预算暂存文件超出保存清单。')
            temp.unlink()  # Only this exact, intent-owned incomplete file.
        with temp.open('xb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        self._fault('after_budget_temp_fsync', value)
        self._require_budget_intent(value, intent)
        resolver.resolve(ref, expected_guard=guard)
        if (_file_hash(path) if os.path.lexists(path) else None) != before or _file_hash(temp) != record:
            raise RestoreHandoffError('预算发布目标或暂存内容变化。')
        if os.name == 'nt': _windows_move_file(temp, path, replace_existing=before is not None)
        else: os.replace(temp, path)
        _fsync_directory(path.parent)
        resolver.resolve(ref, expected_guard=guard)
        if _file_hash(path) != record:
            raise RestoreHandoffError('预算文件发布后核验失败。')
        self._fault('after_budget_member', value)

    def _prepare(self, package, *, handoff_id, temporary_root, resume):
        with self.owner.locked():
            value = self._pending(handoff_id)
            with self._budget_context(package, value) as (intent, source):
                path = self._path(handoff_id, 'budget'); done = self._path(handoff_id, 'budget-done')
                exists = os.path.lexists(path)
                if exists:
                    if _read(path.parent, path.name) != intent:
                        raise RestoreHandoffError('预算准备意图与当前原始证据不同。')
                    if not resume and not os.path.lexists(done):
                        raise RestoreHandoffError('原预算准备已开始，请显式续接。')
                else:
                    if resume or os.path.lexists(done):
                        raise RestoreHandoffError('预算准备意图丢失，不能补造。')
                    # Finish the original full-copy proof before authorizing any
                    # extra file or mutable budget replacement.
                    with self._target(package, value['target'], temporary_root): pass
                    atomic_create_json(path, intent)
                    self._fault('after_budget_intent', value)
                overlay = _BudgetOverlay(self, value, intent, source, _capability=_CAPABILITY)
                result = self._budget_receipt(value, intent)
                if os.path.lexists(done) and _read(done.parent, done.name) != result:
                    raise RestoreHandoffError('预算准备完成凭据变化。')
                try:
                    with self._target(package, value['target'], temporary_root, _usage_overlay=overlay):
                        if not overlay.complete:
                            temporary_by_target = {item['target']:name for name,item in intent['temporaries'].items()}
                            for name, record in intent['reconciliation']['files'].items():
                                raw = source.raw(source.root, name)
                                self._apply_file(Path(value['target'])/'book', name, record,
                                    temporary_by_target[name], raw, value, intent)
                            overlay.complete = True
                        target = _TargetEvidence(Path(value['target'])/'book', source.tree,
                            intent['reconciliation']['files'], intent['reconciliation']['directories'])
                        projected = _usage(target, self.owner.book_id, lambda code, reference:None)
                        if digest(projected) != digest(result['usage']):
                            raise RestoreHandoffError('恢复副本实际预算与已核验合并结果不同。')
                        self._fault('before_budget_completion', value)
                finally:
                    overlay.live = False
            if (_read(path.parent, path.name) != intent or self._pending(handoff_id) != value
                    or self._read_completed(value)['receipt_sha256'] != intent['usage_receipt_sha256']):
                raise RestoreHandoffError('签发预算准备凭据前原证据变化。')
            if not os.path.lexists(done):
                atomic_create_json(done, result)
                self._fault('after_budget_completion', value)
            elif _read(done.parent, done.name) != result:
                raise RestoreHandoffError('预算准备完成凭据不一致。')
            return result

    def prepare_budget(self, package, *, handoff_id, temporary_root=None):
        return self._prepare(package, handoff_id=handoff_id, temporary_root=temporary_root, resume=False)

    def resume_budget(self, package, *, handoff_id, temporary_root=None):
        return self._prepare(package, handoff_id=handoff_id, temporary_root=temporary_root, resume=True)
