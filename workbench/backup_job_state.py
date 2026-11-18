"""Pure proof that a claimed job paused before creating its first artifacts."""
import json
import re
from pathlib import PurePosixPath


def empty_cancelled_stdout(connection, root_key, relative, raw):
    """Retain empty output as diagnostics only with a matching failed child.

    This proves neither no model calls nor no writes. Separate usage and full
    transaction-history verification remain mandatory and are never relaxed.
    """
    if raw != b'' or not root_key.startswith('jobs/'):
        return None
    path=PurePosixPath(relative)
    if len(path.parts)!=2 or path.name!='stdout.json':return None
    match=re.fullmatch(r'chapter_([0-9]{4,})',path.parts[0])
    if match is None:return None
    chapter=int(match.group(1));job_id=root_key[5:]
    job=connection.execute('SELECT state FROM jobs WHERE id=?',(job_id,)).fetchone()
    if job is None or job['state']!='cancelled':return None
    for index,event in enumerate(connection.execute(
            'SELECT sequence,event_type,payload_json FROM job_events WHERE job_id=? ORDER BY sequence DESC',(job_id,))):
        if index>=10000:return None
        try:
            if len(event['payload_json'])>32*1024**2:return None
            payload=json.loads(event['payload_json'])
        except (ValueError,TypeError):return None
        if not isinstance(payload,dict):return None
        if payload.get('chapter')!=chapter:continue
        error=payload.get('error')
        known_failure=(isinstance(error,dict) and (event['event_type'],error.get('code')) in {
            ('chapter.needs_revision','engine_result_unreadable'),
            ('chapter.needs_recovery','engine_result_unreadable'),
            ('chapter.repair_not_converging','chapter_repair_not_converging')})
        if (known_failure and payload.get('automatic_retry') is False
                and type(error.get('exit_code')) is int and error['exit_code']!=0):
            return {'root':root_key,'path':relative,'job_id':job_id,'chapter':chapter,
                    'event_sequence':event['sequence'],'classification':'empty_output_of_cancelled_failed_child'}
        return None  # A later chapter event must not be replaced by an older failure.
    return None


def first_boundary_pause(job,event):
    if (event is None or job['state']!='awaiting_input' or job['next_chapter']!=job['start_chapter']
            or job['result_json'] is not None or event['event_type']!='job.paused'):
        return False
    try:
        if len(event['payload_json'])>32*1024**2:return False
        payload=json.loads(event['payload_json'])
        return (isinstance(payload,dict) and payload.get('reason')=='workbench_maintenance'
            and type(payload.get('completed_chapters')) is int and payload['completed_chapters']==0
            and type(payload.get('chapter')) is int and payload['chapter']==job['next_chapter']
            and payload.get('automatic_resume') is False and isinstance(payload.get('operation_id'),str)
            and re.fullmatch('[0-9a-f]{32}',payload['operation_id']) is not None)
    except (TypeError,ValueError):return False
