"""Exact retained job rows across restore generations, with no live ancestors."""
import json

from core.story_project.reviewed_state_document import digest


FIRST_TABLE = 'workbench_restored_job_history'
GENERATION_TABLE = 'workbench_restored_job_generations'
INVALIDATED_CONTRACT = 'restored_job_history_v1'
TERMINAL = {'completed', 'cancelled', 'failed'}
FIRST_COLUMNS = ['job_id', 'handoff_id', 'original_row_json', 'row_sha256']
GENERATION_COLUMNS = ['scope_generation', *FIRST_COLUMNS]
EVENT_COLUMNS = {'id','job_id','sequence','event_type','payload_json','created_at'}


class RestoreJobHistoryError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def validate_job_intent(intent, scope, inherited):
    """Bind the row snapshots to the exact archived predecessor, not local edits."""
    try:
        generation = scope.get('scope_generation', 1)
        if type(generation) is not int or not 1 <= generation <= 17:
            raise RestoreJobHistoryError('恢复任务代际无效。')
        schema = 'restore_job_preparation_v1' if generation == 1 else 'restore_job_preparation_v2'
        if (intent['schema_version'] != schema or intent['invalidated_contract'] != INVALIDATED_CONTRACT
                or intent['intent_sha256'] != digest({k:v for k,v in intent.items() if k != 'intent_sha256'})):
            raise RestoreJobHistoryError('恢复任务意图的代际、格式或摘要不同。')
        if generation == 1:
            if inherited is not None or 'scope_generation' in intent or 'source_provenance_sha256' in intent:
                raise RestoreJobHistoryError('首次任务准备不能继承恢复历史。')
        elif (inherited is None or generation != inherited['scope'].get('scope_generation', 1)+1
                or intent['scope_generation'] != generation
                or intent['source_provenance_sha256'] != inherited['provenance_sha256']
                or scope['predecessor_scope'] != inherited['scope']
                or scope['source_provenance_sha256'] != inherited['provenance_sha256']):
            raise RestoreJobHistoryError('恢复任务历史不属于原归档代际。')
        history = intent['history']
        if (not isinstance(history, list) or len(history) > 10000
                or len(canonical(history).encode('utf-8')) > 16*1024**2):
            raise RestoreJobHistoryError('恢复任务历史超过支持大小。')
        ids = set()
        for item in history:
            original = item['original']
            if (not isinstance(item['id'], str) or not item['id'] or item['id'] in ids
                    or original['id'] != item['id'] or digest(original) != item['original_sha256']
                    or type(original['revision']) is not int or original['revision'] < 0):
                raise RestoreJobHistoryError('恢复任务完整行、标识或摘要不同。')
            ids.add(item['id'])
        if inherited is not None:
            current = {item['id']:item['original'] for item in history}
            for item in inherited['jobs_intent']['history']:
                if current.get(item['id']) != invalidated_row(item['original'], inherited['jobs_intent']):
                    raise RestoreJobHistoryError('上一代已失效任务缺失或重新取得执行资格。')
        event_fields = {'event_history','event_history_sha256'}
        if inherited is not None and 'event_history' in inherited['jobs_intent'] and not event_fields <= set(intent):
            raise RestoreJobHistoryError('新代际不能丢弃原任务事件快照。')
        if event_fields & set(intent):
            if not event_fields <= set(intent):
                raise RestoreJobHistoryError('原任务事件快照与摘要必须同时存在。')
            events = intent['event_history']
            if (not isinstance(events,list) or len(events)>100000
                    or len(canonical(events).encode('utf-8'))>16*1024**2
                    or intent['event_history_sha256']!=digest(events)):
                raise RestoreJobHistoryError('原任务事件快照超过支持大小或摘要不同。')
            event_ids=set();sequences=set()
            for event in events:
                if (not isinstance(event,dict) or set(event)!=EVENT_COLUMNS
                        or type(event['id']) is not int or event['id']<1 or event['id'] in event_ids
                        or event['job_id'] not in ids or type(event['sequence']) is not int or event['sequence']<1
                        or (event['job_id'],event['sequence']) in sequences
                        or not all(isinstance(event[key],str) for key in ('event_type','payload_json','created_at'))):
                    raise RestoreJobHistoryError('原任务完整事件、顺序或归属无效。')
                event_ids.add(event['id']);sequences.add((event['job_id'],event['sequence']))
            if [event['id'] for event in events]!=sorted(event_ids):
                raise RestoreJobHistoryError('原任务事件必须保持数据库标识顺序。')
            if inherited is not None and 'event_history' in inherited['jobs_intent']:
                previous=inherited['jobs_intent'];old_ids={item['id'] for item in previous['history']}
                if [event for event in events if event['job_id'] in old_ids]!=previous['event_history']:
                    raise RestoreJobHistoryError('上一代原任务事件被替换、删除或追加。')
        return generation
    except (KeyError, TypeError, AttributeError) as exc:
        raise RestoreJobHistoryError('恢复任务代际证据缺失。') from exc


def invalidated_row(original, intent):
    return {**original, 'execution_contract':INVALIDATED_CONTRACT,
        'state':original['state'] if original['state'] in TERMINAL else 'cancelled',
        'updated_at':intent['created_at'], 'revision':original['revision']+1}


def expected_history(provenance, *, depth=0):
    """Derive every history table row from the recorded, bounded archive chain."""
    if depth > 16:
        raise RestoreJobHistoryError('恢复任务历史代际超过支持范围。')
    try:
        if provenance['provenance_sha256'] != digest({k:v for k,v in provenance.items() if k != 'provenance_sha256'}):
            raise RestoreJobHistoryError('原恢复任务来源摘要不同。')
        intent = provenance['jobs_intent']
        inherited = provenance['plan'].get('source_restore_provenance')
        generation = validate_job_intent(intent, provenance['scope'], inherited)
        tables = expected_history(inherited, depth=depth+1) if inherited is not None else {FIRST_TABLE:[]}
        table = FIRST_TABLE if generation == 1 else GENERATION_TABLE
        snapshots = []
        for item in intent['history']:
            row = {'job_id':item['id'], 'handoff_id':intent['handoff_id'],
                'original_row_json':canonical(item['original']), 'row_sha256':item['original_sha256']}
            if generation > 1: row = {'scope_generation':generation, **row}
            snapshots.append(row)
        tables.setdefault(table, []).extend(snapshots)
        tables[table].sort(key=lambda row:(row.get('scope_generation', 1), row['job_id']))
        return tables
    except (KeyError, TypeError, AttributeError) as exc:
        raise RestoreJobHistoryError('原恢复任务来源链缺失。') from exc


def validate_retained_tables(connection, provenance):
    tables = expected_history(provenance)
    for table in (FIRST_TABLE, GENERATION_TABLE):
        schema = list(connection.execute('PRAGMA table_info('+table+')'))
        if table not in tables:
            if schema: raise RestoreJobHistoryError('恢复任务数据库存在未绑定的历史代际。')
            continue
        columns = FIRST_COLUMNS if table == FIRST_TABLE else GENERATION_COLUMNS
        if [row[1] for row in schema] != columns:
            raise RestoreJobHistoryError('恢复任务历史表缺失或格式不同。')
        if table == GENERATION_TABLE and [(row[2], row[3], row[5]) for row in schema] != [
                ('INTEGER', 1, 1), ('TEXT', 1, 2), ('TEXT', 1, 0), ('TEXT', 1, 0), ('TEXT', 1, 0)]:
            raise RestoreJobHistoryError('恢复任务代际表的完整约束不同。')
        order = 'scope_generation,job_id' if table == GENERATION_TABLE else 'job_id'
        expected = tables[table]
        actual = [dict(row) for row in connection.execute(
            'SELECT * FROM '+table+' ORDER BY '+order+' LIMIT ?', (len(expected)+1,))]
        if actual != expected:
            raise RestoreJobHistoryError('恢复任务完整历史被删除、替换或增加了未绑定记录。')
    return tables


def validate_retained_events(connection, intent):
    """New jobs may append events; every imported job keeps its exact events."""
    if 'event_history' not in intent:return
    ids=[item['id'] for item in intent['history']];actual=[]
    for start in range(0,len(ids),500):
        batch=ids[start:start+500]
        actual.extend(dict(row) for row in connection.execute('SELECT * FROM job_events WHERE job_id IN ('
            +','.join('?' for _ in batch)+') LIMIT 100001',batch))
        if len(actual)>100000:raise RestoreJobHistoryError('原任务当前事件数量超过支持范围。')
    actual.sort(key=lambda row:row['id'])
    if actual!=intent['event_history']:
        raise RestoreJobHistoryError('原任务完整事件被修改、删除或追加。')
