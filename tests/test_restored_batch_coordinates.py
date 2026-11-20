"""A physical move may change only declared outline coordinates, never content."""
import copy,json
from pathlib import Path

import pytest

from tests.test_reviewed_upgrade import case
from tests.test_reviewed_batches import batch_case
from core.engine.reviewed_batch_control import BatchControlError
from workbench.restored_batch_continuation import restored_outlines


def test_restored_outline_mapping_requires_exact_relative_member_and_entire_blueprint(batch_case):
    c=batch_case;m=c['service'].preview(**c['route'])['manifest']
    # Emulate only the original coordinate fields; the current root/files are
    # real fixture content read by the ordinary blueprint parser.
    old=c['root'].parent/'original-book';moved=copy.deepcopy(m);moved['source_root']=str(old)
    for item in moved['outlines']:
        for field in ('outline_path','source_path'):item['blueprint'][field]=str(old/item['relative_path'])
    passed,mappings=restored_outlines(c['root'],moved)
    assert passed and len(mappings)==4
    assert all(row['before']!=row['after'] and (c['root']/row['relative_path']).is_file() for row in mappings)
    original=json.dumps(moved,sort_keys=True)
    for field in ('text','source_sha256'):
        altered=copy.deepcopy(moved);altered['outlines'][0][field]+='changed'
        assert not restored_outlines(c['root'],altered)[0]
    altered=copy.deepcopy(moved);altered['outlines'][0]['blueprint']['core_event']+='changed'
    assert not restored_outlines(c['root'],altered)[0]
    altered=copy.deepcopy(moved);altered['outlines'][0]['blueprint']['source_path']=str(old/'different.md')
    with pytest.raises(BatchControlError):restored_outlines(c['root'],altered)
    altered=copy.deepcopy(moved);altered['outlines'][0]['blueprint']['outline_path']=str(old.parent/'outside.md')
    with pytest.raises(BatchControlError):restored_outlines(c['root'],altered)
    assert json.dumps(moved,sort_keys=True)==original
