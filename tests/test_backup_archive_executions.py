"""Offline reads of actual app-created Candidate and FactExtraction artifacts."""
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from api.contracts import ModelResponse
from core.engine import chapter_candidate as candidate,chapter_fact_extraction as facts
from core.model_call_runtime import current_model_call_runtime
from core.model_calls import ModelCallStore
from core.memory_v2.canonical import canonical_json_hash
from tests.test_backup_archive import software_tree,capture
from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_backup_archive_history import absent_source
from tests.test_workbench_chapter_candidates import PROSE,_fixed_provenance,_unlock
from tests.test_workbench_chapter_fact_extractions import _setup,_prepare_draft,_start_extraction
from workbench.backup_archive_executions import inspect_archived_executions
from workbench.backup_files import inventory_tree


@pytest.fixture
def execution_archive(tmp_path,software_tree,request):
    root,cf,ff,app,project=_setup(tmp_path)
    app.state.backups.machine_store=tmp_path/'machine'
    mode=getattr(request,'param','model')
    if mode=='model':
        def generator(prompt):
            runtime=current_model_call_runtime();assert runtime is not None
            return runtime.execute_attempt(call_id='synthetic-candidate',attempt_number=1,provider='test',model='fixture',
                stage='generation',endpoint_type='unknown',request={'prompt_hash':candidate._sha256_text(prompt)},
                max_output_tokens=1000,operation=lambda:ModelResponse(PROSE,actual_model='fixture',endpoint_type='unknown')).text
        service=app.state.chapter_candidates
        if hasattr(service,'legacy'):service=service.legacy
        service.executor_factory=lambda root:candidate.ChapterCandidateExecutor(story_project_root=root,generator=generator,
            execution_kind='model',provenance_factory=_fixed_provenance)
    with TestClient(app) as client:
        _unlock(client);draft=_prepare_draft(client,project['id'],key='offline-candidate')
        response=_start_extraction(client,draft,key='offline-facts');assert response.status_code==200,response.text
        assert response.json()['fact_extraction']['status']=='completed'
        record_dir=next((root/'.novelagent/runtime/fact_extractions').iterdir())
        gate=app.state.maintenance;op=gate.begin()['operation_id']
        yield {'root':root,'project':app.state.projects.get(project['id']),'draft':draft,'fact_dir':record_dir,
            'candidate_dir':root/'.novelagent/runtime/candidates'/draft['candidate_id'],
            'service':app.state.backups,'op':op,'app':app,'fact_factory':ff}
        gate.finish(op)


def inspect(c,package):return inspect_archived_executions(package,project_id=c['project'].id,book_id=c['project'].book_id)


def forbid_live(monkeypatch):
    def reject(*args,**kwargs):pytest.fail('archive execution verifier entered live source or runtime constructor')
    for module,cls in ((candidate,candidate.ChapterCandidateStore),(facts,facts.ChapterFactExtractionStore)):
        monkeypatch.setattr(cls,'__init__',reject);monkeypatch.setattr(module,'_load_json',reject)
        monkeypatch.setattr(module,'_resolve_relative_artifact',reject)
    monkeypatch.setattr(ModelCallStore,'__init__',reject)


@pytest.mark.parametrize('execution_archive',['model','deterministic'],indirect=True)
def test_actual_candidate_and_fact_records_match_live_with_source_unavailable(execution_archive,monkeypatch):
    c=execution_archive
    expected=[candidate.ChapterCandidateStore(c['root']).load(c['candidate_dir'].name),
        facts.ChapterFactExtractionStore(c['root']).load(c['fact_dir'].name)]
    package=Path(capture(c)['path']);before=inventory_tree(package);source_before=inventory_tree(c['root'])
    forbid_live(monkeypatch)
    with absent_source(c['root']):first=inspect(c,package);second=inspect(c,package)
    assert first==second and first['executions_verified'],first['blockers']
    assert [item['content_sha256'] for item in first['executions']]==[candidate.canonical_json_hash(x,exclude_environment_fields=False) for x in expected]
    assert not first['restore_plan_ready'] and not first['recoverable']
    assert inventory_tree(package)==before and inventory_tree(c['root'])==source_before


@pytest.mark.parametrize('damage',['candidate_input','candidate_prose','candidate_provenance','fact_source',
    'fact_view','fact_proposal','fact_response','candidate_record'])
def test_required_execution_artifact_damage_blocks_without_live_reads(execution_archive,monkeypatch,damage):
    c=execution_archive;intent=json.loads((c['fact_dir']/'intent.json').read_bytes())
    if damage=='candidate_input':path=c['candidate_dir']/'input.md'
    elif damage=='candidate_prose':path=c['candidate_dir']/'draft.md'
    elif damage=='candidate_record':path=c['candidate_dir']/'record.json'
    elif damage=='candidate_provenance':
        ci=json.loads((c['candidate_dir']/'intent.json').read_bytes())
        path=c['root']/'.novelagent/runtime/runs'/ci['execution']['provenance_artifact_ref']
    elif damage=='fact_source':path=c['root']/'.novelagent/runtime'/intent['draft']['artifact_ref']
    elif damage=='fact_view':path=c['root']/'.novelagent/runtime'/intent['authority_view']['artifact_ref']
    elif damage=='fact_proposal':path=c['fact_dir']/'proposal.json'
    else:
        store=c['root']/'.novelagent/runtime/runs'/intent['execution']['model_calls_ref']
        receipt=json.loads(next((store/'receipts').glob('*.json')).read_bytes());path=store/receipt['response_artifact_ref']
    path.write_text('broken',encoding='utf-8');package=Path(capture(c)['path']);before=inventory_tree(package)
    forbid_live(monkeypatch)
    with absent_source(c['root']):result=inspect(c,package)
    assert not result['executions_verified'] and 'execution_evidence_unverified' in {b['code'] for b in result['blockers']}
    assert inventory_tree(package)==before


@pytest.mark.parametrize('kind',['candidate','fact'])
def test_missing_terminal_record_stays_unsettled_after_archive(execution_archive,monkeypatch,kind):
    c=execution_archive;(c[kind+'_dir']/'record.json').unlink()
    package=Path(capture(c)['path']);forbid_live(monkeypatch)
    with absent_source(c['root']):result=inspect(c,package)
    assert not result['executions_verified'] and 'execution_not_settled' in {b['code'] for b in result['blockers']}


def test_rehashed_fact_proposal_must_still_match_original_model_response(execution_archive):
    c=execution_archive;path=c['fact_dir']/'proposal.json';value=json.loads(path.read_bytes());assert value['facts']
    value['facts']=[];value['proposal_hash']=facts._record_hash(value,'proposal_hash');path.write_text(json.dumps(value))
    path=c['fact_dir']/'record.json';record=json.loads(path.read_bytes())
    record['proposal'].update(sha256=value['proposal_hash'],fact_count=0)
    record['record_hash']=facts._record_hash(record,'record_hash');facts._validate_extraction_record(record)
    path.write_text(json.dumps(record));package=Path(capture(c)['path']);result=inspect(c,package)
    assert not result['executions_verified'] and result['blockers']==[{'code':'execution_evidence_unverified','reference':'fact_extractions'}]
