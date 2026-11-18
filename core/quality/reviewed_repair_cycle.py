"""Immutable one-cycle consumption evidence; never an execution permit.

The future batch service must derive/verify its grant under book ownership.
A digest supplied to this internal builder is a binding, not authentication.
No HTTP caller may supply this claim or a quality report as authority.
"""
import copy
import re

from core.chapter_budget_contract import validate_chapter_budget
from core.memory_v2.canonical import canonical_json_hash
from core.quality.reviewed_disposition import project_reviewed_quality_disposition


class ReviewedRepairCycleError(ValueError):
    code='reviewed_repair_cycle_invalid'


def _hash(value):
    return canonical_json_hash(value,exclude_environment_fields=False)


_HASH_FIELDS=('budget_sha256','batch_authorization_sha256','policy_sha256',
    'preparation_sha256','quality_sha256','disposition_sha256','final_prose_sha256')
_FIELDS={'schema_version','claim_id','execution_id','book_id','chapter','batch_id',
    *_HASH_FIELDS,'repair_cycles_consumed','execution_authorized','claim_sha256'}


def validate_repair_cycle_claim(value,*,budget):
    """Check stored consumption/bindings, without inventing grant verification."""
    budget=validate_chapter_budget(budget)
    if (not isinstance(value,dict) or set(value)!=_FIELDS
            or value['schema_version']!='reviewed_repair_cycle_claim_v1'
            or value['book_id']!=budget['book_id'] or type(value['chapter']) is not int
            or value['chapter']!=budget['chapter'] or value['budget_sha256']!=budget['budget_sha256']
            or type(value['repair_cycles_consumed']) is not int or value['repair_cycles_consumed']!=1
            or value['execution_authorized'] is not False
            or not isinstance(value['batch_id'],str) or re.fullmatch(r'batch_[0-9a-f]{32}',value['batch_id']) is None
            or any(not isinstance(value[k],str) or re.fullmatch(r'[0-9a-f]{64}',value[k]) is None for k in _HASH_FIELDS)):
        raise ReviewedRepairCycleError('修复周期消耗记录、预算或批次绑定无效。')
    base={k:v for k,v in value.items() if k not in {'claim_id','execution_id','claim_sha256'}}
    identity=_hash(base)
    if (value['claim_id']!='repair_'+identity[:40]
            or value['execution_id']!='execution_repair_'+identity[:32]
            or value['claim_sha256']!=_hash({k:v for k,v in value.items() if k!='claim_sha256'})):
        raise ReviewedRepairCycleError('修复周期原始身份或校验值不同。')
    return copy.deepcopy(value)


def build_repair_cycle_claim(*,preparation,quality,budget,batch_id,batch_authorization_sha256,policy_snapshot=None):
    """Bind a server-owned failed check to its frozen batch and chapter budget.

    This builder does not authorize spending. The owner service must verify
    the real stored batch grant, source freshness and budget before claiming.
    """
    budget=validate_chapter_budget(budget)
    disposition=project_reviewed_quality_disposition(preparation=preparation,quality=quality,policy_snapshot=policy_snapshot)
    if (disposition['automatic_action']!='repair_once'
            or disposition['binding']['book_id']!=budget['book_id']
            or disposition['binding']['chapter']!=budget['chapter']):
        raise ReviewedRepairCycleError('本次检查不具备已列明且有正文依据的单次修复资格。')
    result={'schema_version':'reviewed_repair_cycle_claim_v1',
        'book_id':budget['book_id'],'chapter':budget['chapter'],'budget_sha256':budget['budget_sha256'],
        'batch_id':batch_id,'batch_authorization_sha256':batch_authorization_sha256,
        'policy_sha256':disposition['policy_sha256'],
        'preparation_sha256':preparation['preparation_sha256'],'quality_sha256':quality['quality_sha256'],
        'disposition_sha256':disposition['disposition_sha256'],
        'final_prose_sha256':disposition['binding']['final_prose_sha256'],
        'repair_cycles_consumed':1,'execution_authorized':False}
    identity=_hash(result)
    result.update(claim_id='repair_'+identity[:40],execution_id='execution_repair_'+identity[:32])
    result['claim_sha256']=_hash(result)
    return validate_repair_cycle_claim(result,budget=budget)
