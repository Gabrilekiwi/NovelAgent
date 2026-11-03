"""Complete real reviewed chapters; inspect their archived ledger without replay."""
from contextlib import closing
from pathlib import Path
import copy
import json

import pytest

from tests.test_backup_mixed_history import case,mixed,transaction
from tests.test_backup_archive import capture,software_tree
from tests.test_backup_archive_history import absent_source
from tests.test_backup_autonomy_settlement import forbid_recovery
from tests.test_backup_archive_usage import forbid_runtime
from tests.test_autonomy_plans import trusted_profiles
from core.autonomy.plans import build_source_snapshot,compile_instruction_plan
from core.autonomy.profiles import TrustedProfiles
from core.autonomy.session import AutonomySessionStore
from core.autonomy.outline import OutlineCheckpointStore,build_outline_checkpoint
from core.autonomy.receipts import StageReceiptStore,CompletionLedger
from core.autonomy.common import canonical_hash
from core.stage_control import build_stage_authorization,build_stage_receipt
from core.delivery import DeliveryQueue,FileDeliveryAdapter,delivery_payload_hash
from core.engine.reviewed_chapter_commit import build_reviewed_chapter_transaction
from workbench.backup_archive import BackupArchiveService
from workbench.backup_auxiliary_history import inspect_archived_auxiliary_history
from workbench.backup_files import inventory_tree
from workbench.book_ownership import BookOwnership
from workbench.instance import InstanceLease
from workbench.maintenance import maintenance_for


NOW='2026-07-14T00:00:00+00:00'
LATER='2026-07-14T00:01:00+00:00'


def complete_chapter(c,*,state='completed',delivery=None,renew=False):
    """Original real storage APIs, real typed chapter and durable publication check."""
    profiles=copy.deepcopy(trusted_profiles().payload)
    profiles['story_projects'][0]['book_id']=c['project'].book_id
    profiles=TrustedProfiles.from_dict(profiles)
    def source(chapter):
        return build_source_snapshot(book_id=c['project'].book_id,root_uuid='root-autonomy',authority_epoch=2,
            authority_head_event_hash='2'*64,canonical_next_chapter=chapter,source_digest=str(chapter)*64,captured_at=NOW)
    plan=compile_instruction_plan('write 2 chapters' if state=='abandoned' else 'write 1 chapter',
        trusted_profiles=profiles,source_snapshot=source(2),created_at=NOW)
    roots={'story_project':c['root'],'runtime':c['root']/'.novelagent/runtime'}
    store=AutonomySessionStore(c['root']/'.novelagent/runtime/autonomy',trusted_profiles=profiles,publication_root_map=roots)
    started=store.execute_plan(plan,source_snapshot_loader=lambda:source(2),at=NOW)
    sid=started['session_id'];arc=store.arc_plans.load(started['arc_plan_id']);target=arc['targets'][0]
    outline=build_outline_checkpoint(book_id=c['project'].book_id,session_id=sid,plan_id=plan['plan_id'],
        arc_plan_id=arc['arc_plan_id'],chapter_index=2,planned_target_hash=target['target_hash'],
        source_snapshot_hash=plan['source_snapshot']['snapshot_hash'],authority_epoch=2,
        authority_head_event_hash='2'*64,outline_input_digest='3'*64,provider_profile='synthetic',
        execution_kind='deterministic',outline_text='# Synthetic archived outline\n',
        canonical_relative_path='outlines/chapter-2.md',canonical_before_sha256=None,created_at=NOW)
    OutlineCheckpointStore(store.root).create(outline,invalidated_at=NOW)
    previous=None
    for stage in ('outline','scene_plan','draft','polish','validator'):
        authorization=build_stage_authorization(stage=stage,book_id=c['project'].book_id,session_id=sid,
            plan_id=plan['plan_id'],chapter_index=2,authority_epoch=2,authority_head_event_hash='2'*64,
            input_digest='3'*64,previous_stage_receipt_hash=previous,provider_profile='synthetic',max_output_tokens=100,
            execution_kind='deterministic',issued_at=NOW)
        final=build_stage_receipt(authorization,status='succeeded',output_digest=outline['outline_hash'] if stage=='outline' else '4'*64,
            model_call_receipt_hash=None,execution_evidence_hash='e'*64,created_at=NOW)
        store.stage_receipts.append(final,authorization=authorization,expected_lease_hash=started['lease_hash'],at=NOW)
        previous=final['receipt_hash']
    tx,arguments=build_reviewed_chapter_transaction(trusted_context=c['trusted'],chapter_preparation=c['preparation'],
        request=c['request'],run_id='chapter-2')
    payload={'content':'isolated local export'}
    if delivery:
        arguments['delivery_jobs']=[{'id':'required-export','payload_hash':delivery_payload_hash(payload),
            'policy':{'required':True,'target':'file'}}]
    tx.prepare(**arguments);assert tx.commit()['committed']
    publication=json.loads((c['root']/'.novelagent/runtime/reviewed/receipts/chapter-2.json').read_bytes())
    body=next(item['sha256'] for item in publication['apply_targets'] if item['target_id']=='reviewed_chapter')
    if renew:
        store.leases.renew(book_id=c['project'].book_id,session_id=sid,plan_id=plan['plan_id'],
            expected_lease_hash=started['lease_hash'],at=NOW)
    receipt=store.completion_ledger(sid).append(final_stage_receipt=final,publication_receipt=publication,
        planned_target_hash=target['target_hash'],chapter_body_hash=body,source_snapshot_after=source(3),
        status='local_committed_delivery_blocked' if delivery else 'committed',created_at=NOW)
    store.arc_plans.record_fulfillment(arc['arc_plan_id'],chapter_index=2,fulfilled=target['planned'],
        completion_receipt_hash=receipt['receipt_hash'],expected_arc_plan_hash=arc['arc_plan_hash'],recorded_at=NOW)
    if delivery:
        queue=DeliveryQueue(c['root']/'.novelagent/runtime/deliveries')
        queue.enqueue(job_id='required-export',book_id=c['project'].book_id,run_id='chapter-2',
            publication_receipt_hash=publication['receipt_hash'],target_type='file',
            target={'path_ref':{'root_id':'story_project','relative_path':'.novelagent/runtime/export.json'}},payload=payload)
        if delivery=='succeeded':
            queue.attempt('required-export',worker_id='synthetic',adapter=FileDeliveryAdapter(root_map=roots))
        # Preserve the originally blocked immutable receipt, then close explicitly.
        store.abandon(sid,at=LATER)
    elif state=='completed':assert store.complete(sid,at=LATER)['state']=='completed'
    else:store.abandon(sid,at=LATER)
    return {**c,'autonomy':store,'started':started,'completion':receipt,'tx':tx,'publication':publication}


def enroll_archive_owner(c):
    base=c['root'].parent;machine=base/'m'
    with BookOwnership(c['project'].book_id,machine_store=machine).locked() as owner:
        with c['db'].transaction() as connection:
            owner.enroll(root=c['root'],database=c['db'],project_id=c['project'].id,connection=connection)
    c['archive_owner_enrolled']=True


def save(c,*,certify=False):
    base=c['root'].parent;machine=base/'m'
    if not c.get('archive_owner_enrolled'):enroll_archive_owner(c)
    with closing(InstanceLease(base).acquire()) as lease:
        gate=maintenance_for(lease);op=gate.begin()['operation_id']
        service=BackupArchiveService(projects=c['projects'],maintenance=gate,task_runtime_root=c['runtime'],
            archive_root=base/'archives',machine_store=machine,runtime_config={'workbench_version':'0.1.0-beta',
                'job_execution_contract':'workbench_execution_v1','worker_enabled':False,'auto_import_active':False})
        package=Path(capture({**c,'service':service,'op':op})['path'])
        if certify:
            from workbench.backup_certification import RecoveryCertification
            certifier=RecoveryCertification(service);scratch=base/'scratch';scratch.mkdir()
            preview=certifier.preview(c['project'].id,archive_id=package.name,operation_id=op,temporary_root=scratch)
            assert preview['can_certify'],preview['blockers']
            result=certifier.issue(c['project'].id,archive_id=package.name,operation_id=op,
                expected_assessment_sha256=preview['assessment_sha256'],temporary_root=scratch)
            c.update(certificate=result,scratch=scratch)
        gate.finish(op)
    return package


def inspect(c,package):
    return inspect_archived_auxiliary_history(package,project_id=c['project'].id,book_id=c['project'].book_id)


def forbid_live(monkeypatch):
    forbid_recovery(monkeypatch);forbid_runtime(monkeypatch)
    def forbidden(*args,**kwargs):pytest.fail('offline ledger verifier opened a live control store')
    for cls in (StageReceiptStore,CompletionLedger,OutlineCheckpointStore,DeliveryQueue):
        monkeypatch.setattr(cls,'__init__',forbidden)


@pytest.mark.parametrize('layout',['completed','abandoned','required_resolved','renewed_lease'])
def test_actual_typed_chapter_ledger_arc_and_final_session_verify_offline(mixed,software_tree,layout,monkeypatch):
    c=complete_chapter(mixed,state='abandoned' if layout=='abandoned' else 'completed',
        delivery='succeeded' if layout=='required_resolved' else None,renew=layout=='renewed_lease')
    package=save(c);book=inventory_tree(c['root']);archive=inventory_tree(package);forbid_live(monkeypatch)
    with absent_source(c['root']):result=inspect(c,package);again=inspect(c,package)
    assert result==again and result['complete_history_certified'],result['blockers']
    history=next(item for item in result['histories'] if item['kind']=='autonomy')
    assert history['completion_groups'][0]['receipts']==[c['completion']['receipt_hash']]
    assert history['outline_groups'] and len(history['stage_groups'][0]['receipts'])==5
    assert inventory_tree(c['root'])==book and inventory_tree(package)==archive


@pytest.mark.parametrize('damage',['body','planned_target','final_stage','lease','source_root','missing_receipt',
    'missing_publication','fake_publication','missing_transaction_marker','missing_validator','required_pending'])
def test_completion_claim_requires_original_body_stage_target_lease_and_publication(mixed,software_tree,damage,monkeypatch):
    c=complete_chapter(mixed,delivery='pending' if damage=='required_pending' else None)
    enroll_archive_owner(c)
    root=c['autonomy'].root;name=next((root/'completion_ledgers').rglob('receipts/*.json'));missing=None
    if damage in {'body','planned_target','final_stage','lease','source_root'}:
        value=json.loads(name.read_bytes())
        field={'body':'chapter_body_hash','planned_target':'planned_target_hash','final_stage':'final_stage_receipt_hash',
            'lease':'lease_hash'}.get(damage)
        if field:value[field]='f'*64
        else:
            after=value['source_snapshot_after'];after['root_uuid']='another-root'
            after['snapshot_hash']=canonical_hash(after,exclude_fields=('snapshot_hash',))
        value['receipt_hash']=canonical_hash(value,exclude_fields=('receipt_hash',));name.unlink()
        name.with_name(f"000001-000002-{value['receipt_hash'][:20]}.json").write_text(json.dumps(value))
    elif damage=='missing_receipt':missing=name
    elif damage=='missing_publication':missing=next((root/'completion_ledgers').rglob('publications/*.json'))
    elif damage=='fake_publication':next((root/'completion_ledgers').rglob('publications/*.json')).write_text('{}')
    elif damage=='missing_transaction_marker':missing=c['tx'].journal_dir/'commit.marker'
    elif damage=='missing_validator':missing=next((root/'stage_receipts').rglob('0005-*.json'))
    if missing is not None:missing.unlink()
    package=save(c);book=inventory_tree(c['root']);forbid_live(monkeypatch)
    with absent_source(c['root']):result=inspect(c,package)
    assert not result['complete_history_certified'] and result['blockers']
    if damage=='required_pending':assert 'autonomy_required_delivery_unresolved' in {item['code'] for item in result['blockers']}
    assert inventory_tree(c['root'])==book
    if missing is not None:assert not missing.exists()


def test_later_same_owner_cannot_hide_an_intervening_writer_in_completion_fence(mixed,software_tree,monkeypatch):
    from core.autonomy.lease import BookLeaseError
    c=complete_chapter(mixed);store=c['autonomy'];original=c['completion']
    stages=store.stage_receipts.load_chain(original['session_id'],2)
    fence=store.stage_receipts.fence_for(stages[-1])
    other=store.leases.acquire(book_id=c['project'].book_id,session_id='other-writer',plan_id='other-plan',at=LATER)
    store.leases.release(book_id=c['project'].book_id,session_id='other-writer',plan_id='other-plan',
        expected_lease_hash=other['lease_hash'],at=LATER)
    replacement=store.leases.acquire(book_id=c['project'].book_id,session_id=original['session_id'],
        plan_id=original['plan_id'],at=LATER)
    with pytest.raises(BookLeaseError,match='book_lease_fence_owner_discontinuity'):
        store.leases.assert_descends_from(c['project'].book_id,current_lease_hash=replacement['lease_hash'],
            ancestor_lease_hash=fence['lease_hash'])
    store.leases.release(book_id=c['project'].book_id,session_id=original['session_id'],plan_id=original['plan_id'],
        expected_lease_hash=replacement['lease_hash'],at=LATER)
    # Rehash the linked completion and arc to test the semantic fence, beyond
    # malformed hashes or a stale arc pointer.
    name=next((store.root/'completion_ledgers').rglob('receipts/*.json'));value=json.loads(name.read_bytes())
    value.update(lease_hash=replacement['lease_hash'],lease_generation=replacement['generation'])
    value['receipt_hash']=canonical_hash(value,exclude_fields=('receipt_hash',));name.unlink()
    name.with_name(f"000001-000002-{value['receipt_hash'][:20]}.json").write_text(json.dumps(value))
    arc_dir=store.root/'arc_plans'/original['arc_plan_id'];revision=arc_dir/'revisions/000002.json'
    arc=json.loads(revision.read_bytes());arc['targets'][0]['completion_receipt_hash']=value['receipt_hash']
    arc['arc_plan_hash']=canonical_hash(arc,exclude_fields=('arc_plan_hash',));revision.write_text(json.dumps(arc))
    from core.autonomy.arc import _head
    (arc_dir/'head.json').write_text(json.dumps(_head(arc)))
    package=save(c);book=inventory_tree(c['root']);forbid_live(monkeypatch)
    with absent_source(c['root']):result=inspect(c,package)
    assert not result['complete_history_certified'],result
    assert inventory_tree(c['root'])==book
