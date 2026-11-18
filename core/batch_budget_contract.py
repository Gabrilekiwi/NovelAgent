"""Finite, single-currency batch caps over original priced chapter budgets."""
import copy,re
from decimal import Decimal,localcontext

from core.chapter_budget_contract import validate_chapter_budget,normalize_budget_decimal,MAX_COUNT
from core.memory_v2.canonical import canonical_json_hash


def _hash(value):return canonical_json_hash(value,exclude_environment_fields=False)


def freeze_batch_budget(*,batch_id,chapter_budgets,amount_limit,max_provider_calls,max_input_tokens,max_output_tokens):
    if not isinstance(batch_id,str) or re.fullmatch(r'batch_[0-9a-f]{32}',batch_id) is None:
        raise ValueError('invalid batch budget identity')
    if not isinstance(chapter_budgets,list) or not 1<=len(chapter_budgets)<=64:
        raise ValueError('batch requires 1..64 finite contiguous chapter budgets')
    chapters=[validate_chapter_budget(b) for b in chapter_budgets]
    chapters.sort(key=lambda b:b['chapter'])
    first=chapters[0]
    if (any(b['book_id']!=first['book_id'] or b['currency']!=first['currency']
            or b['amount_limit'] is None or not b['pricing'] for b in chapters)
            or [b['chapter'] for b in chapters]!=list(range(first['chapter'],first['chapter']+len(chapters)))):
        raise ValueError('batch requires priced budgets in one book/currency and consecutive chapters')
    caps=dict(max_provider_calls=max_provider_calls,max_input_tokens=max_input_tokens,max_output_tokens=max_output_tokens)
    if any(type(n) is not int or not 1<=n<=MAX_COUNT for n in caps.values()):
        raise ValueError('batch call/token caps must be positive bounded integers')
    result=dict(schema_version='batch_budget_v1',batch_id=batch_id,book_id=first['book_id'],
        currency=first['currency'],chapter_budgets=chapters,
        amount_limit=normalize_budget_decimal(amount_limit,label='batch amount_limit'),**caps)
    result['batch_budget_sha256']=_hash(result)
    return result


def validate_batch_budget(value,*,book_id=None):
    fields={'schema_version','batch_id','book_id','currency','chapter_budgets','amount_limit',
        'max_provider_calls','max_input_tokens','max_output_tokens','batch_budget_sha256'}
    if not isinstance(value,dict) or set(value)!=fields or value['schema_version']!='batch_budget_v1':
        raise ValueError('invalid batch budget fields')
    result=freeze_batch_budget(**{k:value[k] for k in ('batch_id','chapter_budgets','amount_limit',
        'max_provider_calls','max_input_tokens','max_output_tokens')})
    if value!=result or _hash(value)!=_hash(result) or book_id is not None and result['book_id']!=book_id:
        raise ValueError('batch budget hash, canonical values or book differs')
    return result


def chapter_batch_binding(batch,chapter):
    batch=validate_batch_budget(batch)
    budget=next((b for b in batch['chapter_budgets'] if b['chapter']==chapter),None)
    if budget is None or type(chapter) is not int:raise ValueError('chapter outside batch')
    value=dict(schema_version='chapter_batch_budget_binding_v1',book_id=batch['book_id'],chapter=chapter,
        budget_sha256=budget['budget_sha256'],batch_id=batch['batch_id'],batch_budget_sha256=batch['batch_budget_sha256'])
    return {**value,'binding_sha256':_hash(value)}


def validate_chapter_batch_binding(value,budget):
    fields={'schema_version','book_id','chapter','budget_sha256','batch_id','batch_budget_sha256','binding_sha256'}
    if (not isinstance(value,dict) or set(value)!=fields or value['schema_version']!='chapter_batch_budget_binding_v1'
            or type(value['chapter']) is not int or any(value[k]!=budget[k] for k in ('book_id','chapter','budget_sha256'))
            or not isinstance(value['batch_id'],str) or re.fullmatch(r'batch_[0-9a-f]{32}',value['batch_id']) is None
            or not isinstance(value['batch_budget_sha256'],str) or re.fullmatch(r'[0-9a-f]{64}',value['batch_budget_sha256']) is None
            or value['binding_sha256']!=_hash({k:v for k,v in value.items() if k!='binding_sha256'})):
        raise ValueError('invalid original chapter/batch binding')
    return copy.deepcopy(value)


def batch_ready(batch):
    value=dict(schema_version='batch_budget_ready_v1',batch_id=batch['batch_id'],book_id=batch['book_id'],
        batch_budget_sha256=batch['batch_budget_sha256'])
    return {**value,'ready_sha256':_hash(value)}


def project_batch_usage(batch,snapshots):
    """Sum original reservations/receipts exactly once; never settle or reset."""
    batch=validate_batch_budget(batch)
    reports=[]
    for budget in batch['chapter_budgets']:
        snapshot=snapshots[budget['chapter']]
        if snapshot['budget']!=budget:raise ValueError('batch original chapter budget differs')
        report=snapshot['report']
        if report['currency']!=batch['currency'] or not report['amount_controlled'] or report['charged_amount'] is None:
            raise ValueError('batch chapter spending is not explainable in its frozen currency')
        reports.append(report)
    totals={name:sum(r['charged'][name] for r in reports) for name in ('calls','input_tokens','output_tokens')}
    with localcontext() as context:
        context.prec=80
        amount=sum((Decimal(r['charged_amount']) for r in reports),Decimal(0))
        amount_text=format(amount,'f').rstrip('0').rstrip('.') if '.' in format(amount,'f') else format(amount,'f')
    limits={'calls':'max_provider_calls','input_tokens':'max_input_tokens','output_tokens':'max_output_tokens'}
    exceeded=[limits[n] for n in limits if totals[n]>batch[limits[n]]]
    if amount>Decimal(batch['amount_limit']):exceeded.append('amount_limit')
    return dict(batch_id=batch['batch_id'],batch_budget_sha256=batch['batch_budget_sha256'],book_id=batch['book_id'],
        currency=batch['currency'],amount_controlled=True,charged_amount=amount_text,charged=totals,
        exceeded=exceeded,unresolved_attempts=[dict(chapter=r['chapter'],execution_id=e,attempt_id=a)
            for r in reports for e,a in r['unresolved_attempts']],
        chapter_budgets=[{'chapter':b['chapter'],'budget_sha256':b['budget_sha256']} for b in batch['chapter_budgets']])
