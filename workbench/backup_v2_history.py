"""Inventory-bound v2 evidence for a joint v1/v2 reverse target-chain check.

This reads historical evidence without applying old PathGuards or opening a
recovery service. It grants neither a restore mapping nor write permission.
"""
import json
from pathlib import Path,PurePosixPath
import re

from core.engine.persistence_v2 import (validate_persistence_manifest_v2,validate_commit_marker_v2,
    _verify_marker_against_manifest,_assert_receipt_matches_manifest,canonical_json_hash)
from core.engine.root_registry import validate_root_registry,validate_registry_manifest_binding
from core.engine.safe_paths import validate_path_ref
from core.schema import validate_schema
from core.story_project.reviewed_state_document import digest,read_project_bytes
from workbench.backup_history_discovery import below,discover_history_layout


def read_v2_transactions(root,tree,*,book_id,layout=None,read_bytes=None,coordinates=None):
    root=Path(root).absolute();files=tree['files'];directories=set(tree['directories'])
    if tree['source_root']!=str(root):raise ValueError('v2 历史来源与清单不同。')
    read_bytes=read_bytes or read_project_bytes
    count=0;total=0
    def content(name):
        nonlocal count,total
        record=files.get(name)
        if record is None or record['size']>32*1024**2:raise ValueError('v2 历史文件缺失或超过单文件上限。')
        count+=1;total+=record['size']
        if count>20000 or total>256*1024**2:raise ValueError('v2 历史读取超过支持的上限。')
        raw=read_bytes(root,name)
        if len(raw)!=record['size'] or digest(raw)!=record['sha256']:raise ValueError('v2 历史偏离来源清单。')
        return raw
    def record(name):
        value=json.loads(content(name))
        if not isinstance(value,dict):raise ValueError('v2 历史记录必须是对象。')
        return value
    def blob(name,sha,size):
        if not isinstance(sha,str) or re.fullmatch('[0-9a-f]{64}',sha) is None or type(size) is not int or size<0:
            raise ValueError('v2 历史内容摘要或大小无效。')
        raw=content(name)
        if digest(raw)!=sha or len(raw)!=size:raise ValueError('v2 历史暂存或前像损坏。')
    def local(path,*,directory=False):
        path=Path(path)
        if coordinates is not None:path=coordinates.path(path,evidence=journal+'/manifest.json')
        if not path.is_absolute() or '..' in path.parts or not path.is_relative_to(root):
            raise ValueError('v2 历史引用未纳入归档的外部根。')
        name=path.relative_to(root).as_posix()
        if directory and name!='.' and name not in directories:raise ValueError('v2 历史所需根缺失。')
        return name

    layout=layout or discover_history_layout(root,tree,read_bytes=read_bytes)
    journals=layout.journals(layout.v2_controls,'journals')
    transactions={};evidence=[];registries={};manifests={};receipts={}
    for journal in sorted(journals):
        manifest=validate_persistence_manifest_v2(record(journal+'/manifest.json'))
        immutable=manifest['immutable'];run_id=PurePosixPath(journal).name
        if immutable['run_id']!=run_id or immutable['book_id']!=book_id or manifest['state'] not in {'completed','rolled_back'}:
            raise ValueError('v2 历史事务归属或终态无效。')
        control=str(PurePosixPath(journal).parent.parent)
        if manifest['schema_version']=='2.1':
            if control not in registries:
                registries[control]=validate_root_registry(record(below(control,'root_registry.json')))
            registry=registries[control]
            validate_registry_manifest_binding(immutable['root_registry'],registry,require_same_revision=False)
            bindings=registry['roots']
        else:bindings=immutable['root_map']
        # Validate every declared root before reading any referenced content.
        for binding in bindings.values():local(binding['path'],directory=True)
        def ref_name(value):
            ref=validate_path_ref(value)
            binding=bindings.get(ref.root_id)
            if binding is None or ref.root_uuid is not None and ref.root_uuid!=binding.get('root_uuid'):
                raise ValueError('v2 历史路径与逻辑根不同。')
            return local(Path(binding['path'])/ref.relative_path)
        if ref_name(immutable['manifest_path_ref'])!=journal+'/manifest.json':
            raise ValueError('v2 历史 manifest 定位不同。')
        candidate=immutable['candidate']
        blob(journal+'/candidate_result.json',candidate['digest'],candidate['size'])
        completed=manifest['state']=='completed'
        marker_name=ref_name(immutable['marker_path_ref'])
        receipt_name=ref_name(immutable['publication_receipt']['path_ref'])
        if completed:
            marker=validate_commit_marker_v2(record(marker_name))
            _verify_marker_against_manifest(marker,manifest)
            receipt=record(receipt_name)
            _assert_receipt_matches_manifest(receipt,manifest)
            if receipt['marker']['sha256']!=marker['marker_hash']:raise ValueError('v2 回执与提交标记不同。')
            receipts[journal]={'id':receipt['receipt_id'],'path_ref':immutable['publication_receipt']['path_ref'],
                'receipt_hash':receipt['receipt_hash']}
        else:
            if marker_name in files or receipt_name in files:raise ValueError('v2 回滚事务含提交或发布凭据。')
            failure=record(journal+'/failure_receipt.json')
            if (set(failure)!={'schema_version','book_id','run_id','manifest_digest','state','errors','recorded_at','receipt_hash'}
                    or any(failure.get(key)!=expected for key,expected in {
                        'schema_version':manifest['schema_version'],'book_id':book_id,'run_id':run_id,
                        'manifest_digest':manifest['manifest_digest'],'state':'rolled_back','errors':manifest['errors']}.items())
                    or failure['receipt_hash']!=canonical_json_hash(failure,exclude_fields=('receipt_hash',))):
                raise ValueError('v2 回滚凭据无效。')
            receipts[journal]=None
        targets={}
        for index,target in enumerate(immutable['targets']):
            name=ref_name(target['path_ref']);before=target.get('before_sha256')
            if (target.get('index')!=index or type(target.get('before_exists')) is not bool
                    or name in targets or name in directories):raise ValueError('v2 历史目标重复或字段无效。')
            if any(name==item['path'] or name.startswith(item['path']+'/') for item in tree['excluded']):
                raise ValueError('v2 必要历史目标被排除。')
            blob(journal+'/'+target['staged_relative_path'],target['after_sha256'],target['after_size'])
            if target['before_exists']:
                if target['phase']=='apply':
                    if not target.get('backup_relative_path'):raise ValueError('v2 历史缺少前像定位。')
                    blob(journal+'/'+target['backup_relative_path'],before,target['before_size'])
                elif before!=target['after_sha256'] or target['before_size']!=target['after_size']:
                    raise ValueError('v2 不可变发布目标被覆盖。')
            elif before is not None or target.get('before_size') is not None or target.get('backup_relative_path') is not None:
                raise ValueError('v2 新建目标含错误前像。')
            targets[name]=(before,target['after_sha256'] if completed else before)
        transactions[journal]=targets
        manifests[journal]=manifest
        evidence.append({'journal':journal,'manifest_sha256':files[journal+'/manifest.json']['sha256'],
            'run_id':run_id,'state':manifest['state'],'schema_version':manifest['schema_version'],
            'publication_receipt':{'path':receipt_name,'receipt_hash':receipts[journal]['receipt_hash']} if completed else None})
    seen=set()
    for control in sorted(layout.v2_controls):
        registry_root=below(control,'registry')
        allowed_states={'pending','recovery_required','completed','rolled_back'}
        if (control in files or registry_root in files
                or any(PurePosixPath(name).name not in allowed_states or name in files
                       for name in layout.children.get(registry_root,()))):
            raise ValueError('v2 登记命名空间损坏或含未知状态。')
        for namespace in ('staging','registry/pending','registry/recovery_required'):
            directory=below(control,namespace)
            if directory in files or layout.children.get(directory):
                raise ValueError('v2 控制面仍有未完成或待恢复记录。')
        for state in ('completed','rolled_back'):
            directory=below(control,'registry/'+state)
            if directory in files:raise ValueError('v2 终态登记目录无效。')
            for name in sorted(layout.children.get(directory,())):
                path=PurePosixPath(name)
                if str(path.parent)!=directory or path.suffix!='.json' or name not in files:
                    raise ValueError('v2 终态登记含未知目录或文件。')
                entry=validate_schema(record(name),'persistence_registry_entry.schema.json')
                journal=below(control,'journals/'+path.stem)
                manifest=manifests.get(journal)
                if manifest is None or journal in seen:raise ValueError('v2 登记缺少完整事务或有矛盾终态。')
                if any(entry.get(key)!=expected for key,expected in {
                    'book_id':book_id,'run_id':path.stem,'state':state,'schema_version':manifest['schema_version'],
                    'journal_relative_path':'journals/'+path.stem,'manifest_digest':manifest['manifest_digest'],
                    'receipt':receipts[journal],'error_count':len(manifest['errors'])}.items()) or manifest['state']!=state:
                    raise ValueError('v2 登记与事务或发布凭据不同。')
                seen.add(journal)
    if seen!=set(manifests):raise ValueError('v2 历史尚未完成终态登记。')
    return transactions,evidence
