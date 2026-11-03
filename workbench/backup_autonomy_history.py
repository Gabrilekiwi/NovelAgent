"""Pure retained autonomy histories, without runtime head reconstruction."""
from pathlib import PurePosixPath

from core.autonomy.arc import validate_run_arc_plan,_head
from core.autonomy.plans import validate_instruction_plan
from workbench.backup_autonomy_controls import _leases,_sessions,_operations,path
from workbench.backup_stage_history import verify_stage_history
from workbench.backup_outline_history import verify_outline_history
from workbench.backup_completion_history import verify_completion_history


def verify_autonomy_history(evidence,root,book_id,block,*,model_receipts=None,publications=None,deliveries=()):
    plans={}
    for name in sorted(evidence.files):
        if PurePosixPath(name).parent.as_posix().casefold()!=(root+'/plans').casefold():continue
        plan=validate_instruction_plan(evidence.read(name))
        if (plan['source_snapshot']['book_id']!=book_id or name.casefold()!=
                f"{root}/plans/{plan['plan_id']}-{plan['plan_hash'][:16]}.json".casefold()):
            raise ValueError('autonomy preview belongs to another plan or book')
        plans[plan['plan_hash']]=plan
    leases,lease_count=_leases(evidence,root,book_id,block)
    sessions,events=_sessions(evidence,root,book_id,block)
    operations=_operations(evidence,root,book_id,sessions,events,leases,block)
    arcs={}
    for directory in evidence.entries(path(root,'arc_plans')):
        if directory not in evidence.directories:raise ValueError('arc namespace is not a directory')
        chain=[];previous=None
        for sequence,name in enumerate(evidence.entries(path(directory,'revisions')),1):
            arc=validate_run_arc_plan(evidence.read(name))
            if (arc['book_id']!=book_id or directory!=path(root,'arc_plans',arc['arc_plan_id'])
                    or name!=path(directory,'revisions',f'{sequence:06d}.json')
                    or arc['revision']!=sequence or arc['previous_arc_plan_hash']!=previous):
                raise ValueError('arc history has a gap, fork or another owner')
            if chain and any(arc[key]!=chain[0][key] for key in ('book_id','session_id',
                    'instruction_plan_id','instruction_plan_hash','source_snapshot_hash','arc_plan_id')):
                raise ValueError('arc revision changes its immutable scope')
            chain.append(arc);previous=arc['arc_plan_hash']
        if not chain:raise ValueError('arc history has no initial revision')
        if evidence.read(path(directory,'head.json'))!=_head(chain[-1]):
            raise ValueError('arc head is missing or would require recovery')
        arcs[chain[0]['arc_plan_id']]=chain
    expected_indexes={}
    for session_id,genesis in sessions.items():
        directory=path(root,'sessions',session_id)
        plan=evidence.read(path(directory,'instruction_plan.json'))
        if genesis['source_snapshot']['snapshot_hash']!=plan['source_snapshot']['snapshot_hash']:
            raise ValueError('session source differs from its immutable plan')
        preview=plans.get(plan['plan_hash'])
        if preview is not None and preview!=plan:
            raise ValueError('preview and session retain different versions of a plan')
        chain=arcs.get(genesis['arc_plan_id'])
        if (chain is None or chain[0]['arc_plan_hash']!=genesis['initial_arc_plan_hash']
                or chain[0]['session_id']!=session_id
                or chain[0]['instruction_plan_id']!=plan['plan_id']
                or chain[0]['instruction_plan_hash']!=plan['plan_hash']
                or chain[0]['source_snapshot_hash']!=plan['source_snapshot']['snapshot_hash']):
            raise ValueError('session initial arc is missing or belongs to another plan')
        key=path(root,'plan_sessions',plan['plan_hash'][:20]+'.json')
        value={'schema_version':'1.0','plan_hash':plan['plan_hash'],
            'session_id':session_id,'genesis_hash':genesis['genesis_hash']}
        if key in expected_indexes and expected_indexes[key]!=value:
            raise ValueError('multiple sessions claim the same immutable plan index')
        expected_indexes[key]=value
    if set(evidence.entries(path(root,'plan_sessions')))!=set(expected_indexes):
        raise ValueError('plan/session index is missing or orphaned')
    for name,expected in expected_indexes.items():
        if evidence.read(name)!=expected:raise ValueError('plan/session index scope differs')
    stage_receipts,stage_groups=verify_stage_history(evidence,root,book_id,leases,sessions,block,model_receipts)
    outline_groups=verify_outline_history(evidence,root,book_id,sessions,arcs,stage_receipts)
    completions,completion_groups=verify_completion_history(evidence,root,book_id,leases,sessions,arcs,
        stage_receipts,stage_groups,publications or (lambda:{}),block,deliveries)
    for chain in arcs.values():
        for revision in chain:
            for target in revision['targets']:
                identifier=target['completion_receipt_hash']
                if identifier is None:continue
                receipt=completions.get(identifier)
                if (receipt is None or receipt['arc_plan_id']!=revision['arc_plan_id']
                        or receipt['session_id']!=revision['session_id'] or receipt['chapter_index']!=target['chapter_index']
                        or receipt['planned_target_hash']!=target['target_hash']):
                    raise ValueError('arc fulfillment has no exact original completion')
    for sid,genesis in sessions.items():
        if not any(event['session_id']==sid and event['event_type']=='completed' for event in events.values()):continue
        plan=evidence.read(path(root,'sessions',sid,'instruction_plan.json'))
        actual={item['chapter_index'] for item in completions.values() if item['session_id']==sid}
        if len(actual)!=plan['chapter_end']-plan['chapter_start']+1:
            raise ValueError('completed session lacks its complete contiguous chapter ledger')
    used={name for name in evidence.cache if name.startswith(root+'/')}
    coordinator=path(root,'_session_creation','.persistence.lock')
    if coordinator in evidence.files:used.add(coordinator)
    allowed_dirs={path(root,name) for name in ('plans','leases','sessions','operations','arc_plans','plan_sessions',
        'stage_receipts','outline_checkpoints','completion_ledgers')}
    for group in completion_groups:
        allowed_dirs.update((group['root'],path(group['root'],'receipts'),path(group['root'],'publications')))
    allowed_dirs.add(path(root,'_session_creation'))
    allowed_dirs.update(str(PurePosixPath(name).parent) for name in used)
    for name in used:
        allowed_dirs.update(str(parent) for parent in PurePosixPath(name).parents
            if str(parent).startswith(root+'/'))
    records={name:evidence.tree['files'][name]['sha256'] for name in sorted(used)}
    kind='autonomy_preview' if not (sessions or lease_count or operations or arcs or stage_groups or outline_groups) else 'autonomy'
    return {'kind':kind,'root':root,'plans':[{'plan_id':value['plan_id'],'plan_hash':key,
            'source_snapshot_hash':value['source_snapshot']['snapshot_hash']} for key,value in sorted(plans.items())],
        'sessions':len(sessions),'lease_heads':lease_count,'operations':operations,
        'arcs':{key:len(chain) for key,chain in sorted(arcs.items())},'record_hashes':records,
        'stage_groups':stage_groups,
        'outline_groups':outline_groups,
        'completion_groups':completion_groups,
        'automatic_resume_allowed':False},used,allowed_dirs
