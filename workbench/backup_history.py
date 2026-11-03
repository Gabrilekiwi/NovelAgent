"""Read-only verification of retained legacy v1 transaction target histories.

Verify immutable staged/before/candidate/marker evidence, then unwind complete
transactions from the observed snapshot. Comparing every old after-image with
today's file would incorrectly reject ordinary overlapping chapter commits.
The order found is a consistent order, not a claim about wall-clock chronology.
"""
import json
from pathlib import Path,PurePosixPath
import re

from core.story_project.reviewed_state_document import digest,read_project_bytes
from workbench.backup_history_discovery import discover_history_layout


class BackupHistoryError(ValueError):
    code='backup_history_unverified'


def verify_legacy_history(root,tree,*,book_id,max_search=1000000):
    try:
        return _verify_legacy_history(root,tree,book_id=book_id,max_search=max_search)
    except (OSError,KeyError,TypeError,AttributeError) as exc:
        raise BackupHistoryError('历史文件、目标或字段缺失，无法完整核验。') from exc


def verify_persistence_history(root,tree,*,book_id,max_search=1000000,read_bytes=None,provenance=None):
    """Unwind retained old and new transactions in one consistent target graph."""
    from workbench.backup_v2_history import read_v2_transactions
    try:
        read_bytes=read_bytes or read_project_bytes
        from workbench.backup_restore_provenance import HistoricalCoordinates
        coordinates=HistoricalCoordinates(root,tree,book_id,provenance) if provenance is not None else None
        layout=discover_history_layout(root,tree,read_bytes=read_bytes)
        additional=read_v2_transactions(root,tree,book_id=book_id,layout=layout,read_bytes=read_bytes,coordinates=coordinates)
        return _verify_legacy_history(root,tree,book_id=book_id,max_search=max_search,
            additional=additional,layout=layout,read_bytes=read_bytes,coordinates=coordinates)
    except (OSError,KeyError,TypeError,AttributeError) as exc:
        raise BackupHistoryError('新旧持久化历史的必要证据缺失或无效。') from exc


def _verify_legacy_history(root,tree,*,book_id,max_search,additional=None,layout=None,read_bytes=None,coordinates=None):
    root=Path(root).absolute()
    if tree['source_root']!=str(root) or type(max_search) is not int or max_search<1:
        raise BackupHistoryError('历史核验来源或计算上限无效。')
    files=tree['files'];directories=set(tree['directories'])
    read_bytes=read_bytes or read_project_bytes

    def content(name):
        if name not in files:raise BackupHistoryError('历史所需文件缺失或被排除：'+name)
        raw=read_bytes(root,name)
        if digest(raw)!=files[name]['sha256'] or len(raw)!=files[name]['size']:
            raise BackupHistoryError('历史文件偏离锁定清单：'+name)
        return raw

    def record(name):
        value=json.loads(content(name))
        if not isinstance(value,dict):raise BackupHistoryError('历史记录不是对象：'+name)
        return value

    def check_blob(name,sha,size):
        if (not isinstance(sha,str) or re.fullmatch('[0-9a-f]{64}',sha) is None
                or type(size) is not int or size<0):raise BackupHistoryError('历史内容摘要或大小无效。')
        raw=content(name)
        if digest(raw)!=sha or len(raw)!=size:raise BackupHistoryError('历史暂存或前像内容损坏：'+name)

    layout=layout or discover_history_layout(root,tree,read_bytes=read_bytes)
    journals=layout.journals(layout.legacy_controls,'transactions')
    transactions={};observed={};evidence=[]
    for journal in sorted(journals):
        manifest=record(journal+'/manifest.json');run_id=PurePosixPath(journal).name
        if (type(manifest.get('schema_version')) is not int or manifest['schema_version']!=1
                or manifest.get('run_id')!=run_id or manifest.get('book_id') not in (None,book_id)
                or manifest.get('state') not in {'completed','rolled_back'}
                or not isinstance(manifest.get('targets'),list) or not manifest['targets']):
            raise BackupHistoryError('历史事务格式、作品或终态无法核验：'+journal)
        allowed=manifest.get('allowed_roots')
        if not isinstance(allowed,list) or not allowed:raise BackupHistoryError('历史事务来源根缺失。')
        mapped_allowed=[]
        for value in allowed:
            if not isinstance(value,str):raise BackupHistoryError('历史事务根无效。')
            path=Path(value)
            if coordinates is not None:path=coordinates.path(path,evidence=journal+'/manifest.json')
            if (not path.is_absolute() or '..' in path.parts or not path.is_relative_to(root)
                    or path!=root and path.relative_to(root).as_posix() not in directories):
                raise BackupHistoryError('历史事务引用未知或缺失根。')
            mapped_allowed.append(path)
        candidate_path=manifest.get('candidate_result_path');candidate_hash=manifest.get('candidate_sha256')
        if candidate_path is not None:
            if candidate_path!='candidate_result.json':raise BackupHistoryError('历史候选路径无效。')
            raw=content(journal+'/'+candidate_path)
            if digest(raw)!=candidate_hash:raise BackupHistoryError('历史候选摘要不同。')
            candidate=json.loads(raw)
            if (not isinstance(candidate,dict) or not isinstance(candidate.get('run'),dict)
                    or candidate['run'].get('id')!=run_id):raise BackupHistoryError('历史候选与事务不同。')
        elif candidate_hash is not None or journal+'/candidate_result.json' in files:
            raise BackupHistoryError('历史候选说明缺失。')
        marker_name=journal+'/commit.marker';completed=manifest['state']=='completed'
        if manifest.get('commit_marker')!='commit.marker':raise BackupHistoryError('历史提交标记路径无效。')
        if completed:
            marker=record(marker_name)
            if (marker.get('run_id')!=run_id or marker.get('candidate_sha256')!=candidate_hash
                    or not isinstance(marker.get('committed_at'),str) or not marker['committed_at']):
                raise BackupHistoryError('历史提交标记与事务不一致。')
        elif marker_name in files:raise BackupHistoryError('回滚事务含提交标记。')
        targets={}
        for index,target in enumerate(manifest['targets']):
            if (not isinstance(target,dict) or type(target.get('index')) is not int or target['index']!=index
                    or target.get('id')!=f'target-{index:03d}' or type(target.get('existed')) is not bool
                    or target.get('status')!=('verified' if completed else 'rolled_back')
                    or target.get('error') is not None):raise BackupHistoryError('历史目标未可靠结束。')
            path=Path(target['path'])
            if coordinates is not None:path=coordinates.path(path,evidence=journal+'/manifest.json')
            if (not path.is_absolute() or '..' in path.parts or path==root or not path.is_relative_to(root)
                    or not any(path.is_relative_to(value) for value in mapped_allowed)):
                raise BackupHistoryError('历史目标越过允许范围。')
            name=path.relative_to(root).as_posix()
            if name in targets or name in directories:raise BackupHistoryError('历史目标重复或为目录。')
            if any(name==e['path'] or name.startswith(e['path']+'/') for e in tree['excluded']):
                raise BackupHistoryError('必要历史目标被凭据排除规则排除。')
            staged=f'staged/{index:03d}.bin'
            if target.get('staged_path')!=staged:raise BackupHistoryError('历史暂存路径无效。')
            check_blob(journal+'/'+staged,target['after_sha256'],target['after_size'])
            if target['existed']:
                backup=f'backups/{index:03d}.bin'
                if target.get('backup_path')!=backup:raise BackupHistoryError('历史前像路径无效。')
                check_blob(journal+'/'+backup,target['before_sha256'],target['before_size'])
            elif any(target.get(k) is not None for k in ('before_sha256','before_size','backup_path')):
                raise BackupHistoryError('历史新建目标含错误前像。')
            before=target['before_sha256'];after=target['after_sha256'] if completed else before
            targets[name]=(before,after)
            observed[name]=files[name]['sha256'] if name in files else None
        transactions[journal]=targets
        evidence.append({'journal':journal,'manifest_sha256':files[journal+'/manifest.json']['sha256'],
                         'run_id':run_id,'state':manifest['state']})

    if additional:
        newer,new_evidence=additional
        if set(newer)&set(transactions):raise BackupHistoryError('新旧事务路径重复。')
        transactions.update(newer);evidence.extend(new_evidence)
        for targets in newer.values():
            for name in targets:observed[name]=files[name]['sha256'] if name in files else None

    # A surviving final run can expose a journal deleted as a whole (including
    # its directory). Do not infer completeness merely from directories left.
    run_roots=layout.legacy_controls
    final_records=[]
    for name in files:
        path=PurePosixPath(name)
        if path.suffix!='.json' or str(path.parent) not in run_roots:continue
        value=record(name)
        run=value.get('run');persistence=value.get('persistence')
        if persistence is None and isinstance(run,dict):persistence=run.get('persistence')
        if persistence is None:continue
        if (not isinstance(persistence,dict) or not isinstance(run,dict)
                or persistence.get('partial') is not False
                or persistence.get('state') not in {'completed','rolled_back'}
                or type(persistence.get('committed')) is not bool):
            raise BackupHistoryError('运行记录中的持久化未可靠结束。')
        journal=Path(persistence['journal_path'])
        if coordinates is not None:journal=coordinates.path(journal,evidence=name)
        if not journal.is_absolute() or '..' in journal.parts or not journal.is_relative_to(root):
            raise BackupHistoryError('运行记录引用未知历史根。')
        relative=journal.relative_to(root).as_posix()
        if relative not in transactions:raise BackupHistoryError('运行记录引用的完整事务目录缺失。')
        entry=next(item for item in evidence if item['journal']==relative)
        if (run.get('id')!=entry['run_id'] or persistence.get('run_id')!=entry['run_id']
                or persistence['state']!=entry['state']
                or persistence['committed']!=(entry['state']=='completed')):
            raise BackupHistoryError('最终运行记录与保留事务不同。')
        final_records.append({'path':name,'sha256':files[name]['sha256'],'journal':relative})

    # Reverse all transactions atomically across their complete target sets.
    # Repeated hashes/no-op writes and independent roots can create ties; search
    # those orders, bounded explicitly, rather than trust clocks or filenames.
    stack=[(frozenset(transactions),observed,[])];visited=set();work=0
    while stack:
        remaining,state,order=stack.pop()
        if not remaining:
            result={'schema_version':'legacy_backup_history_v1','state':'verified',
                    'coverage':'retained_v1_journals_and_known_final_run_references','transactions':evidence,
                    'final_run_records':final_records,
                    'consistent_reverse_order':order,'current_targets':observed,'baseline_targets':state}
            if additional and additional[0]:
                result.update(schema_version='mixed_backup_history_v1',
                    coverage='retained_v1_v2_journals_and_known_final_run_references')
            result['history_sha256']=digest(result)
            return result
        key=(remaining,tuple(sorted(state.items())))
        if key in visited:continue
        visited.add(key)
        for journal in sorted(remaining):
            targets=transactions[journal];work+=len(targets)
            if work>max_search:raise BackupHistoryError('历史排序核验超过计算上限，未获得完整证明。')
            if all(state[name]==after for name,(_,after) in targets.items()):
                work+=len(state)
                if work>max_search:raise BackupHistoryError('历史排序核验超过计算上限，未获得完整证明。')
                previous={**state,**{name:before for name,(before,_) in targets.items()}}
                stack.append((remaining-{journal},previous,order+[journal]))
    raise BackupHistoryError('历史目标链无法衔接到当前完整文件状态。')
