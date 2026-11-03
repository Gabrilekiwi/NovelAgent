"""Current denial gates also protect unchanged, qualified older policies."""
from contextlib import contextmanager
import json
from pathlib import Path

import pytest

from tests.test_backup_restore_database import logical
from tests.test_restore_job_artifacts_internal import internal_case,make_second_internal_case
from workbench.backup_files import inventory_tree
from workbench.restore_execution_policy import require_restored_execution_paths,RestoredExecutionBlocked
from workbench.restore_handoff import _signed

REPO=Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def legacy_second_case():
    from workbench import restore_activation,restore_executions
    original=restore_executions.execution_policy
    def older_policy(*args,**kwargs):
        policy=original(*args,**kwargs)
        value={k:v for k,v in policy.items() if k!='policy_sha256'}
        # Reproduce the old producer's omission while retaining its actual
        # canonical handoff, SQL, qualification and completed receipts.
        value['blocked_paths']=[name for name in value['blocked_paths']
            if not name.rsplit('/',1)[-1].startswith('job_')]
        return _signed(value,'policy_sha256')
    with pytest.MonkeyPatch.context() as producer:
        producer.setattr(restore_executions,'execution_policy',older_policy)
        producer.setattr(restore_activation,'execution_policy',older_policy)
        with contextmanager(internal_case.__wrapped__)() as first:
            yield make_second_internal_case(first)


def test_qualified_old_second_generation_rejects_internal_history_without_rewriting_policy(legacy_second_case):
    c=legacy_second_case;target=c['target'];path=target/'execution-policy.json'
    policy=json.loads(path.read_text(encoding='utf-8'))
    assert not any(name.endswith('/'+c['job'].id) for name in policy['blocked_paths'])
    before=inventory_tree(target/'book');sql=logical(target/'workbench.sqlite')
    control={p.name:p.read_bytes() for p in target.iterdir() if p.is_file() and p.suffix=='.json'}
    from workbench.book_ownership import BookOwnership
    directory=BookOwnership(c['project'].book_id,machine_store=c['store']).directory
    machine={p.name:p.read_bytes() for p in directory.iterdir() if p.is_file() and p.suffix=='.json'}
    for path in (c['internal_artifact'],c['internal_artifact']/'stdout.json'):
        with pytest.raises(RestoredExecutionBlocked):require_restored_execution_paths(path,require_active=True)
    require_restored_execution_paths(target/'next-jobs/job_new',require_active=True)
    assert inventory_tree(target/'book')==before and logical(target/'workbench.sqlite')==sql
    assert {p.name:p.read_bytes() for p in target.iterdir() if p.is_file() and p.suffix=='.json'}==control
    assert {p.name:p.read_bytes() for p in directory.iterdir() if p.is_file() and p.suffix=='.json'}==machine
