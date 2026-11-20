"""Versioned automatic-admission advice over server-produced local quality.

This is a projection, not a write or repair permit. Manual publication keeps
its existing author/quality/transaction gates. Automatic callers must still
freeze policy, consume their durable repair cycle and reserve chapter budget.
"""
import copy
import hashlib
import json

from core.memory_v2.canonical import canonical_json_hash
from core.schema import validate_schema
from core.engine.chapter_commit_prepare import PREPARATION_VERSION
from core.quality.prose_evidence import validate_prose_excerpt,review_short_prose_repetition
from core.quality.reviewed_policy_versions import ACTION_POLICY_V1,validate_action_policy_snapshot


class ReviewedDispositionError(ValueError):
    code = 'reviewed_quality_disposition_invalid'


_REPETITION = frozenset({'duplicate_scene_text', 'near_duplicate_scene_text',
    'duplicate_opening', 'duplicate_ending_hook'})
_RELATED = _REPETITION | {'repetition_or_stalling'}
_ADVICE = frozenset({'chapter_length_reasonable'})
_POLICY = copy.deepcopy(ACTION_POLICY_V1)


def _hash(value):
    return canonical_json_hash(value, exclude_environment_fields=False)


def reviewed_quality_action_policy():
    """Freeze the supported automatic-admission policy without exposing globals."""
    return copy.deepcopy(_POLICY)


def _text_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _witnesses(report, text):
    """Read actual producer ranges; summaries and messages are never quotes."""
    by_code, invalid = {}, set()
    for finding in report['findings']:
        code = finding['code']
        if code not in _REPETITION:
            continue
        locations = finding['locations']
        if not 2 <= len(locations) <= 100:
            invalid.add(code)
            continue
        spans = []
        for location in locations:
            start, end = location.get('start_char'), location.get('end_char')
            if (type(start) is not int or type(end) is not int
                    or not 0 <= start < end <= len(text)):
                invalid.add(code)
                break
            # Display a bounded exact excerpt, with the complete source range
            # retained separately. Never present a generated summary as prose.
            excerpt_end = min(end, start + 512)
            spans.append({'start_char': start, 'end_char': excerpt_end,
                'source_end_char': end, 'quote': text[start:excerpt_end]})
        else:
            if len({(p['start_char'], p['source_end_char']) for p in spans}) < 2:
                invalid.add(code)
            else:
                by_code.setdefault(code, []).extend(spans)
    for code in invalid:
        by_code.pop(code, None)
    return by_code


def project_reviewed_quality_disposition(*, preparation, quality, policy_snapshot=None):
    """Classify actual evidence without changing historical quality reports.

    Call only after server-owned preparation/context validation. Checksums bind
    these observations; they do not make caller-supplied quality trustworthy.
    """
    try:
        policy=validate_action_policy_snapshot(_POLICY if policy_snapshot is None else policy_snapshot)
        text = preparation['final_prose']
        if (not isinstance(text, str)
                or preparation['preparation_version'] != PREPARATION_VERSION
                or preparation['stage'] != 'facts_prepared' or preparation['publication_authorized'] is not False
                or preparation['preparation_sha256'] != _hash({k:v for k,v in preparation.items() if k!='preparation_sha256'})
                or quality['quality_sha256'] != _hash({k:v for k,v in quality.items() if k!='quality_sha256'})
                or quality['stage'] != 'candidate_quality_checked'
                or quality['quality_preparation_version'] != 'reviewed_candidate_quality_v1'
                or quality['publication_authorized'] is not False
                or quality['binding']['chapter_preparation_sha256'] != preparation['preparation_sha256']
                or quality['binding']['final_prose_sha256'] != _text_hash(text)
                or any(quality['binding'].get(k)!=v for k,v in preparation['binding'].items())):
            raise ReviewedDispositionError('质量处置记录与本次最终正文或准备记录不同。')
        decision = validate_schema(quality['local_policy_decision'], 'quality_decision.schema.json')
        integrity = validate_schema(quality['final_artifact_integrity'], 'final_artifact_integrity.schema.json')
        if integrity['artifact_sha256'] != _text_hash(text):
            raise ReviewedDispositionError('原文证据不属于本次最终正文。')
        if len(decision['findings'])>1000 or len(integrity['findings'])>1000:
            raise ReviewedDispositionError('质量问题数量超过本次处置支持范围，请保留完整报告。')
        witnesses = _witnesses(integrity, text)
        gates = []
        if quality['quality_coverage_complete'] is not True or quality['pending_gates']:
            gates.append('quality_coverage_incomplete')
        if quality['authoritative_state_validation']['accepted'] is not True:
            gates.append('authoritative_state_not_accepted')
        if quality['review_gate']['status'] == 'error':
            gates.append('review_producer_failed')
        admission=quality.get('admission_review')
        if admission is None:
            gates.append('admission_review_not_executed')
        else:
            expected_admission=review_short_prose_repetition(text)
            if admission!=expected_admission or _hash(admission)!=_hash(expected_admission):
                raise ReviewedDispositionError('短正文风险检查与本次原文不同。')
            if admission['scope_complete'] is not True:
                gates.append('short_prose_review_incomplete')
        rows = []
        for finding in decision['findings']:
            codes = set(finding['codes'])
            quotes = []
            matching = codes & _REPETITION
            if codes == {'repetition_or_stalling'}:
                matching = set(witnesses)
            for code in sorted(matching):
                for span in witnesses.get(code, []):
                    if span not in quotes:
                        quotes.append(span)
            invalid_declared=False
            for evidence in finding['evidence']:
                if evidence.get('kind')!='prose_excerpt':continue
                try:
                    span=validate_prose_excerpt(json.loads(evidence['value']),text)
                    span={key:span[key] for key in ('start_char','end_char','quote')}
                    span['source_end_char']=span['end_char']
                    if span not in quotes:quotes.append(span)
                except (TypeError,ValueError,KeyError):invalid_declared=True
            producers = {p['producer'] for p in finding['producer_evidence']}
            if invalid_declared:
                disposition, reason = 'pause', 'declared_prose_evidence_invalid'
                quotes=[]
            elif (codes <= _ADVICE and finding['severity'] in {'info', 'warning'}
                    and producers <= {'deterministic_review'}):
                disposition, reason = 'advice', 'nonbinding_length_preference'
            elif (codes <= _RELATED and matching and quotes and all(code in witnesses for code in matching)
                    and producers <= set(policy['repair_producers'])):
                disposition, reason = 'repair_once', 'listed_repetition_with_exact_prose_ranges'
            else:
                disposition, reason = 'pause', 'unlisted_or_unverified_semantic_finding'
            messages = [e['value'] for e in finding['evidence']
                if e.get('kind')=='message' and isinstance(e.get('value'),str)]
            rows.append({'finding_id': finding['id'], 'codes': sorted(codes),
                'severity': finding['severity'], 'disposition': disposition, 'reason': reason,
                'message': messages[0] if messages else '请核对本项原始检查记录。',
                'quotes': quotes[:6], 'quote_status': 'verified_final_prose' if quotes else 'not_provided',
                'producer_evidence': copy.deepcopy(finding['producer_evidence'])})
        if admission is not None:
            for pair in admission['pairs']:
                spans=[{key:pair[side][key] for key in ('start_char','end_char','quote')} for side in ('left','right')]
                for span in spans:span['source_end_char']=span['end_char']
                rows.append({'finding_id':'reviewed-risk:'+_hash(pair),'codes':['short_near_prose_repetition_risk'],
                    'severity':'warning','disposition':'pause','reason':'short_similarity_requires_human_review',
                    'message':'两处短正文高度相似，请核对是否重复描述同一进展。相似度不能代替事实判断。',
                    'quotes':spans,'quote_status':'verified_final_prose',
                    'producer_evidence':[{'producer':'reviewed_short_prose_repetition','similarity':pair['similarity']}]})
        manual_passed = (quality['quality_coverage_complete'] is True and not quality['pending_gates']
            and quality['local_quality_passed'] is True and decision['accepted'] is True
            and integrity['accepted'] is True
            and quality['authoritative_state_validation']['accepted'] is True
            and quality['review_gate']['status']=='pass')
        if gates or any(row['disposition']=='pause' for row in rows):
            action = 'pause'
        elif any(row['disposition']=='repair_once' for row in rows):
            action = 'repair_once'
        elif manual_passed:
            action = 'continue'
        else:
            action = 'pause'
            gates.append('unexplained_local_gate_failure')
        result = {'schema_version': 'reviewed_quality_disposition_v1',
            'policy': copy.deepcopy(policy), 'policy_sha256': _hash(policy),
            'binding': {'book_id': preparation['binding']['book_id'],
                'chapter': preparation['binding']['chapter'],
                'final_prose_sha256': _text_hash(text),
                'preparation_sha256': preparation['preparation_sha256'],
                'quality_sha256': quality['quality_sha256']},
            'automatic_action': action, 'manual_quality_check_passed': manual_passed,
            'pause_reasons': gates, 'findings': rows,
            'repair_authorized': False, 'automatic_acceptance_authorized': False,
            'publication_authorized': False}
        result['disposition_sha256'] = _hash(result)
        return result
    except ReviewedDispositionError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ReviewedDispositionError('本次质量报告未能形成完整的处置与原文证据。') from exc


def validate_reviewed_quality_disposition(value, *, preparation, quality):
    expected=project_reviewed_quality_disposition(preparation=preparation,quality=quality)
    if value!=expected or _hash(value)!=_hash(expected):
        raise ReviewedDispositionError('处置策略、正文证据或原质量绑定发生变化。')
    return expected
