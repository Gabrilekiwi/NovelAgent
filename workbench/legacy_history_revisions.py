"""Explicit present-day acknowledgement of retained external memory edits.

These are not backdated commits. Original journals and fees are never changed.
Each revision retains exact reviewed bytes and links them to a verified old
transaction's after-image (and, for a historical edit, a later before-image).
"""
from datetime import datetime, timezone
from pathlib import PurePosixPath
import json
import re

from core.story_project.reviewed_state_document import digest

DIRECTORY = '.novelagent/runtime/history_revisions'
VERSION = 'observed_legacy_memory_revision_v1'
TARGETS = frozenset({'追踪/上下文.md', '追踪/角色状态.md', '追踪/时间线.md', '追踪/伏笔.md',
    '.novelagent/runtime/snapshot.json', '.novelagent/runtime/memory/v2/canonical_memory.json'})


def validate_revision(value, *, book_id, transactions):
    keys={'schema_version','id','book_id','recorded_at','kind','from_journal','to_journal',
          'reason','reviewer','author_confirmed','targets','record_sha256','source_inventory_sha256'}
    if (not isinstance(value,dict) or set(value)-{'supersedes'}!=keys or value['schema_version']!=VERSION
            or value['book_id']!=book_id or value['kind']!='observed_external_edit'
            or value['reviewer'] not in {'author','author_delegate'} or value['author_confirmed'] is not True
            or not isinstance(value['reason'],str) or not value['reason'].strip() or len(value['reason'])>2000
            or not re.fullmatch(r'revision_[0-9a-f]{32}',str(value['id']))
            or not re.fullmatch(r'[0-9a-f]{64}',str(value['source_inventory_sha256']))
            or value['record_sha256']!=digest({k:v for k,v in value.items() if k!='record_sha256'})):
        raise ValueError('历史记忆修订的身份、审核或内容校验失败。')
    at=datetime.fromisoformat(value['recorded_at'])
    if at.tzinfo is None:raise ValueError('历史修订必须保存实际记录时间。')
    supersedes=value.get('supersedes',[])
    if (not isinstance(supersedes,list) or len(supersedes)>16 or len(set(supersedes))!=len(supersedes)
            or any(not isinstance(v,str) or not re.fullmatch('revision_[0-9a-f]{32}',v) or v==value['id'] for v in supersedes)):
        raise ValueError('被更正的历史修订标识无效。')
    before=transactions.get(value['from_journal'])
    after=transactions.get(value['to_journal']) if value['to_journal'] is not None else None
    if before is None or value['to_journal'] is not None and after is None:
        raise ValueError('历史修订必须引用已核验的原始事务。')
    targets=value['targets']
    if not isinstance(targets,list) or not 1<=len(targets)<=len(TARGETS):
        raise ValueError('历史修订没有有限的记忆目标。')
    changes={}
    for item in targets:
        if not isinstance(item,dict) or set(item)!={'relative','before_sha256','after_sha256','after_text'}:
            raise ValueError('历史修订目标字段无效。')
        name=item['relative']
        if (name not in TARGETS or name in changes or name not in before
                or before[name][1]!=item['before_sha256'] or item['before_sha256'] is None
                or item['before_sha256']==item['after_sha256']
                or not isinstance(item['after_text'],str) or len(item['after_text'])>8*1024**2
                or digest(item['after_text'].encode('utf-8'))!=item['after_sha256']
                or after is not None and (name not in after or after[name][0]!=item['after_sha256'])):
            raise ValueError('历史修订不能替代缺失的原始证据或修改正文/身份。')
        changes[name]=(item['before_sha256'],item['after_sha256'])
    return changes


def read_revisions(*, tree, book_id, transactions, record):
    if any(n.startswith(DIRECTORY+'/') for n in tree['directories']):
        raise ValueError('历史修订目录含未知子目录。')
    entries=sorted(n for n in tree['files'] if n.startswith(DIRECTORY+'/'))
    if len(entries)>1000:raise ValueError('历史修订数量超过支持上限。')
    changes={};evidence=[];values={};superseded=set()
    for name in entries:
        path=PurePosixPath(name)
        if str(path.parent)!=DIRECTORY or not re.fullmatch(r'revision_[0-9a-f]{32}\.json',path.name):
            raise ValueError('历史修订目录存在未完成或未知内容。')
        value=record(name)
        if value.get('id')!=path.stem:raise ValueError('历史修订文件名与记录不符。')
        changes['external-revision:'+value['id']]=validate_revision(value,book_id=book_id,transactions=transactions)
        values[value['id']]=value
        evidence.append({'record':name,'record_sha256':value['record_sha256'],
            'file_sha256':tree['files'][name]['sha256'],'kind':value['kind'],
            'recorded_at':value['recorded_at'],'reviewer':value['reviewer']})
    for value in values.values():
        for identifier in value.get('supersedes',[]):
            old=values.get(identifier)
            if (old is None or identifier in superseded or old['from_journal']!=value['from_journal']
                    or {t['relative'] for t in old['targets']}!={t['relative'] for t in value['targets']}
                    or datetime.fromisoformat(old['recorded_at'])>=datetime.fromisoformat(value['recorded_at'])):
                raise ValueError('历史修订更正缺少同源原记录或存在冲突。')
            superseded.add(identifier)
            changes.pop('external-revision:'+identifier,None)
    for item in evidence:
        identifier=PurePosixPath(item['record']).stem
        item['state']='superseded' if identifier in superseded else 'active'
    return changes,evidence


def prepare_observed_revision(*, root, tree, book_id, transactions, from_journal, to_journal,
                              read_bytes, reason, reviewer='author_delegate', supersedes=None):
    """Prepare exact review content; no author confirmation or writes here."""
    before=transactions.get(from_journal)
    after=transactions.get(to_journal) if to_journal is not None else None
    if before is None or to_journal is not None and after is None:
        raise ValueError('请选择已核验的原始记忆事务。')
    targets=[]
    for name,(_,old_hash) in sorted(before.items()):
        if name not in TARGETS:continue
        if after is not None:
            new_hash=after.get(name,(None,None))[0]
            manifest_name=to_journal+'/manifest.json'
            manifest=json.loads(read_bytes(root,manifest_name))
            if 'immutable' in manifest:
                targets_v2=manifest['immutable']['targets']
                # Original v2 history already verified path_ref/root-map hashes;
                # bind the selected before-image by both canonical path and hash.
                def matches(t):
                    ref=t['path_ref'];base=manifest['immutable']['root_map'][ref['root_id']]['path']
                    from pathlib import Path
                    return Path(base)/ref['relative_path']==root/name
                target=next((t for t in targets_v2 if matches(t)),None)
                if target is None:continue
                relative=to_journal+'/'+str(target.get('backup_relative_path'))
            else:
                target=next((t for t in manifest['targets'] if str((root/name).absolute())==t['path']),None)
                if target is None:continue
                relative=to_journal+'/'+str(target.get('backup_path'))
        else:
            new_hash=tree['files'].get(name,{}).get('sha256');relative=name
        if old_hash is None or new_hash is None or old_hash==new_hash:continue
        if any(j not in {from_journal,to_journal} and name in change
                and change[name][1]==new_hash and change[name][0] not in {old_hash,new_hash}
                for j,change in transactions.items()):
            raise ValueError('所选区间跨过了已有原始事务，请把修订终点定位到该事务的前像。')
        raw=read_bytes(root,relative)
        if digest(raw)!=new_hash:raise ValueError('待审核修订内容已偏离核验来源。')
        targets.append({'relative':name,'before_sha256':old_hash,'after_sha256':new_hash,
                        'after_text':raw.decode('utf-8')})
    request={'schema_version':VERSION,'book_id':book_id,'kind':'observed_external_edit',
             'from_journal':from_journal,'to_journal':to_journal,'reason':reason,'reviewer':reviewer,'targets':targets,
             'source_inventory_sha256':tree['inventory_sha256']}
    if supersedes:request['supersedes']=supersedes
    request['preview_sha256']=digest(request)
    return request


def confirm_observed_revision(preview, *, expected_sha256, book_id, transactions, now=None):
    if (preview.get('preview_sha256')!=expected_sha256
            or expected_sha256!=digest({k:v for k,v in preview.items() if k!='preview_sha256'})):
        raise ValueError('待审核修订已变化。')
    value={k:v for k,v in preview.items() if k!='preview_sha256'}
    value.update(id='revision_'+expected_sha256[:32],author_confirmed=True,
                 recorded_at=now or datetime.now(timezone.utc).isoformat())
    value['record_sha256']=digest(value)
    validate_revision(value,book_id=book_id,transactions=transactions)
    return value
