"""Offline auxiliary-history coverage, distinct from control settlement.

Preview-only autonomy roots have no lease/session and may have no local lock.
Discover their exact machine filenames as well as canonical/locked namespaces.
Unknown or not-yet-covered history blocks certification; it is never dropped.
"""
import re
from pathlib import Path,PurePosixPath

from core.story_project.reviewed_state_document import digest
from workbench.backup_archive import BackupArchiveError
from workbench.backup_archive_view import verified_archive_view
from workbench.backup_auxiliary import Evidence,_roots,inspect_auxiliary_settlement
from workbench.backup_delivery_history import verify_delivery_history
from workbench.backup_autonomy_history import verify_autonomy_history


PROTOCOL='archived_auxiliary_history_v1'
_AUTONOMY=('leases','sessions','operations','arc_plans','plans','plan_sessions',
           'stage_receipts','completion_ledgers','outline_checkpoints')
_PLAN_NAME=re.compile(r'plan_[0-9a-f]{24}-[0-9a-f]{16}\.json',re.I)


def inspect_archived_auxiliary_history(package,*,project_id,book_id):
    with verified_archive_view(package) as view:
        preview=view.manifest['preview'];tree=preview['scope']['inventories']['story_project']
        if preview['project_id']!=project_id or preview['book_id']!=book_id:
            raise BackupArchiveError('辅助历史认证目标与归档不同。')
        root=Path(tree['source_root'])
        def read(asked,name):
            if Path(asked)!=root:raise BackupArchiveError('辅助历史读取越过目标范围。')
            return view.read_root('story_project',name)
        evidence=Evidence(root,tree,_read_bytes=read)
        settlement=inspect_auxiliary_settlement(root,tree,book_id,_read_bytes=read)
        blockers=list(settlement['blockers']);histories=[]
        def block(code,reference):
            item={'code':code,'reference':reference}
            if item not in blockers:blockers.append(item)
        models=None
        def model_receipts():
            nonlocal models
            if models is None:
                from workbench.backup_archive_usage import _StoryEvidence,_usage
                usage=_usage(_StoryEvidence(view),book_id,block)
                models={item['receipt_hash']:item for item in usage['items'] if item['receipt_hash'] is not None}
            return models
        committed=None
        def publications():
            nonlocal committed
            if committed is None:
                from workbench.backup_history import verify_persistence_history
                history=verify_persistence_history(root,tree,book_id=book_id,read_bytes=read,
                    provenance=preview['scope'].get('restored_provenance'))
                committed={};reviewed=None
                for transaction in history['transactions']:
                    binding=transaction.get('publication_receipt')
                    if binding is None:continue
                    import json
                    receipt=json.loads(read(root,binding['path']))
                    manifest=json.loads(read(root,transaction['journal']+'/manifest.json'))
                    immutable=manifest['immutable']
                    target=next(item for item in immutable['targets'] if item['target_id']==immutable['final_run']['target_id'])
                    final=json.loads(read(root,transaction['journal']+'/'+target['staged_relative_path']))
                    final=final.get('run',final)
                    prose=[item['sha256'] for item in receipt['apply_targets'] if item['kind']=='prose']
                    chapter=final.get('chapter_index');prose_hash=prose[0] if len(prose)==1 else None
                    if final.get('record_type')=='reviewed_legacy_commit' and final.get('kind')=='chapter':
                        if reviewed is None:
                            from workbench.backup_archive_reviewed import verify_archived_reviewed_state
                            proof=verify_archived_reviewed_state(package,project_id=project_id,book_id=book_id)
                            reviewed={item['receipt_hash']:item for item in proof['chapter_publications']}
                        typed=reviewed.get(receipt['receipt_hash'])
                        if typed is None:raise ValueError('reviewed publication lacks its typed chapter history')
                        chapter=typed['chapter'];prose_hash=typed['prose_hash']
                    if receipt['receipt_hash']!=binding['receipt_hash']:raise ValueError('verified publication binding changed')
                    committed[receipt['receipt_hash']]={'receipt':receipt,'chapter':chapter,'prose_hash':prose_hash}
            return committed
        stores={item['root']:item['kind'] for item in settlement['stores']}
        for name in _roots(evidence,'.novelagent/runtime/autonomy',_AUTONOMY):stores[name]='autonomy'
        for name in tree['files']:
            path=PurePosixPath(name)
            if path.name.casefold()=='.persistence.lock':
                children=evidence.entries(str(path.parent))
                if any(PurePosixPath(child).name.casefold() in _AUTONOMY for child in children):
                    stores[str(path.parent)]='autonomy'
                elif any(PurePosixPath(child).name.casefold() in {'jobs','attempts'} for child in children):
                    stores[str(path.parent)]='delivery'
            if path.parent.name.casefold()=='plans' and _PLAN_NAME.fullmatch(path.name):
                stores[str(path.parent.parent)]='autonomy'
        deliveries=[]
        for store,kind in sorted(stores.items(),key=lambda item:(item[1]!='delivery',item[0])):
            if store=='.':
                block('auxiliary_history_not_certified',store);continue
            prefix=store+'/'
            files={name for name in tree['files'] if name.startswith(prefix)}
            directories={name for name in tree['directories'] if name.startswith(prefix)}
            try:
                if kind=='autonomy':history,used,allowed_dirs=verify_autonomy_history(evidence,store,book_id,block,
                    model_receipts=model_receipts,publications=publications,deliveries=deliveries)
                else:
                    history,used,allowed_dirs=verify_delivery_history(evidence,store,book_id,block)
                    deliveries.extend(history['jobs'])
                allowed_files=used|{name for name in files if name.casefold()==(store+'/.persistence.lock').casefold()}
                allowed_casefold={value.casefold() for value in allowed_dirs}
                unknown=(files-allowed_files)|{name for name in directories if name.casefold() not in allowed_casefold}
                if unknown:block('auxiliary_history_not_certified',min(unknown))
                histories.append(history)
            except (OSError,ValueError,KeyError,TypeError,RuntimeError):
                block('auxiliary_history_unverified',store)
        result={'schema_version':PROTOCOL,'archive_id':view.verified['archive_id'],
            'archive_receipt_sha256':view.verified['receipt_sha256'],
            'project_id':project_id,'book_id':book_id,'histories':histories,
            'blockers':sorted(blockers,key=lambda item:(item['code'],item['reference'])),
            'complete_history_certified':not blockers,'files_read':evidence.read_count,
            'recoverable':False,'write_activation_allowed':False}
        result['verification_sha256']=digest(result)
        return result
