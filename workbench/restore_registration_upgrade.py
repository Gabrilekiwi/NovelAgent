"""Actual original-registration upgrade and rollback ancestry for handoff v5.

One supported legacy-to-reviewed upgrade changes the identity binding. Exact
ordered rollback evidence connects archives on either side of that upgrade.
Live checks require original machine files and actual transaction publication.
"""
import json

from workbench.book_ownership import OWNERSHIP_V2
from workbench.restore_handoff import RestoreHandoffError
from workbench.restore_rollback_ancestry import (validate_rollback_ancestry,
    verify_rollback_ancestry_files,load_rollback_ancestry,_same_registration)


def _chain(before,after,items,book_id,*,depth):
    if before==after:
        _same_registration(before,after,book_id)
        if items!=[]:raise RestoreHandoffError('相同机器记录不能附加未知回退。')
    else:
        validate_rollback_ancestry(before,after,items,book_id,depth=depth)


def validate_registration_upgrade_proof(archived,source,proof,book_id,*,depth=0):
    from workbench.registered_identity_upgrade import PROTOCOL,validate_identity_upgrade_publication
    try:
        if (depth>16 or not isinstance(proof,dict) or set(proof)!={
                'upgrade_intent','upgrade_done','publication_evidence','pre_upgrade_rollbacks','post_upgrade_rollbacks'}
                or len(json.dumps(proof,ensure_ascii=False).encode('utf-8'))>32*1024**2):
            raise ValueError('unsupported registration upgrade proof')
        intent=proof['upgrade_intent'];before=intent['source_record'];active=intent['active_record']
        if intent['schema_version']!=PROTOCOL or source['version']!=OWNERSHIP_V2:
            raise ValueError('original registration proof required')
        validate_identity_upgrade_publication(intent,proof['publication_evidence'],proof['upgrade_done'],depth=depth+1)
        if before['binding']['book_id']!=book_id:
            raise ValueError('upgrade belongs to another book')
        pre=proof['pre_upgrade_rollbacks'];post=proof['post_upgrade_rollbacks']
        if not isinstance(pre,list) or not isinstance(post,list) or len(pre)+len(post)+1>16:
            raise ValueError('upgrade ancestry exceeds supported size')
        _chain(active,source,post,book_id,depth=depth+1)
        if archived['binding']['identity_sha256']==before['binding']['identity_sha256']:
            _chain(archived,before,pre,book_id,depth=depth+1)
        else:
            if pre or archived not in [active,*[item['rollback_decision']['record'] for item in post]]:
                raise ValueError('archive is not on the exact completed post-upgrade chain')
        return proof
    except (OSError,ValueError,KeyError,TypeError,AttributeError,RuntimeError) as exc:
        raise RestoreHandoffError('原登记归档的升级及回退证明无法核验。') from exc


def verify_registration_upgrade_files(directory,archived,source,proof,book_id,*,depth=0):
    from workbench.registered_identity_upgrade import (_read_intent,_name,_completed,
        identity_upgrade_publication_evidence)
    from workbench.backup_restore_roots import _read
    validate_registration_upgrade_proof(archived,source,proof,book_id,depth=depth)
    try:
        intent=proof['upgrade_intent'];identity=intent['active_record']['binding']['identity_sha256']
        if (_read_intent(directory,identity)!=intent or _read(directory,_name(identity,'done'))!=proof['upgrade_done']
                or _completed(intent)!=proof['upgrade_done']
                or identity_upgrade_publication_evidence(intent)!=proof['publication_evidence']):
            raise RestoreHandoffError('原登记升级证明与实际原文件不同。')
        before=intent['source_record'];active=intent['active_record']
        pre=proof['pre_upgrade_rollbacks'];post=proof['post_upgrade_rollbacks']
        if pre:verify_rollback_ancestry_files(directory,archived,before,pre,book_id,depth=depth+1)
        if post:verify_rollback_ancestry_files(directory,active,source,post,book_id,depth=depth+1)
        return proof
    except (OSError,ValueError,KeyError,TypeError,RuntimeError) as exc:
        raise RestoreHandoffError('原登记升级或回退的实际完成凭据缺失或变化。') from exc


def load_registration_upgrade_proof(directory,archived,source,book_id):
    from workbench.registered_identity_upgrade import (_read_intent,_completed,
        identity_upgrade_publication_evidence)
    try:
        intent=_read_intent(directory,source['binding']['identity_sha256'])
        before=intent['source_record'];active=intent['active_record']
        pre=[]
        if archived['binding']['identity_sha256']==before['binding']['identity_sha256'] and archived!=before:
            pre=load_rollback_ancestry(directory,archived,before,book_id)
        post=[] if active==source else load_rollback_ancestry(directory,active,source,book_id)
        proof={'upgrade_intent':intent,'upgrade_done':_completed(intent),
            'publication_evidence':identity_upgrade_publication_evidence(intent),
            'pre_upgrade_rollbacks':pre,'post_upgrade_rollbacks':post}
        return verify_registration_upgrade_files(directory,archived,source,proof,book_id)
    except (OSError,ValueError,KeyError,TypeError,RuntimeError) as exc:
        raise RestoreHandoffError('原归档没有完整的原登记升级和回退证明。') from exc
