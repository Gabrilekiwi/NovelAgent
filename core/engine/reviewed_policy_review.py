"""Portable policy review evidence, never client consent or a write permit.

The owned service must independently load the original machine/book grant and
extraction artifacts before building this record. Hashes here bind that input;
they do not authenticate arbitrary caller-supplied grant dictionaries.
"""
import copy,re

from core.engine.chapter_fact_candidate import normalize_fact_review_items
from core.engine.chapter_commit_prepare import normalize_commit_prose
from core.batch_budget_contract import validate_batch_budget
from core.quality.reviewed_disposition import reviewed_quality_action_policy
from core.quality_decision import resolve_quality_policy
from core.quality.reviewed_policy_versions import validate_action_policy_snapshot,validate_standard_policy_snapshot
from core.story_project.reviewed_state_document import digest


class PolicyReviewError(ValueError):
    code='reviewed_policy_review_invalid'


def _sha(value):return isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value) is not None


def build_policy_fact_review(*,grant,project_id,book_id,chapter,draft,candidate_intent,context_sha256,
                            authority,extraction,model_facts,_historical=False):
    """Accept complete unedited model suggestions for deterministic checking.

    Unknown identity, baselines, ambiguous ordering and state contradictions
    still fail the common fact reducer. No author identity/support decisions
    are invented here. Publication additionally requires complete final quality.
    """
    try:
        m=grant['manifest'];budget=validate_batch_budget(m['budget'],book_id=book_id)
        action=validate_action_policy_snapshot(m['action_policy'])
        quality=validate_standard_policy_snapshot(m['quality_policy'])
        if not _historical and (action!=reviewed_quality_action_policy() or digest(action)!=digest(reviewed_quality_action_policy())
                or quality!=resolve_quality_policy('standard').to_dict() or digest(quality)!=digest(resolve_quality_policy('standard').to_dict())):
            raise PolicyReviewError('旧批次策略与当前策略不同，不能开始新审核。')
        if (set(grant)!={'schema_version','manifest','grant_source','author_confirmed','automatic_review_source','grant_sha256'}
                or grant['schema_version']!='reviewed_batch_author_grant_v1' or grant['grant_source']!='author'
                or grant['author_confirmed'] is not True or grant['automatic_review_source']!='policy'
                or grant['grant_sha256']!=digest({k:v for k,v in grant.items() if k!='grant_sha256'})
                or m['manifest_sha256']!=digest({k:v for k,v in m.items() if k!='manifest_sha256'})
                or m['book_id']!=book_id or m['project_id']!=project_id or type(chapter) is not int
                or chapter not in m['chapters'] or m['batch_id']!=budget['batch_id']
                or m['chapters']!=[b['chapter'] for b in budget['chapter_budgets']]
                or any(type(c) is not int for c in m['chapters'])
                or type(m['start_chapter']) is not int or type(m['end_chapter']) is not int
                or m['start_chapter']!=m['chapters'][0] or m['end_chapter']!=m['chapters'][-1]
                or draft['project_id']!=project_id or draft['chapter']!=chapter or draft['state']!='editing'
                or type(draft['revision']) is not int or draft['revision']<1
                or draft['sha256']!=digest(draft['text'].encode('utf-8'))
                or set(extraction)!={'extraction_id','intent_hash','record_hash','proposal_hash','execution_id'}
                or not all(_sha(extraction[k]) for k in ('intent_hash','record_hash','proposal_hash'))
                or not isinstance(extraction['extraction_id'],str) or re.fullmatch('fact_extraction_[0-9a-f]{40}',extraction['extraction_id']) is None
                or not isinstance(extraction['execution_id'],str) or re.fullmatch('execution_fx_[0-9a-f]{24}',extraction['execution_id']) is None
                or not _sha(context_sha256) or not _sha(candidate_intent)):
            raise PolicyReviewError('策略审核的原批次、草稿或提取凭据绑定无效。')
        facts=normalize_fact_review_items(model_facts,chapter_text=draft['text'])
        if any(f['review']!={'origin':'model','decision':'pending','author_edited':False,'reason':None} for f in facts):
            raise PolicyReviewError('自动审核只接受本次完整提取的未编辑模型建议。')
        for fact in facts:fact['review']['decision']='accepted'
        record=dict(schema_version='reviewed_policy_fact_review_v1',record_type='policy_fact_review',
            review_source='policy',author_confirmed=False,state='accepted_for_quality_check',publication_authorized=False,
            project_id=project_id,book_id=book_id,chapter=chapter,draft_id=draft['id'],
            source_draft_revision=draft['revision'],source_draft_sha256=draft['sha256'],source_text=draft['text'],
            candidate_id=draft['candidate_id'],candidate_intent_hash=candidate_intent,
            context_sha256=context_sha256,authority=copy.deepcopy(authority),authority_sha256=digest(authority),
            facts=facts,facts_sha256=digest(facts),extraction=copy.deepcopy(extraction),batch_grant=copy.deepcopy(grant))
        record['id']='policy_review_'+digest(record)[:32]
        record['revision']=1
        record['record_sha256']=digest(record)
        return record
    except (KeyError,TypeError) as exc:raise PolicyReviewError('策略审核原始证据不完整。') from exc


def validate_policy_fact_review(value):
    try:
        draft=dict(id=value['draft_id'],project_id=value['project_id'],chapter=value['chapter'],
            state='editing',revision=value['source_draft_revision'],sha256=value['source_draft_sha256'],
            text=value['source_text'],candidate_id=value['candidate_id'])
        facts=copy.deepcopy(value['facts'])
        if any(f['review']['decision']!='accepted' for f in facts):raise PolicyReviewError('策略审核不能隐去或拒绝部分模型建议。')
        for fact in facts:fact['review']['decision']='pending'
        expected=build_policy_fact_review(grant=value['batch_grant'],project_id=value['project_id'],book_id=value['book_id'],
            chapter=value['chapter'],draft=draft,candidate_intent=value['candidate_intent_hash'],context_sha256=value['context_sha256'],
            authority=value['authority'],extraction=value['extraction'],model_facts=facts,_historical=True)
        if value!=expected or digest(value)!=digest(expected):raise PolicyReviewError('策略审核记录被改写或字段类型不一致。')
        return expected
    except (KeyError,TypeError) as exc:raise PolicyReviewError('策略审核记录不完整。') from exc


def policy_acceptance(review):
    r=validate_policy_fact_review(review);m=r['batch_grant']['manifest']
    return dict(schema_version='reviewed_policy_acceptance_v1',review_source='policy',
        batch_id=m['batch_id'],batch_authorization_sha256=r['batch_grant']['grant_sha256'],
        batch_budget_sha256=m['budget']['batch_budget_sha256'],policy_sha256=digest(m['action_policy']),
        policy_review_sha256=r['record_sha256'],extraction_record_sha256=r['extraction']['record_hash'])


def verify_policy_preparation(prepared):
    """Portable deterministic binding; owned grant authentication stays service-side."""
    try:
        r=validate_policy_fact_review(prepared['policy_review']);b=prepared['binding']
        fields={'book_id':'book_id','project_id':'project_id','chapter':'chapter','draft_id':'draft_id',
            'draft_revision':'source_draft_revision','draft_sha256':'source_draft_sha256',
            'review_id':'id','review_revision':'revision','facts_sha256':'facts_sha256',
            'candidate_id':'candidate_id','candidate_intent_hash':'candidate_intent_hash',
            'context_sha256':'context_sha256','review_authority_sha256':'authority_sha256'}
        if (b['acceptance_kind']!='policy' or 'confirmation_sha256' in b
                or b['policy_acceptance']!=policy_acceptance(r) or digest(b['policy_acceptance'])!=digest(policy_acceptance(r))
                or any(b[k]!=r[v] for k,v in fields.items()) or prepared['facts']!=r['facts']
                or prepared['final_prose']!=normalize_commit_prose(r['source_text'])
                or prepared.get('support') is not None):
            raise PolicyReviewError('策略审核与最终正文、原事实或提交准备不同。')
        return r
    except (KeyError,TypeError) as exc:raise PolicyReviewError('策略提交准备证据不完整。') from exc
