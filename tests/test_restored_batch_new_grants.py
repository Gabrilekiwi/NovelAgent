"""A real restored book may authorize another batch after completing its old one."""
from fastapi.testclient import TestClient
from tests.test_reviewed_upgrade import case
from tests.test_reviewed_batches import batch_case
from tests.test_reviewed_batch_api import web
from tests.test_final_batch_restore import exercise_final_restore
from tests.test_reviewed_batch_driver import calls
from workbench.app import create_app


def test_completed_restored_batch_and_new_current_root_grant_share_public_list_without_dispatch(web):
    c,_,_,_=web
    exercise_final_restore(web)
    target=c['root'].parent/'restored';root=target/'book'
    outline=root/'大纲/细纲_第005章.md'
    outline.write_text((root/'大纲/细纲_第004章.md').read_text(encoding='utf-8')
        .replace('第4章','第5章').replace('第004章','第005章'),encoding='utf-8')
    base='/api/projects/'+c['project'].id+'/reviewed-batches'
    app=create_app(database_path=target/'workbench.sqlite',start_worker=True,auto_import_active=True,
        chapter_publication_machine_store=c['machine'])
    with TestClient(app) as client:
        assert client.get('/reviewed-batches').status_code==200
        before=client.get(base);assert before.status_code==200,before.text
        original=before.json()['items'];assert len(original)==1
        body={'end_chapter':5,'configuration':c['route']['configuration']}
        headers={'Idempotency-Key':'new-current-root-after-restore'}
        preview=client.post(base+'/preview',json=body,headers=headers);assert preview.status_code==200,preview.text
        body.update(expected_manifest_sha256=preview.json()['manifest']['manifest_sha256'],author_confirmed=True)
        result=client.post(base,json=body,headers=headers);assert result.status_code==200,result.text
        view=result.json();new=view['grant']['manifest']['batch_id']
        assert view['grant']['manifest']['source_root']==str(root)
        assert view['control']['progress']['status']=='paused' and not view['execution_live']
        for _ in range(2):
            listed=client.get(base);assert listed.status_code==200,listed.text
            assert {item['batch_id'] for item in listed.json()['items']}=={original[0]['batch_id'],new}
            current=client.get(base+'/'+new);assert current.status_code==200,current.text
            assert current.json()['control']['progress']['status']=='paused' and not current.json()['execution_live']
            old=client.get(base+'/'+original[0]['batch_id']);assert old.status_code==200,old.text
            assert old.json()['control']['progress']['status']=='completed'
        assert app.state.reviewed_batches.tasks._active is None and not app.state.reviewed_batches.controls.handles
        context={'root':root,'project':c['project']}
        # Includes the explicit author fact extraction in exercise_final_restore.
        assert [calls(context,n) for n in (2,3,4,5)]==[2,2,2,0]
