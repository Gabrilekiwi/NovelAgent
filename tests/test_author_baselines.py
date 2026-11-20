"""Real Strict source binding, durable CAS review history and honest invalidation."""
import copy
import json

import pytest

from tests.test_reviewed_upgrade import case
from core.engine.chapter_candidate_context import ChapterCandidateContextBuilder
from core.story_project.reviewed_state_document import digest,RELATIVE_PATH
from workbench.database import WorkbenchDatabase
from workbench.projects import ProjectRegistry
from workbench.author_baselines import (AuthorBaselineStore,AuthorBaselineError,CATEGORIES,
    build_baseline_draft,assert_baseline_current,validate_baseline_revision)
from workbench.backup_files import inventory_tree


@pytest.fixture
def baseline_case(case):
    c=case;db=WorkbenchDatabase(c['root'].parent/'reviews.db')
    projects=ProjectRegistry(db,allowed_workspaces=[c['root'].parent]);project=projects.import_project(c['root'])
    store=AuthorBaselineStore(db);context=ChapterCandidateContextBuilder(c['root']).build(2)
    draft=build_baseline_draft(project_id=project.id,trusted_context=context,state=c['state'],calibration_report=c['report'])
    return {**c,'db':db,'projects':projects,'project':project,'store':store,'context':context,'draft':draft}


def revise(c,old,**kwargs):
    return c['store'].revise(old['id'],project_id=c['project'].id,expected_revision=old['revision'],
        expected_sha256=old['record_sha256'],trusted_context=c['context'],**kwargs)


def test_real_seven_category_review_is_versioned_and_never_publishes_the_book(baseline_case):
    c=baseline_case;before=inventory_tree(c['root']);draft=c['store'].create(c['draft'],trusted_context=c['context'])
    state=copy.deepcopy(draft['state']);next(iter(state['characters'].values()))['current_goal']='作者修改后的当前目标'
    edited=revise(c,draft,state=state)
    confirmed=revise(c,edited,author_confirmed=True,confirmed_categories=CATEGORIES)
    assert confirmed['status']=='confirmed' and confirmed['state']==state and confirmed['author_confirmed']
    assert confirmed['confirmed_categories']==list(CATEGORIES)
    later=revise(c,confirmed,state=c['state'])
    assert later['status']=='draft' and not later['author_confirmed'] and later['confirmed_categories']==[]
    history=AuthorBaselineStore(c['db']).history(draft['id'],project_id=c['project'].id)
    assert history==[draft,edited,confirmed,later]
    assert [v['revision'] for v in history]==[1,2,3,4]
    assert inventory_tree(c['root'])==before and not (c['root']/RELATIVE_PATH).exists()


def test_review_storage_limit_refuses_append_without_deleting_history(baseline_case,monkeypatch):
    c=baseline_case;first=c['store'].create(c['draft'],trusted_context=c['context'])
    with c['db'].connection() as connection:
        size=connection.execute('SELECT SUM(length(CAST(payload_json AS BLOB))) FROM author_baseline_revisions').fetchone()[0]
    monkeypatch.setattr('workbench.author_baselines._MAX_PROJECT_BYTES',size+1)
    with pytest.raises(AuthorBaselineError,match='上限'):
        revise(c,first,state=c['state'])
    assert c['store'].history(first['id'],project_id=c['project'].id)==[first]


def test_history_rejects_oversized_row_before_collecting_the_chain(baseline_case):
    c=baseline_case;first=c['store'].create(c['draft'],trusted_context=c['context'])
    with c['db'].transaction() as connection:
        connection.execute('UPDATE author_baseline_revisions SET payload_json=?',('x'*(2*1024**2+1),))
    with pytest.raises(AuthorBaselineError,match='大小'):
        c['store'].history(first['id'],project_id=c['project'].id)


@pytest.mark.parametrize('damage',['missing_category','duplicate_category','string_confirmation','edit_and_confirm',
    'stale_revision','wrong_hash','other_project'])
def test_confirmation_is_exact_and_does_not_overwrite_a_concurrent_revision(baseline_case,damage):
    c=baseline_case;draft=c['store'].create(c['draft'],trusted_context=c['context'])
    kwargs=dict(project_id=c['project'].id,expected_revision=1,expected_sha256=draft['record_sha256'],
        trusted_context=c['context'],author_confirmed=True,confirmed_categories=CATEGORIES)
    if damage=='missing_category':kwargs['confirmed_categories']=CATEGORIES[:-1]
    elif damage=='duplicate_category':kwargs['confirmed_categories']=(*CATEGORIES,CATEGORIES[0])
    elif damage=='string_confirmation':kwargs['author_confirmed']='true'
    elif damage=='edit_and_confirm':kwargs['state']=c['state']
    elif damage=='stale_revision':kwargs['expected_revision']=2
    elif damage=='wrong_hash':kwargs['expected_sha256']='f'*64
    else:kwargs['project_id']='different-project'
    with pytest.raises(ValueError):c['store'].revise(draft['id'],**kwargs)
    assert c['store'].history(draft['id'],project_id=c['project'].id)==[draft]


@pytest.mark.parametrize('change',['manual_source','prose','new_source','identity','other_context'])
def test_actual_source_or_book_change_invalidates_even_a_confirmed_review(baseline_case,change):
    c=baseline_case;draft=c['store'].create(c['draft'],trusted_context=c['context'])
    confirmed=revise(c,draft,author_confirmed=True,confirmed_categories=CATEGORIES)
    before_history=c['store'].history(draft['id'],project_id=c['project'].id)
    if change=='manual_source':
        name=next((c['root']/'设定').rglob('*.md'));name.write_bytes(name.read_bytes()+b'\nnew author source\n')
    elif change=='prose':
        name=next((c['root']/'正文').glob('*.md'));name.write_bytes(name.read_bytes()+b'\nnew author prose\n')
    elif change=='new_source':(c['root']/'设定/新资料.md').write_text('作者新增来源',encoding='utf-8')
    elif change=='identity':
        name=c['root']/'.novelagent/project.json';value=json.loads(name.read_bytes());value['book_id']='another-book'
        name.write_text(json.dumps(value),encoding='utf-8')
    else:
        from tests.test_reviewed_upgrade import case as other_case
        other=other_case.__wrapped__();c['context']=ChapterCandidateContextBuilder(other['root']).build(2)
    book=inventory_tree(c['root'])
    with pytest.raises(ValueError):assert_baseline_current(confirmed,trusted_context=c['context'])
    with pytest.raises(ValueError):revise(c,confirmed,state=c['state'])
    # A stale approval is retained as history, never silently rebound or erased.
    assert c['store'].history(draft['id'],project_id=c['project'].id)==before_history
    assert inventory_tree(c['root'])==book


def test_change_after_sql_insert_rolls_back_the_draft_instead_of_accepting_stale_confirmation(baseline_case,monkeypatch):
    c=baseline_case;original=c['store']._append
    def changed(connection,value):
        saved=original(connection,value)
        (c['root']/'设定/late.md').write_text('changed while saving',encoding='utf-8')
        return saved
    monkeypatch.setattr(c['store'],'_append',changed)
    with pytest.raises(ValueError):c['store'].create(c['draft'],trusted_context=c['context'])
    assert c['store'].history(c['draft']['id'],project_id=c['project'].id)==[]


@pytest.mark.parametrize('damage',['wrong_report','malformed_state','location_mirror','wrong_owner'])
def test_unproved_strict_report_or_invalid_state_cannot_become_an_author_baseline(baseline_case,damage):
    c=baseline_case;draft=copy.deepcopy(c['draft'])
    if damage=='wrong_report':
        from core.story_project.activation import calibration_report_sha256
        draft['calibration_report']['book_id']='different-book'
        draft['calibration_report']['report_sha256']=calibration_report_sha256(draft['calibration_report'])
    elif damage=='malformed_state':draft['state']['characters']=[]
    elif damage=='location_mirror':next(iter(draft['state']['characters'].values()))['current_location']='another-location'
    else:draft['book_id']='different-book'
    draft['record_sha256']=digest({k:v for k,v in draft.items() if k!='record_sha256'})
    with pytest.raises((ValueError,TypeError)):validate_baseline_revision(draft)
    assert c['store'].history(c['draft']['id'],project_id=c['project'].id)==[]


def test_rehashed_confirmed_revision_cannot_change_the_reviewed_draft_content(baseline_case):
    c=baseline_case;draft=c['store'].create(c['draft'],trusted_context=c['context'])
    confirmed=revise(c,draft,author_confirmed=True,confirmed_categories=CATEGORIES)
    value=copy.deepcopy(confirmed);next(iter(value['state']['characters'].values()))['current_goal']='unreviewed overwrite'
    value['record_sha256']=digest({k:v for k,v in value.items() if k!='record_sha256'})
    with c['db'].transaction() as connection:
        connection.execute('UPDATE author_baseline_revisions SET payload_json=?,record_sha256=? WHERE id=? AND revision=2',
            (json.dumps(value),value['record_sha256'],value['id']))
    with pytest.raises(AuthorBaselineError,match='确认内容'):c['store'].history(draft['id'],project_id=c['project'].id)


def test_baseline_requires_the_entire_contiguous_canonical_chapter_sequence(baseline_case):
    c=baseline_case
    old_outline=next((c['root']/'大纲').glob('*.md'))
    (c['root']/'大纲/细纲_第004章.md').write_text(old_outline.read_text(encoding='utf-8').replace('第2章','第4章'),encoding='utf-8')
    prose=next((c['root']/'正文').glob('*.md'))
    (c['root']/'正文/第003章_缺中间章.md').write_text(prose.read_text(encoding='utf-8'),encoding='utf-8')
    future=ChapterCandidateContextBuilder(c['root']).build(4)
    with pytest.raises(AuthorBaselineError):
        build_baseline_draft(project_id=c['project'].id,trusted_context=future,state=c['state'],calibration_report=c['report'])


@pytest.mark.parametrize('connection_kind',['no_transaction','other_database'])
def test_borrowed_review_write_requires_the_actual_database_and_an_active_transaction(baseline_case,connection_kind):
    c=baseline_case
    if connection_kind=='other_database':
        other=WorkbenchDatabase(c['root'].parent/'other.db');other.initialize();unit=other.transaction()
    else:unit=c['db'].connection()
    with unit as connection,pytest.raises(AuthorBaselineError):
        c['store'].create(c['draft'],trusted_context=c['context'],_connection=connection)
    assert c['store'].history(c['draft']['id'],project_id=c['project'].id)==[]
