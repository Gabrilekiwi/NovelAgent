"""Exercise the public author routes without a provider or a pre-upgraded fixture."""
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient
from core.state.authoritative import empty_authoritative_state
from core.engine.reviewed_candidate import ReviewedChapterCandidateExecutor
from core.story_project.reviewed_identity import load_verified_reviewed_identity
from modules.chapter_generator.generator import prepare_chapter_messages
from tests.test_chapter_fact_support import identity, support
from workbench.app import create_app


@contextmanager
def open_desk(base, *, machine_store=None):
    app = create_app(database_path=base/'data/w.db', runtime_root=base/'jobs', allowed_workspaces=[base],
                     chapter_publication_machine_store=machine_store or base/'owner', start_worker=False, auto_import_active=False)
    with TestClient(app) as client:
        client.get('/')
        yield client


def response(r):
    assert r.is_success, (r.status_code, r.text)
    return r.json()


OUTLINE = dict(title='备用通道', core_event='林雪在备用通道找到窄梯', required_beats=['备用通道', '窄梯'], ending_pressure='身后，陈岚第一次开枪。')
SETTING = '林雪负责主控室的夜班巡查。'
QUOTE = '林雪把铜哨留在窄梯的第三级。'
EVENT = dict(local_key='left_whistle', kind='event', summary='铜哨被留在窄梯的第三级', evidence_quotes=[QUOTE], proposal={
    'event_type':'left_whistle','status':'completed','occurrence_location':'local:stairs',
    'onsite_participants':[{'entity_id':'local:lin','role':'actor'}], 'remote_participants':[], 'transported_items':[], 'related_entities':[]})


def create(c, base, settings):
    return response(c.post('/api/projects',json={'title':'窄梯','target':str(base/'book'), 'writing_mode':'review_each_chapter', 'initial_settings':settings}))['project']


def outline(c, path, data):
    return response(c.post(path+'/author-outline',json=data))


def messages(root, number, card):
    request = ReviewedChapterCandidateExecutor(story_project_root=root).prepare_policy_request(chapter_index=number, chapter_intent=card['intent_defaults'])
    return '\n'.join(m['content'] for m in prepare_chapter_messages(request['input_pack'])[0])


def test_new_book_first_publication_and_cold_next_chapter(tmp_path):
    root = tmp_path/'book'
    # Machine store + book digest + transient lease filename exceed MAX_PATH.
    machine_store = tmp_path / 'owner'
    while len(str(machine_store)) < 210:
        machine_store /= 'nested-machine-records'
    with open_desk(tmp_path, machine_store=machine_store) as c:
        project=create(c,tmp_path,SETTING)
        assert project['chapter_count']==0 and project['capabilities']['new_book_outline']
        base='/api/projects/'+project['id'];path=base+'/chapters/1'
        outline(c,path,OUTLINE)
        assert c.post(path+'/author-outline',json={**OUTLINE,'title':'覆盖'}).status_code==409
        card=response(c.get(path+'/card'))['chapter_card']
        assert SETTING in messages(root,1,card)
        confirm=response(c.post(path+'/card/confirm',json={'confirmed':True,'expected_card_sha256':card['confirmation']['card_sha256'],
            'goal':OUTLINE['core_event'],'validation_focus':['continuity','logic'],'notes_text':''}))['confirmation']
        assert response(c.get(path+'/budget'))['state']=='not_configured'
        assert response(c.post(path+'/generation-estimate',json={'confirmation':confirm}))['estimate']['input_tokens_estimate']>0
        assert response(c.get(path+'/budget'))['state']=='not_configured' and not response(c.get(path+'/candidates'))['items']
        initial=Path('tests/fixtures/chapter_quality/good_chapter.md').read_text(encoding='utf8')
        draft=response(c.post(path+'/author-drafts',json={'confirmation':confirm,'origin_label':'作者手稿','text':initial,'idempotency_key':'first-draft'}))['candidate']['draft']
        edited=initial.replace('她咬住牙，继续往下。',QUOTE+'她咬住牙，继续往下。')
        draft=response(c.put('/api/candidate-drafts/'+draft['id'],json={'text':edited,'expected_revision':draft['revision']}))['draft']
        review=response(c.post('/api/candidate-drafts/'+draft['id']+'/fact-reviews',json={'proposals':[EVENT, {**EVENT, 'local_key':'rejected_event','summary':'铜哨已被人取走','proposal':{**EVENT['proposal'],'event_type':'took_whistle'}}], 'author_confirmed':True,
            'expected_draft_revision':draft['revision'],'expected_draft_sha256':draft['sha256']}))['fact_review']
        rev='/api/fact-reviews/'+review['id']
        for f in review['facts']:
            f['review']['decision']='accepted' if f['summary']==EVENT['summary'] else 'rejected'
        review=response(c.put(rev,json={'expected_revision':review['revision'],'facts':review['facts']}))['fact_review']
        review=response(c.put(rev+'/support',json={'expected_revision':review['revision'],'author_confirmed':True,
            'support':support([identity('lin','character','林雪'),identity('stairs','location','窄梯')],[])}))['fact_review']
        details=dict(expected_draft_revision=draft['revision'],expected_draft_sha256=draft['sha256'])
        review=response(c.post(rev+'/confirm',json={**details,'expected_revision':review['revision'],'expected_facts_sha256':review['facts_sha256'],'author_confirmed':True}))['fact_review']
        body={**details,'review_id':review['id'],'expected_review_revision':review['revision'],'expected_facts_sha256':review['facts_sha256']}
        headers={'Idempotency-Key':'new-book-first-publication'}
        preview=response(c.post(path+'/publication-preview',json=body,headers=headers));assert preview['quality']['local_quality_passed'], preview['quality']
        assert not list((root/'正文').iterdir())
        result=response(c.post(path+'/publication',json={**body,'author_confirmed':True},headers=headers));assert result['publication']['publication_verified'], result
    with open_desk(tmp_path, machine_store=machine_store) as c:
        assert response(c.get(path))['current_prose']==edited
        assert response(c.get(base))['project']['next_chapter']==2
        second=base+'/chapters/2';outline(c,second,{**OUTLINE,'title':'回声'})
        prompt=messages(root,2,response(c.get(second+'/card'))['chapter_card'])
        memory=response(c.get(second))['chapter_memory']
        assert memory == {'settings':[SETTING], 'previous_facts':[{'summary':EVENT['summary'], 'quotes':[QUOTE]}]}
        assert '铜哨已被人取走' not in prompt
        assert SETTING in prompt and QUOTE in prompt and EVENT['summary'] in prompt and 'Previous Chapter Confirmed Facts' in prompt
        assert len(list((root/'正文').iterdir()))==1


def test_blank_start_and_corrupt_receipt_fail_closed(tmp_path):
    root=tmp_path/'book'
    with open_desk(tmp_path) as c:
        project=create(c,tmp_path,''); value=load_verified_reviewed_identity(root)['history']['value']
        assert value['current_state'] == empty_authoritative_state() and value['static_references']==[]
        start=root/'.novelagent/runtime/reviewed/receipts/start.json';start.write_text('{}')
        detail=response(c.get('/api/projects/'+project['id']))['project']
        assert detail['reviewed_status']['state']=='evidence_invalid' and detail['next_chapter'] is None
