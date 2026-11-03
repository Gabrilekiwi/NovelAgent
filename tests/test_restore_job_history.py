"""Bounded offline history checking, including three genuine SQL transitions."""
import copy
import json
import sqlite3

import pytest

from core.story_project.reviewed_state_document import digest
from workbench.backup_restore_provenance import validate_restored_database, RestoreProvenanceError
from workbench.restore_handoff import _signed
from workbench.restore_job_history import (FIRST_TABLE, GENERATION_TABLE, INVALIDATED_CONTRACT,
    validate_retained_tables, validate_job_intent, RestoreJobHistoryError)
from workbench.restore_jobs import _apply_job_intent, _job_history


def transition(connection, previous=None):
    generation = previous['scope'].get('scope_generation', 1)+1 if previous else 1
    scope = {'schema_version':'restored_workbench_scope_v1' if generation == 1 else 'restored_workbench_scope_v2',
        'record_sha256':'scope-'+str(generation)}
    fields = {}
    if previous:
        scope.update(scope_generation=generation, predecessor_scope=previous['scope'],
            source_provenance_sha256=previous['provenance_sha256'])
        fields = {'scope_generation':generation, 'source_provenance_sha256':previous['provenance_sha256']}
    intent = _signed({'schema_version':'restore_job_preparation_v1' if generation == 1 else 'restore_job_preparation_v2',
        'handoff_id':'handoff-'+str(generation), 'created_at':'2026-10-01T00:00:0'+str(generation)+'+00:00',
        'history':_job_history(connection), 'invalidated_contract':INVALIDATED_CONTRACT, **fields}, 'intent_sha256')
    connection.execute('BEGIN IMMEDIATE')
    _apply_job_intent(connection,intent)
    connection.execute('UPDATE workbench_restore_scope SET document_json=?',(json.dumps(scope),))
    connection.commit()
    return _signed({'schema_version':'restored_backup_provenance_v1', 'scope':scope,
        'jobs_intent':intent, 'plan':{'source_restore_provenance':previous} if previous else {}}, 'provenance_sha256')


@pytest.fixture
def chain():
    connection = sqlite3.connect(':memory:',isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute('CREATE TABLE jobs (id TEXT PRIMARY KEY, execution_contract TEXT, state TEXT, '
        'updated_at TEXT, revision INTEGER, config_json TEXT, result_json TEXT, error_json TEXT)')
    connection.execute('CREATE TABLE workbench_restore_scope (singleton INTEGER PRIMARY KEY, document_json TEXT)')
    connection.execute("INSERT INTO workbench_restore_scope VALUES (1,'{}')")
    connection.execute("INSERT INTO jobs VALUES ('job-a','original','running','original-time',4,'config','result','error')")
    first = transition(connection)
    second = transition(connection,first)
    connection.execute("INSERT INTO jobs VALUES ('job-b','new-after-second','queued','new-time',0,'new-config',NULL,NULL)")
    third = transition(connection,second)
    yield connection, first, second, third
    connection.close()


def test_three_sql_generations_keep_distinct_snapshots_and_classify_all_bound_tables(chain):
    connection,first,second,third = chain
    tables = validate_retained_tables(connection,third)
    assert json.loads(tables[FIRST_TABLE][0]['original_row_json'])['revision'] == 4
    assert [(row['scope_generation'],row['job_id']) for row in tables[GENERATION_TABLE]] == [
        (2,'job-a'),(3,'job-a'),(3,'job-b')]
    assert [json.loads(row['original_row_json'])['revision'] for row in tables[GENERATION_TABLE]] == [5,6,0]
    result = validate_restored_database(connection,third)
    assert result['original_job_count'] == 2 and result['classified_tables'] == sorted([
        'workbench_restore_scope',FIRST_TABLE,GENERATION_TABLE])
    assert dict(connection.execute("SELECT * FROM jobs WHERE id='job-b'").fetchone())['state'] == 'cancelled'


@pytest.mark.parametrize('damage', ['first-row','prior-row','new-row','row-hash','extra-generation',
    'missing-generation','current-contract','current-result','current-delete','primary-key'])
def test_offline_three_generation_checker_rejects_history_and_current_job_damage(chain,damage):
    connection,first,second,third = chain
    if damage == 'first-row': connection.execute('UPDATE '+FIRST_TABLE+" SET original_row_json='{}'")
    elif damage in {'prior-row','new-row'}:
        generation = 2 if damage == 'prior-row' else 3
        connection.execute('UPDATE '+GENERATION_TABLE+" SET original_row_json='{}' WHERE scope_generation=?",(generation,))
    elif damage == 'row-hash': connection.execute('UPDATE '+GENERATION_TABLE+" SET row_sha256='changed'")
    elif damage == 'extra-generation':
        connection.execute('INSERT INTO '+GENERATION_TABLE+' SELECT 4,job_id,handoff_id,original_row_json,row_sha256 FROM '
            +GENERATION_TABLE+' WHERE scope_generation=2')
    elif damage == 'missing-generation': connection.execute('DELETE FROM '+GENERATION_TABLE+' WHERE scope_generation=2')
    elif damage == 'current-contract': connection.execute("UPDATE jobs SET execution_contract='original',state='queued'")
    elif damage == 'current-result': connection.execute("UPDATE jobs SET result_json='changed'")
    elif damage == 'current-delete': connection.execute("DELETE FROM jobs WHERE id='job-a'")
    else:
        connection.execute('ALTER TABLE '+GENERATION_TABLE+' RENAME TO old_history')
        connection.execute('CREATE TABLE '+GENERATION_TABLE+' AS SELECT * FROM old_history')
    with pytest.raises(RestoreProvenanceError): validate_restored_database(connection,third)


@pytest.mark.parametrize('damage', ['generation','source-hash','ancestor','snapshot','duplicate','prior-reactivated'])
def test_rehashed_job_intent_cannot_change_generation_or_ancestor_contract(chain,damage):
    connection,first,second,third = chain
    scope = copy.deepcopy(third['scope']); intent = copy.deepcopy(third['jobs_intent'])
    if damage == 'generation': intent['scope_generation'] += 1
    elif damage == 'source-hash': intent['source_provenance_sha256'] = 'another-source'
    elif damage == 'ancestor': scope['predecessor_scope'] = first['scope']
    elif damage == 'snapshot': intent['history'][0]['original_sha256'] = 'changed'
    elif damage == 'duplicate': intent['history'].append(copy.deepcopy(intent['history'][0]))
    else:
        original = intent['history'][0]['original']; original['execution_contract'] = 'reactivated'
        intent['history'][0]['original_sha256'] = digest(original)
    intent = _signed({k:v for k,v in intent.items() if k != 'intent_sha256'},'intent_sha256')
    with pytest.raises(RestoreJobHistoryError): validate_job_intent(intent,scope,second)


def test_first_generation_cannot_classify_unbound_even_empty_later_history(chain):
    connection,first,second,third = chain
    connection.execute('DELETE FROM '+GENERATION_TABLE)
    with pytest.raises(RestoreJobHistoryError): validate_retained_tables(connection,first)
