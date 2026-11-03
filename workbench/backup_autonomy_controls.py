"""Pure validators for autonomy control records, without runtime recovery."""
from pathlib import PurePosixPath

from core.autonomy.common import canonical_hash
from core.autonomy.lease import validate_book_lease
from core.autonomy.operations import (validate_operation_intent,validate_operation_result,
    validate_source_verification,validate_operation_commit_marker)
from core.autonomy.session import validate_session_genesis,validate_session_event,_assert_transition
from core.autonomy.plans import validate_instruction_plan


def path(root,*parts):return str(PurePosixPath(root).joinpath(*parts))


def _leases(evidence,root,book_id,block):
    histories={};count=0
    for directory in evidence.entries(path(root,'leases')):
        if directory not in evidence.directories:raise ValueError('invalid lease entry')
        history=[]
        for name in evidence.entries(path(directory,'history')):
            record=validate_book_lease(evidence.read(name))
            if (record['book_id']!=book_id
                    or directory!=path(root,'leases',canonical_hash({'book_id':book_id})[:16])
                    or name!=path(directory,'history',f"{record['generation']:06d}-{record['lease_hash'][:20]}.json")):
                raise ValueError('lease scope or filename changed')
            history.append(record)
        history.sort(key=lambda r:r['generation'])
        previous=None
        for sequence,record in enumerate(history,1):
            if record['generation']!=sequence or record['previous_lease_hash']!=previous:
                raise ValueError('lease history has a gap or fork')
            histories[record['lease_hash']]=record;previous=record['lease_hash']
        if not history:raise ValueError('lease history missing')
        current_path=path(directory,'current.json')
        if current_path not in evidence.files:
            block('autonomy_lease_head_requires_recovery',current_path)
        else:
            current=validate_book_lease(evidence.read(current_path))
            if current!=history[-1]:block('autonomy_lease_head_requires_recovery',current_path)
        if history[-1]['status']=='active':block('autonomy_active_lease',directory)
        count+=1
    return histories,count


def _sessions(evidence,root,book_id,block):
    sessions={};events_by_hash={};pointers=[]
    sessions_dir=path(root,'sessions')
    for directory in evidence.entries(sessions_dir):
        if directory==path(sessions_dir,'latest.json'):continue
        if directory not in evidence.directories:raise ValueError('invalid session entry')
        genesis=validate_session_genesis(evidence.read(path(directory,'genesis.json')))
        if genesis['book_id']!=book_id or directory!=path(sessions_dir,genesis['session_id']):
            raise ValueError('session scope changed')
        plan=validate_instruction_plan(evidence.read(path(directory,'instruction_plan.json')))
        if (plan['plan_id']!=genesis['plan_id'] or plan['plan_hash']!=genesis['plan_hash']
                or genesis['source_snapshot']['book_id']!=book_id or plan['source_snapshot']['book_id']!=book_id):
            raise ValueError('session plan or initial source scope changed')
        events=[]
        for name in evidence.entries(path(directory,'events')):
            event=validate_session_event(evidence.read(name));sequence=len(events)+1
            previous=events[-1]['event_hash'] if events else None
            if (event['sequence']!=sequence or event['previous_event_hash']!=previous
                    or name!=path(directory,'events',f"{sequence:06d}-{event['event_hash'][:20]}.json")
                    or any(event[key]!=genesis[key] for key in ('session_id','book_id','plan_id','plan_hash','genesis_hash'))):
                raise ValueError('session event sequence or scope changed')
            _assert_transition(events,event['event_type'])
            events.append(event);events_by_hash[event['event_hash']]=event
        if not events:raise ValueError('session events missing')
        if events[-1]['event_type'] in {'started','resumed'}:block('autonomy_active_session',directory)
        sessions[genesis['session_id']]=genesis
        pointers.append((events[0]['recorded_at'],genesis['created_at'],genesis['session_id'],
            {'schema_version':'1.0','session_id':genesis['session_id'],'genesis_hash':genesis['genesis_hash']}))
    latest=path(sessions_dir,'latest.json')
    if pointers:
        if latest not in evidence.files or evidence.read(latest)!=max(pointers,key=lambda item:item[:3])[3]:
            block('autonomy_latest_requires_recovery',latest)
    elif latest in evidence.files:raise ValueError('orphan latest session pointer')
    return sessions,events_by_hash


def _operations(evidence,root,book_id,sessions,events,leases,block):
    count=0
    for directory in evidence.entries(path(root,'operations')):
        if directory not in evidence.directories:raise ValueError('invalid operation entry')
        if set(evidence.entries(directory))-{
                path(directory,name) for name in ('intent.json','source-verified.json','commit.marker','result.json')}:
            raise ValueError('unknown operation artifact')
        intent=validate_operation_intent(evidence.read(path(directory,'intent.json')))
        if intent['book_id']!=book_id or directory!=path(root,'operations',intent['operation_id']):
            raise ValueError('operation scope changed')
        source_path=path(directory,'source-verified.json');marker_path=path(directory,'commit.marker')
        source=None;marker=None
        if source_path in evidence.files:
            source=validate_source_verification(evidence.read(source_path))
            if (source['operation_id']!=intent['operation_id'] or source['intent_hash']!=intent['intent_hash']
                    or intent['operation_type'] not in {'execute','resume'}):
                raise ValueError('operation source evidence scope changed')
            source_lease=leases.get(source['lease_hash'])
            if (source_lease is None or source_lease['session_id']!=intent['session_id']
                    or source_lease['plan_id']!=intent['plan_id']):
                raise ValueError('operation source lease history missing')
        if marker_path in evidence.files:
            marker=validate_operation_commit_marker(evidence.read(marker_path))
            if (intent['schema_version']!='1.1' or marker['transaction_id']!=intent['operation_id']
                    or marker['intent_hash']!=intent['intent_hash']
                    or marker['metadata']['operation_type']!=intent['operation_type']):
                raise ValueError('operation commit marker scope changed')
            source_guarded=intent['operation_type'] in {'execute','resume'}
            expected=source['verification_hash'] if source_guarded and source else intent['intent_hash']
            if (marker['evidence_kind']!=('source_verified' if source_guarded else 'terminal_preconditions')
                    or source_guarded and source is None or marker['evidence_hash']!=expected):
                raise ValueError('operation commit marker evidence missing')
        result_path=path(directory,'result.json');count+=1
        if result_path not in evidence.files:
            block('autonomy_operation_not_settled',directory);continue
        result=validate_operation_result(evidence.read(result_path))
        if result['operation_id']!=intent['operation_id'] or result['intent_hash']!=intent['intent_hash']:
            raise ValueError('operation result scope changed')
        committed=marker is not None if intent['schema_version']=='1.1' else source is not None
        if result['outcome']=='rolled_back':
            if committed or intent['schema_version']=='1.0' and intent['operation_type'] not in {'execute','resume'}:
                raise ValueError('rollback contradicts operation evidence')
            continue
        if intent['schema_version']=='1.1' and not committed:raise ValueError('completed operation lacks marker')
        genesis=sessions.get(intent['session_id']);event=events.get(result['event_hash'])
        if (genesis is None or event is None or event['session_id']!=intent['session_id']
                or any(genesis[k]!=intent[k] for k in ('book_id','plan_id','plan_hash'))
                or intent['target_event_type'] is not None and event['event_type']!=intent['target_event_type']):
            raise ValueError('completed operation has no bound session event')
        if ((event['previous_event_hash'] if intent['target_event_type'] is not None else event['event_hash'])
                !=intent['expected_event_hash']):
            raise ValueError('operation event does not extend its original precondition')
        if result['lease_hash'] is not None:
            lease=leases.get(result['lease_hash'])
            if lease is None or lease['session_id']!=intent['session_id'] or lease['plan_id']!=intent['plan_id']:
                raise ValueError('operation lease history missing')
    return count


def inspect_autonomy_controls(evidence,root,book_id,block):
    if root not in evidence.directories:raise ValueError('autonomy root is not a directory')
    for namespace in ('leases','sessions','operations'):
        if path(root,namespace) in evidence.files:raise ValueError('autonomy namespace is not a directory')
    leases,lease_count=_leases(evidence,root,book_id,block)
    sessions,events=_sessions(evidence,root,book_id,block)
    operations=_operations(evidence,root,book_id,sessions,events,leases,block)
    return {'root':root,'sessions':len(sessions),'lease_heads':lease_count,'operations':operations}
