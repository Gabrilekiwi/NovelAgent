"""Real qualified rollback deaths preserve the original multi-step lineage."""
import json
import subprocess
import sys

from tests.test_restore_qualified_rollback_archive import (
    tmp_path, original, case, restored_client, begin_second, unchanged,
)
from tests.test_recovery_sessions import start, advance, step
from workbench.backup_files import inventory_tree
from workbench.book_ownership import BookOwnership
from workbench.restore_handoff import rollback_decision
from workbench.restore_reinstatement import reinstatement_name
from workbench.restore_scope import load_restore_scope


def test_three_actual_rollback_exits_then_original_archive_uses_two_completed_renewals(case):
    c = case; archived = inventory_tree(c['rebackup'])
    qualification = (c['source']/'write-qualification.json').read_bytes()
    with restored_client(c) as (app, client):
        first = advance(client, begin_second(c, client), 'source_revoked')
        response = step(client, first, action='rollback')
        assert response.status_code == 200, response.text
        assert client.post('/api/maintenance/finish', json={'operation_id':first['operation_id']}).status_code == 200
    target = c['target'].parent/'资格中断 恢复副本'
    with restored_client(c) as (app, client):
        second = advance(client, start(client, {**c, 'package':c['rebackup']}, target), 'source_revoked')
        _, state = app.state.recovery_sessions._load(second['session_id'])
        handoff = state['receipts']['preview_handoff']
        assert handoff['schema_version'] == 'machine_restore_handoff_v4'
    owner = BookOwnership(c['project'].book_id, machine_store=c['machine'])
    rolled = rollback_decision(handoff)['record']
    completion = owner.directory/reinstatement_name(rolled, done=True)
    code = '''import os,sys
from workbench.restore_handoff import RestoreHandoff
def fault(event,value):
 if event==sys.argv[4]:os._exit(91)
RestoreHandoff(sys.argv[1],machine_store=sys.argv[2],fault_injector=fault).rollback(handoff_id=sys.argv[3])
'''
    for phase in ('after_reinstatement_intent', 'after_source_reactivated', 'after_reinstatement_completion'):
        child = subprocess.run([sys.executable, '-B', '-c', code, c['project'].book_id,
            str(c['machine']), handoff['handoff_id'], phase], capture_output=True, timeout=180,
            **({'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}))
        assert child.returncode == 91, child.stderr
        if phase != 'after_reinstatement_completion':
            assert not completion.exists()
        else:
            assert completion.exists() and load_restore_scope(c['source']/'workbench.sqlite', machine_store=c['machine']).active
        unchanged(c)
        assert inventory_tree(c['rebackup']) == archived
    with restored_client(c) as (app, client):
        assert client.post('/api/maintenance/resume', json={'operation_id':second['operation_id']}).status_code == 200
        response = step(client, second, action='rollback')
        assert response.status_code == 200, response.text
        assert client.post('/api/maintenance/finish', json={'operation_id':second['operation_id']}).status_code == 200

    final_target = c['target'].parent/'两次回退后 恢复副本'
    with restored_client(c) as (app, client):
        third = advance(client, start(client, {**c, 'package':c['rebackup']}, final_target), 'source_revoked')
        _, state = app.state.recovery_sessions._load(third['session_id'])
        final_handoff = state['receipts']['preview_handoff']
        proof = final_handoff['source_restore_provenance']
        assert len(proof['qualification_reinstatements']) == 2
        assert proof['qualification_reinstatements'][1]['intent']['handoff_intent'] == handoff
        assert final_handoff['archived_source_restore_provenance'] == handoff['archived_source_restore_provenance']
        third = advance(client, third, 'active')
        assert client.post('/api/maintenance/finish', json={'operation_id':third['operation_id']}).status_code == 200
    assert (c['source']/'write-qualification.json').read_bytes() == qualification
    assert inventory_tree(c['rebackup']) == archived
    unchanged(c)
