"""Chapter-wide, durable reservations derived from immutable call evidence.

The service owns the book and supplies its fixed chapter budget directory.
This store never dispatches a Provider or invents missing usage. Source records
remain authoritative; the runtime must reconcile stored receipts before a new
attempt and may not turn an old, unbudgeted execution into an empty budget.
"""
from contextlib import contextmanager
import copy
from decimal import Decimal, localcontext
import json
from pathlib import Path
import re

from core.chapter_budget_contract import (ChapterBudgetContractError,
    estimate_attempt_cost, reserve_attempt_cost, select_pricing, validate_chapter_budget)
from core.chapter_budget_lock import chapter_budget_lock
from core.engine.persistence import atomic_create_json, atomic_write_json
from core.engine.safe_paths import SafePathError, assert_safe_local_tree
from core.memory_v2.canonical import canonical_json_hash
from core.model_calls import validate_model_call_intent, validate_model_call_receipt
from core.model_call_usage import project_model_attempt
from core.story_project.reviewed_state_document import read_project_bytes


class ChapterBudgetError(RuntimeError):
    code = 'chapter_budget_evidence_invalid'
    failure_category = 'local_budget'
    retryable = False


class ChapterBudgetConflict(ChapterBudgetError):
    code = 'chapter_budget_conflict'


class ChapterBudgetExceeded(ChapterBudgetError):
    code = 'chapter_budget_exceeded'


class ChapterBudgetUncertain(ChapterBudgetError):
    code = 'chapter_budget_uncertain'


def _hash(value):
    return canonical_json_hash(value, exclude_environment_fields=False)


def _amount(value):
    raw = format(value, 'f')
    return raw.rstrip('0').rstrip('.') if '.' in raw else raw


def _id(value):
    if not isinstance(value,str) or not re.fullmatch(r'execution_[a-zA-Z0-9_-]{1,180}',value):
        raise ChapterBudgetError('invalid budget execution identity')
    return value


def _count(value):
    return value if type(value) is int and 0 <= value <= 10**15 else None


def _field(value,path):
    for key in path.split('.'):
        if not isinstance(value,dict) or key not in value:return None
        value=value[key]
    return value


def _billing_quantities(projection, receipt):
    """Only explicit Provider-reported counters qualify for releasing a reserve."""
    usage=projection['usage']
    for key in ('input_tokens','output_tokens','cache_read_tokens'):
        if usage[key]['source']!='provider_reported' or usage[key]['value'] is None:return None
    raw=receipt['usage']
    five=_count(_field(raw,'cache_creation.ephemeral_5m_input_tokens'))
    hour=_count(_field(raw,'cache_creation.ephemeral_1h_input_tokens'))
    total=usage['cache_creation_tokens']['value']
    if total is not None and usage['cache_creation_tokens']['source']!='provider_reported':return None
    if total==0:
        if (five is not None and five!=0) or (hour is not None and hour!=0):return None
        five=hour=0
    if five is None or hour is None or total is not None and total!=five+hour:return None
    return dict(input_tokens=usage['input_tokens']['value'],output_tokens=usage['output_tokens']['value'],
        cache_read_tokens=usage['cache_read_tokens']['value'],cache_write_5m_tokens=five,cache_write_1h_tokens=hour)


class ChapterBudgetStore:
    def __init__(self,root,*,book_id,chapter):
        self.root=assert_safe_local_tree(Path(root))
        self.book_id,self.chapter=book_id,chapter

    @contextmanager
    def _locked(self):
        assert_safe_local_tree(self.root)
        lock=assert_safe_local_tree(self.root/'budget-lock')
        with chapter_budget_lock(lock):
            assert_safe_local_tree(self.root)
            yield

    def _read(self,name):
        try:return json.loads(read_project_bytes(self.root,name))
        except (OSError,ValueError,SafePathError) as exc:
            raise ChapterBudgetError('budget evidence is missing or unreadable') from exc

    def initialize(self,budget):
        """First-time freeze only. Existing or incomplete evidence is not reset."""
        budget=validate_chapter_budget(budget,expected_book_id=self.book_id,expected_chapter=self.chapter)
        with self._locked():
            if (self.root/'budget.json').exists():
                old,ledger=self._load()
                if old!=budget:raise ChapterBudgetConflict('chapter budget is already frozen')
                return copy.deepcopy(old)
            if (self.root/'ledger.json').exists():
                raise ChapterBudgetError('ledger has lost its frozen budget')
            atomic_create_json(self.root/'budget.json',budget)
            self._save({'schema_version':'chapter_budget_ledger_v2','budget_sha256':budget['budget_sha256'],
                        'attempts':[]},create=True)
        return copy.deepcopy(budget)

    def _save(self,value,*,create=False):
        assert_safe_local_tree(self.root)
        if not create:read_project_bytes(self.root,'ledger.json')
        result={k:v for k,v in value.items() if k!='ledger_sha256'}
        result['ledger_sha256']=_hash(result)
        (atomic_create_json if create else atomic_write_json)(self.root/'ledger.json',result)

    def _plan(self,budget,intent,*,endpoint_type,endpoint_fingerprint,input_bound,billing_plan=None):
        intent=validate_model_call_intent(intent)
        if (not isinstance(endpoint_type,str) or not endpoint_type or len(endpoint_type)>200
                or not isinstance(endpoint_fingerprint,str) or not re.fullmatch('[0-9a-f]{64}',endpoint_fingerprint)):
            raise ChapterBudgetError('attempt endpoint binding is invalid')
        reserved=intent['budget_reservation']
        inputs=_count(reserved.get('reserved_input_tokens'));outputs=_count(reserved.get('reserved_output_tokens'))
        if inputs is None or outputs is None or reserved.get('reserved_total_tokens')!=inputs+outputs:
            raise ChapterBudgetError('attempt token reservation is invalid')
        if input_bound is not None:
            if (not isinstance(input_bound,dict) or set(input_bound)!={'max_billable_input_tokens','basis','request_digest','evidence_sha256'}
                    or _count(input_bound['max_billable_input_tokens']) is None
                    or input_bound['max_billable_input_tokens']<inputs
                    or input_bound['basis'] not in {'verified_token_count','frozen_context_limit'}
                    or input_bound['request_digest']!=intent['request_digest']
                    or not isinstance(input_bound['evidence_sha256'],str)
                    or not re.fullmatch('[0-9a-f]{64}',input_bound['evidence_sha256'])):
                raise ChapterBudgetError('billable input bound must be tied to this exact request')
            inputs=input_bound['max_billable_input_tokens']
        if billing_plan is not None:
            from core.provider_billing_request import validate_stored_openai_billing_plan
            billing_plan=validate_stored_openai_billing_plan(billing_plan)
            if (input_bound is not None or intent['provider']!=billing_plan['provider']
                    or intent['model']!=billing_plan['model'] or endpoint_type!='official'
                    or endpoint_fingerprint!=billing_plan['endpoint_fingerprint']
                    or intent['request_digest']!=billing_plan['request_digest']
                    or outputs!=billing_plan['max_billable_output_tokens']
                    or inputs>billing_plan['max_billable_input_tokens']):
                raise ChapterBudgetError('reviewed billing plan differs from physical intent or endpoint')
            inputs=billing_plan['max_billable_input_tokens']
        price=None;cost=None
        if budget['amount_limit'] is not None:
            if input_bound is None and billing_plan is None:
                raise ChapterBudgetError('amount control requires a justified billable input upper bound')
            price=select_pricing(budget,provider=intent['provider'],model=intent['model'],
                                endpoint_type=endpoint_type,endpoint_fingerprint=endpoint_fingerprint)
            if billing_plan is not None or 'billing_policy' in price:
                from core.provider_billing_request import openai_text_billing_policy
                if billing_plan is None or price.get('billing_policy')!=openai_text_billing_policy():
                    raise ChapterBudgetError('reviewed monetary reservation requires both request and frozen tariff policy')
            cost=reserve_attempt_cost(price,billable_input_upper_bound=inputs,max_output_tokens=outputs)
        result=dict(intent=intent,endpoint_type=endpoint_type,endpoint_fingerprint=endpoint_fingerprint,
                    input_bound=copy.deepcopy(input_bound),pricing=price,reserved_input_tokens=inputs,
                    reserved_output_tokens=outputs,reserved_cost=cost)
        if billing_plan is not None:result['billing_request_plan']=copy.deepcopy(billing_plan)
        return result

    def _load(self):
        budget=validate_chapter_budget(self._read('budget.json'),expected_book_id=self.book_id,expected_chapter=self.chapter)
        value=self._read('ledger.json')
        schema=value.get('schema_version') if isinstance(value,dict) else None
        fields={'schema_version','budget_sha256','attempts','ledger_sha256'}
        if schema in ('chapter_budget_ledger_v3','chapter_budget_ledger_v4'):fields.add('repair_cycle')
        if schema=='chapter_budget_ledger_v4':fields.add('batch_budget')
        if (not isinstance(value,dict)
                or schema not in ('chapter_budget_ledger_v2','chapter_budget_ledger_v3','chapter_budget_ledger_v4') or set(value)!=fields
                or value['budget_sha256']!=budget['budget_sha256']
                or value['ledger_sha256']!=_hash({k:v for k,v in value.items() if k!='ledger_sha256'})
                or not isinstance(value['attempts'],list) or len(value['attempts'])>10000):
            raise ChapterBudgetError('budget ledger shape, hash or frozen binding differs')
        if schema=='chapter_budget_ledger_v3' or schema=='chapter_budget_ledger_v4' and value['repair_cycle'] is not None:
            from core.quality.reviewed_repair_cycle import validate_repair_cycle_claim
            try:validate_repair_cycle_claim(value['repair_cycle'],budget=budget)
            except (ValueError,TypeError) as exc:raise ChapterBudgetError('repair cycle claim is invalid') from exc
        if schema=='chapter_budget_ledger_v4':
            from core.batch_budget_contract import validate_chapter_batch_binding
            try:validate_chapter_batch_binding(value['batch_budget'],budget)
            except (ValueError,TypeError) as exc:raise ChapterBudgetError('chapter batch binding is invalid') from exc
            if value['repair_cycle'] is not None and value['repair_cycle']['batch_id']!=value['batch_budget']['batch_id']:
                raise ChapterBudgetError('repair cycle belongs to a different original batch')
        keys=set()
        for row in value['attempts']:
            if not isinstance(row,dict) or set(row)!={'execution_id','plan','receipt','local_rejection','dispatch_claimed'}:
                raise ChapterBudgetError('invalid budget attempt entry')
            if type(row['dispatch_claimed']) is not bool or row['local_rejection'] and row['dispatch_claimed']:
                raise ChapterBudgetError('invalid dispatch claim')
            execution=_id(row['execution_id']);plan=row['plan']
            if not isinstance(plan,dict):raise ChapterBudgetError('invalid attempt plan')
            try:
                expected=self._plan(budget,plan['intent'],endpoint_type=plan['endpoint_type'],
                    endpoint_fingerprint=plan['endpoint_fingerprint'],input_bound=plan['input_bound'],
                    billing_plan=plan.get('billing_request_plan'))
            except KeyError as exc:raise ChapterBudgetError('incomplete attempt plan') from exc
            if plan!=expected:raise ChapterBudgetError('stored reservation differs from frozen inputs')
            key=(execution,plan['intent']['attempt_id'])
            if key in keys:raise ChapterBudgetError('duplicate budget attempt identity')
            keys.add(key)
            if row['local_rejection'] not in {None,'max_provider_calls','max_input_tokens','max_output_tokens','amount_limit',
                    'batch_max_provider_calls','batch_max_input_tokens','batch_max_output_tokens','batch_amount_limit'}:
                raise ChapterBudgetError('invalid local budget rejection')
            if row['receipt'] is not None:
                receipt=validate_model_call_receipt(row['receipt'])
                if any(receipt[k]!=plan['intent'][k] for k in ('call_id','attempt_id','intent_hash')):
                    raise ChapterBudgetError('receipt belongs to a different reserved attempt')
                if row['local_rejection'] and receipt['status'] not in {'budget_rejected','request_not_sent'}:
                    raise ChapterBudgetError('locally rejected attempt has a dispatched receipt')
                self._require_receipt_phase(row,receipt)
        return budget,value

    def _account(self,row):
        plan=row['plan'];receipt=row['receipt'];intent=plan['intent']
        cost=plan['reserved_cost']
        item=dict(execution_id=row['execution_id'],attempt_id=intent['attempt_id'],intent_hash=intent['intent_hash'],
            receipt_hash=receipt['receipt_hash'] if receipt else None,stage=intent['stage'],
            accounting_state='reserved',charged_calls=1,charged_input_tokens=plan['reserved_input_tokens'],
            charged_output_tokens=plan['reserved_output_tokens'],charged_amount=cost['amount'] if cost else None,
            cost_classification='reservation' if cost else 'unpriced',calculation=None,reason=None)
        if row['local_rejection'] or receipt and receipt['status'] in {'budget_rejected','request_not_sent'}:
            return {**item,'accounting_state':'released','charged_calls':0,'charged_input_tokens':0,
                'charged_output_tokens':0,'charged_amount':'0' if cost else None,'cost_classification':'not_sent',
                'reason':row['local_rejection'] or receipt['status']}
        if receipt is None:return item
        projection=project_model_attempt(row['execution_id'],intent,receipt)
        tokens=projection['usage']
        # Known counters above the reservation are always retained, including
        # failures. A partial response never refunds a potentially larger bill.
        for name in ('input_tokens','output_tokens'):
            if tokens[name]['value'] is not None:
                item['charged_'+name]=max(item['charged_'+name],tokens[name]['value'])
        item.update(accounting_state='held_unknown',reason='terminal_usage_not_settled')
        if receipt['actual_model']!=intent['model'] or receipt['endpoint_type']!=plan['endpoint_type']:
            return {**item,'reason':'returned_model_or_endpoint_differs'}
        known=all(tokens[k]['source']=='provider_reported' and tokens[k]['value'] is not None for k in ('input_tokens','output_tokens'))
        if cost:
            reviewed=plan.get('billing_request_plan')
            if reviewed is not None:
                from core.provider_billing_response import reviewed_openai_billing_quantities
                quantities,reason=reviewed_openai_billing_quantities(reviewed,receipt,projection)
                if reason:return {**item,'reason':reason}
            else:quantities=_billing_quantities(projection,receipt)
            if quantities is None:return {**item,'reason':'billing_quantities_unknown'}
            try:calculation=estimate_attempt_cost(plan['pricing'],**quantities)
            except ChapterBudgetContractError:return {**item,'reason':'billing_tariff_unsupported'}
            outside_bound=(reviewed is not None and (calculation['billable_input_tokens']>plan['reserved_input_tokens']
                or quantities['output_tokens']>plan['reserved_output_tokens']))
            if receipt['status']=='succeeded' and not outside_bound:
                return {**item,'accounting_state':'settled','reason':None,
                    'charged_input_tokens':calculation['billable_input_tokens'],
                    'charged_output_tokens':quantities['output_tokens'],
                    'charged_amount':calculation['amount'],'cost_classification':'estimated','calculation':calculation}
            item['charged_amount']=_amount(max(Decimal(item['charged_amount']),Decimal(calculation['amount'])))
            item['charged_input_tokens']=max(item['charged_input_tokens'],calculation['billable_input_tokens'])
            if outside_bound:item['reason']='billing_bound_exceeded'
        elif known and receipt['status']=='succeeded':
            # Without a frozen input/cache semantics, keep the input reserve;
            # the Provider's visible counter may exclude extra cache tokens.
            return {**item,'accounting_state':'settled','reason':None,
                    'charged_output_tokens':tokens['output_tokens']['value']}
        return item

    def _report(self,budget,value):
        items=[self._account(row) for row in value['attempts']]
        with localcontext() as context:
            context.prec=80
            amount=sum((Decimal(x['charged_amount']) for x in items if x['charged_amount'] is not None),Decimal(0))
        totals={name:sum(x['charged_'+name] for x in items) for name in ('calls','input_tokens','output_tokens')}
        limits={'calls':budget['max_provider_calls'],'input_tokens':budget['max_input_tokens'],'output_tokens':budget['max_output_tokens']}
        exceeded=[name for name in totals if totals[name]>limits[name]]
        if budget['amount_limit'] is not None and amount>Decimal(budget['amount_limit']):exceeded.append('amount')
        result=dict(book_id=budget['book_id'],chapter=budget['chapter'],budget_sha256=budget['budget_sha256'],
            currency=budget['currency'],amount_controlled=budget['amount_limit'] is not None,
            charged_amount=_amount(amount) if budget['amount_limit'] is not None else None,
            charged=totals,items=items,exceeded=exceeded,
            unresolved_attempts=[(x['execution_id'],x['attempt_id']) for x in items if x['accounting_state'] in {'reserved','held_unknown'}])
        if 'repair_cycle' in value:result['repair_cycle']=copy.deepcopy(value['repair_cycle'])
        return result

    def repair_cycle_claim(self):
        """Read only; a missing/corrupt ledger never becomes a new allowance."""
        _,value=self._load()
        return copy.deepcopy(value.get('repair_cycle'))

    def claim_repair_cycle(self,claim,*,before_claim,fault_injector=None):
        """Consume once per original chapter budget, before any repair action.

        The service must hold book ownership and Provider exclusion, and use
        before_claim to revalidate its real batch grant and original source.
        This internal storage primitive grants no Provider or publish authority.
        A different batch/request cannot replace an already consumed cycle.
        """
        from core.quality.reviewed_repair_cycle import validate_repair_cycle_claim
        if not callable(before_claim):raise ChapterBudgetError('repair owner/source preflight is required')
        with self._locked():
            budget,value=self._load()
            claim=validate_repair_cycle_claim(claim,budget=budget)
            if 'batch_budget' in value and claim['batch_id']!=value['batch_budget']['batch_id']:
                raise ChapterBudgetConflict('repair cycle belongs to a different original batch')
            existing=value.get('repair_cycle')
            if existing is not None:
                if existing!=claim or _hash(existing)!=_hash(claim):
                    raise ChapterBudgetConflict('this chapter has already consumed its one repair cycle')
                return copy.deepcopy(existing)
            report=self._report(budget,value)
            if report['unresolved_attempts']:raise ChapterBudgetUncertain('unresolved calls block a new repair cycle')
            if report['exceeded']:raise ChapterBudgetExceeded('chapter budget already exceeded before repair')
            before_claim()
            # Persist in the original ledger: there is no optional side file
            # whose disappearance could be mistaken for an unused allowance.
            value.update(schema_version='chapter_budget_ledger_v4' if 'batch_budget' in value else 'chapter_budget_ledger_v3',repair_cycle=claim)
            self._save(value)
            if fault_injector is not None:fault_injector('after_repair_cycle_claim')
            return copy.deepcopy(claim)

    def report(self):
        budget,value=self._load()
        return self._report(budget,value)

    def usage_snapshot(self):
        """One validated ledger revision for read-only cost attribution.

        Never acquire a writing lock, reconcile receipts, or initialize files.
        Plans and accounting must come from the same revision while a call runs.
        """
        budget,value=self._load()
        result=dict(budget=budget,attempts=value['attempts'],report=self._report(budget,value))
        if 'batch_budget' in value:result['batch_budget']=value['batch_budget']
        return copy.deepcopy(result)

    def frozen_budget(self):
        budget,_=self._load()
        return copy.deepcopy(budget)

    def execution_attempts(self,execution_id):
        """Read original plans for recovery; never reconstruct an Intent timestamp."""
        execution_id=_id(execution_id)
        _,value=self._load()
        return copy.deepcopy([x for x in value['attempts'] if x['execution_id']==execution_id])

    def mark_dispatched(self,execution_id,intent):
        """Claim once, before invoking the Provider; a claim cannot be reset."""
        execution_id=_id(execution_id);intent=validate_model_call_intent(intent)
        with self._locked():
            budget,value=self._load()
            row=next((x for x in value['attempts'] if x['execution_id']==execution_id
                and x['plan']['intent']['attempt_id']==intent['attempt_id']),None)
            if row is None or row['plan']['intent']!=intent:
                raise ChapterBudgetConflict('dispatch has no matching original reservation')
            if row['dispatch_claimed'] or row['receipt'] is not None or row['local_rejection']:
                raise ChapterBudgetConflict('attempt cannot be dispatched again or after local rejection')
            from core.chapter_budget_continuation import require_dispatch
            require_dispatch(self,budget,value,row)
            from core.batch_budget_store import BatchBudgetRegistry
            from core.batch_budget_contract import project_batch_usage
            batch=BatchBudgetRegistry(self.root.parent,book_id=self.book_id).for_chapter(self.chapter,value)
            if batch is not None and project_batch_usage(*batch)['exceeded']:
                raise ChapterBudgetExceeded('original batch budget is exceeded before dispatch')
            row['dispatch_claimed']=True
            self._save(value)

    @staticmethod
    def _require_receipt_phase(row,receipt):
        if receipt['status']=='budget_rejected' and row['dispatch_claimed']:
            raise ChapterBudgetConflict('a dispatched attempt cannot be a local budget rejection')
        if receipt['status'] not in {'budget_rejected','request_not_sent'} and not row['dispatch_claimed']:
            raise ChapterBudgetConflict('provider receipt has no durable dispatch claim')

    def reserve(self,execution_id,intent,*,endpoint_type,endpoint_fingerprint,input_bound=None,
                billing_plan=None,request=None,base_url=None):
        from core.batch_budget_store import coordinator_lock
        # Every reservation uses the same book-budget coordinator, including
        # manual calls. A bound chapter cannot opt out by constructing a plain
        # ChapterBudgetStore or by changing the runtime adapter.
        with coordinator_lock(self.root.parent):
            return self._reserve_in_coordinator(execution_id,intent,endpoint_type=endpoint_type,
                endpoint_fingerprint=endpoint_fingerprint,input_bound=input_bound,
                billing_plan=billing_plan,request=request,base_url=base_url)

    def _reserve_in_coordinator(self,execution_id,intent,*,endpoint_type,endpoint_fingerprint,input_bound=None,
                billing_plan=None,request=None,base_url=None):
        execution_id=_id(execution_id)
        if billing_plan is not None:
            from core.provider_billing_request import validate_openai_billing_plan
            billing_plan=validate_openai_billing_plan(billing_plan,request,base_url=base_url)
        rejection=None
        with self._locked():
            budget,value=self._load()
            plan=self._plan(budget,intent,endpoint_type=endpoint_type,endpoint_fingerprint=endpoint_fingerprint,
                input_bound=input_bound,billing_plan=billing_plan)
            key=(execution_id,plan['intent']['attempt_id'])
            for old in value['attempts']:
                if (old['execution_id'],old['plan']['intent']['attempt_id'])==key:
                    if old['plan']!=plan:raise ChapterBudgetConflict('physical attempt reused with different budget inputs')
                    if old['local_rejection']:raise ChapterBudgetExceeded(old['local_rejection'])
                    return self._account(old)
            before=self._report(budget,value)
            from core.chapter_budget_continuation import allows, load as load_continuation
            continuation=load_continuation(self,budget)
            if before['unresolved_attempts'] or continuation and execution_id==continuation['target_execution_id']:
                if not allows(self,budget,value,execution_id,plan):
                    raise ChapterBudgetUncertain('earlier chapter attempt needs receipt reconciliation or its exact one-shot continuation before new spending')
            from core.batch_budget_store import BatchBudgetRegistry
            from core.batch_budget_contract import project_batch_usage
            batch=BatchBudgetRegistry(self.root.parent,book_id=self.book_id).for_chapter(self.chapter,value)
            if batch is not None and project_batch_usage(*batch)['unresolved_attempts']:
                raise ChapterBudgetUncertain('earlier batch attempt needs receipt reconciliation before new spending')
            if len(value['attempts'])>=10000:raise ChapterBudgetError('chapter budget attempt limit reached')
            row={'execution_id':execution_id,'plan':plan,'receipt':None,'local_rejection':None,'dispatch_claimed':False}
            value['attempts'].append(row)
            after=self._report(budget,value)
            mapping={'calls':'max_provider_calls','input_tokens':'max_input_tokens','output_tokens':'max_output_tokens','amount':'amount_limit'}
            if after['exceeded']:
                rejection=mapping[after['exceeded'][0]]
                row['local_rejection']=rejection
            elif batch is not None:
                contract,snapshots=batch
                snapshots[self.chapter]=dict(budget=budget,report=after)
                exceeded=project_batch_usage(contract,snapshots)['exceeded']
                if exceeded:
                    rejection='batch_'+exceeded[0]
                    row['local_rejection']=rejection
            self._save(value)
            result=self._account(row)
        if rejection:raise ChapterBudgetExceeded(rejection)
        return result

    def settle(self,execution_id,receipt):
        """Call only with the runtime's durably published, verified Receipt."""
        execution_id=_id(execution_id);receipt=validate_model_call_receipt(receipt)
        with self._locked():
            budget,value=self._load()
            row=next((x for x in value['attempts'] if x['execution_id']==execution_id
                and x['plan']['intent']['attempt_id']==receipt['attempt_id']),None)
            if row is None:raise ChapterBudgetConflict('receipt has no original chapter reservation')
            intent=row['plan']['intent']
            if any(receipt[k]!=intent[k] for k in ('call_id','attempt_id','intent_hash')):
                raise ChapterBudgetConflict('receipt differs from reserved request')
            if row['receipt'] is not None:
                if row['receipt']!=receipt:raise ChapterBudgetConflict('attempt already has a different terminal receipt')
                return self._account(row)
            if row['local_rejection'] and receipt['status'] not in {'budget_rejected','request_not_sent'}:
                raise ChapterBudgetConflict('locally rejected attempt cannot have been dispatched')
            self._require_receipt_phase(row,receipt)
            row['receipt']=copy.deepcopy(receipt)
            self._save(value)
            return self._account(row)
