"""Completed, same-binding source rollbacks supporting an older archive.

Generation numbers cannot prove ancestry. Each step requires the original
machine handoff, its rollback decision and its post-reactivation receipt.
Reading this evidence never repairs or recreates a missing control file.
"""
import json

from workbench.backup_restore_roots import _read
from workbench.book_ownership import OWNERSHIP_V2, _sha256
from workbench.restore_handoff import (
    RestoreHandoffError, _signed, rollback_decision, validate_handoff_intent,
)


def _control(directory, name):
    try:
        return _read(directory, name)
    except (OSError, ValueError, TypeError) as exc:
        raise RestoreHandoffError('原机器回退证明缺失或无法核验。') from exc


def rollback_receipt(handoff):
    decision = rollback_decision(handoff)
    return _signed({'schema_version': 'machine_restore_rollback_completed_v1',
        'handoff_id': handoff['handoff_id'], 'intent_sha256': handoff['intent_sha256'],
        'decision_sha256': decision['decision_sha256'],
        'record_sha256': decision['record']['record_sha256'],
        'source_reactivated': True, 'target_writable': False}, 'receipt_sha256')


def rollback_receipt_name(record):
    if not _sha256(record.get('record_sha256')):
        raise RestoreHandoffError('回退后的登记摘要无效。')
    return 'rollback-'+record['record_sha256']+'-done.json'


def _same_registration(archived, source, book_id):
    for record in (archived, source):
        if (not isinstance(record, dict) or record.get('version') != OWNERSHIP_V2
                or record.get('state') != 'active' or record.get('binding', {}).get('book_id') != book_id
                or type(record.get('generation')) is not int or record['generation'] < 1
                or record != _signed(record)):
            raise RestoreHandoffError('旧归档复用需要同一原始登记的完整活动记录。')
    unchanged = lambda record: {k: v for k, v in record.items() if k not in {'generation', 'record_sha256'}}
    if unchanged(archived) != unchanged(source) or source['generation'] < archived['generation']:
        raise RestoreHandoffError('旧归档与当前登记的身份、凭证或物理绑定不同。')


def validate_rollback_ancestry(archived, source, chain, book_id, *, depth=0):
    """Validate the complete ordered proof without opening old book roots."""
    try:
        if (depth > 16 or not isinstance(chain, list) or not 1 <= len(chain) <= 16
                or len(json.dumps(chain, ensure_ascii=False).encode('utf-8')) > 32*1024**2):
            raise ValueError('unsupported rollback ancestry')
        _same_registration(archived, source, book_id)
        record = archived
        for item in chain:
            if not isinstance(item, dict) or set(item) != {'handoff_intent', 'rollback_decision', 'rollback_receipt'}:
                raise ValueError('invalid rollback evidence')
            handoff = validate_handoff_intent(item['handoff_intent'], book_id, depth=depth+1)
            decision = rollback_decision(handoff)
            if (handoff['source_record'] != record or item['rollback_decision'] != decision
                    or item['rollback_receipt'] != rollback_receipt(handoff)):
                raise ValueError('rollback evidence belongs to another generation or action')
            _same_registration(record, decision['record'], book_id)
            record = decision['record']
        if record != source:
            raise ValueError('rollback ancestry does not reach the current source')
        return chain
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise RestoreHandoffError('原归档到当前登记的回退链无法核验。') from exc


def verify_rollback_ancestry_files(directory, archived, source, chain, book_id, *, depth=0):
    """Require the original machine files, including nested earlier evidence."""
    validate_rollback_ancestry(archived, source, chain, book_id,depth=depth)
    seen = {}

    def visit(items, depth=0):
        if depth > 16:
            raise RestoreHandoffError('回退证据超过支持代数。')
        for item in items:
            handoff = item['handoff_intent']; identifier = handoff['handoff_id']
            if identifier in seen:
                if seen[identifier] != item:
                    raise RestoreHandoffError('同一回退操作出现不同证明。')
                continue
            if len(seen) >= 256:
                raise RestoreHandoffError('回退证据超过支持数量。')
            seen[identifier] = item
            prefix = 'handoff-'+identifier+'-'
            record = item['rollback_decision']['record']
            if (_control(directory, prefix+'intent.json') != handoff
                    or _control(directory, prefix+'rollback.json') != item['rollback_decision']
                    or _control(directory, rollback_receipt_name(record)) != item['rollback_receipt']):
                raise RestoreHandoffError('回退链与原机器交接、决议或完成凭据不同。')
            visit(handoff.get('source_rollback_ancestry', []), depth+1)
            if handoff.get('schema_version')=='machine_restore_handoff_v5':
                from workbench.restore_registration_upgrade import verify_registration_upgrade_files
                verify_registration_upgrade_files(directory,handoff['archived_source_record'],
                    handoff['source_record'],handoff['source_identity_upgrade'],book_id,depth=depth+1)

    visit(chain,depth)
    return chain


def load_rollback_ancestry(directory, archived, source, book_id):
    """Walk deterministic completion filenames; no directory scan or repair."""
    _same_registration(archived, source, book_id)
    chain = []; current = source
    while current != archived:
        if len(chain) >= 16 or current['generation'] <= archived['generation']:
            raise RestoreHandoffError('回退链不能到达原归档，或超过支持代数。')
        receipt = _control(directory, rollback_receipt_name(current))
        identifier = receipt.get('handoff_id')
        if not _sha256(identifier):
            raise RestoreHandoffError('回退完成凭据的交接标识无效。')
        prefix = 'handoff-'+identifier+'-'
        handoff = _control(directory, prefix+'intent.json')
        decision = _control(directory, prefix+'rollback.json')
        validate_handoff_intent(handoff, book_id)
        if decision != rollback_decision(handoff) or decision['record'] != current:
            raise RestoreHandoffError('回退凭据不属于当前登记，拒绝推断旧资格。')
        chain.append({'handoff_intent': handoff, 'rollback_decision': decision, 'rollback_receipt': receipt})
        current = handoff['source_record']
    chain.reverse()
    return verify_rollback_ancestry_files(directory, archived, source, chain, book_id)
