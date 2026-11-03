"""Adversarial, internally valid snapshots derived from actual call evidence."""
import json
from pathlib import PurePosixPath

import pytest

from core.story_project.reviewed_state_document import digest
from tests.test_restore_budget_preparation import full_source
from workbench.backup_archive_view import verified_archive_view
from workbench.backup_archive_usage import _StoryEvidence, _usage
from workbench.backup_auxiliary import Evidence
from workbench.backup_files import inventory_tree
from workbench.restore_budget_merge import reconcile_usage_evidence
from workbench.restore_handoff import RestoreHandoffError


class MemoryEvidence(_StoryEvidence):
    def __init__(self, root, payload):
        self.payload = payload
        directories = {str(parent) for name in payload for parent in PurePosixPath(name).parents if str(parent) != '.'}
        tree = {'files':{name:{'size':len(raw),'sha256':digest(raw)} for name,raw in payload.items()},
            'directories':sorted(directories)}
        Evidence.__init__(self, root, tree, _read_bytes=self.raw)

    def raw(self, root, name):
        return self.payload[self.relative(root/name)]


def payload(c):
    tree = inventory_tree(c['root'])
    return {name:(c['root']/name).read_bytes() for name in tree['files']}


def replace_document(files, name, value, checksum):
    value[checksum] = digest({key:item for key,item in value.items() if key != checksum})
    files[name] = json.dumps(value, ensure_ascii=False).encode('utf-8')


def test_valid_descendant_selects_latest_ledger_without_adding_archive_total(full_source):
    c = full_source; files = payload(c)
    with verified_archive_view(c['package']) as view:
        result = reconcile_usage_evidence(_StoryEvidence(view), MemoryEvidence(c['root'], files), book_id=c['project'].book_id)
    assert result['usage']['summary']['attempt_count'] == 3
    assert result['usage']['chapter_budgets'][0]['charged_amount'] == '0.000769'
    assert result['usage']['chapter_times'][0]['interval_count'] == 2
    assert not result['settled'] and not result['write_activation_allowed']
    assert c['outside_finance'] not in result['files']


@pytest.mark.parametrize('damage', ['lost_old_settlement','lost_old_attempt','lost_measured_interval'])
def test_higher_or_valid_current_totals_do_not_hide_loss_of_archive_evidence(full_source, damage):
    c = full_source; files = payload(c)
    ledger_name = '.novelagent/runtime/budgets/c3/ledger.json'
    time_name = '.novelagent/runtime/budgets/c3/time.json'
    ledger = json.loads(files[ledger_name]); first = ledger['attempts'][0]
    if damage == 'lost_old_settlement':
        first['receipt'] = None
        replace_document(files, ledger_name, ledger, 'ledger_sha256')
    elif damage == 'lost_old_attempt':
        prefix = '.novelagent/runtime/runs/executions/execution_shared/model_calls/'
        del files[prefix+'intents/call-a1.json']
        del files[prefix+'receipts/call-a1.json']
        del files[prefix+first['receipt']['response_artifact_ref']]
        ledger['attempts'].pop(0)
        replace_document(files, ledger_name, ledger, 'ledger_sha256')
    else:
        clock = json.loads(files[time_name]); clock['intervals'].pop(0)
        replace_document(files, time_name, clock, 'record_sha256')
    evidence = MemoryEvidence(c['root'], files)
    current = _usage(evidence, c['project'].book_id, lambda code, reference:None)
    assert current['summary']['attempt_count'] >= 2
    assert current['chapter_budgets'][0]['charged_amount'] != '0'
    # Its standalone evidence is still valid. The cross-snapshot relationship
    # must independently reject the lost earlier receipt/attempt/interval.
    with verified_archive_view(c['package']) as view, pytest.raises(RestoreHandoffError):
        reconcile_usage_evidence(_StoryEvidence(view), MemoryEvidence(c['root'], files), book_id=c['project'].book_id)
