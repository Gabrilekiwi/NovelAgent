"""Portable policy generation evidence; live permission stays process local."""
import copy,re

from core.engine.reviewed_batch_control import BatchControlError,BatchControlPaused,current_policy_step
from core.story_project.reviewed_state_document import digest


def generation_input_sha256(prepared):
    return digest({'book_id':prepared['identity']['book_id'],
        **{k:prepared[k] for k in ('chapter_index','authority','source','chapter_intent','input_sha256')}})


def policy_generation_authorization(*,grant,prepared,binding,repair=None):
    m=grant['manifest'];chapter=prepared['chapter_index']
    outline=next(o for o in m['outlines'] if o['chapter']==chapter)
    result=dict(schema_version='reviewed_policy_generation_v1',request_source='policy',author_confirmed=False,
        book_id=m['book_id'],chapter=chapter,batch_id=m['batch_id'],grant_sha256=grant['grant_sha256'],
        manifest_sha256=m['manifest_sha256'],outline_sha256=outline['source_sha256'],
        context_sha256=prepared['source']['context_sha256'],chapter_intent_sha256=digest(prepared['chapter_intent']),
        step_id=binding['step_id'],execution_id=binding['execution_id'],input_sha256=generation_input_sha256(prepared))
    if repair is not None:
        result.update(schema_version='reviewed_policy_generation_v2',**repair)
    return validate_policy_generation_authorization(result)


def validate_policy_generation_authorization(value):
    fields={'schema_version','request_source','author_confirmed','book_id','chapter','batch_id','grant_sha256',
        'manifest_sha256','outline_sha256','context_sha256','chapter_intent_sha256','step_id','execution_id','input_sha256'}
    repair=isinstance(value,dict) and value.get('schema_version')=='reviewed_policy_generation_v2'
    if repair:fields|={'repair_claim_sha256','original_quality_result_sha256','original_draft_sha256'}
    if (not isinstance(value,dict) or set(value)!=fields or value['schema_version'] not in {'reviewed_policy_generation_v1','reviewed_policy_generation_v2'}
            or value['request_source']!='policy' or value['author_confirmed'] is not False
            or not isinstance(value['book_id'],str) or not value['book_id'] or type(value['chapter']) is not int or value['chapter']<1
            or not isinstance(value['batch_id'],str) or re.fullmatch('batch_[0-9a-f]{32}',value['batch_id']) is None
            or not isinstance(value['step_id'],str) or re.fullmatch('step_[0-9a-f]{32}',value['step_id']) is None
            or not isinstance(value['execution_id'],str) or re.fullmatch('execution_candidate_[0-9a-f]{32}',value['execution_id']) is None
            or any(not isinstance(value[k],str) or re.fullmatch('[0-9a-f]{64}',value[k]) is None for k in fields if k.endswith('_sha256'))):
        raise BatchControlError('自动生成的原策略来源凭据无效。')
    expected='step_'+digest({'batch_id':value['batch_id'],'chapter':value['chapter'],'stage':'repair_generation' if repair else 'generation'})[:32]
    if value['step_id']!=expected:raise BatchControlError('自动生成不属于原确定步骤。')
    return copy.deepcopy(value)


def require_policy_generation_scope(authorization,*,prepared=None):
    a=validate_policy_generation_authorization(authorization);permit=current_policy_step()
    if permit is None:raise BatchControlPaused('自动生成需要本次明确开始或继续的原步骤许可。')
    repair=a['schema_version']=='reviewed_policy_generation_v2'
    permit.require_scope(stage='repair_generation' if repair else 'generation',book_id=a['book_id'],chapter=a['chapter'],
        execution_id=a['execution_id'],input_sha256=a['input_sha256'])
    if permit.binding['batch_id']!=a['batch_id'] or permit.binding['grant_sha256']!=a['grant_sha256']:
        raise BatchControlError('自动生成不是本次原作者批次授权。')
    if repair and permit.binding.get('repair_claim_sha256')!=a['repair_claim_sha256']:
        raise BatchControlError('修复生成不属于原已消耗周期。')
    if prepared is not None and (generation_input_sha256(prepared)!=a['input_sha256']
            or prepared['identity']['book_id']!=a['book_id'] or prepared['chapter_index']!=a['chapter']
            or prepared['source']['context_sha256']!=a['context_sha256']
            or digest(prepared['chapter_intent'])!=a['chapter_intent_sha256']):
        raise BatchControlError('自动生成的完整输入与原策略凭据不同。')
    return a
