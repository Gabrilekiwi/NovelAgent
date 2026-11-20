"""Verify immutable stage receipts and their real authorizations/fences/markers."""
import re

from core.autonomy.common import canonical_hash
from core.autonomy.receipts import validate_stage_lease_fence,_validate_autonomy_stage_chain
from core.engine.recovery_protocol import validate_marker_envelope,MARKER_RECOVERY_PROTOCOL
from core.stage_control import (validate_stage_receipt,validate_stage_authorization,
    validate_stage_receipt_chain,assert_receipt_matches_authorization)
from workbench.backup_autonomy_controls import path


_FINAL=re.compile(r'[0-9]{4}-[0-9a-f]{20}\.json')


def verify_stage_history(evidence,root,book_id,leases,sessions,block,model_receipts=None):
    receipts={};groups=[]
    for session_dir in evidence.entries(path(root,'stage_receipts')):
        if session_dir not in evidence.directories:raise ValueError('stage session is not a directory')
        for directory in evidence.entries(session_dir):
            if directory not in evidence.directories:raise ValueError('stage chapter is not a directory')
            names=[name for name in evidence.entries(directory) if _FINAL.fullmatch(name.rsplit('/',1)[-1])]
            if not names:raise ValueError('stage chapter has no completed receipt')
            chain=[];previous_generation=0
            for sequence,name in enumerate(names,1):
                receipt=validate_stage_receipt(evidence.read(name))
                session=receipt['session_id'];chapter=receipt['chapter_index'];identifier=receipt['receipt_hash']
                if (receipt['book_id']!=book_id
                        or session_dir!=path(root,'stage_receipts',canonical_hash({'session_id':session})[:16])
                        or directory!=path(session_dir,f'chapter-{chapter:06d}')
                        or name!=path(directory,f'{sequence:04d}-{identifier[:20]}.json')):
                    raise ValueError('stage store scope or receipt sequence differs')
                genesis=sessions.get(session)
                if genesis is not None and genesis['plan_id']!=receipt['plan_id']:
                    raise ValueError('stage receipt belongs to another session plan')
                authorization=validate_stage_authorization(evidence.read(path(directory,'authorizations',
                    receipt['authorization_hash'][:20]+'.json')))
                assert_receipt_matches_authorization(receipt,authorization)
                fence=validate_stage_lease_fence(evidence.read(path(directory,'fences',identifier[:20]+'.json')))
                for key in ('book_id','session_id','plan_id','chapter_index','receipt_hash','authorization_hash'):
                    if fence[key]!=receipt[key]:raise ValueError('stage fence scope differs')
                lease=leases.get(fence['lease_hash'])
                if (lease is None or lease['status']!='active' or lease['generation']!=fence['lease_generation']
                        or any(lease[key]!=receipt[key] for key in ('book_id','session_id','plan_id'))
                        or lease['generation']<previous_generation):
                    raise ValueError('stage lease history is missing or regresses')
                previous_generation=lease['generation']
                staged=path(directory,'staged',identifier+'.json');marker=path(directory,'commit_markers',identifier+'.json')
                if (receipt.get('recovery_protocol')==MARKER_RECOVERY_PROTOCOL
                        or staged in evidence.files or marker in evidence.files):
                    if evidence.read(staged)!=receipt:raise ValueError('stage final/staged receipts differ')
                    committed=validate_marker_envelope(evidence.read(marker))
                    expected={'session_id':session,'chapter_index':chapter,'sequence':sequence,
                        'authorization_hash':receipt['authorization_hash'],'target_name':name.rsplit('/',1)[-1]}
                    if (committed['evidence_kind']!='stage_receipt' or committed['evidence_hash']!=identifier
                            or committed['transaction_id']!='stage_'+identifier
                            or committed['intent_hash']!=receipt['authorization_hash'] or committed['metadata']!=expected):
                        raise ValueError('stage commit marker does not bind its exact receipt')
                hashes=receipt.get('model_call_receipt_hashes',[])
                if receipt.get('model_call_receipt_hash') is not None:
                    hashes=[*hashes,receipt['model_call_receipt_hash']]
                if hashes:
                    actual=model_receipts() if model_receipts is not None else {}
                    if not set(hashes).issubset(actual):
                        block('autonomy_stage_model_receipt_unverified',name)
                    elif any(actual[item].get('budget_accounting',{}).get('chapter') not in (None,chapter) for item in hashes):
                        block('autonomy_stage_model_chapter_mismatch',name)
                if receipt['status']=='provider_call_uncertain':block('autonomy_stage_provider_uncertain',name)
                chain.append(receipt);receipts[identifier]=receipt
            validate_stage_receipt_chain(chain);_validate_autonomy_stage_chain(chain)
            groups.append({'session_id':chain[0]['session_id'],'chapter':chain[0]['chapter_index'],
                'receipts':[item['receipt_hash'] for item in chain]})
    return receipts,groups
