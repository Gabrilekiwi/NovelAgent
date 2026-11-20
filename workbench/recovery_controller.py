"""Original-session control after a restored source relinquishes write authority.

This is a read fence over existing session and machine evidence. It creates no
permission, repairs no receipt, and never makes an ordinary database writable.
"""
from contextlib import closing
import os
from pathlib import Path
import re


def _current_belongs(handoff, current, directory):
    from workbench.backup_restore_roots import _read
    from workbench.book_ownership import OWNERSHIP_V3
    from workbench.restore_handoff import _signed, rollback_decision
    from workbench.restore_qualification import activation_receipt
    from workbench.restore_reinstatement import reinstatement_intent, reinstatement_name, reinstatement_receipt
    prefix='handoff-'+handoff['handoff_id']+'-'
    if current==handoff['restoring_record']:
        return True
    rollback=rollback_decision(handoff)
    if current==rollback['record']:
        renewal=reinstatement_intent(handoff)
        if (_read(directory,prefix+'rollback.json')!=rollback
                or _read(directory,reinstatement_name(current))!=renewal):
            return False
        done=directory/reinstatement_name(current,done=True)
        return not os.path.lexists(done) or _read(directory,done.name)==reinstatement_receipt(renewal)
    if current is None or current['version']!=OWNERSHIP_V3:
        return False
    intent=_read(directory,prefix+'activation.json')
    q=intent['qualification']
    expected=_signed({**handoff['restoring_record'],'version':OWNERSHIP_V3,
        'generation':handoff['restoring_record']['generation']+1,'state':'active',
        'restore_qualification_sha256':q['qualification_sha256']})
    if (intent!=_signed(intent,'intent_sha256')
            or intent['schema_version']!='restored_write_activation_intent_v1'
            or q!=_signed(q,'qualification_sha256')
            or q['handoff_id']!=handoff['handoff_id'] or q['handoff_intent_sha256']!=handoff['intent_sha256']
            or q['plan_sha256']!=handoff['plan_sha256'] or q['book_id']!=handoff['source_record']['binding']['book_id']
            or q['database']!=handoff['restoring_record']['binding']['database']
            or q['book_root']!=handoff['restoring_record']['binding']['root']
            or intent['active_record']!=expected or current!=expected):
        return False
    done=directory/(prefix+'activation-done.json')
    return not os.path.lexists(done) or _read(directory,done.name)==activation_receipt(intent)


def load_recovery_controller(scope, *, machine_store=None):
    """Return only the original physical controller's single matching session."""
    from workbench.backup_archive import _lineage
    from workbench.backup_restore_roots import _read
    from workbench.book_ownership import _machine_gate, _record, digest
    from workbench.recovery_sessions import load_session_record
    from workbench.restore_handoff import validate_handoff_intent, verify_qualified_source_evidence
    from workbench.restore_qualification import machine_store_for
    from workbench.restore_scope import RestoreScopeError, _connection

    controller=Path(scope['database']).parent
    root=controller/'recovery-sessions'
    if not os.path.lexists(root):
        return None
    _lineage(root)
    sessions=sorted(root.iterdir())
    if len(sessions)>1000:
        raise RestoreScopeError('原恢复控制会话超过支持数量。')
    result=None
    store=machine_store_for(scope['book_id'],machine_store)
    directory=store/digest(scope['book_id'].encode('utf-8'))
    with _machine_gate(scope['book_id'],store,wait_timeout=2.0):
        current=_record(directory,scope['book_id'])
        for path in sessions:
            if re.fullmatch('restore_[0-9a-f]{32}',path.name) is None:
                continue
            intent,state=load_session_record(controller,path.name)
            plan=intent['plan']; proof=plan.get('source_restore_provenance')
            if proof is None or proof['scope']!=scope:
                continue
            handoff=state['receipts'].get('preview_handoff')
            if handoff is None or state['phase'] in {'planned','materialized','roots_bound','readonly_ready','rolled_back','cancelled'}:
                continue
            if state['phase']=='handoff_ready' and state['pending_action'] not in {'handoff','rollback'}:
                continue
            validate_handoff_intent(handoff,scope['book_id'])
            source_proof=handoff.get('source_restore_provenance')
            archived_proof=handoff.get('archived_source_restore_provenance',source_proof)
            if (archived_proof!=proof
                    or handoff['plan_sha256']!=plan['plan_sha256']
                    or handoff['target']!=plan['root_plan']['container']
                    or source_proof is None or handoff['source_record']!=source_proof['active_record']):
                raise RestoreScopeError('原恢复会话与原来源交接证明不同。')
            if current==handoff['source_record']:
                continue  # Nothing was revoked. Damaged activation cannot use this path.
            prefix='handoff-'+handoff['handoff_id']+'-'
            if (_read(directory,prefix+'intent.json')!=handoff
                    or not _current_belongs(handoff,current,directory)):
                raise RestoreScopeError('机器登记已不属于原恢复控制会话。')
            with closing(_connection(Path(scope['database']))) as connection:
                connection.execute('BEGIN')
                verify_qualified_source_evidence(handoff,connection,directory)
            candidate={'session_id':path.name,'operation_id':intent['operation_id'],
                'intent_sha256':intent['intent_sha256'],'handoff_id':handoff['handoff_id'],
                'destination':handoff['target'],'project_id':scope['project_id'],
                'automatic_provider_calls_allowed':False}
            if result is not None and result!=candidate:
                raise RestoreScopeError('原恢复控制会话不明确，拒绝自动选择。')
            result=candidate
    return result
