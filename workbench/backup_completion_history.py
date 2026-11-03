"""Verify chapter completion against real archived commits and stage evidence."""
from core.autonomy.common import canonical_hash
from core.autonomy.receipts import validate_chapter_completion_receipt
from workbench.backup_autonomy_controls import path


def verify_completion_history(evidence,root,book_id,leases,sessions,arcs,stages,stage_groups,publications,block,deliveries):
    completed={};groups=[]
    heads={(group['session_id'],group['chapter']):group['receipts'][-1] for group in stage_groups}
    for directory in evidence.entries(path(root,'completion_ledgers')):
        if directory not in evidence.directories:raise ValueError('completion ledger is not a directory')
        names=evidence.entries(path(directory,'receipts'))
        if not names:
            # Empty ledgers carry no completion claim, but must have an exact
            # existing session/plan identity and no unpublished artifacts.
            known=False
            for sid in sessions:
                plan=evidence.read(path(root,'sessions',sid,'instruction_plan.json'))
                if directory==path(root,'completion_ledgers',canonical_hash({'session_id':sid,'plan_hash':plan['plan_hash']})[:16]):known=True
            if not known or any(name.startswith(directory+'/') for name in evidence.files):
                raise ValueError('completion ledger is empty, unknown or interrupted')
            groups.append({'root':directory,'receipts':[]});continue
        previous=None;expected_chapter=None;source=None;plan=None;first=None;chain=[]
        for sequence,name in enumerate(names,1):
            receipt=validate_chapter_completion_receipt(evidence.read(name));sid=receipt['session_id'];chapter=receipt['chapter_index']
            genesis=sessions.get(sid)
            if genesis is None:raise ValueError('completion has no original session/plan evidence')
            current_plan=evidence.read(path(root,'sessions',sid,'instruction_plan.json'))
            if plan is None:
                plan=current_plan;source=plan['source_snapshot'];expected_chapter=plan['chapter_start'];first=receipt
            key=canonical_hash({'session_id':sid,'plan_hash':plan['plan_hash']})[:16]
            if (receipt['book_id']!=book_id or current_plan!=plan or chapter!=expected_chapter
                    or chapter>plan['chapter_end'] or receipt['session_id']!=first['session_id']
                    or receipt['plan_id']!=plan['plan_id'] or receipt['arc_plan_id']!=genesis['arc_plan_id']
                    or directory!=path(root,'completion_ledgers',key)
                    or name!=path(directory,'receipts',f"{sequence:06d}-{chapter:06d}-{receipt['receipt_hash'][:20]}.json")
                    or receipt['previous_completion_receipt_hash']!=previous):
                raise ValueError('completion receipt sequence, range or scope differs')
            after=receipt['source_snapshot_after']
            if after['root_uuid']!=source['root_uuid'] or after['authority_epoch']<source['authority_epoch']:
                raise ValueError('completion source snapshot regresses or changes root')
            stage=stages.get(receipt['final_stage_receipt_hash'])
            if (stage is None or stage['stage']!='validator' or stage['status']!='succeeded'
                    or any(stage[key]!=receipt[key] for key in ('book_id','session_id','plan_id','chapter_index'))
                    or heads.get((sid,chapter))!=stage['receipt_hash']):
                raise ValueError('completion does not bind its actual final validator')
            fence=evidence.read(path(root,'stage_receipts',canonical_hash({'session_id':sid})[:16],
                f'chapter-{chapter:06d}','fences',stage['receipt_hash'][:20]+'.json'))
            lease=leases.get(receipt['lease_hash'])
            if (lease is None or lease['status']!='active' or lease['generation']!=receipt['lease_generation']
                    or lease['generation']<fence['lease_generation']
                    or any(lease[key]!=receipt[key] for key in ('book_id','session_id','plan_id'))):
                raise ValueError('completion lease fence is missing or belongs to another writer')
            # Match the runtime's assert_descends_from rule. A larger generation
            # with the same current owner cannot hide an intervening writer.
            cursor=lease
            while True:
                if any(cursor[key]!=receipt[key] for key in ('book_id','session_id','plan_id')):
                    raise ValueError('completion lease ancestry crosses another writer')
                if cursor['lease_hash']==fence['lease_hash']:break
                cursor=leases.get(cursor['previous_lease_hash'])
                if cursor is None:raise ValueError('completion stage fence is not a retained lease ancestor')
            arc=arcs.get(receipt['arc_plan_id'])
            if arc is None or not any(target['chapter_index']==chapter and target['target_hash']==receipt['planned_target_hash']
                    for revision in arc for target in revision['targets']):
                raise ValueError('completion planned target is absent from the real arc history')
            publication=evidence.read(path(directory,'publications',receipt['publication_receipt_hash'][:20]+'.json'))
            actual=publications().get(receipt['publication_receipt_hash'])
            if (actual is None or actual['receipt']!=publication or actual['chapter']!=chapter
                    or actual['prose_hash']!=receipt['chapter_body_hash']):
                raise ValueError('completion publication/body differs from actual archived transaction')
            if receipt['status']=='local_committed_delivery_blocked':
                candidates=[item for item in deliveries if item['publication_receipt_hash']==receipt['publication_receipt_hash']
                    and item['run_id']==publication['run_id'] and item['policy']=='required']
                required=[item for item in publication['delivery_jobs'] if item['policy']['required']]
                resolved=bool(required) and bool(candidates) and all(item['state']=='succeeded' for item in candidates)
                for item in required:
                    matches=[job for job in candidates if job['job_id']==item['id'] and job['payload_hash']==item['payload_hash']
                        and job['target_type']==item['policy']['target']]
                    resolved=resolved and len(matches)==1
                if not resolved:block('autonomy_required_delivery_unresolved',name)
            completed[receipt['receipt_hash']]=receipt;chain.append(receipt['receipt_hash'])
            previous=receipt['receipt_hash'];source=after;expected_chapter+=1
        groups.append({'root':directory,'session_id':first['session_id'],'receipts':chain})
    return completed,groups
