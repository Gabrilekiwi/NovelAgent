"""Pure proof that a claimed job paused before creating its first artifacts."""
import json
import re


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
