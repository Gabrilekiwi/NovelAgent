"""Policy extraction input binding; durable hashes do not create live permission."""
import copy,re

from core.engine.reviewed_batch_control import BatchControlError,BatchControlPaused,current_policy_step
from core.story_project.reviewed_state_document import digest


def extraction_input_sha256(prepared):
    return digest({**{k:prepared[k] for k in ('book_id','chapter_index','candidate','context','authority',
        'authority_view_sha256','authority_view_chars')},
        'draft':{k:prepared['draft'][k] for k in ('draft_id','revision','sha256')}})


def policy_extraction_authorization(*,grant,prepared,generation_result_sha256,binding,repair_claim_sha256=None):
    value=dict(schema_version='reviewed_policy_extraction_v1',
        request_source='policy',author_confirmed=False,book_id=prepared['book_id'],chapter=prepared['chapter_index'],
        batch_id=grant['manifest']['batch_id'],grant_sha256=grant['grant_sha256'],
        manifest_sha256=grant['manifest']['manifest_sha256'],generation_result_sha256=generation_result_sha256,
        step_id=binding['step_id'],execution_id=binding['execution_id'],input_sha256=extraction_input_sha256(prepared))
    if repair_claim_sha256 is not None:value.update(schema_version='reviewed_policy_extraction_v2',repair_claim_sha256=repair_claim_sha256)
    return validate_policy_extraction_authorization(value)


def validate_policy_extraction_authorization(value):
    fields={'schema_version','request_source','author_confirmed','book_id','chapter','batch_id','grant_sha256',
        'manifest_sha256','generation_result_sha256','step_id','execution_id','input_sha256'}
    repair=isinstance(value,dict) and value.get('schema_version')=='reviewed_policy_extraction_v2'
    if repair:fields.add('repair_claim_sha256')
    if (not isinstance(value,dict) or set(value)!=fields or value['schema_version'] not in {'reviewed_policy_extraction_v1','reviewed_policy_extraction_v2'}
            or value['request_source']!='policy' or value['author_confirmed'] is not False
            or not isinstance(value['book_id'],str) or not value['book_id'] or type(value['chapter']) is not int or value['chapter']<1
            or not isinstance(value['batch_id'],str) or re.fullmatch('batch_[0-9a-f]{32}',value['batch_id']) is None
            or not isinstance(value['step_id'],str) or re.fullmatch('step_[0-9a-f]{32}',value['step_id']) is None
            or not isinstance(value['execution_id'],str) or re.fullmatch('execution_fx_[0-9a-f]{24}',value['execution_id']) is None
            or any(not isinstance(value[k],str) or re.fullmatch('[0-9a-f]{64}',value[k]) is None for k in fields if k.endswith('_sha256'))):
        raise BatchControlError('自动事实提取的原策略来源凭据无效。')
    if value['step_id']!='step_'+digest({'batch_id':value['batch_id'],'chapter':value['chapter'],'stage':'repair_extraction' if repair else 'extraction'})[:32]:
        raise BatchControlError('自动事实提取不属于原确定步骤。')
    return copy.deepcopy(value)


def require_policy_extraction_scope(authorization,*,prepared=None):
    a=validate_policy_extraction_authorization(authorization);permit=current_policy_step()
    if permit is None:raise BatchControlPaused('自动事实提取需要本次明确执行的原步骤许可。')
    repair=a['schema_version']=='reviewed_policy_extraction_v2'
    permit.require_scope(stage='repair_extraction' if repair else 'extraction',book_id=a['book_id'],chapter=a['chapter'],execution_id=a['execution_id'],input_sha256=a['input_sha256'])
    if repair and permit.binding.get('repair_claim_sha256')!=a['repair_claim_sha256']:
        raise BatchControlError('修复后提取不属于原已消耗周期。')
    if permit.binding['batch_id']!=a['batch_id'] or permit.binding['grant_sha256']!=a['grant_sha256']:
        raise BatchControlError('自动事实提取不是本次原作者批次授权。')
    if prepared is not None and (prepared['book_id']!=a['book_id'] or prepared['chapter_index']!=a['chapter']
            or extraction_input_sha256(prepared)!=a['input_sha256']):
        raise BatchControlError('自动事实提取的完整正文、提取视图或来源已变化。')
    return a
