"""Actual owned reviewed books and immutable original author grant witnesses."""
import copy,json,socket,subprocess,sys
from dataclasses import replace
from pathlib import Path

import pytest

from api.budget_endpoint import endpoint_fingerprint
from core.config import get_config
from core.engine.chapter_candidate_context import ChapterCandidateContextBuilder
from core.engine.reviewed_candidate_context import ReviewedChapterCandidateContextBuilder
from core.story_project.reviewed_state_document import digest
from tests.test_reviewed_upgrade import case
from tests.test_reviewed_candidate_context import registered_upgrade
from workbench.book_ownership import BookOwnership
from workbench.chapter_candidates import CandidateDraftStore
from workbench.chapter_facts import ChapterFactReviewService
from workbench.chapter_commits import ChapterCommitPreparationService
from workbench.chapter_publication import ChapterPublicationService
from workbench.reviewed_batches import ReviewedBatchService,ReviewedBatchError
from workbench.backup_files import inventory_tree


@pytest.fixture
def batch_case(case,monkeypatch):
    root=case['root'];outline=next((root/'大纲').glob('*.md'))
    (root/'大纲/细纲_第003章.md').write_text(outline.read_text(encoding='utf-8').replace('第2章','第3章').replace('第002章','第003章'),encoding='utf-8')
    case['inputs']['trusted_context']=ChapterCandidateContextBuilder(root).build(2)
    db,projects,project,upgrade=registered_upgrade(case)
    drafts=CandidateDraftStore(db)
    reviews=ChapterFactReviewService(database=db,projects=projects,candidate_drafts=drafts,
        context_builder_factory=ReviewedChapterCandidateContextBuilder)
    preparations=ChapterCommitPreparationService(database=db,projects=projects,candidate_drafts=drafts,
        fact_reviews=reviews,context_builder_factory=ReviewedChapterCandidateContextBuilder)
    machine=root.parent/'m'
    with BookOwnership(project.book_id,machine_store=machine).locked() as owner:
        with db.transaction() as connection:owner.enroll(root=root,database=db,project_id=project.id,connection=connection)
    publication=ChapterPublicationService(preparations=preparations,machine_store=machine)
    # Synthetic exact model/tariff; no actual account or SDK calls.
    config=replace(get_config(),openai_model='fixture',openai_base_url='https://api.openai.com/v1',openai_max_output_tokens=200)
    monkeypatch.setattr('workbench.reviewed_batches.get_config',lambda:config)
    def deny(*args,**kwargs):pytest.fail('batch authorization called a Provider/network')
    monkeypatch.setattr(socket.socket,'connect',deny)
    price=dict(provider='openai',model='fixture',endpoint_type='official',
        endpoint_fingerprint=endpoint_fingerprint('openai','https://api.openai.com/v1'),currency='USD',
        input_price_per_million='1',output_price_per_million='2',input_token_semantics='input_includes_cache',
        cache_read_price_per_million='0.1',cache_write_5m_price_per_million='1.25',cache_write_1h_price_per_million='2')
    configuration={'chapter_limits':dict(max_provider_calls=10,max_input_tokens=100000,max_output_tokens=10000,
        max_active_seconds='900',amount_limit='1',currency='USD',pricing=[price]),
        'batch_limits':dict(max_provider_calls=20,max_input_tokens=200000,max_output_tokens=20000,amount_limit='1.5')}
    service=ReviewedBatchService(publication)
    route=dict(project_id=project.id,end_chapter=3,configuration=configuration,idempotency_key='finite-batch')
    return dict(root=root,db=db,project=project,publication=publication,service=service,route=route,machine=machine,config=config)


def approved(c):
    preview=c['service'].preview(**c['route'])
    return c['service'].authorize(**c['route'],expected_manifest_sha256=preview['manifest']['manifest_sha256'],author_confirmed=True)


def paths(c,result):
    batch=result['grant']['manifest']['batch_id']
    owner=BookOwnership(c['project'].book_id,machine_store=c['machine'])
    return owner.directory/('bg-'+batch+'.json'),c['root']/'.novelagent/runtime/reviewed/batches'/(batch+'.json'),owner.directory/('bgr-'+batch+'.json')


def test_real_server_preview_is_read_only_and_original_grant_persists_without_execution(batch_case):
    c=batch_case;before=inventory_tree(c['root'])
    preview=c['service'].preview(**c['route']);manifest=preview['manifest']
    after=inventory_tree(c['root'])
    # The existing owned boundary lazily creates its one-byte Provider mutex
    # on first use. It must not create budget, grant, draft or formal content.
    new=set(after['files'])-set(before['files'])
    assert new=={'.novelagent/runtime/runs/.provider-boundary-lock/.persistence.lock'}
    assert all(after['files'][p]==value for p,value in before['files'].items())
    assert after['total_bytes']==before['total_bytes']+1
    c['service'].preview(**c['route'])
    assert inventory_tree(c['root'])==after
    assert manifest['chapters']==[2,3] and [b['blueprint']['chapter_index'] for b in manifest['outlines']]==[2,3]
    assert manifest['model_selection']['openai_model']=='fixture' and 'api_key' not in json.dumps(manifest)
    assert manifest['action_policy']['max_repair_cycles']==1 and manifest['review_source']=='policy'
    assert manifest['restart_behavior']=='explicit_resume_required'
    result=c['service'].authorize(**c['route'],expected_manifest_sha256=manifest['manifest_sha256'],author_confirmed=True)
    assert result['state']=='authorized_paused' and not result['execution_permit'] and not result['provider_calls_started']
    assert result['grant']['grant_source']=='author' and result['grant']['automatic_review_source']=='policy'
    machine,book,ready=paths(c,result);original=[p.read_bytes() for p in (machine,book,ready)]
    restarted=ReviewedBatchService(c['publication'])
    assert restarted.get(project_id=c['project'].id,batch_id=manifest['batch_id'])==result
    assert restarted.require_current_start(project_id=c['project'].id,batch_id=manifest['batch_id'])==result
    assert [p.read_bytes() for p in (machine,book,ready)]==original
    assert not (c['root']/'.novelagent/runtime/candidates').exists()
    assert not (c['root']/'.novelagent/runtime/reviewed/intents').exists()


@pytest.mark.parametrize('kind',['false','stale','forged_config'])
def test_authorization_rejects_missing_confirmation_or_unreviewed_changes_before_writes(batch_case,kind):
    c=batch_case;preview=c['service'].preview(**c['route']);route=copy.deepcopy(c['route'])
    expected=preview['manifest']['manifest_sha256'];confirm=kind!='false'
    if kind=='stale':expected='f'*64
    if kind=='forged_config':route['configuration']['batch_limits']['amount_limit']='10'
    before=inventory_tree(c['root'])
    with pytest.raises(ReviewedBatchError):c['service'].authorize(**route,expected_manifest_sha256=expected,author_confirmed=confirm)
    assert inventory_tree(c['root'])==before
    assert not list(c['machine'].rglob('bg-*.json'))


@pytest.mark.parametrize('kind',['future_outline','model','policy'])
def test_drift_after_original_grant_prevents_start_but_preserves_original_author_consent(batch_case,monkeypatch,kind):
    c=batch_case;result=approved(c);manifest=result['grant']['manifest'];machine,book,ready=paths(c,result)
    original=[p.read_bytes() for p in (machine,book,ready)]
    if kind=='future_outline':
        path=c['root']/'大纲/细纲_第003章.md';path.write_text(path.read_text(encoding='utf-8')+'\n作者新增节拍。',encoding='utf-8')
    elif kind=='model':monkeypatch.setattr('workbench.reviewed_batches.get_config',lambda:replace(c['config'],openai_model='changed-model'))
    else:monkeypatch.setattr('workbench.reviewed_batches.reviewed_quality_action_policy',lambda:{'version':'changed'})
    assert c['service'].get(project_id=c['project'].id,batch_id=manifest['batch_id'])==result
    with pytest.raises(ValueError):c['service'].require_current_start(project_id=c['project'].id,batch_id=manifest['batch_id'])
    assert [p.read_bytes() for p in (machine,book,ready)]==original


@pytest.mark.parametrize('kind',['machine','book','ready','forged_book'])
def test_lost_or_forged_original_grant_cannot_be_reconstructed_from_client_or_book_hash(batch_case,kind):
    c=batch_case;result=approved(c);manifest=result['grant']['manifest'];machine,book,ready=paths(c,result)
    if kind=='forged_book':
        value=json.loads(book.read_text(encoding='utf-8'));value['manifest']['end_chapter']=64
        value['manifest']['manifest_sha256']=digest({k:v for k,v in value['manifest'].items() if k!='manifest_sha256'})
        value['grant_sha256']=digest({k:v for k,v in value.items() if k!='grant_sha256'})
        book.write_text(json.dumps(value),encoding='utf-8')
    else:{'machine':machine,'book':book,'ready':ready}[kind].unlink()
    before=inventory_tree(c['root'])
    with pytest.raises(ReviewedBatchError):c['service'].get(project_id=c['project'].id,batch_id=manifest['batch_id'])
    assert inventory_tree(c['root'])==before
    if kind=='machine':
        with pytest.raises(ReviewedBatchError):c['service'].authorize(**c['route'],expected_manifest_sha256=manifest['manifest_sha256'],author_confirmed=True)
        assert not machine.exists()
    if kind in {'book','forged_book'}:
        with pytest.raises(ReviewedBatchError):c['service'].authorize(**c['route'],expected_manifest_sha256=manifest['manifest_sha256'],author_confirmed=True)
        assert inventory_tree(c['root'])==before


@pytest.mark.parametrize('phase',['after_machine_grant','after_batch_budget','after_book_grant','after_grant_ready'])
def test_explicit_original_authorization_retry_recovers_partial_publication_without_auto_execution(batch_case,phase):
    c=batch_case;preview=c['service'].preview(**c['route']);manifest=preview['manifest']
    class Stop(BaseException):pass
    def stop(event):
        if event==phase:raise Stop()
    c['service'].fault_injector=stop
    with pytest.raises(Stop):c['service'].authorize(**c['route'],expected_manifest_sha256=manifest['manifest_sha256'],author_confirmed=True)
    restarted=ReviewedBatchService(c['publication'])
    if phase!='after_grant_ready':
        with pytest.raises(ReviewedBatchError):restarted.get(project_id=c['project'].id,batch_id=manifest['batch_id'])
    result=restarted.authorize(**c['route'],expected_manifest_sha256=manifest['manifest_sha256'],author_confirmed=True)
    assert result['state']=='authorized_paused' and not result['execution_permit']
    machine,book,ready=paths(c,result)
    assert json.loads(machine.read_bytes())==json.loads(book.read_bytes())


def cold_service(spec):
    """Build real owned services using existing SQL/files; no fixture creation."""
    from types import SimpleNamespace
    from workbench.database import WorkbenchDatabase
    from workbench.reviewed_projects import ReviewedProjectRegistry
    import workbench.reviewed_batches as batches
    db=WorkbenchDatabase(spec['database']);root=Path(spec['root'])
    projects=ReviewedProjectRegistry(db,allowed_workspaces=[root.parent])
    drafts=CandidateDraftStore(db)
    reviews=ChapterFactReviewService(database=db,projects=projects,candidate_drafts=drafts,
        context_builder_factory=ReviewedChapterCandidateContextBuilder)
    preparations=ChapterCommitPreparationService(database=db,projects=projects,candidate_drafts=drafts,
        fact_reviews=reviews,context_builder_factory=ReviewedChapterCandidateContextBuilder)
    publication=ChapterPublicationService(preparations=preparations,machine_store=spec['machine'])
    batches.get_config=lambda:SimpleNamespace(**spec['config'])
    def deny(*args,**kwargs):raise AssertionError('cold grant operation attempted network')
    socket.socket.connect=deny
    return ReviewedBatchService(publication)


CHILD=r'''
import json,os,sys
from pathlib import Path
from tests.test_reviewed_batches import cold_service
spec=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
service=cold_service(spec)
def stop(event):
    if event==spec['phase']:os._exit(77)
service.fault_injector=stop
if sys.argv[2]=='crash':
    service.authorize(**spec['route'],expected_manifest_sha256=spec['expected'],author_confirmed=True)
else:
    result=service.get(project_id=spec['route']['project_id'],batch_id=spec['batch_id'])
    assert result['state']=='authorized_paused' and not result['execution_permit']
    assert service.require_current_start(project_id=spec['route']['project_id'],batch_id=spec['batch_id'])==result
    print(result['grant']['grant_sha256'],flush=True)
'''


@pytest.mark.parametrize('phase',['after_machine_grant','after_batch_budget','after_book_grant','after_grant_ready'])
def test_actual_grant_process_death_and_cold_service_keep_original_authorization_paused(batch_case,phase):
    c=batch_case;preview=c['service'].preview(**c['route']);manifest=preview['manifest']
    selection=manifest['model_selection']
    spec={'root':str(c['root']),'database':str(c['db'].path),'machine':str(c['machine']),
        'config':{k:v for k,v in selection.items() if k.startswith(('openai_','provider_'))},
        'route':c['route'],'expected':manifest['manifest_sha256'],'batch_id':manifest['batch_id'],'phase':phase}
    spec['config']['openai_base_url']=c['config'].openai_base_url
    path=c['root'].parent/'grant-process.fixture.json';path.write_text(json.dumps(spec),encoding='utf-8')
    child=subprocess.run([sys.executable,'-B','-c',CHILD,str(path),'crash'],capture_output=True,text=True,timeout=120)
    assert child.returncode==77,child.stderr
    owner=BookOwnership(c['project'].book_id,machine_store=c['machine'])
    original=(owner.directory/('bg-'+manifest['batch_id']+'.json')).read_bytes()
    service=ReviewedBatchService(c['publication'])
    if phase!='after_grant_ready':
        with pytest.raises(ReviewedBatchError):service.get(project_id=c['project'].id,batch_id=manifest['batch_id'])
    result=service.authorize(**c['route'],expected_manifest_sha256=manifest['manifest_sha256'],author_confirmed=True)
    assert (owner.directory/('bg-'+manifest['batch_id']+'.json')).read_bytes()==original
    before=inventory_tree(c['root'])
    cold=subprocess.run([sys.executable,'-B','-c',CHILD,str(path),'read'],capture_output=True,text=True,timeout=60)
    assert cold.returncode==0,cold.stderr
    assert cold.stdout.strip()==result['grant']['grant_sha256']
    assert inventory_tree(c['root'])==before
    assert not (c['root']/'.novelagent/runtime/candidates').exists()
