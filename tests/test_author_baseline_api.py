"""Actual public review flow, retained history and fail-closed source checks."""
import copy
import json

import pytest

from tests.test_author_baseline_service import service_case,create
from tests.test_legacy_registration import registration_case,contents
from tests.test_legacy_restore_identity import legacy
from tests.test_registration_upgrade_archive import tmp_path
from tests.test_backup_restore_database import logical
from workbench.author_baselines import CATEGORIES


def endpoint(c):
    return f"/api/projects/{c['project'].id}/onboarding/baselines"


def post(c,path='',**body):
    return c['client'].post(endpoint(c)+path,json={'operation_id':c['op'],**body})


def test_public_author_review_proposal_sources_save_confirm_edit_history_and_expiry(service_case):
    c=service_case;client=c['client'];book=contents(c['root']);sql=logical(c['app'].state.database.path)
    page=client.get('/baseline-review?project_id='+c['project'].id)
    assert page.status_code==200 and 'fact-editors' in page.text
    assert client.get('/static/baseline-review.js').status_code==200
    assert client.get(endpoint(c)).json()['items']==[]
    proposal=post(c,'/proposal');assert proposal.status_code==200,proposal.text
    proposal=proposal.json();assert set(proposal['unreviewed_categories'])==set(CATEGORIES)
    assert proposal['automatic_seed_covers']==['character_state','location','counter']
    assert not proposal['can_upgrade'] and not proposal['publication_authorized']
    assert logical(c['app'].state.database.path)==sql
    path=proposal['source_paths'][0]
    source=post(c,'/source',relative_path=path);assert source.status_code==200,source.text
    source=source.json();assert source['text']==(c['root']/path).read_bytes().decode('utf-8')
    for forbidden in ('../workbench.db','.novelagent/project.json','正文/第001章.md'):
        assert post(c,'/source',relative_path=forbidden).status_code==409
    body={'state':proposal['state'],'calibration_report':json.dumps(c['report'],ensure_ascii=False),'static_references':[]}
    for invalid in ({'state':[]},{'calibration_report':None},{'static_references':None},{'calibration_report':'{invalid'}):
        assert post(c,'/preview',**{**body,**invalid}).status_code==409
    # Reproduce JS parse/stringify numeric loss; never weaken report verification.
    def javascript_numbers(value):
        if isinstance(value,float) and value.is_integer():return int(value)
        if isinstance(value,dict):return {k:javascript_numbers(v) for k,v in value.items()}
        if isinstance(value,list):return [javascript_numbers(v) for v in value]
        return value
    assert post(c,'/preview',**{**body,'calibration_report':javascript_numbers(c['report'])}).status_code==400
    p=post(c,'/preview',**body);assert p.status_code==200,p.text
    assert logical(c['app'].state.database.path)==sql
    created=post(c,expected_preview_sha256=p.json()['preview_sha256'],**body)
    assert created.status_code==200,created.text
    draft=created.json()['record'];identifier=draft['id']
    listed=client.get(endpoint(c)).json()
    assert len(listed['items'])==1 and listed['items'][0]['id']==identifier
    assert not listed['history_verified'] and not listed['source_checked']
    version={'expected_revision':1,'expected_sha256':draft['record_sha256']}
    # Refused confirmations leave precisely the original draft, including
    # string booleans, missing categories and a hidden content replacement.
    for extra in ({'author_confirmed':'true','confirmed_categories':list(CATEGORIES)},
                  {'author_confirmed':True,'confirmed_categories':list(CATEGORIES[:-1])},
                  {'author_confirmed':True,'confirmed_categories':list(CATEGORIES),'state':draft['state']}):
        assert post(c,'/'+identifier+'/confirm',**version,**extra).status_code==409
    assert len(c['reviews'].history(c['project'].id,identifier)['items'])==1
    checked=post(c,'/'+identifier+'/inspect');assert checked.json()['source_current'] and checked.json()['can_confirm']
    confirmed=post(c,'/'+identifier+'/confirm',**version,author_confirmed=True,confirmed_categories=list(CATEGORIES))
    assert confirmed.status_code==200,confirmed.text
    confirmed=confirmed.json()['record'];assert confirmed['revision']==2 and confirmed['status']=='confirmed'
    assert post(c,'/'+identifier+'/confirm',**version,author_confirmed=True,confirmed_categories=list(CATEGORIES)).status_code==409
    historical=client.get(endpoint(c)+'/'+identifier+'?revision=1').json()
    assert historical['record']==draft and historical['head_revision']==2 and len(historical['revisions'])==2
    assert '1.0' in historical['calibration_report_json']
    assert json.loads(historical['calibration_report_json'])==c['report']
    assert not historical['source_checked'] and not historical['can_upgrade']
    assert client.get(endpoint(c)+'/'+identifier+'?revision=3').status_code==409
    state=copy.deepcopy(draft['state']);next(iter(state['characters'].values()))['current_goal']='作者页面修改'
    edited=post(c,'/'+identifier+'/edit',state=state,expected_revision=2,expected_sha256=confirmed['record_sha256'])
    assert edited.status_code==200,edited.text
    edited=edited.json()['record'];assert edited['revision']==3 and edited['status']=='draft' and not edited['confirmed_categories']
    assert contents(c['root'])==book
    # Current manual source drift retains the draft but cannot be confirmed.
    (c['root']/path).write_bytes((c['root']/path).read_bytes()+b'\nlate author note\n')
    checked=post(c,'/'+identifier+'/inspect');assert checked.status_code==200
    assert not checked.json()['source_current'] and not checked.json()['can_confirm']
    assert post(c,'/'+identifier+'/confirm',expected_revision=3,expected_sha256=edited['record_sha256'],
                author_confirmed=True,confirmed_categories=list(CATEGORIES)).status_code==409
    assert client.get(endpoint(c)+'/'+identifier).json()['record']==edited
    c['app'].state.maintenance.finish(c['op'])
    assert post(c,'/proposal').status_code==409
    assert client.get(endpoint(c)+'/'+identifier).json()['record']==edited


def test_discovery_only_pages_real_heads_and_rejects_corruption_without_mutation(service_case):
    c=service_case;first=create(c)['record'];second=create(c)['record']
    records=sorted([first,second],key=lambda r:r['id']);client=c['client']
    listed=client.get(endpoint(c)+'?after='+records[0]['id']).json()
    assert [r['id'] for r in listed['items']]==[records[1]['id']]
    assert client.get(endpoint(c)+'?after=../other').status_code==409
    with c['app'].state.database.transaction() as connection:
        connection.execute('UPDATE author_baseline_revisions SET payload_json=? WHERE id=?',('{broken',second['id']))
    before=logical(c['app'].state.database.path)
    result=client.get(endpoint(c));assert result.status_code==409,result.text
    assert logical(c['app'].state.database.path)==before
