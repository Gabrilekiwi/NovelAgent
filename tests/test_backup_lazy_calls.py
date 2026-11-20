import json
from pathlib import Path

import pytest

from tests import test_execution_provenance as provenance_fixture
from tests.test_backup_archive import archive_case,capture,software_tree
from tests.test_backup_archive_database import private_temporary_directory
from tests.test_backup_archive_history import absent_source
from tests.test_backup_scope import legacy,inspect,codes
from workbench.backup_archive_references import inspect_archived_references


def seed(c, damage):
    root=c['root'];base=root/'.novelagent/runtime/runs/executions/execution_zero'
    base.mkdir(parents=True,exist_ok=True)
    provenance=provenance_fixture.ExecutionProvenanceTest()._build().to_dict()
    path=base/'provenance.json';path.write_text(json.dumps(provenance),encoding='utf-8')
    budget={k:0 for k in ('provider_calls','unsettled_attempt_count','total_input_tokens',
        'total_output_tokens','charged_output_tokens','reserved_output_tokens','estimated_cost')}
    value={'execution_id':'execution_zero','model_calls_ref':'executions/execution_zero/model_calls',
        'provenance_artifact_ref':'executions/execution_zero/provenance.json',
        'provenance_hash':provenance['provenance_hash'],'budget':budget}
    if damage=='positive':budget['provider_calls']=1
    if damage=='unsettled':budget['unsettled_attempt_count']=1
    if damage=='missing_counter':budget.pop('charged_output_tokens')
    if damage=='bool':budget['provider_calls']=False
    if damage=='missing_provenance':path.unlink()
    if damage=='wrong_provenance':value['provenance_hash']='0'*64
    if damage=='invalid_provenance':path.write_text('{}')
    if damage=='foreign_directory':value['model_calls_ref']='executions/execution_other/model_calls'
    record=root/'.novelagent/runtime/runs/zero.json'
    record.write_text(json.dumps({'run':{'id':'zero','status':'running' if damage=='running' else 'failed',
        'execution_evidence':value}}),encoding='utf-8')


@pytest.mark.parametrize('damage',['none','positive','unsettled','missing_counter','bool',
    'missing_provenance','wrong_provenance','invalid_provenance','foreign_directory','running'])
def test_zero_call_directory_has_narrow_verified_semantics(legacy,damage):
    c=legacy;seed(c,damage);result=inspect(c)
    if damage=='none':
        assert result['scope_checks_passed'],result['blockers']
        check=next(x for x in result['checks'] if x['check']=='legacy_lazy_call_directories')
        assert len(check['evidence'])==1
    else:
        assert 'required_directory_missing_or_excluded' in codes(result)


def test_zero_call_semantics_are_rechecked_in_archive_without_original(archive_case,private_temporary_directory):
    c=archive_case;seed(c,'none');package=Path(capture(c)['path'])
    with absent_source(c['root']):
        result=inspect_archived_references(package,project_id=c['project'].id,book_id=c['project'].book_id,
            temporary_root=private_temporary_directory)
    assert result['references_verified'],result['blockers']
    assert len(result['lazy_call_directories'])==1
    assert result['recoverable'] is False
