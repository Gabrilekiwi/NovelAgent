"""Immutable event snapshots in actual SQL, including additive old compatibility."""
import copy
import json
import sqlite3

import pytest

from core.story_project.reviewed_state_document import digest
from tests.test_restore_job_history import transition
from workbench.restore_handoff import _signed
from workbench.restore_job_history import validate_job_intent, validate_retained_events, RestoreJobHistoryError
from workbench.restore_jobs import _event_history


def signed(intent):return _signed({k:v for k,v in intent.items() if k!='intent_sha256'},'intent_sha256')


@pytest.fixture
def events():
    connection=sqlite3.connect(':memory:',isolation_level=None);connection.row_factory=sqlite3.Row
    connection.execute('CREATE TABLE jobs (id TEXT PRIMARY KEY,execution_contract TEXT,state TEXT,updated_at TEXT,'
        'revision INTEGER,config_json TEXT,result_json TEXT,error_json TEXT)')
    connection.execute('CREATE TABLE job_events (id INTEGER PRIMARY KEY,job_id TEXT,sequence INTEGER,'
        'event_type TEXT,payload_json TEXT,created_at TEXT)')
    connection.execute('CREATE TABLE workbench_restore_scope (singleton INTEGER PRIMARY KEY, document_json TEXT)')
    connection.execute("INSERT INTO workbench_restore_scope VALUES (1,'{}')")
    connection.execute("INSERT INTO jobs VALUES ('job-a','old','completed','old-time',1,'{}','{}',NULL)")
    connection.execute("INSERT INTO job_events VALUES (1,'job-a',1,'job.created','{}','old-time')")
    connection.execute("INSERT INTO job_events VALUES (2,'job-a',2,'job.completed','{\"path\":\"original\"}','old-time')")
    first=transition(connection);intent=first['jobs_intent']
    intent.update(event_history=_event_history(connection),event_history_sha256=digest(_event_history(connection)))
    first['jobs_intent']=signed(intent);first=_signed({k:v for k,v in first.items() if k!='provenance_sha256'},'provenance_sha256')
    yield connection,first
    connection.close()


def test_imported_events_keep_all_original_fields_and_allow_only_new_job_events(events):
    connection,first=events
    assert validate_job_intent(first['jobs_intent'],first['scope'],None)==1
    validate_retained_events(connection,first['jobs_intent'])
    connection.execute("INSERT INTO jobs VALUES ('job-b','fresh','queued','new-time',0,'{}',NULL,NULL)")
    connection.execute("INSERT INTO job_events VALUES (3,'job-b',1,'job.created','{}','new-time')")
    validate_retained_events(connection,first['jobs_intent'])
    second=transition(connection,first);second['jobs_intent'].update(
        event_history=_event_history(connection),event_history_sha256=digest(_event_history(connection)))
    second['jobs_intent']=signed(second['jobs_intent'])
    assert validate_job_intent(second['jobs_intent'],second['scope'],first)==2
    validate_retained_events(connection,second['jobs_intent'])


@pytest.mark.parametrize('damage',['payload','type','time','identity','sequence','job','delete','append'])
def test_changed_imported_event_is_rejected_from_actual_sql(events,damage):
    connection,first=events
    if damage=='delete':connection.execute('DELETE FROM job_events WHERE id=2')
    elif damage=='append':connection.execute("INSERT INTO job_events VALUES (3,'job-a',3,'job.completed','{}','new-time')")
    else:
        field={'payload':'payload_json','type':'event_type','time':'created_at','identity':'id',
            'sequence':'sequence','job':'job_id'}[damage]
        value=99 if field in {'id','sequence'} else 'changed'
        connection.execute('UPDATE job_events SET '+field+'=? WHERE id=2',(value,))
    with pytest.raises(RestoreJobHistoryError):validate_retained_events(connection,first['jobs_intent'])


@pytest.mark.parametrize('damage',['hash','one-field','shape','boolean-id','boolean-sequence',
    'duplicate-id','duplicate-sequence','wrong-job','wrong-order'])
def test_rehashed_malformed_snapshot_is_rejected(events,damage):
    _,first=events;intent=copy.deepcopy(first['jobs_intent']);rows=intent['event_history']
    if damage=='hash':intent['event_history_sha256']='changed'
    elif damage=='one-field':del intent['event_history_sha256']
    else:
        if damage=='shape':rows[0]['extra']='unexpected'
        elif damage=='boolean-id':rows[0]['id']=True
        elif damage=='boolean-sequence':rows[0]['sequence']=True
        elif damage=='duplicate-id':rows[1]['id']=rows[0]['id']
        elif damage=='duplicate-sequence':rows[1]['sequence']=rows[0]['sequence']
        elif damage=='wrong-job':rows[0]['job_id']='job-unknown'
        elif damage=='wrong-order':rows.reverse()
        intent['event_history_sha256']=digest(rows)
    with pytest.raises(RestoreJobHistoryError):validate_job_intent(signed(intent),first['scope'],None)


@pytest.mark.parametrize('damage',['remove-snapshot','replace','delete','append'])
def test_new_generation_cannot_rehash_away_ancestor_events(events,damage):
    connection,first=events;second=transition(connection,first);intent=second['jobs_intent']
    if damage!='remove-snapshot':
        rows=copy.deepcopy(first['jobs_intent']['event_history'])
        if damage=='replace':rows[0]['payload_json']='changed'
        elif damage=='delete':rows.pop()
        else:rows.append({**rows[-1],'id':3,'sequence':3})
        intent.update(event_history=rows,event_history_sha256=digest(rows))
    with pytest.raises(RestoreJobHistoryError):validate_job_intent(signed(intent),second['scope'],first)


def test_older_completed_intent_without_event_snapshot_keeps_compatibility(events):
    connection,first=events;intent=copy.deepcopy(first['jobs_intent'])
    del intent['event_history'];del intent['event_history_sha256'];intent=signed(intent)
    assert validate_job_intent(intent,first['scope'],None)==1
    validate_retained_events(connection,intent)
