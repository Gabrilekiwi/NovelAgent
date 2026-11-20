"""Actual restored chapter publication with prior spending and fresh execution IDs."""
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from api.contracts import ModelResponse, reported_model_usage
from core.chapter_budget_store import ChapterBudgetStore
from core.chapter_model_budget import ChapterModelCallBudget
from core.engine.chapter_fact_contract import fact_contract_example
from core.engine.reviewed_candidate import ReviewedChapterCandidateExecutor
from core.engine.reviewed_candidate_context import ReviewedChapterCandidateContextBuilder
from core.model_call_runtime import ModelCallRuntimeContext
from core.model_calls import ModelCallStore
from core.story_project.paths import infer_next_chapter, resolve_prose
from core.story_project.reviewed_identity import load_verified_reviewed_identity
from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_backup_archive_database import private_temporary_directory
from tests.test_backup_archive_history import absent_source
from tests.test_backup_restore_database import bound_copy
from tests.test_chapter_budget_runtime import args
from tests.test_restore_activation import full_source, prepared, activate
from tests.test_restore_budget_preparation import financial
from tests.test_restore_executions import prepared as jobs_prepared
from tests.test_restore_handoff import restored, deny
from tests.test_restore_jobs import prepared as budget_prepared
from tests.test_restore_usage import pending
from tests.test_workbench_chapter_candidates import _fixed_provenance
from workbench.backup_files import inventory_tree
from workbench.app import create_app
from workbench.chapter_candidates import CandidateDraftStore
from workbench.chapter_cards import ChapterCardService
from workbench.chapter_commits import ChapterCommitPreparationService
from workbench.chapter_facts import ChapterFactReviewService
from workbench.chapter_publication import ChapterPublicationService
from workbench.database import WorkbenchDatabase
from workbench.model_usage import read_project_model_usage
from workbench.reviewed_projects import ReviewedProjectRegistry


def publish_restored_next_chapter(c):
    """Shared actual candidate/review/publication flow for restore acceptance."""
    activate(c); root = c['target']/'book'; source = inventory_tree(c['root'])
    with absent_source(c['root']):
        database = WorkbenchDatabase(c['target']/'workbench.sqlite', machine_store=c['store'])
        projects = ReviewedProjectRegistry(database, allowed_workspaces=[root.parent])
        project = projects.get(c['project'].id)
        assert project.root == root and infer_next_chapter(root) == 3
        chapter2 = resolve_prose(root, 2).path.read_bytes()
        previous = load_verified_reviewed_identity(root)['history']['value']['current_state']
        builder = ReviewedChapterCandidateContextBuilder
        assert builder(root).build(3).story_project_context['reviewed_state']['authoritative_state'] == previous
        cards = ChapterCardService(projects, context_builder_factory=builder)
        card = cards.get(project.id, 3).to_dict()
        confirmation = cards.confirm(project.id, 3, {'confirmed':True,
            'expected_card_sha256':card['confirmation']['card_sha256'], 'goal':'恢复后继续检查闸门',
            'validation_focus':['continuity','spatial','logic'], 'notes_text':''})
        example = fact_contract_example()
        event = '周岚与林澈在旧城站检查闸门。'
        counter = '旧城站警戒值增加1点'
        blueprint = builder(root).build(3).story_project_context['chapter_blueprint']
        good = Path('tests/fixtures/chapter_quality/good_chapter.md').read_text(encoding='utf-8')
        good = good.replace('林雪','林澈').replace('陈岚','周禾')
        text = event+counter+'。\n\n'+good+'\n\n'+'。'.join(b['text'] for b in blueprint['required_beats'])
        text += '。\n\n'+blueprint['ending_pressure']+'。'
        budget = ChapterBudgetStore(root/'.novelagent/runtime/budgets/c3', book_id=project.book_id, chapter=3)
        retained = budget.usage_snapshot()
        assert retained['report']['charged_amount'] == '0.000169' and len(retained['attempts']) == 2
        execution = 'execution_restored_chapter_3'
        runtime = ModelCallRuntimeContext(ModelCallStore(root/'.novelagent/runtime/runs/executions'/execution/'model_calls'),
            chapter_budget=ChapterModelCallBudget(budget, execution_id=execution))
        calls = []
        def synthetic_response():
            calls.append(1)
            return ModelResponse(text, actual_model='fixture', endpoint_type='official',
                usage=reported_model_usage({'input_tokens':10,'output_tokens':20,
                    'cache_read_input_tokens':0,'cache_creation_input_tokens':0}))
        def generate(_):
            return runtime.execute_attempt(**args(call_id='restored-chapter-3', operation=synthetic_response))
        executor = ReviewedChapterCandidateExecutor(story_project_root=root, generator=generate,
            execution_kind='deterministic', provenance_factory=_fixed_provenance)
        record = executor.execute(chapter_index=3, confirmation=confirmation, idempotency_key='restored-candidate-3')
        assert record['status'] == 'completed' and record['candidate_id'] != c['record']['candidate_id']
        drafts = CandidateDraftStore(database)
        draft = drafts.create_from_candidate(project_id=project.id, chapter=3, candidate_id=record['candidate_id'],
            source_sha256=record['prose']['sha256'], source_artifact_ref=record['prose']['artifact_ref'], text=text)
        proposals = [f for f in example['proposal']['facts'] if f['kind'] in {'event','counter'}]
        proposals[0]['summary'] = event; proposals[0]['evidence_quotes'] = [event]
        proposals[1]['summary'] = counter; proposals[1]['evidence_quotes'] = [counter]
        proposals[1]['proposal']['value'] = 1
        reviews = ChapterFactReviewService(database=database, projects=projects, candidate_drafts=drafts,
            context_builder_factory=builder)
        review = reviews.create(draft.id, expected_draft_revision=draft.revision,
            expected_draft_sha256=draft.sha256, proposals=proposals, author_confirmed=True)
        for fact in review['facts']: fact['review']['decision'] = 'accepted'
        review = reviews.update(review['id'], facts=review['facts'], expected_revision=review['revision'])
        review = reviews.confirm(review['id'], expected_revision=review['revision'],
            expected_facts_sha256=review['facts_sha256'], expected_draft_revision=draft.revision,
            expected_draft_sha256=draft.sha256, author_confirmed=True)
        preparations = ChapterCommitPreparationService(database=database, projects=projects, candidate_drafts=drafts,
            fact_reviews=reviews, context_builder_factory=builder)
        route = dict(project_id=project.id, chapter=3, review_id=review['id'],
            expected_draft_revision=draft.revision, expected_review_revision=review['revision'],
            expected_draft_sha256=draft.sha256, expected_facts_sha256=review['facts_sha256'],
            idempotency_key='restored-publication-3')
        preparation = preparations.prepare(**route)
        publication = ChapterPublicationService(preparations=preparations, machine_store=c['store'])
        result = publication.commit(**route)
        assert result['publication_verified'] and result['chapter'] == 3
        assert infer_next_chapter(root) == 4
        assert resolve_prose(root, 3).path.read_text(encoding='utf-8') == preparation['final_prose']
        assert resolve_prose(root, 2).path.read_bytes() == chapter2
        verified = load_verified_reviewed_identity(root)
        assert verified['history']['value']['current_state'] == preparation['fact_preparation']['state_after']
        assert verified['history']['value']['committed_through'] == 3
        assert publication.commit(**route) == result and calls == [1]
        usage = read_project_model_usage(root, book_id=project.book_id)
        assert usage['summary']['attempt_count'] == 3
        assert budget.report()['charged_amount'] == '0.000219'
        attempts = budget.usage_snapshot()['attempts']
        assert all(item in attempts for item in retained['attempts']) and len(attempts) == 3
        assert activate(c, resume=True)['target_writable']
    assert inventory_tree(c['root']) == source
    deny(c['root'])
    # The old physical source can reappear. Recovery must not treat roots in
    # archived terminal manifests as live write targets of this workbench.
    app = create_app(database_path=c['target']/'workbench.sqlite',start_worker=True,auto_import_active=True,
        chapter_publication_machine_store=c['store'])
    with TestClient(app) as client:
        assert client.get('/').status_code == 200
        changed = client.put('/api/candidate-drafts/'+draft.id,
            json={'expected_revision':draft.revision,'text':'恢复后作者保留的工作台草稿修订。'})
        assert changed.status_code == 200, changed.text
        assert changed.json()['draft']['revision'] == draft.revision+1
        # Missing old terminal registration must fail without reconstructing it
        # or replaying an old transaction against the original source.
        scope = database.restore_scope
        retained = scope.activation['archived_recovery_history']['.novelagent/runtime/reviewed/p']
        run, item = next(iter(retained.items()))
        registration = root/'.novelagent/runtime/reviewed/p/registry'/item['state']/(run+'.json')
        raw = registration.read_bytes(); registration.unlink()
        try:
            before = inventory_tree(root)
            rejected = client.put('/api/candidate-drafts/'+draft.id,
                json={'expected_revision':draft.revision+1,'text':'不得保存的修改。'})
            assert rejected.status_code == 409, rejected.text
            assert not registration.exists() and inventory_tree(root) == before
        finally:
            registration.write_bytes(raw)
    assert inventory_tree(c['root']) == source


def test_restored_book_publishes_next_chapter_with_retained_budget(prepared):
    publish_restored_next_chapter(prepared)
