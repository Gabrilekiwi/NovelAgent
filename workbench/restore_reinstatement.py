"""Proof-backed renewal of an existing restore qualification after rollback.

The original local qualification and activation receipts stay immutable. A
new machine generation becomes usable only with its own completed renewal.
Recorded chains can be verified offline without opening any previous copy.
"""
from workbench.backup_restore_roots import _read
from workbench.restore_handoff import (RestoreHandoffError, _signed,
    validate_handoff_intent, rollback_decision)


def reinstatement_intent(handoff):
    return _signed({'schema_version':'restored_qualification_reinstatement_v1',
        'handoff_intent':handoff,'rollback_decision':rollback_decision(handoff)},'intent_sha256')


def reinstatement_receipt(intent):
    record=intent['rollback_decision']['record']
    return _signed({'schema_version':'restored_qualification_reinstated_v1',
        'intent_sha256':intent['intent_sha256'],'record_sha256':record['record_sha256'],
        'qualification_sha256':record['restore_qualification_sha256'],
        'automatic_provider_calls_allowed':False},'receipt_sha256')


def reinstatement_name(record, *, done=False):
    return 'qualification-'+record['record_sha256']+'-reinstatement'+('-done' if done else '')+'.json'


def validate_reinstatements(base_record, qualification, chain, *, depth=0):
    """Return the final record only for the complete, ordered recorded chain."""
    import json
    from workbench.book_ownership import OWNERSHIP_V3
    try:
        if (depth > 16 or not isinstance(chain,list) or len(chain)>16
                or len(json.dumps(chain,ensure_ascii=False).encode('utf-8')) > 32*1024**2):
            raise ValueError('renewal chain exceeds supported size')
        record=base_record
        for index,item in enumerate(chain):
            if not isinstance(item,dict) or set(item)!={'intent','done'}:
                raise ValueError('invalid renewal evidence')
            intent=item['intent']
            from workbench.restored_identity_upgrade import PROTOCOL as UPGRADE,validate_upgrade_renewal
            if intent.get('schema_version')==UPGRADE:
                record=validate_upgrade_renewal(item,record,base_record,qualification,chain[:index],depth=depth)
                continue
            if (set(intent)!={'schema_version','handoff_intent','rollback_decision','intent_sha256'}
                    or intent.get('schema_version')!='restored_qualification_reinstatement_v1'
                    or intent!=_signed(intent,'intent_sha256')):
                raise ValueError('invalid renewal intent')
            handoff=validate_handoff_intent(intent['handoff_intent'],qualification['book_id'],depth=depth+1)
            proof=handoff.get('source_restore_provenance')
            if (handoff['source_record']!=record or record['version']!=OWNERSHIP_V3
                    or record['restore_qualification_sha256']!=qualification['qualification_sha256']
                    or proof is None or proof['qualification']!=qualification
                    or proof['activation_intent']['active_record']!=base_record
                    or proof.get('qualification_reinstatements',[])!=chain[:index]
                    or intent['rollback_decision']!=rollback_decision(handoff)
                    or item['done']!=reinstatement_receipt(intent)):
                raise ValueError('renewal chain bindings differ')
            record=intent['rollback_decision']['record']
        return record
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise RestoreHandoffError('恢复资格续接链无法核验。') from exc


def load_reinstatements(directory, base_record, qualification, record):
    """Read only completed machine-side renewals, each tied to its operation."""
    chain=[]; current=record
    while current!=base_record:
        if len(chain)>=16:
            raise RestoreHandoffError('恢复资格续接链超过支持代数。')
        intent=_read(directory,reinstatement_name(current))
        done=_read(directory,reinstatement_name(current,done=True))
        try:
            from workbench.restored_identity_upgrade import PROTOCOL as UPGRADE,verify_upgrade_files
            if intent.get('schema_version')==UPGRADE:
                previous=verify_upgrade_files(directory,intent,done)
                if intent['upgrade_intent']['active_record']!=current:
                    raise RestoreHandoffError('恢复升级续接不属于当前机器记录。')
                chain.append({'intent':intent,'done':done});current=previous
                continue
            handoff=intent['handoff_intent']; handoff_id=handoff['handoff_id']
            if (intent['rollback_decision']['record']!=current
                    or _read(directory,'handoff-'+handoff_id+'-intent.json')!=handoff
                    or _read(directory,'handoff-'+handoff_id+'-rollback.json')!=intent['rollback_decision']):
                raise RestoreHandoffError('资格续接与原交接或回退决议不同。')
            chain.append({'intent':intent,'done':done})
            current=handoff['source_record']
        except (KeyError, TypeError, AttributeError) as exc:
            raise RestoreHandoffError('恢复资格续接的必要证据缺失。') from exc
    chain.reverse()
    if validate_reinstatements(base_record,qualification,chain)!=record:
        raise RestoreHandoffError('恢复资格续接未到达当前机器记录。')
    return chain
