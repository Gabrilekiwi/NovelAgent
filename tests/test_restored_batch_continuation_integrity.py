"""Real restored preparation rejects damaged proofs and never replays old work."""
import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from tests.test_reviewed_upgrade import case
from tests.test_reviewed_batches import batch_case
from tests.test_reviewed_batch_api import web
from tests.test_final_batch_restore import exercise_final_restore
from tests.test_reviewed_batch_driver import calls
from workbench.app import create_app
from workbench.book_ownership import BookOwnership
from core.story_project.reviewed_state_document import digest
from core.chapter_execution_budget import ChapterExecutionBudget


def test_restored_preparation_cold_reads_original_request_and_each_required_proof_never_dispatch(web):
    c,_,_,_=web
    exercise_final_restore(web,preparation_only=True)
    target=c['root'].parent/'restored';root=target/'book';runtime=root/'.novelagent/runtime'
    grants=list((runtime/'reviewed/batches').glob('*.json'));assert len(grants)==1
    grant=json.loads(grants[0].read_text(encoding='utf-8'));batch=grant['manifest']['batch_id']
    path='/api/projects/'+c['project'].id+'/reviewed-batches/'+batch
    folders=list((runtime/'reviewed/continuations').iterdir());assert len(folders)==1
    base=folders[0];intent=json.loads((base/'intent.json').read_text(encoding='utf-8'))
    owner=BookOwnership(c['project'].book_id,machine_store=c['machine'])
    def spending():
        return {n:ChapterExecutionBudget(root,book_id=c['project'].book_id,chapter=n).store.report()['charged'] for n in (2,3,4)}
    initial_spending=spending();matrix=[]
    original=json.loads((base/'control-origin.json').read_text(encoding='utf-8'))
    start=next(cmd for cmd in original['commands'] if cmd['action']=='start')
    prefix={**original,'commands':original['commands'][:start['sequence']-1]}
    prefix['record_sha256']=digest({k:v for k,v in prefix.items() if k!='record_sha256'})
    original_request={'action':'start','expected_control_sha256':prefix['record_sha256'],'author_confirmed':True}
    proof_paths=[base/name for name in ('intent.json','grant.json','anchor.json','control.json','control-origin.json','grant-ready.json','ready.json')]
    proof_paths += [owner.directory/name for name in ('brc-'+base.name+'.json','brcc-'+base.name+'.json','bg-'+batch+'.json','bc-'+batch+'.json')]
    unchanged={p:p.read_bytes() for p in proof_paths}
    for cold in range(2):
        app=create_app(database_path=target/'workbench.sqlite',start_worker=True,auto_import_active=True,
            chapter_publication_machine_store=c['machine'])
        with TestClient(app) as client:
            # Match the real browser entry, which establishes this new
            # workbench instance's session before any POST request.
            assert client.get('/reviewed-batches').status_code==200
            service=app.state.reviewed_batches
            service.tasks.driver_factory=lambda _:pytest.fail('cold read/invalid evidence created a driver')
            assert app.state.supervisor._thread is None
            response=client.get(path);assert response.status_code==200,response.text
            view=response.json();assert not view['execution_live'] and view['explicit_resume_required']
            replay=client.post(path+'/commands',json=original_request,headers={'Idempotency-Key':'restore-source-start'})
            assert replay.status_code==200,replay.text
            again=client.post(path+'/restored-continuation',json={'expected_preview_sha256':intent['preview']['record_sha256'],'author_confirmed':True})
            assert again.status_code==200,again.text
            assert service.tasks._active is None and not service.controls.handles
            if cold==0:
                for proof in proof_paths:
                    for damage in ('missing','malformed'):
                        try:
                            if damage=='missing':proof.unlink()
                            else:proof.write_bytes(b'{invalid proof')
                            observed=client.get(path)
                            if proof==base/'ready.json' and damage=='missing':
                                assert observed.status_code==200 and observed.json()['history_only'],observed.text
                            else:assert observed.status_code==409,(str(proof),damage,observed.text)
                            denied=client.post(path+'/commands',json={'action':'resume','expected_control_sha256':view['control']['control']['record_sha256'],
                                'author_confirmed':True},headers={'Idempotency-Key':'must-not-dispatch'})
                            assert denied.status_code==409,(str(proof),damage,denied.text)
                            assert service.tasks._active is None and not service.controls.handles
                            matrix.append({'proof':str(proof.relative_to(c['root'].parent)),'damage':damage,'resume_status':denied.status_code})
                        finally:proof.write_bytes(unchanged[proof])
                valid=client.get(path);assert valid.status_code==200,valid.text
            assert spending()==initial_spending
            assert [calls({'root':root,'project':c['project']},n) for n in (2,3,4)]==[2,1,0]
            assert {p:p.read_bytes() for p in proof_paths}==unchanged
    assert len(matrix)==22
    (c['root'].parent/'t72-integrity-evidence.json').write_text(json.dumps({'cases':matrix,'cold_instances':2,'calls':[2,1,0]},indent=2),encoding='utf-8')
