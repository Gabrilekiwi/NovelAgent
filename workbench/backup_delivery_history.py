"""Read complete retained delivery control history; never reconcile or send."""
from pathlib import PurePosixPath

from core.path_refs import validate_path_ref
from workbench.backup_auxiliary import _delivery


def verify_delivery_history(evidence,root,book_id,block):
    summary=_delivery(evidence,root,book_id,block)
    jobs=[];allowed_dirs={root+'/jobs',root+'/attempts'}
    for name in evidence.entries(root+'/jobs'):
        job=evidence.read(name);job_id=job['job_id']
        if job['target_type']=='file':validate_path_ref(job['target']['path_ref'])
        directory=root+'/attempts/'+job_id;allowed_dirs.add(directory)
        attempts=sorted((evidence.read(path) for path in evidence.entries(directory)),
            key=lambda value:int(value['attempt_id'].split(':')[1]))
        previous=None
        for sequence,receipt in enumerate(attempts,1):
            expected='pending' if previous is None else previous['outcome']['state']
            reopened=(previous is not None and job['target_type']=='notion'
                and expected=='uncertain' and previous['query_only']
                and previous['outcome']['code']=='notion_absent_during_uncertain_reconcile'
                and job['confirmed_absent_at'] is not None and receipt['previous_state']=='pending')
            if receipt['previous_state']!=expected and not reopened:
                raise ValueError('delivery history has an impossible state transition')
            number=receipt['diagnostics'].get('attempt_number')
            if number is not None and (type(number) is not int or number!=sequence):
                raise ValueError('delivery attempt counter disagrees with its immutable receipt')
            previous=receipt
        jobs.append({'job_id':job_id,'run_id':job['run_id'],'payload_hash':job['payload_hash'],
            'publication_receipt_hash':job['publication_receipt_hash'],'state':job['state'],
            'policy':job['policy'],'target_type':job['target_type'],
            'attempts':[{'attempt_id':item['attempt_id'],'receipt_hash':item['attempt_receipt_hash'],
                'previous_state':item['previous_state'],'outcome':item['outcome']['state']} for item in attempts]})
    used={name for name in evidence.cache if name.startswith(root+'/')}
    for name in used:
        allowed_dirs.update(str(parent) for parent in PurePosixPath(name).parents
            if str(parent).startswith(root+'/'))
    return {'kind':'delivery','root':root,'jobs':jobs,'counts':summary,
        'record_hashes':{name:evidence.tree['files'][name]['sha256'] for name in sorted(used)},
        'automatic_delivery_allowed':False},used,allowed_dirs
