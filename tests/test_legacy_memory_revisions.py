import json
from pathlib import Path

import pytest

from tests.test_backup_archive import archive_case,legacy,software_tree,capture
from tests.test_backup_history import commit
from tests.test_backup_archive_history import absent_source
from workbench.backup_files import inventory_tree
from workbench.backup_history import BackupHistoryError,verify_persistence_history
from workbench.backup_archive_history import verify_archived_persistence_history
from workbench.legacy_history_revisions import DIRECTORY,prepare_observed_revision,confirm_observed_revision
from workbench.legacy_memory_revision_service import LegacyMemoryRevisionService
from core.story_project.reviewed_state_document import read_project_bytes,digest

NAME='追踪/上下文.md'


def verify(c):
    return verify_persistence_history(c['root'],inventory_tree(c['root']),book_id=c['project'].book_id)


def seed(c,past):
    (c['root']/NAME).parent.mkdir(parents=True,exist_ok=True)
    first=commit(c,'first',{NAME:'原来的场景记录'})
    (c['root']/NAME).write_text('人工修正后的场景',encoding='utf-8')
    second=commit(c,'second',{NAME:'下一章的新场景'}) if past else None
    return first.relative_to(c['root']).as_posix(), second.relative_to(c['root']).as_posix() if second else None


def prepare(c,first,second):
    with pytest.raises(BackupHistoryError) as failure:verify(c)
    assert failure.value.gap is not None
    graph=failure.value.gap['transactions']
    value=prepare_observed_revision(root=c['root'],tree=inventory_tree(c['root']),book_id=c['project'].book_id,
        transactions=graph,from_journal=first,to_journal=second,read_bytes=read_project_bytes,
        reason='核对保存的前后内容，明确记录过去的人工修订。')
    return value,graph


@pytest.mark.parametrize('past',[True,False])
def test_explicit_revision_connects_real_gap_without_rewriting_old_history(archive_case,past):
    c=archive_case;first,second=seed(c,past);before=inventory_tree(c['root'])
    value,graph=prepare(c,first,second)
    record=confirm_observed_revision(value,expected_sha256=value['preview_sha256'],book_id=c['project'].book_id,transactions=graph)
    directory=c['root']/DIRECTORY;directory.mkdir(parents=True)
    (directory/(record['id']+'.json')).write_text(json.dumps(record,ensure_ascii=False),encoding='utf-8')
    result=verify(c)
    assert result['state']=='verified' and len(result['external_revisions'])==1
    assert result['coverage']=='retained_transactions_and_explicit_observed_external_revisions'
    after=inventory_tree(c['root'])
    assert all(after['files'][name]==meta for name,meta in before['files'].items())
    # A subsequent normal commit must still unwind through the retained edit.
    commit(c,'later',{NAME:'之后正式提交的新记忆'})
    assert verify(c)['state']=='verified'
    package=Path(capture(c)['path'])
    with absent_source(c['root']):
        restored=verify_archived_persistence_history(package,project_id=c['project'].id,book_id=c['project'].book_id)
    assert restored['history_verified']


@pytest.mark.parametrize('damage',['wrong_book','unconfirmed','prose','content','before','successor','unknown_file'])
def test_revision_cannot_hide_bad_original_evidence_or_unreviewed_targets(legacy,damage):
    c=legacy;first,second=seed(c,True);value,graph=prepare(c,first,second)
    record=confirm_observed_revision(value,expected_sha256=value['preview_sha256'],book_id=c['project'].book_id,transactions=graph)
    if damage=='wrong_book':record['book_id']='other-book'
    if damage=='unconfirmed':record['author_confirmed']=False
    if damage=='prose':record['targets'][0]['relative']='正文/第001章.md'
    if damage=='content':record['targets'][0]['after_text']='changed without matching retained bytes'
    if damage=='before':record['targets'][0]['before_sha256']='a'*64
    if damage=='successor':record['to_journal']='absent'
    record['record_sha256']=digest({k:v for k,v in record.items() if k!='record_sha256'})
    directory=c['root']/DIRECTORY;directory.mkdir(parents=True)
    (directory/(record['id']+'.json')).write_text(json.dumps(record),encoding='utf-8')
    if damage=='unknown_file':(directory/'unfinished.json').write_text('{}')
    with pytest.raises(ValueError):verify(c)


def test_product_service_rejects_stale_preview_and_is_idempotent(tmp_path):
    from tests.test_reviewed_upgrade import case
    from fastapi.testclient import TestClient
    from workbench.app import create_app
    c=case.__wrapped__()
    app=create_app(database_path=tmp_path/'workbench.db',start_worker=False,auto_import_active=False,
        allowed_workspaces=[c['root'].parent],chapter_publication_machine_store=tmp_path/'machine')
    with TestClient(app):
        project=app.state.projects.import_project(c['root'])
        c.update(project=project,service=app.state.backups,op=app.state.maintenance.begin()['operation_id'])
        _exercise_service(c)


def _exercise_service(c):
    first,second=seed(c,False)
    service=LegacyMemoryRevisionService(c['service'])
    args=dict(operation_id=c['op'],from_journal=first,to_journal=second,reason='按作者委托核对测试故事中的场景修正。',reviewer='author_delegate')
    preview=service.preview(c['project'].id,**args)
    with pytest.raises(ValueError,match='确认'):
        service.confirm(c['project'].id,**args,expected_sha256=preview['preview_sha256'])
    original=(c['root']/NAME).read_bytes();(c['root']/NAME).write_bytes(original+b' changed')
    with pytest.raises(ValueError,match='变化'):
        service.confirm(c['project'].id,**args,expected_sha256=preview['preview_sha256'],confirmed=True)
    (c['root']/NAME).write_bytes(original)
    preview=service.preview(c['project'].id,**args)
    value=service.confirm(c['project'].id,**args,expected_sha256=preview['preview_sha256'],confirmed=True)
    assert service.confirm(c['project'].id,**args,expected_sha256=preview['preview_sha256'],confirmed=True)==value
    assert verify(c)['state']=='verified'


def test_correction_preserves_old_observation_and_original_intervening_transaction(legacy):
    c=legacy;root=c['root'];(root/NAME).parent.mkdir(parents=True,exist_ok=True)
    first=commit(c,'start',{NAME:'A'}).relative_to(root).as_posix()
    (root/NAME).write_text('B',encoding='utf-8')
    middle=commit(c,'original-migration',{NAME:'C'}).relative_to(root).as_posix()
    last=commit(c,'next',{NAME:'D'}).relative_to(root).as_posix()
    preview,graph=prepare(c,first,middle)
    args=dict(root=root,tree=inventory_tree(root),book_id=c['project'].book_id,transactions=graph,
        from_journal=first,to_journal=last,read_bytes=read_project_bytes,reason='跨越已知迁移的错误定位')
    with pytest.raises(ValueError,match='跨过'):prepare_observed_revision(**args)
    # Retain a previously published overly broad observation, then correct it.
    old={**preview,'to_journal':last,'targets':[dict(preview['targets'][0],after_sha256=digest(b'C'),after_text='C')]}
    old['preview_sha256']=digest({k:v for k,v in old.items() if k!='preview_sha256'})
    old=confirm_observed_revision(old,expected_sha256=old['preview_sha256'],book_id=c['project'].book_id,transactions=graph,now='2026-01-01T00:00:00+00:00')
    directory=root/DIRECTORY;directory.mkdir(parents=True)
    oldpath=directory/(old['id']+'.json');oldpath.write_text(json.dumps(old),encoding='utf-8');oldbytes=oldpath.read_bytes()
    fixed=prepare_observed_revision(**{**args,'tree':inventory_tree(root),'to_journal':middle,
        'reason':'将修订终点校正到原始迁移前，保留旧记录','supersedes':[old['id']]})
    fixed=confirm_observed_revision(fixed,expected_sha256=fixed['preview_sha256'],book_id=c['project'].book_id,transactions=graph)
    (directory/(fixed['id']+'.json')).write_text(json.dumps(fixed),encoding='utf-8')
    result=verify(c)
    assert result['state']=='verified' and len(result['transactions'])==3
    assert sorted(r['state'] for r in result['external_revisions'])==['active','superseded']
    assert oldpath.read_bytes()==oldbytes
