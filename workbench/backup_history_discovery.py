"""Discover managed persistence namespaces from a locked book inventory.

The runtime tree and control marker filenames are reserved. Elsewhere, a
namespace name alone is insufficient: recognizable persistence evidence must
identify its owner. Recognition only selects records for strict verification.
"""
from dataclasses import dataclass, field
import json
from pathlib import Path, PurePosixPath

from core.story_project.reviewed_state_document import digest, read_project_bytes


def below(control, relative):
    return str(PurePosixPath(control) / relative)


@dataclass
class HistoryLayout:
    legacy_controls: set
    v2_controls: set
    children: dict
    files: dict
    flat_legacy_journals: set = field(default_factory=set)

    def journals(self, controls, namespace):
        result = set(self.flat_legacy_journals) if namespace == 'transactions' else set()
        for control in controls:
            directory = below(control, namespace)
            if directory in self.files:
                raise ValueError('持久化历史命名空间不是目录。')
            result.update(self.children.get(directory, ()))
        if len(result) > 10000 or result.intersection(self.files):
            raise ValueError('持久化历史事务目录无效或超过上限。')
        return result


def discover_history_layout(root, tree, *, read_bytes=None):
    read_bytes = read_bytes or read_project_bytes
    root = Path(root).absolute()
    if tree['source_root'] != str(root):
        raise ValueError('持久化历史发现来源与锁定清单不同。')
    files = tree['files']
    children = {}
    legacy = {'.novelagent/runtime/runs'}
    newer = {'.novelagent/runtime/persistence', '.novelagent/runtime/reviewed/p',
             '.novelagent/runtime/ea'}
    candidates = []
    for name in sorted(set(files) | set(tree['directories'])):
        path = PurePosixPath(name)
        parts = path.parts
        children.setdefault(str(path.parent), set()).add(name)
        if name in files and path.name in {'.persistence.lock', 'root_registry.json'}:
            control = str(path.parent)
            newer.add(control)
            if path.name == '.persistence.lock':
                legacy.add(control)
        # Anything under the reserved runtime namespace is managed, including
        # remnants whose manifest, lock or root registry has been lost.
        if parts[:2] == ('.novelagent', 'runtime'):
            for index, part in enumerate(parts[2:], 2):
                if part in {'transactions', 'journals', 'registry'}:
                    control = '/'.join(parts[:index])
                    (legacy if part == 'transactions' else newer).add(control)
        if name not in files:
            continue
        if len(parts) >= 3 and parts[-1] == 'manifest.json' and parts[-3] in {'transactions', 'journals'}:
            candidates.append((name, parts[-3], str(path.parent.parent.parent)))
        elif len(parts) >= 3 and parts[-3] == 'registry' and parts[-2] in {
                'pending', 'recovery_required', 'completed', 'rolled_back'} and path.suffix == '.json':
            candidates.append((name, 'registry', str(path.parent.parent.parent)))

    count = total = 0
    for name, kind, control in candidates:
        known = legacy if kind == 'transactions' else newer
        if control in known:
            continue
        meta = files[name]
        count += 1
        total += meta['size']
        if meta['size'] > 32 * 1024**2 or count > 20000 or total > 256 * 1024**2:
            raise ValueError('持久化历史发现读取超过支持的上限。')
        raw = read_bytes(root, name)
        if len(raw) != meta['size'] or digest(raw) != meta['sha256']:
            raise ValueError('持久化历史发现证据偏离锁定清单。')
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(value, dict):
            continue
        version = value.get('schema_version')
        if kind == 'transactions':
            recognized = type(version) is int and version == 1 and 'targets' in value and 'allowed_roots' in value
        elif kind == 'journals':
            recognized = version in ('2.0', '2.1') and 'immutable' in value and 'manifest_digest' in value
        else:
            recognized = version in ('2.0', '2.1') and 'journal_relative_path' in value and 'manifest_digest' in value
        if recognized:
            known.add(control)
    # StoryProject's v1 writer explicitly supplied transactions_dir=runtime /
    # 'persistence', so its journals are direct children, not /transactions/.
    # This is a reserved writer namespace. Include orphan directories too:
    # discovering a journal must not certify its missing or damaged manifest.
    # The v2 namespaces are checked independently by read_v2_transactions.
    flat_root = '.novelagent/runtime/persistence'
    flat = {name for name in children.get(flat_root, ())
            if name not in files and PurePosixPath(name).name not in
            {'journals', 'registry', 'staging', 'transactions'}}
    return HistoryLayout(legacy, newer, children, files, flat)
