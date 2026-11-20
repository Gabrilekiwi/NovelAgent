"""Real local producers, source ranges and owned API; no Provider calls."""
import copy
import json
from pathlib import Path

import pytest

from core.engine.chapter_commit_prepare import PREPARATION_VERSION
from core.quality.final_artifact_integrity import FinalArtifactIntegrityGate,merge_integrity_report_into_validation
from core.quality.prose_evidence import prose_excerpt,validate_prose_excerpt,review_short_prose_repetition
from core.quality.reviewed_disposition import (ReviewedDispositionError,
    project_reviewed_quality_disposition,validate_reviewed_quality_disposition)
from core.quality_decision import build_quality_decision
from core.story_project.reviewed_state_document import digest
from tests.test_chapter_publication_api import case,ready,service_case,api_case,send


PARAGRAPH='周岚沿着旧车站狭窄的走廊向前走，手电光照亮了斑驳的门牌。她停在控制室外，仔细听着门后的动静，又回头确认林澈已经抵达楼梯口。'


def isolated_report(text=None,*,checks=(),pending=False,authoritative=True):
    text=PARAGRAPH if text is None else text
    integrity=FinalArtifactIntegrityGate().evaluate(artifact_text=text,stage='final_gate')
    validation=merge_integrity_report_into_validation({'ok':True,'problems':[],
        'executed_checks':['story_project'],'requested_focus':[],'skipped_checks':[]},integrity)
    decision=build_quality_decision(policy='standard',validation=validation,
        chapter_quality_report={'checks':list(checks)},rule_validation_report={'violations':[]},chapter_index=2)
    preparation={'preparation_version':PREPARATION_VERSION,'stage':'facts_prepared',
        'publication_authorized':False,'final_prose':text,
        'binding':{'book_id':'synthetic-book','chapter':2,'final_prose_sha256':digest(text.encode('utf-8'))}}
    preparation['preparation_sha256']=digest(preparation)
    quality={'quality_preparation_version':'reviewed_candidate_quality_v1','stage':'candidate_quality_checked',
        'publication_authorized':False,'binding':{**preparation['binding'],
            'chapter_preparation_sha256':preparation['preparation_sha256']},
        'quality_coverage_complete':not pending,'pending_gates':['llm_validator'] if pending else [],
        'local_quality_passed':decision['accepted'] and integrity['accepted'] and authoritative and not pending,
        'local_policy_decision':decision,'final_artifact_integrity':integrity,
        'admission_review':review_short_prose_repetition(text),
        'authoritative_state_validation':{'accepted':authoritative},'review_gate':{'status':'pass'}}
    quality['quality_sha256']=digest(quality)
    return preparation,quality


def rehash(quality):
    quality['quality_sha256']=digest({k:v for k,v in quality.items() if k!='quality_sha256'})


def projected(preparation,quality):
    return project_reviewed_quality_disposition(preparation=preparation,quality=quality)


def check(code,*,severity='high',status='warning',evidence=None):
    return {'code':code,'status':status,'severity':severity,'message':'synthetic controlled finding',
        'evidence':{} if evidence is None else evidence}


def test_real_duplicate_ranges_are_bound_and_repair_advice_grants_no_authority():
    p,q=isolated_report(PARAGRAPH+'\n\n'+PARAGRAPH)
    before=copy.deepcopy((p,q));result=projected(p,q)
    assert result['automatic_action']=='repair_once'
    assert result['policy']['max_repair_cycles']==1
    assert not result['repair_authorized'] and not result['automatic_acceptance_authorized'] and not result['publication_authorized']
    assert (p,q)==before
    for row in result['findings']:
        assert row['quotes'] and row['disposition']=='repair_once'
        for span in row['quotes']:
            assert p['final_prose'][span['start_char']:span['end_char']]==span['quote']
    assert validate_reviewed_quality_disposition(result,preparation=p,quality=q)==result


@pytest.mark.parametrize('kind',['negative','out_of_range','same_location','boolean'])
def test_invalid_or_ambiguous_integrity_locations_never_admit_repair(kind):
    p,q=isolated_report(PARAGRAPH+'\n\n'+PARAGRAPH)
    for finding in q['final_artifact_integrity']['findings']:
        if kind=='negative':finding['locations'][0]['start_char']=-1
        if kind=='out_of_range':finding['locations'][0]['end_char']=len(p['final_prose'])+1
        if kind=='same_location':finding['locations'][1]=copy.deepcopy(finding['locations'][0])
        if kind=='boolean':finding['locations'][0]['start_char']=True
    rehash(q)
    try:result=projected(p,q)
    except ReviewedDispositionError:return
    assert result['automatic_action']=='pause' and all(r['disposition']=='pause' for r in result['findings'])


def test_a_message_containing_text_is_not_promoted_to_prose_evidence():
    p,q=isolated_report(checks=[check('preserves_last_scene_location',evidence={'message':PARAGRAPH})])
    result=projected(p,q)
    assert result['automatic_action']=='pause' and result['findings'][0]['quotes']==[]


def test_semantic_suspicion_with_exact_excerpt_still_needs_manual_review():
    p,q=isolated_report(checks=[check('preserves_last_scene_location',evidence={
        'prose_excerpt':prose_excerpt(PARAGRAPH)})])
    result=projected(p,q);row=result['findings'][0]
    assert result['automatic_action']=='pause' and row['disposition']=='pause'
    assert row['quotes'][0]['quote']==PARAGRAPH and row['quote_status']=='verified_final_prose'
    # This automatic-admission advice does not forge or remove human acceptance.
    assert result['manual_quality_check_passed'] is True and not result['publication_authorized']


def test_only_listed_nonsemantic_warning_is_advisory():
    p,q=isolated_report(checks=[check('chapter_length_reasonable',severity='low')])
    result=projected(p,q)
    assert result['automatic_action']=='continue' and result['findings'][0]['disposition']=='advice'
    p,q=isolated_report(checks=[check('chapter_length_reasonable',status='fail')])
    assert projected(p,q)['automatic_action']=='pause'


@pytest.mark.parametrize('option',[{'pending':True},{'authoritative':False}])
def test_missing_required_check_or_invalid_state_cannot_be_repaired_away(option):
    p,q=isolated_report(PARAGRAPH+'\n\n'+PARAGRAPH,**option)
    assert projected(p,q)['automatic_action']=='pause'


@pytest.mark.parametrize('change',['quote','policy','type'])
def test_rehashed_projection_cannot_change_original_evidence_or_policy(change):
    p,q=isolated_report(PARAGRAPH+'\n\n'+PARAGRAPH);result=projected(p,q)
    if change=='quote':result['findings'][0]['quotes'][0]['quote']='invented prose'
    elif change=='policy':result['policy']['max_repair_cycles']=3
    else:result['repair_authorized']=0
    result['disposition_sha256']=digest({k:v for k,v in result.items() if k!='disposition_sha256'})
    with pytest.raises(ReviewedDispositionError):validate_reviewed_quality_disposition(result,preparation=p,quality=q)


def test_forged_declared_excerpt_pauses_even_after_outer_hash_is_recomputed():
    span=prose_excerpt(PARAGRAPH);span['quote']='invented words'
    p,q=isolated_report(checks=[check('preserves_last_scene_location',evidence={'prose_excerpt':span})])
    row=projected(p,q)['findings'][0]
    assert row['reason']=='declared_prose_evidence_invalid' and row['quotes']==[]


def test_changed_prose_cannot_reuse_old_quality_or_ranges():
    p,q=isolated_report(PARAGRAPH+'\n\n'+PARAGRAPH)
    p['final_prose']='different '+p['final_prose'];p['preparation_sha256']=digest({k:v for k,v in p.items() if k!='preparation_sha256'})
    with pytest.raises(ReviewedDispositionError):projected(p,q)


def test_unicode_excerpt_checks_full_source_and_rejects_python_equal_wrong_types():
    text='😀中文正文'+PARAGRAPH
    value=prose_excerpt(text,start_char=1,end_char=8)
    assert value['quote']==text[1:8] and validate_prose_excerpt(value,text)==value
    with pytest.raises(ValueError):validate_prose_excerpt(value,'changed '+text)
    with pytest.raises(ValueError):validate_prose_excerpt({**value,'start_char':True},text)


def test_short_near_pair_has_two_exact_sources_without_changing_manual_gate():
    text=PARAGRAPH+'\n\n'+PARAGRAPH.replace('手电光','灯光')
    p,q=isolated_report(text);result=projected(p,q)
    assert not q['final_artifact_integrity']['findings']
    assert result['manual_quality_check_passed'] is True
    assert result['automatic_action']=='pause'
    row=result['findings'][0]
    assert row['codes']==['short_near_prose_repetition_risk'] and len(row['quotes'])==2
    assert row['quotes'][0]['start_char']!=row['quotes'][1]['start_char']
    for span in row['quotes']:assert text[span['start_char']:span['end_char']]==span['quote']


@pytest.mark.parametrize('kind',['candidates','pairs','oversized_line'])
def test_short_probe_limits_cannot_silently_report_complete(kind):
    if kind=='candidates':text='\n'.join(PARAGRAPH+str(i) for i in range(65))
    elif kind=='pairs':text='\n'.join(PARAGRAPH.replace('手电光','灯光'+str(i)) for i in range(6))
    else:text=PARAGRAPH+'。'*512
    p,q=isolated_report(text);report=q['admission_review']
    assert report['scope_complete'] is False
    assert report['candidate_count']<=64 and len(report['pairs'])<=8
    result=projected(p,q)
    assert result['automatic_action']=='pause' and 'short_prose_review_incomplete' in result['pause_reasons']


def test_old_quality_without_admission_probe_remains_readable_but_has_no_auto_clearance():
    p,q=isolated_report();del q['admission_review'];rehash(q)
    result=projected(p,q)
    assert result['automatic_action']=='pause' and result['manual_quality_check_passed'] is True
    assert result['pause_reasons']==['admission_review_not_executed']


@pytest.mark.parametrize('change',['quote','range','type','scope'])
def test_rehashed_short_probe_forgery_is_rederived_from_original_prose(change):
    p,q=isolated_report(PARAGRAPH+'\n'+PARAGRAPH.replace('手电光','灯光'))
    report=q['admission_review']
    if change=='quote':report['pairs'][0]['left']['quote']='forged'
    elif change=='range':report['pairs'][0]['left']['start_char']+=1
    elif change=='type':report['scope_complete']=1
    else:report['scope']['maximum_pairs']=9
    rehash(q)
    with pytest.raises(ReviewedDispositionError):projected(p,q)


def test_short_probe_does_not_invent_sources_for_empty_or_tiny_prose():
    for text in ('','门响了。\n门响了。'):
        report=review_short_prose_repetition(text)
        assert report['pairs']==[] and report['candidate_count']==0 and report['scope_complete'] is True


def test_short_dialogue_and_punctuation_do_not_bypass_risk_probe():
    for line in ('“'+PARAGRAPH+'”','甲'*33+'。'*8):
        report=review_short_prose_repetition(line+'\n'+line)
        assert report['pairs'] and report['scope_complete'] is True


def reconfirm(c,text):
    draft=c['drafts'].update(c['draft'].id,text=text,expected_revision=c['draft'].revision)
    proposals=[p for p in c['example']['proposal']['facts'] if p['kind'] in {'event','counter'}]
    review=c['reviews'].create(draft.id,expected_draft_revision=draft.revision,
        expected_draft_sha256=draft.sha256,proposals=proposals,author_confirmed=True)
    for fact in review['facts']:fact['review']['decision']='accepted'
    review=c['reviews'].update(review['id'],facts=review['facts'],expected_revision=review['revision'])
    review=c['reviews'].confirm(review['id'],expected_revision=review['revision'],
        expected_facts_sha256=review['facts_sha256'],expected_draft_revision=draft.revision,
        expected_draft_sha256=draft.sha256,author_confirmed=True)
    c['route']={**c['route'],'review_id':review['id'],'expected_draft_revision':draft.revision,
        'expected_review_revision':review['revision'],'expected_draft_sha256':draft.sha256,
        'expected_facts_sha256':review['facts_sha256']}
    return draft,review


def test_public_preview_returns_classified_exact_quotes_without_repair_or_formal_write(api_case):
    c=api_case
    draft,review=reconfirm(c,c['text']+'\n\n'+PARAGRAPH+'\n\n'+PARAGRAPH)
    body={k:v for k,v in c['route'].items() if k not in {'project_id','chapter','idempotency_key'}}
    response=send(c,'-preview',body=body)
    assert response.status_code==200,response.text
    result=response.json();disposition=result['quality_disposition']
    assert disposition['binding']['final_prose_sha256']==result['quality']['binding']['final_prose_sha256']
    assert not disposition['repair_authorized'] and not disposition['publication_authorized']
    repeated=[r for r in disposition['findings'] if set(r['codes']) & {'duplicate_scene_text','near_duplicate_scene_text'}]
    assert repeated and all(r['quotes'] for r in repeated)
    mapped_rules=[r for r in disposition['findings'] if 'continue_previous_ending' in r['codes']]
    assert mapped_rules and all(r['quotes'] and r['disposition']=='pause' for r in mapped_rules)
    for row in repeated:
        for span in row['quotes']:assert result['preparation']['final_prose'][span['start_char']:span['end_char']]==span['quote']
    assert c['drafts'].get(draft.id)==draft and c['reviews'].reviews.get(review['id']).revision==review['revision']
    assert c['calls']==[1] and not (c['root']/'.novelagent/runtime/reviewed/intents').exists()
    assert c['client'].post(c['path']+'/publication-preview',json={**body,'quality_disposition':disposition},
        headers=c['headers']).status_code==422


def test_rule_mapping_preserves_only_its_actual_check_excerpt():
    rules={'violations':[{'rule_code':'continue_previous_ending','quality_check_codes':['continues_previous_ending'],
        'status':'warning','severity':'medium','message':'rule summary is not prose'}]}
    excerpt=prose_excerpt(PARAGRAPH)
    for checks,expected in (([check('other_check',evidence={'prose_excerpt':excerpt})],False),
                            ([check('continues_previous_ending',evidence={'prose_excerpt':excerpt})],True)):
        decision=build_quality_decision(policy='standard',chapter_quality_report={'checks':checks},
            rule_validation_report=rules,chapter_index=2)
        finding=next(r for r in decision['findings'] if 'continue_previous_ending' in r['codes'])
        assert any(e['kind']=='prose_excerpt' for e in finding['evidence']) is expected


def test_versioned_offline_casebook_records_admission_errors_and_zero_extra_calls(monkeypatch):
    import socket
    def forbidden(*args,**kwargs):pytest.fail('offline policy evaluation called a Provider/network')
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    path=Path('tests/fixtures/reviewed_quality_policy_v1/cases.json')
    casebook=json.loads(path.read_text(encoding='utf-8'));rows=[]
    for case in casebook['cases']:
        kind=case['kind']
        text=PARAGRAPH+'\n\n'+PARAGRAPH if kind in {'duplicate','coverage','state','invalid_range'} else PARAGRAPH
        checks=[]
        if kind=='near_duplicate':text=PARAGRAPH+'\n\n'+PARAGRAPH.replace('手电光','灯光')
        if kind=='long_near_duplicate':
            longer=PARAGRAPH+'控制室的仪表正在逐渐恢复，时钟还停在停电之前。'
            text=longer+'\n\n'+longer.replace('手电光','灯光')
        if kind=='short_repeat':
            text='门响了。\n门响了。';checks=[check('repetition_or_stalling',evidence={'prose_excerpt':prose_excerpt(text)})]
        if kind=='semantic':checks=[check('preserves_last_scene_location',evidence={'prose_excerpt':prose_excerpt(text)})]
        if kind=='style':checks=[check('chapter_length_reasonable',severity='low')]
        if kind=='summary':checks=[check('preserves_last_scene_location',evidence={'summary':text})]
        p,q=isolated_report(text,checks=checks,pending=kind=='coverage',authoritative=kind!='state')
        if kind=='invalid_range':
            for finding in q['final_artifact_integrity']['findings']:
                finding['locations'][0]['end_char']=len(text)+1
            rehash(q)
        result=projected(p,q)
        observed_quotes=any(row['quotes'] for row in result['findings'])
        integrity_detected=bool(q['final_artifact_integrity']['findings'])
        detected=integrity_detected or bool(q['admission_review']['pairs'])
        rows.append({'id':case['id'],'kind':kind,'expected_action':case['expected_action'],
            'observed_action':result['automatic_action'],'expected_quotes':case['expected_quotes'],
            'observed_quotes':observed_quotes,'expected_detector_defect':case['detector_defect'],
            'detector_found':detected,'integrity_gate_found':integrity_detected,
            'policy_sha256':result['policy_sha256']})
    detector_rows=[r for r in rows if r['expected_detector_defect'] is not None]
    metrics={'schema_version':'reviewed_quality_policy_evaluation_v1','casebook_sha256':digest(path.read_bytes()),
        'source':casebook['source'],'cases':rows,'routing_mismatch_count':sum(r['expected_action']!=r['observed_action'] for r in rows),
        'quote_mismatch_count':sum(r['expected_quotes']!=r['observed_quotes'] for r in rows),
        'local_detector_labeled_cases':len(detector_rows),
        'local_detector_false_positives':sum(not r['expected_detector_defect'] and r['detector_found'] for r in detector_rows),
        'local_detector_false_negatives':sum(r['expected_detector_defect'] and not r['detector_found'] for r in detector_rows),
        'known_integrity_gate_misses':[r['id'] for r in detector_rows if r['expected_detector_defect'] and not r['integrity_gate_found']],
        'extra_model_calls':0,'limitations':casebook['limitations']}
    Path('.tmp/t24-offline-policy-evaluation.json').write_text(json.dumps(metrics,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    assert metrics['routing_mismatch_count']==metrics['quote_mismatch_count']==0,rows
    assert metrics['local_detector_false_positives']==metrics['local_detector_false_negatives']==0,rows
    assert metrics['known_integrity_gate_misses']==['near_repetition']
