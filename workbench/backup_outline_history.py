"""Read outline revisions, committed predecessors and invalidations offline."""
from core.autonomy.common import canonical_hash,validate_mapping
from core.autonomy.outline import validate_outline_checkpoint,_validate_outline_commit_marker
from core.engine.recovery_protocol import MARKER_RECOVERY_PROTOCOL
from workbench.backup_autonomy_controls import path


def verify_outline_history(evidence,root,book_id,sessions,arcs,stage_receipts):
    groups=[]
    for session_dir in evidence.entries(path(root,'outline_checkpoints')):
        if session_dir not in evidence.directories:raise ValueError('outline session is not a directory')
        for directory in evidence.entries(session_dir):
            if directory not in evidence.directories:raise ValueError('outline chapter is not a directory')
            checkpoints={};markers={};previous={};invalidations={}
            for name in evidence.entries(path(directory,'revisions')):
                checkpoint=validate_outline_checkpoint(evidence.read(name))
                session=checkpoint['session_id'];chapter=checkpoint['chapter_index'];identifier=checkpoint['checkpoint_id']
                if (checkpoint['book_id']!=book_id or session_dir!=path(root,'outline_checkpoints',session)
                        or directory!=path(session_dir,f'chapter-{chapter:06d}')
                        or name!=path(directory,'revisions',identifier+'.json')):
                    raise ValueError('outline revision scope or filename differs')
                genesis=sessions.get(session)
                if genesis is not None:
                    arc=arcs.get(checkpoint['arc_plan_id'])
                    if (genesis['plan_id']!=checkpoint['plan_id'] or genesis['arc_plan_id']!=checkpoint['arc_plan_id']
                            or arc is None or checkpoint['planned_target_hash'] not in {
                                target['target_hash'] for revision in arc for target in revision['targets']
                                if target['chapter_index']==chapter}):
                        raise ValueError('outline checkpoint does not belong to its session/arc target')
                checkpoints[checkpoint['checkpoint_hash']]=checkpoint
            if not checkpoints:raise ValueError('outline chapter has no completed revision')
            for name in evidence.entries(path(directory,'commit_markers')):
                marker=_validate_outline_commit_marker(evidence.read(name))
                checkpoint=checkpoints.get(marker['checkpoint_hash'])
                if (checkpoint is None or marker['checkpoint_id']!=checkpoint['checkpoint_id']
                        or marker['session_id']!=checkpoint['session_id'] or marker['chapter_index']!=checkpoint['chapter_index']
                        or name!=path(directory,'commit_markers',checkpoint['checkpoint_id']+'.json')):
                    raise ValueError('outline marker has no exact completed checkpoint')
                if evidence.read(path(directory,'staged',checkpoint['checkpoint_id']+'.json'))!=checkpoint:
                    raise ValueError('outline final and staged records differ')
                markers[checkpoint['checkpoint_hash']]=marker
                previous[checkpoint['checkpoint_hash']]=marker['previous_checkpoint_hash']
            for value in checkpoints.values():
                staged=path(directory,'staged',value['checkpoint_id']+'.json')
                if (value.get('recovery_protocol')==MARKER_RECOVERY_PROTOCOL or staged in evidence.files) and value['checkpoint_hash'] not in markers:
                    raise ValueError('outline revision lacks its original commit marker')
            for name in evidence.entries(path(directory,'invalidations')):
                value=validate_mapping(evidence.read(name),'autonomy_outline_invalidation.schema.json','AutonomyOutlineInvalidation')
                before=value['invalidated_checkpoint_hash'];after=value['replacement_checkpoint_hash']
                if (before not in checkpoints or after not in checkpoints
                        or value['session_id']!=checkpoints[after]['session_id'] or value['chapter_index']!=checkpoints[after]['chapter_index']
                        or value['invalidation_hash']!=canonical_hash(value,exclude_fields=('invalidation_hash',))
                        or name!=path(directory,'invalidations',value['invalidation_hash']+'.json')
                        or after in invalidations or after in previous and previous[after]!=before):
                    raise ValueError('outline invalidation chain or scope differs')
                invalidations[after]=value;previous[after]=before
            for key,marker in markers.items():
                before=marker['previous_checkpoint_hash']
                if before is not None:
                    expected={'schema_version':'1.0','session_id':marker['session_id'],'chapter_index':marker['chapter_index'],
                        'invalidated_checkpoint_hash':before,'replacement_checkpoint_hash':key,
                        'reason':'authority_or_context_changed','recorded_at':marker['recorded_at']}
                    expected['invalidation_hash']=canonical_hash(expected)
                    if invalidations.get(key)!=expected:raise ValueError('committed outline replacement lacks exact invalidation')
                elif key in invalidations:raise ValueError('initial outline cannot invalidate another checkpoint')
            remaining=set(checkpoints);current=None;ordered=[]
            while remaining:
                ready=[key for key in remaining if previous.get(key)==current]
                if len(ready)!=1:raise ValueError('outline history is forked, cyclic or incomplete')
                current=ready[0];ordered.append(current);remaining.remove(current)
            head=checkpoints[current]
            if evidence.read(path(directory,'latest.json'))!={'schema_version':'1.0',
                    'checkpoint_id':head['checkpoint_id'],'checkpoint_hash':head['checkpoint_hash']}:
                raise ValueError('outline latest pointer is missing or stale')
            for stage in stage_receipts.values():
                if (stage['session_id']==head['session_id'] and stage['chapter_index']==head['chapter_index']
                        and stage['stage']=='outline' and stage['status']=='succeeded'):
                    if not any(value['plan_id']==stage['plan_id'] and value['authority']==stage['authority']
                            and value['outline_input_digest']==stage['input_digest']
                            and value['outline_hash']==stage['output_digest'] for value in checkpoints.values()):
                        raise ValueError('outline stage does not bind any retained checkpoint output')
            groups.append({'session_id':head['session_id'],'chapter':head['chapter_index'],
                'checkpoints':ordered,'head':current})
    return groups
