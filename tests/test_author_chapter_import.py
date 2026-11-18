import json
import socket
from pathlib import Path

import pytest

from tests.test_reviewed_upgrade import case
from tests.test_reviewed_candidate_context import registered_upgrade, author_confirmation
from core.engine.reviewed_candidate_context import ReviewedChapterCandidateContextBuilder
from core.engine.chapter_candidate import ChapterCandidateStore, ChapterCandidateIntegrityError
from core.engine.chapter_fact_contract import fact_contract_example
from core.story_project.paths import infer_next_chapter, resolve_prose
from core.story_project.reviewed_identity import load_verified_reviewed_identity
from workbench.chapter_cards import ChapterCardService
from workbench.chapter_candidates import ChapterCandidateService
from workbench.chapter_facts import ChapterFactReviewService
from workbench.chapter_commits import ChapterCommitPreparationService
from workbench.chapter_publication import ChapterPublicationService


@pytest.fixture
def imported(case, monkeypatch):
    root=case['root']
    outline=next((root/'大纲').glob('*.md'))
    (root/'大纲/细纲_第003章.md').write_text(outline.read_text(encoding='utf-8').replace('第2章','第3章').replace('第002章','第003章'),encoding='utf-8')
    from core.engine.chapter_candidate_context import ChapterCandidateContextBuilder
    case['inputs']['trusted_context']=ChapterCandidateContextBuilder(root).build(2)
    db,projects,project,upgrade=registered_upgrade(case)
    monkeypatch.setattr(socket.socket,'connect',lambda *a,**k:pytest.fail('local import/publication attempted network'))
    cards=ChapterCardService(projects,context_builder_factory=ReviewedChapterCandidateContextBuilder)
    service=ChapterCandidateService(projects=projects,chapter_cards=cards,database=db,
        executor_factory=lambda *_:pytest.fail('author import invoked model executor'))
    confirmation=author_confirmation(projects,project)
    example=fact_contract_example()
    blueprint=ReviewedChapterCandidateContextBuilder(root).build(2).story_project_context['chapter_blueprint']
    good=Path('tests/fixtures/chapter_quality/good_chapter.md').read_text(encoding='utf-8').replace('林雪','林澈').replace('陈岚','周禾')
    text=example['chapter_text']+'\n\n'+good+'\n\n'+'。'.join(b['text'] for b in blueprint['required_beats'])+'。\n\n'+blueprint['ending_pressure']+'。'
    args=dict(confirmation=confirmation,text=text,origin_label='已有作者稿',idempotency_key='author-import-2')
    result=service.import_author_draft(project.id,2,**args)
    return locals()


def test_import_is_immutable_idempotent_and_does_not_create_billing(imported):
    c=imported
    assert c['result']['generation_source']=='author_import'
    assert c['result']['draft']['text']==c['text']
    assert c['service'].import_author_draft(c['project'].id,2,**c['args'])==c['result']
    assert not (c['root']/'.novelagent/runtime/budgets').exists()
    assert not (c['root']/'.novelagent/runtime/runs/executions').exists()
    assert infer_next_chapter(c['root'])==2
    with pytest.raises(Exception,match='同一导入请求'):
        c['service'].import_author_draft(c['project'].id,2,**{**c['args'],'text':'different draft'})


def test_import_rejects_changed_source_and_corrupt_prose(imported):
    c=imported;store=ChapterCandidateStore(c['root']);cid=c['result']['candidate_id']
    outline=next((c['root']/'大纲').glob('*002*'))
    original=outline.read_bytes();outline.write_bytes(original+b'\nchanged source')
    with pytest.raises(Exception):c['service'].import_author_draft(c['project'].id,2,**c['args'])
    outline.write_bytes(original)
    (store.candidate_dir(cid)/'draft.md').write_text('changed',encoding='utf-8')
    with pytest.raises(ChapterCandidateIntegrityError,match='hash mismatch'):
        store.load(cid)


def test_import_proof_loads_from_archive_members_without_live_candidate_reads(imported, monkeypatch):
    c=imported;root=c['root'];cid=c['result']['candidate_id']
    store=ChapterCandidateStore(root);expected=store.load(cid)
    files={path: path.read_bytes() for path in store.candidate_dir(cid).iterdir() if path.is_file()}
    class Archived:
        def __init__(self):self.root=root
        def exists(self,path):return path in files
        def raw(self,base,name):return files[base/name]
        def document(self,base,name):return json.loads(self.raw(base,name))
    from workbench.backup_archive_executions import _Candidates
    monkeypatch.setattr(Path,'read_bytes',lambda *a:pytest.fail('archive import proof accessed live file'))
    monkeypatch.setattr(Path,'read_text',lambda *a,**k:pytest.fail('archive import proof accessed live file'))
    assert _Candidates(Archived()).load(cid)==expected


def test_imported_text_normal_fact_review_quality_commit_and_cold_read(imported):
    c=imported;root=c['root'];draft=c['service'].drafts.get(c['result']['draft']['id'])
    reviews=ChapterFactReviewService(database=c['db'],projects=c['projects'],candidate_drafts=c['service'].drafts,
        context_builder_factory=ReviewedChapterCandidateContextBuilder)
    review=reviews.create(draft.id,expected_draft_revision=draft.revision,expected_draft_sha256=draft.sha256,
        proposals=[f for f in c['example']['proposal']['facts'] if f['kind'] in {'event','counter'}],author_confirmed=True)
    facts=review['facts']
    for fact in facts:fact['review']['decision']='accepted'
    review=reviews.update(review['id'],facts=facts,expected_revision=review['revision'])
    review=reviews.confirm(review['id'],expected_revision=review['revision'],expected_facts_sha256=review['facts_sha256'],
        expected_draft_revision=draft.revision,expected_draft_sha256=draft.sha256,author_confirmed=True)
    preparations=ChapterCommitPreparationService(database=c['db'],projects=c['projects'],candidate_drafts=c['service'].drafts,
        fact_reviews=reviews,context_builder_factory=ReviewedChapterCandidateContextBuilder)
    from workbench.book_ownership import BookOwnership
    machine=root.parent/'machine'
    with BookOwnership(c['project'].book_id,machine_store=machine).locked() as owner:
        with c['db'].transaction() as connection:
            owner.enroll(root=root,database=c['db'],project_id=c['project'].id,connection=connection)
    publication=ChapterPublicationService(preparations=preparations,machine_store=machine,
        execution_budget_factory=lambda *a,**k:pytest.fail('author publication attempted model budget'))
    route=dict(project_id=c['project'].id,chapter=2,review_id=review['id'],expected_draft_revision=draft.revision,
        expected_review_revision=review['revision'],expected_draft_sha256=draft.sha256,
        expected_facts_sha256=review['facts_sha256'],idempotency_key='author-publish-2')
    preview=publication.preview(**route)
    assert preview['execution_mode']=='local_author_review' and preview['provider_calls']==0
    result=publication.commit(**route)
    assert result['publication_verified']
    assert publication.resolve(**route)==result
    assert infer_next_chapter(root)==3
    assert resolve_prose(root,2).path.read_text(encoding='utf-8')==c['text'].rstrip()+'\n'
    verified=load_verified_reviewed_identity(root)
    assert verified['history']['value']['committed_through']==2
    assert ReviewedChapterCandidateContextBuilder(root).build(3).story_project_context['reviewed_state']['authoritative_state']==verified['history']['value']['current_state']
    assert not (root/'.novelagent/runtime/budgets').exists()
