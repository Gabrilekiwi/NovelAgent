"""Nested job/event/artifact metadata must not silently lose required files."""
import json

import pytest

from tests.test_backup_scope import legacy,inspect,codes
from workbench.backup_files import inventory_tree
from workbench.backup_references import inspect_metadata_references


@pytest.mark.parametrize('damage', ['none', 'unknown_detail', 'required', 'executable', 'wrong_context'])
def test_optional_compatibility_probes_are_not_required_archive_files(legacy, damage):
    from core.story_project.oh_story_detection import detect_oh_story_compatibility
    c = legacy
    report = detect_oh_story_compatibility(c['root'], workspace_root=c['root'].parent)
    probe = next(item for item in report['markers'] if item['kind'] == 'active_book')
    probe['path'] = str(c['root'].parent/'absent-probe')
    if damage == 'unknown_detail': probe['details']['actual_output_path'] = str(c['root'].parent/'output')
    elif damage == 'required': probe['required'] = True
    elif damage == 'executable': report['capabilities']['oh_story_js_execution'] = True
    payload = {'story_project': {'oh_story': report}} if damage != 'wrong_context' else {'arbitrary': report}
    job(c, payload)
    result = inspect(c)
    if damage == 'none':
        assert result['scope_checks_passed'], result['blockers']
        assert not any('absent-probe' in item['path'] for item in result['references'])
    else:
        assert not result['scope_checks_passed']


def test_published_prose_and_book_relative_failure_record_are_checked(legacy):
    c = legacy
    record = c['root']/'.novelagent/runtime/runs/failure.json'
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text('{}', encoding='utf-8')
    prose = c['root']/'prose.txt'; prose.write_text('正文', encoding='utf-8')
    job(c, {'verification': {'prose_path': str(prose)},
        'error': {'details': {'run_record_path': record.relative_to(c['root']).as_posix()}}})
    assert inspect(c)['scope_checks_passed']
    record.unlink()
    assert 'required_file_missing_or_excluded' in codes(inspect(c))


@pytest.mark.parametrize('damage', ['none','outside','wrong_kind','unknown_field','wrong_context'])
def test_memory_source_chapter_coordinates_are_not_files(tmp_path, damage):
    source={'kind':'committed_chapter','path':'chapter:23'}
    if damage=='outside': source['path']=str(tmp_path.parent/'outside.txt')
    elif damage=='wrong_kind': source['kind']='file'
    elif damage=='unknown_field': source['artifact_path']=str(tmp_path.parent/'outside.txt')
    value={'memory_v2':{'source_index':{'patch_23':source}}} if damage!='wrong_context' else source
    seen=[];blocked=[]
    inspect_metadata_references(value,source='snapshot',book_root=tmp_path,
        require_path=lambda *a,**k:seen.append(a),block=lambda *a:blocked.append(a))
    if damage=='none': assert not seen and not blocked
    else: assert seen or blocked


def job(c,result=None):
    row=c['jobs'].create_job(project_id=c['project'].id,kind='generation_session',start_chapter=1,end_chapter=1)
    if result is not None:
        with c['db'].transaction() as connection:
            connection.execute('UPDATE jobs SET result_json=? WHERE id=?',(json.dumps(result),row.id))
    return row


@pytest.mark.parametrize('location',['outbox','writeback','nested_array','event','stdout','run_record'])
def test_nested_external_artifact_is_blocked_without_reading_or_collecting_unknown_root(legacy,location):
    c=legacy;outside=c['root'].parent/'not-authorized.txt';outside.write_text('not part of this book')
    meta={'path':str(outside),'format':'text'}
    payload={'outbox':{'persistence':{'publication':{'memory_outbox':meta}}},
             'writeback':{'writeback':{'artifacts':{'result':meta}}},
             'nested_array':{'reports':[{'details':[meta]}]}}.get(location,{'artifact':meta})
    row=job(c,payload if location in {'outbox','writeback','nested_array'} else None)
    if location=='event':
        with c['db'].transaction() as connection:
            sequence=connection.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM job_events WHERE job_id=?',(row.id,)).fetchone()[0]
            connection.execute('INSERT INTO job_events(job_id,sequence,event_type,payload_json,created_at) VALUES(?,?,?,?,?)',
                (row.id,sequence,'synthetic.artifact',json.dumps(payload),'2026-09-11T00:00:00Z'))
    elif location=='stdout':
        directory=c['runtime']/row.id/'chapter_0001';directory.mkdir(parents=True)
        (directory/'stdout.json').write_text(json.dumps(payload),encoding='utf-8')
    elif location=='run_record':
        directory=c['root']/'.novelagent/runtime/runs';directory.mkdir(parents=True,exist_ok=True)
        (directory/'synthetic-run.json').write_text(json.dumps(payload),encoding='utf-8')
    before=inventory_tree(c['root']);result=inspect(c)
    assert 'unknown_external_file_reference' in codes(result)
    assert all(tree['source_root']!=str(outside.parent) for tree in result['inventories'].values())
    assert inventory_tree(c['root'])==before and outside.read_text()=='not part of this book'


def test_nested_approved_job_artifact_and_its_hash_are_recorded(legacy):
    c=legacy;row=job(c);directory=c['runtime']/row.id;directory.mkdir(parents=True)
    artifact=directory/'result.txt';artifact.write_text('preserved')
    from core.story_project.reviewed_state_document import digest
    payload={'writeback':{'artifacts':{'result':{'path':str(artifact),'format':'text','sha256':digest(artifact.read_bytes())}}}}
    with c['db'].transaction() as connection:
        connection.execute('UPDATE jobs SET result_json=? WHERE id=?',(json.dumps(payload),row.id))
    result=inspect(c);assert result['scope_checks_passed'],result['blockers']
    assert any(r['root']=='jobs/'+row.id and r['path']=='result.txt' for r in result['references'])
    artifact.write_text('changed')
    assert 'referenced_artifact_digest_mismatch' in codes(inspect(c))


@pytest.mark.parametrize('damage',['missing','excluded','traversal','unknown_semantics'])
def test_bad_nested_reference_stays_a_preflight_blocker(legacy,damage):
    c=legacy
    path={'missing':str(c['root']/'absent.txt'),'excluded':str(c['root']/'.env'),
          'traversal':str(c['root']/'nested'/'..'/'file.txt'),'unknown_semantics':'opaque-value'}[damage]
    if damage=='excluded':(c['root']/'.env').write_text('FAKE_SECRET')
    payload={'deep':[{'new_runtime_ref':path} if damage=='unknown_semantics' else {'path':path}]}
    job(c,payload);result=inspect(c)
    expected={'missing':'required_file_missing_or_excluded','excluded':'required_file_missing_or_excluded',
              'traversal':'invalid_file_reference','unknown_semantics':'unclassified_metadata_reference'}[damage]
    assert expected in codes(result)


def test_journal_relative_and_logical_references_use_only_explicit_bindings(tmp_path):
    seen=[];blocked=[];journal=tmp_path/'journal';logical=tmp_path/'runtime'
    def require(value,label,**kwargs):seen.append((value,label,kwargs))
    value={'persistence':{'journal_path':str(journal),'targets':[{'staged_path':'staged/000.bin','backup_path':'backups/000.bin'}]},
           'logical':{'root_id':'runtime','root_uuid':'known','relative_path':'receipts/a.json'}}
    inspect_metadata_references(value,source='test',book_root=tmp_path,require_path=require,
        block=lambda *args:blocked.append(args),root_bindings={('runtime','known'):{str(logical)}})
    assert not blocked
    assert {str(journal/'staged/000.bin'),str(journal/'backups/000.bin'),str(logical/'receipts/a.json')}<={r[0] for r in seen}
    seen.clear();value={'logical':{'root_id':'runtime','root_uuid':'unknown','relative_path':'receipts/a.json'}}
    inspect_metadata_references(value,source='test',book_root=tmp_path,require_path=require,
        block=lambda *args:blocked.append(args),root_bindings={('runtime','known'):{str(logical)}})
    assert not seen and blocked[-1][0]=='unresolved_logical_reference_root'


def test_prose_is_not_parsed_as_metadata_and_limits_never_report_partial_success(tmp_path):
    blocked=[];seen=[]
    value={'text':'{"path":"C:/not-a-reference"}','field_path':'characters.hero.name'}
    inspect_metadata_references(value,source='text',book_root=tmp_path,require_path=lambda *a,**k:seen.append(a),
        block=lambda *a:blocked.append(a))
    assert not seen and not blocked
    inspect_metadata_references({'items':[{}]*10},source='oversized',book_root=tmp_path,
        require_path=lambda *a,**k:seen.append(a),block=lambda *a:blocked.append(a),max_nodes=5)
    assert blocked==[('metadata_reference_limits_exceeded','oversized')]


@pytest.mark.parametrize('mode',['cycle','unknown_child','malformed_child'])
def test_json_artifact_graph_is_followed_once_with_unknown_roots_rejected(legacy,mode):
    c=legacy;first=c['root']/'first.json';second=c['root']/'second.json'
    first.write_text(json.dumps({'next':{'path':str(second),'format':'json'}}),encoding='utf-8')
    if mode=='cycle':second.write_text(json.dumps({'previous':{'path':str(first),'format':'json'}}),encoding='utf-8')
    elif mode=='unknown_child':second.write_text(json.dumps({'deep':[{'path':str(c['root'].parent/'outside.txt')}]}),encoding='utf-8')
    else:second.write_text('{incomplete')
    job(c,{'artifact':{'path':str(first),'format':'json'}})
    result=inspect(c)
    if mode=='cycle':
        assert result['scope_checks_passed'],result['blockers']
        assert len([r for r in result['references'] if r['path'] in {'first.json','second.json'}])==3
    elif mode=='unknown_child':assert 'unknown_external_file_reference' in codes(result)
    else:assert 'run_metadata_unreadable' in codes(result)


@pytest.mark.parametrize('value',[
    {'path_ref':{'root_id':'runtime'}},
    {'path_ref':{'root_id':'runtime','relative_path':'file','root_uuid':{}}},
    {'path_ref':{'root_id':'runtime','relative_path':'../outside'}},
    {'new_ref':{'opaque':'future-reference'}},
])
def test_incomplete_or_unknown_structured_reference_cannot_disappear(value,tmp_path):
    seen=[];blocked=[]
    inspect_metadata_references(value,source='test',book_root=tmp_path,
        require_path=lambda *a,**k:seen.append(a),block=lambda *a:blocked.append(a),
        root_bindings={('runtime',None):{str(tmp_path)}})
    assert blocked and not seen
