"""One explicitly approved continuation; unknown reservations stay charged.

The immutable permit names one new execution. Its consumption is the ordinary
ledger reservation, so a crash cannot reopen it between two mutable writes.
Issuance belongs to the Candidate service while it owns Provider exclusion.
This version deliberately excludes batches, repairs and moved/restored roots.
"""
from datetime import datetime, timezone
import copy
import os
import re

from core.engine.persistence import atomic_create_json

FILENAME = 'continuation.json'


def _error(message):
    from core.chapter_budget_store import ChapterBudgetConflict
    raise ChapterBudgetConflict(message)


def load(store, budget):
    from core.chapter_budget_store import _hash, _id
    if not os.path.lexists(store.root / FILENAME):
        return None
    grant = store._read(FILENAME)
    fields = {'schema_version', 'book_id', 'chapter', 'root', 'budget_sha256',
              'source_ledger_sha256', 'source_count', 'source_rows_sha256',
              'source_execution_id', 'source_intent_hash', 'source_resolution_hash',
              'target_execution_id', 'candidate_input_sha256', 'candidate_confirmation_sha256',
              'limits', 'author_confirmation', 'created_at',
              'grant_sha256'}
    if not isinstance(grant, dict) or set(grant) != fields:
        _error('invalid continuation permit shape')
    if (grant['schema_version'] != 'chapter_continuation_v1'
            or grant['book_id'] != budget['book_id'] or grant['chapter'] != budget['chapter']
            or grant['root'] != str(store.root.resolve())
            or grant['budget_sha256'] != budget['budget_sha256']
            or grant['grant_sha256'] != _hash({k:v for k,v in grant.items() if k != 'grant_sha256'})
            or grant['author_confirmation'] != {'confirmed': True, 'warning_version': 'chapter_continuation_v1'}):
        _error('continuation permit binding or confirmation differs')
    for name in ('source_ledger_sha256', 'source_rows_sha256', 'source_intent_hash', 'source_resolution_hash',
                 'candidate_input_sha256', 'candidate_confirmation_sha256'):
        if not isinstance(grant[name], str) or not re.fullmatch('[0-9a-f]{64}', grant[name]):
            _error('invalid continuation evidence hash')
    _id(grant['source_execution_id']); _id(grant['target_execution_id'])
    if grant['source_execution_id'] == grant['target_execution_id']:
        _error('continuation must use a new execution')
    if type(grant['source_count']) is not int or not 0 < grant['source_count'] < 10000:
        _error('invalid continuation source count')
    limits = grant['limits']
    if not isinstance(limits, dict) or set(limits) != {
            'provider', 'model', 'stage', 'endpoint_type', 'endpoint_fingerprint',
            'max_input_tokens', 'max_output_tokens'}:
        _error('invalid continuation request limits')
    for name in ('max_input_tokens', 'max_output_tokens'):
        if type(limits[name]) is not int or not 0 < limits[name] <= 10**15:
            _error('invalid continuation token limit')
    for name in ('provider', 'model', 'stage', 'endpoint_type', 'endpoint_fingerprint'):
        if not isinstance(limits[name], str) or not limits[name]:
            _error('invalid continuation endpoint identity')
    return grant


def authorize(store, *, target_execution_id, source_execution_id,
              source_resolution_hash, expected_ledger_sha256, author_confirmed,
              candidate_input_sha256, candidate_confirmation_sha256):
    from core.chapter_budget_store import _hash, _id
    from core.batch_budget_store import coordinator_lock, BatchBudgetRegistry
    if author_confirmed is not True:
        _error('explicit author approval is required for one budgeted continuation')
    _id(target_execution_id); _id(source_execution_id)
    if not isinstance(source_resolution_hash, str) or not re.fullmatch('[0-9a-f]{64}', source_resolution_hash):
        _error('verified Candidate risk resolution is required')
    for value in (candidate_input_sha256, candidate_confirmation_sha256):
        if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
            _error('exact Candidate context and confirmation binding is required')
    with coordinator_lock(store.root.parent), store._locked():
        budget, value = store._load()
        existing = load(store, budget)
        if existing is not None:
            if (existing['target_execution_id'] != target_execution_id
                    or existing['source_execution_id'] != source_execution_id
                    or existing['source_resolution_hash'] != source_resolution_hash
                    or existing['candidate_input_sha256'] != candidate_input_sha256
                    or existing['candidate_confirmation_sha256'] != candidate_confirmation_sha256
                    or existing['source_ledger_sha256'] != expected_ledger_sha256):
                _error('this chapter already has a different continuation permit')
            return copy.deepcopy(existing)
        if value['ledger_sha256'] != expected_ledger_sha256:
            _error('chapter ledger changed before continuation approval')
        if value.get('repair_cycle') or BatchBudgetRegistry(store.root.parent, book_id=store.book_id).for_chapter(store.chapter, value) is not None:
            _error('one-shot continuation does not authorize batch or repair execution')
        report = store._report(budget, value)
        unknown = report['unresolved_attempts']
        if len(unknown) != 1 or unknown[0][0] != source_execution_id:
            _error('continuation requires exactly one original unresolved execution')
        row = next(r for r in value['attempts'] if (r['execution_id'], r['plan']['intent']['attempt_id']) == tuple(unknown[0]))
        if not row['dispatch_claimed'] or row['receipt'] is not None or row['local_rejection']:
            _error('continuation requires an original dispatched call with no receipt')
        if any(r['execution_id'] == target_execution_id for r in value['attempts']):
            _error('continuation target already has an attempt')
        plan = row['plan']; intent = plan['intent']
        grant = dict(schema_version='chapter_continuation_v1', book_id=budget['book_id'],
            chapter=budget['chapter'], root=str(store.root.resolve()), budget_sha256=budget['budget_sha256'],
            source_ledger_sha256=value['ledger_sha256'], source_count=len(value['attempts']),
            source_rows_sha256=_hash(value['attempts']), source_execution_id=source_execution_id,
            source_intent_hash=intent['intent_hash'], source_resolution_hash=source_resolution_hash,
            target_execution_id=target_execution_id,
            candidate_input_sha256=candidate_input_sha256,
            candidate_confirmation_sha256=candidate_confirmation_sha256,
            limits=dict(provider=intent['provider'], model=intent['model'], stage=intent['stage'],
                endpoint_type=plan['endpoint_type'], endpoint_fingerprint=plan['endpoint_fingerprint'],
                max_input_tokens=plan['reserved_input_tokens'], max_output_tokens=plan['reserved_output_tokens']),
            author_confirmation={'confirmed': True, 'warning_version': 'chapter_continuation_v1'},
            created_at=datetime.now(timezone.utc).isoformat())
        grant['grant_sha256'] = _hash(grant)
        atomic_create_json(store.root / FILENAME, grant)
        return copy.deepcopy(grant)


def require_candidate_binding(scope, execution_id, input_sha256, confirmation):
    if scope is None:
        return
    grant = load(scope.store, scope.store.frozen_budget())
    if grant is not None and execution_id == grant['target_execution_id']:
        if (input_sha256 != grant['candidate_input_sha256'] or not isinstance(confirmation, dict)
                or confirmation.get('confirmation_sha256') != grant['candidate_confirmation_sha256']):
            _error('continuation Candidate differs from the approved input or author intent')


def allows(store, budget, value, execution_id, plan):
    """Called under the existing coordinator and ledger locks, before append."""
    from core.chapter_budget_store import _hash
    grant = load(store, budget)
    if grant is None or execution_id != grant['target_execution_id']:
        return False
    if value.get('batch_budget') or value.get('repair_cycle'):
        return False
    # Any reservation consumes the only slot, including one rejected locally.
    if (len(value['attempts']) != grant['source_count']
            or _hash(value['attempts']) != grant['source_rows_sha256']
            or value['ledger_sha256'] != grant['source_ledger_sha256']):
        return False
    limits = grant['limits']; intent = plan['intent']
    if any(intent[name] != limits[name] for name in ('provider', 'model', 'stage')):
        return False
    if any(plan[name] != limits[name] for name in ('endpoint_type', 'endpoint_fingerprint')):
        return False
    return (plan['reserved_input_tokens'] <= limits['max_input_tokens']
            and plan['reserved_output_tokens'] <= limits['max_output_tokens'])


def require_dispatch(store, budget, value, row):
    """Recheck durable authority at the physical dispatch boundary too."""
    from core.chapter_budget_store import _hash
    grant = load(store, budget)
    others = [r for r in value['attempts'] if r is not row
              and store._account(r)['accounting_state'] in {'reserved', 'held_unknown'}]
    if not others and (grant is None or row['execution_id'] != grant['target_execution_id']):
        return
    if grant is None or row['execution_id'] != grant['target_execution_id']:
        _error('unresolved calls require the original continuation permit at dispatch')
    if len(value['attempts']) != grant['source_count'] + 1 or value['attempts'][-1] is not row:
        _error('continuation permit has already been consumed by another attempt')
    prefix = copy.deepcopy(value)
    prefix['attempts'] = prefix['attempts'][:grant['source_count']]
    prefix['ledger_sha256'] = _hash({k:v for k,v in prefix.items() if k != 'ledger_sha256'})
    if not allows(store, budget, prefix, row['execution_id'], row['plan']):
        _error('continuation authority or original source changed before dispatch')
