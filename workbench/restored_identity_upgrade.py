"""Extend immutable restore qualification with one actual format upgrade.

Offline validation opens no ancestral path. Live qualification additionally
requires the original machine intent/completion and actual upgrade receipt.
"""
from contextlib import closing
from pathlib import Path
import sqlite3

from core.engine.persistence import atomic_create_json
from workbench.backup_restore_roots import _read
from workbench.restore_handoff import RestoreHandoffError, _signed


PROTOCOL = 'restored_qualification_identity_upgrade_v1'


def renewal_intent(upgrade, evidence):
    from workbench.registered_identity_upgrade import identity_upgrade_completion
    return _signed({'schema_version':PROTOCOL,'upgrade_intent':upgrade,
        'upgrade_done':identity_upgrade_completion(upgrade,evidence['publication_receipt']),
        'publication_evidence':evidence},'intent_sha256')


def renewal_receipt(intent):
    record=intent['upgrade_intent']['active_record']
    return _signed({'schema_version':PROTOCOL+'_completed','intent_sha256':intent['intent_sha256'],
        'record_sha256':record['record_sha256'],
        'qualification_sha256':record['restore_qualification_sha256'],
        'upgrade_receipt_sha256':intent['upgrade_done']['receipt_sha256'],
        'automatic_provider_calls_allowed':False},'receipt_sha256')


def validate_upgrade_renewal(item, record, base, qualification, previous, *, depth=0):
    from workbench.registered_identity_upgrade import validate_identity_upgrade_publication,RESTORED_PROTOCOL
    try:
        intent=item['intent']
        if (set(intent)!={'schema_version','upgrade_intent','upgrade_done','publication_evidence','intent_sha256'}
                or intent['schema_version']!=PROTOCOL or intent!=_signed(intent,'intent_sha256')):
            raise ValueError('qualified upgrade envelope changed')
        upgrade=intent['upgrade_intent']; active=upgrade['active_record']
        validate_identity_upgrade_publication(upgrade,intent['publication_evidence'],intent['upgrade_done'],depth=depth+1)
        proof=upgrade.get('source_restore_provenance')
        if (upgrade['schema_version']!=RESTORED_PROTOCOL or upgrade['source_record']!=record
                or proof['qualification']!=qualification or proof['activation_intent']['active_record']!=base
                or proof.get('qualification_reinstatements',[])!=previous):
            raise ValueError('qualified upgrade does not extend this exact qualification prefix')
        if item['done']!=renewal_receipt(intent):
            raise ValueError('qualified upgrade publication or completion differs')
        return active
    except (OSError,ValueError,KeyError,TypeError,AttributeError,RuntimeError) as exc:
        raise RestoreHandoffError('恢复资格的格式升级续接证明无法核验。') from exc


def require_original_qualification(upgrade,directory):
    """Read original evidence even while the machine record is fenced."""
    from workbench.restore_scope import _stored,_sidecar
    from workbench.backup_restore_provenance import validate_restored_database
    from workbench.restore_reinstatement import load_reinstatements
    from workbench.book_ownership import _require_witness
    proof=upgrade['source_restore_provenance']; source=upgrade['source_record']
    database=Path(source['binding']['database']);root=Path(source['binding']['root'])
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as connection:
        connection.row_factory=sqlite3.Row;connection.execute('BEGIN')
        if _stored(connection)!=proof['scope'] or _read(database.parent,_sidecar(database).name)!=proof['scope']:
            raise RestoreHandoffError('升级期间原恢复范围变化。')
        validate_restored_database(connection,proof)
    local={'qualification':'write-qualification.json','plan':'restore-plan.json',
        'materialization':'restore-materialization.json','root_intent':'root-binding.json',
        'root_done':'root-binding-done.json','database_intent':'database-binding.json',
        'database_done':'database-binding-done.json','execution_policy':'execution-policy.json'}
    machine={'activation_intent':'activation','activation_done':'activation-done','jobs_intent':'jobs',
        'jobs_done':'jobs-done','budget_done':'budget-done','execution_intent':'executions','execution_done':'executions-done'}
    prefix='handoff-'+proof['qualification']['handoff_id']+'-'
    for key,name in local.items():
        if _read(database.parent,name)!=proof[key]:
            raise RestoreHandoffError('升级所依赖的原本地恢复凭据缺失或变化。')
    for key,suffix in machine.items():
        if _read(directory,prefix+suffix+'.json')!=proof[key]:
            raise RestoreHandoffError('升级所依赖的原机器恢复凭据缺失或变化。')
    if load_reinstatements(directory,proof['activation_intent']['active_record'],proof['qualification'],source)!=proof.get('qualification_reinstatements',[]):
        raise RestoreHandoffError('升级所依赖的原恢复续接链变化。')
    _require_witness(root,source['binding']['book_id'],source)


def verify_upgrade_files(directory,intent,done):
    from workbench.registered_identity_upgrade import (_read_intent,_name,_completed,
        identity_upgrade_publication_evidence)
    upgrade=intent['upgrade_intent'];identity=upgrade['active_record']['binding']['identity_sha256']
    if (_read_intent(directory,identity)!=upgrade or _read(directory,_name(identity,'done'))!=intent['upgrade_done']
            or _completed(upgrade)!=intent['upgrade_done']
            or identity_upgrade_publication_evidence(upgrade)!=intent['publication_evidence']
            or renewal_receipt(intent)!=done):
        raise RestoreHandoffError('恢复格式升级续接与实际原事务或完成凭据不同。')
    require_original_qualification(upgrade,directory)
    return upgrade['source_record']


def publish_qualified_upgrade(upgrade,directory,*,fault):
    from workbench.registered_identity_upgrade import identity_upgrade_publication_evidence
    from workbench.restore_reinstatement import reinstatement_name
    evidence=identity_upgrade_publication_evidence(upgrade)
    intent=renewal_intent(upgrade,evidence);done=renewal_receipt(intent)
    proof=upgrade['source_restore_provenance']
    validate_upgrade_renewal({'intent':intent,'done':done},upgrade['source_record'],
        proof['activation_intent']['active_record'],proof['qualification'],proof.get('qualification_reinstatements',[]))
    require_original_qualification(upgrade,directory)
    for value,completed,event in ((intent,False,'after_qualified_upgrade_intent'),(done,True,'after_qualified_upgrade_done')):
        path=directory/reinstatement_name(upgrade['active_record'],done=completed)
        if path.exists():
            if _read(directory,path.name)!=value:
                raise RestoreHandoffError('原恢复升级续接记录变化。')
        else:
            atomic_create_json(path,value);fault(event)
