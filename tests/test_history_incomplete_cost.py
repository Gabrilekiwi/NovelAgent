from __future__ import annotations

import json

import pytest

from workbench.history_stats import annotate_run_usage, build_history_stats, load_compact_runs


def test_unsettled_call_cannot_make_partial_cost_look_complete():
    run = dict(provider_calls=2, unsettled_attempt_count=1,
               total_input_tokens=100, total_output_tokens=10, estimated_cost=1.25)
    row = annotate_run_usage(run)
    assert row['cost_label'] == '部分已计价 1.25；1 次待结算'
    stats = build_history_stats(runs=[run])
    assert stats['cost_priced'] is False
    assert stats['cost_label'] == '部分已计价 1.25'
    assert '不是总费用' in stats['token_label']
    assert '1 次待结算' in stats['token_label']


def test_failed_call_without_usage_is_unknown_not_absent():
    run = dict(provider_calls=1, total_input_tokens=0, total_output_tokens=0, estimated_cost=0)
    assert '费用未知' in annotate_run_usage(run)['cost_label']
    stats = build_history_stats(runs=[run])
    assert stats['cost_label'] == '费用未知'
    assert stats['estimated_cost'] is None
    assert stats['cost_priced'] is False


def test_one_missing_usage_run_prevents_complete_total():
    stats = build_history_stats(runs=[dict(estimated_cost=2.5), dict(provider_calls=1)])
    assert stats['cost_label'] == '部分已计价 2.50'
    assert stats['cost_priced'] is False
    assert '不是总费用' in stats['token_label']


def test_zero_activity_does_not_invent_a_call():
    assert annotate_run_usage({})['cost_label'] == '—'
    assert build_history_stats(runs=[{}])['cost_label'] == '—'


@pytest.mark.parametrize('cost', [float('nan'), float('inf'), float('-inf')])
def test_nonfinite_legacy_cost_does_not_break_json_response(cost):
    stats = build_history_stats(runs=[dict(provider_calls=1, estimated_cost=cost)])
    json.dumps(stats, allow_nan=False)
    assert stats['estimated_cost'] is None
    assert stats['cost_label'] == '费用未知'


def test_disappearing_history_record_does_not_break_entire_list(tmp_path):
    runs = tmp_path / '.novelagent/runtime/runs'
    runs.mkdir(parents=True)
    path = runs / 'chapter_1_deleted.json'
    path.write_text('{}', encoding='utf-8')

    def compact(payload):
        path.unlink()
        return {'id': 'removed'}

    assert load_compact_runs(tmp_path, compact) == []


def test_invalid_infinite_token_value_does_not_break_history():
    run = dict(provider_calls=1, total_input_tokens=float('inf'))
    assert annotate_run_usage(run)['token_label'] == '—'
    assert build_history_stats(runs=[run])['cost_label'] == '费用未知'
