"""Read-only auxiliary control-state checks against an existing source inventory.

This is a settlement check, not a complete history/restore certificate. Never
open runtime stores through their recovery-oriented load/status entry points.
"""
import json
from pathlib import Path, PurePosixPath

from core.delivery import validate_delivery_job, validate_delivery_attempt_receipt
from core.story_project.reviewed_state_document import digest, read_project_bytes
from workbench.backup_autonomy_controls import inspect_autonomy_controls


class AuxiliaryEvidenceError(ValueError):
    pass


class Evidence:
    def __init__(self, root, tree, *, max_files=10000, max_bytes=256*1024**2, _read_bytes=None):
        if type(max_files) is not int or max_files<1 or type(max_bytes) is not int or max_bytes<1:
            raise ValueError('invalid auxiliary evidence read limits')
        self.root=Path(root);self.tree=tree;self.cache={};self.bytes=0;self.read_count=0
        self.max_files=max_files;self.max_bytes=max_bytes;self.children={}
        self._read_bytes=_read_bytes or read_project_bytes
        self.files=set(tree['files']);self.directories=set(tree['directories'])
        for name in self.files|self.directories:
            path=PurePosixPath(name)
            self.children.setdefault(str(path.parent),set()).add(name)

    def entries(self, directory):
        return sorted(self.children.get(directory,()))

    def read(self, name):
        if name in self.cache:return self.cache[name]
        record=self.tree['files'].get(name)
        if (record is None or record['size']>32*1024**2
                or self.read_count>=self.max_files or self.bytes+record['size']>self.max_bytes):
            raise AuxiliaryEvidenceError('auxiliary evidence missing or exceeds read limits')
        # Malformed reads consume the same budget as valid documents.
        self.read_count+=1;self.bytes+=record['size']
        raw=self._read_bytes(self.root,name)
        if len(raw)!=record['size'] or digest(raw)!=record['sha256']:
            raise AuxiliaryEvidenceError('auxiliary evidence changed after inventory')
        value=json.loads(raw)
        if not isinstance(value,dict):raise AuxiliaryEvidenceError('auxiliary evidence must be an object')
        self.cache[name]=value
        return value


def _path(root, *parts):
    return str(PurePosixPath(root).joinpath(*parts))


def _roots(evidence, canonical, families):
    roots=set()
    if canonical in evidence.directories or canonical in evidence.files:roots.add(canonical)
    # A managed custom store has its original local lock plus known child
    # namespaces. Do not discover new filesystem roots from JSON declarations.
    for name in evidence.files:
        if PurePosixPath(name).name!='.persistence.lock':continue
        root=str(PurePosixPath(name).parent)
        if any(_path(root,family) in evidence.directories or _path(root,family) in evidence.files
               for family in families):roots.add(root)
    return sorted(roots)


def _delivery(evidence, root, book_id, block):
    if root not in evidence.directories:raise AuxiliaryEvidenceError('delivery root is not a directory')
    jobs={};receipts={};jobs_dir=_path(root,'jobs');attempts_dir=_path(root,'attempts')
    for directory in (jobs_dir,attempts_dir):
        if directory in evidence.files:raise AuxiliaryEvidenceError('delivery namespace is not a directory')
    for path in evidence.entries(jobs_dir):
        job=validate_delivery_job(evidence.read(path))
        if path!=_path(jobs_dir,job['job_id']+'.json') or job['book_id']!=book_id:
            raise AuxiliaryEvidenceError('delivery job path or book binding mismatch')
        jobs[job['job_id']]=job
        if (job['target_type']=='none' and (job['state']!='not_required' or job['attempt_count']!=0)
                or job['target_type']!='none' and job['state'] in {'succeeded','not_required'} and job['attempt_count']==0):
            raise AuxiliaryEvidenceError('delivery state lacks an actual attempt')
        if job['state'] in {'delivering','uncertain'}:block('delivery_not_settled',path)
    for directory in evidence.entries(attempts_dir):
        job_id=PurePosixPath(directory).name
        if directory not in evidence.directories or job_id not in jobs:
            raise AuxiliaryEvidenceError('orphan delivery attempts')
        job=jobs[job_id];by_sequence={}
        for path in evidence.entries(directory):
            receipt=validate_delivery_attempt_receipt(evidence.read(path))
            for field in ('job_id','book_id','run_id','publication_receipt_hash','payload_hash','target_type'):
                if receipt[field]!=job[field]:raise AuxiliaryEvidenceError('delivery receipt scope mismatch')
            expected=_path(directory,receipt['attempt_id'].replace(':','_')+'.json')
            parts=receipt['attempt_id'].split(':')
            if (path!=expected or len(parts)!=3 or parts[0]!=job_id or not parts[1].isdigit()
                    or int(parts[1])<1 or int(parts[1]) in by_sequence):
                raise AuxiliaryEvidenceError('delivery receipt path or sequence mismatch')
            sequence=int(parts[1])
            if sequence>job['attempt_count']:raise AuxiliaryEvidenceError('delivery receipt exceeds job counter')
            by_sequence[sequence]=receipt
        receipts[job_id]=by_sequence
    for job_id,job in jobs.items():
        history=receipts.get(job_id,{})
        # Compare bounded stored records, not range(1, untrusted_counter).
        if len(history)!=job['attempt_count'] or any(n!=i for i,n in enumerate(sorted(history),1)):
            block('delivery_attempt_evidence_incomplete',_path(jobs_dir,job_id+'.json'))
        last=job['last_attempt_receipt']
        if last is not None:
            matching=[r for r in history.values() if r['attempt_id']==last['attempt_id']]
            if (len(matching)!=1 or matching[0]['attempt_receipt_hash']!=last['attempt_receipt_hash']
                    or history.get(job['attempt_count'])!=matching[0]):
                raise AuxiliaryEvidenceError('delivery latest receipt is missing or stale')
            outcome=matching[0]['outcome']
            reopened=(job['state']=='pending' and job['target_type']=='notion'
                and job['confirmed_absent_at'] is not None and matching[0]['query_only']
                and outcome['state']=='uncertain' and outcome['code']=='notion_absent_during_uncertain_reconcile')
            if job['state'] not in {outcome['state'],'delivering','conflict'} and not reopened:
                raise AuxiliaryEvidenceError('delivery state disagrees with its receipt')
        elif job['attempt_count'] and job['state']!='delivering':
            raise AuxiliaryEvidenceError('settled delivery has no latest receipt')
    return {'root':root,'jobs':len(jobs),'attempts':sum(map(len,receipts.values()))}


def inspect_auxiliary_settlement(root, tree, book_id, *, max_files=10000, max_bytes=256*1024**2, _read_bytes=None):
    evidence=Evidence(root,tree,max_files=max_files,max_bytes=max_bytes,_read_bytes=_read_bytes)
    blockers=[];stores=[]
    def block(code,reference):
        item={'code':code,'reference':reference}
        if item not in blockers:blockers.append(item)
    for store in _roots(evidence,'.novelagent/runtime/deliveries',('jobs','attempts')):
        try:stores.append({'kind':'delivery',**_delivery(evidence,store,book_id,block)})
        except (OSError,ValueError,RuntimeError):block('delivery_control_evidence_unverified',store)
    for store in _roots(evidence,'.novelagent/runtime/autonomy',('leases','sessions','operations','arc_plans')):
        try:stores.append({'kind':'autonomy',**inspect_autonomy_controls(evidence,store,book_id,block)})
        except (OSError,ValueError,RuntimeError):block('autonomy_control_evidence_unverified',store)
    return {'check':'auxiliary_control_settlement','state':'settled' if not blockers else 'unverified',
            'coverage':'canonical_and_discovered_managed_delivery_and_autonomy_controls',
            'stores':stores,'blockers':blockers,'files_read':evidence.read_count,
            'complete_history_certified':False}
