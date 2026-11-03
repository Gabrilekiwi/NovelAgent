import os
from pathlib import Path

import pytest

from workbench.backup_files import inventory_tree,copy_inventory,BackupFilesError
from workbench.instance import InstanceLease
from workbench.maintenance import maintenance_for,MaintenanceBlocked


@pytest.fixture
def source(tmp_path):
    root=tmp_path/'作品 中文';root.mkdir()
    (root/'正文').mkdir();(root/'空目录').mkdir()
    (root/'正文/第2章.md').write_text('已确认的中文正文。',encoding='utf-8')
    (root/'.novelagent').mkdir();(root/'.novelagent/receipt.json').write_text('{"proof":"saved"}',encoding='utf-8')
    (root/'.env').write_text('FAKE_API_KEY=synthetic-not-real',encoding='utf-8')
    (root/'credentials.json').write_text('{"token":"synthetic"}',encoding='utf-8')
    lease=InstanceLease(tmp_path/'data').acquire();gate=maintenance_for(lease)
    op=gate.begin()['operation_id']
    try:yield root,gate,op,tmp_path/'备份 副本'
    finally:lease.close()


def test_inventory_and_copy_preserve_content_empty_dirs_and_exclude_credentials(source):
    root,gate,op,destination=source
    inventory=inventory_tree(root)
    assert len(inventory['files'])==2 and len(inventory['excluded'])==2
    with gate.capture(op) as permit:
        report=copy_inventory(root,destination,inventory,snapshot_permit=permit)
    assert report['inventory_sha256']==inventory['inventory_sha256']
    assert (destination/'空目录').is_dir()
    assert (destination/'正文/第2章.md').read_bytes()==(root/'正文/第2章.md').read_bytes()
    assert not (destination/'.env').exists() and not (destination/'credentials.json').exists()
    assert inventory_tree(root)==inventory


@pytest.mark.parametrize('change',['content','added','removed'])
def test_changed_source_preview_is_rejected_without_creating_target(source,change):
    root,gate,op,destination=source;inventory=inventory_tree(root)
    if change=='content':(root/'正文/第2章.md').write_text('后续修改',encoding='utf-8')
    elif change=='added':(root/'new.txt').write_text('later',encoding='utf-8')
    else:(root/'正文/第2章.md').unlink()
    with gate.capture(op) as permit,pytest.raises(BackupFilesError,match='预览'):
        copy_inventory(root,destination,inventory,snapshot_permit=permit)
    assert not destination.exists()


def test_change_during_copy_cannot_return_a_valid_copy_receipt(source):
    root,gate,op,destination=source;inventory=inventory_tree(root)
    def change(relative):
        with pytest.raises(MaintenanceBlocked):gate.finish(op)
        if relative.endswith('第2章.md'):(root/'正文/第2章.md').write_text('changed after copy',encoding='utf-8')
    with gate.capture(op) as permit,pytest.raises(BackupFilesError,match='复制期间'):
        copy_inventory(root,destination,inventory,snapshot_permit=permit,progress=change)
    assert destination.exists() and gate.status()['state']=='quiescent'


def test_hardlinked_member_is_rejected_without_reading_linked_content(source):
    root,_,_,_=source
    os.link(root/'正文/第2章.md',root/'linked.md')
    with pytest.raises(BackupFilesError,match='链接'):inventory_tree(root)


def test_size_and_file_limits_do_not_emit_partial_inventory(source):
    root,_,_,_=source
    for limits in ({'max_files':1},{'max_bytes':1}):
        with pytest.raises(BackupFilesError,match='截断'):inventory_tree(root,**limits)


def test_existing_or_overlapping_destination_is_never_overwritten(source):
    root,gate,op,destination=source;inventory=inventory_tree(root)
    destination.mkdir();(destination/'kept').write_text('original',encoding='utf-8')
    with gate.capture(op) as permit:
        for target in (destination,root/'backup',root.parent):
            with pytest.raises(BackupFilesError):copy_inventory(root,target,inventory,snapshot_permit=permit)
    assert (destination/'kept').read_text()=='original'


def test_target_directory_replacement_cannot_receive_a_success_receipt(source):
    root,gate,op,destination=source;inventory=inventory_tree(root);changed=[]
    def replace(relative):
        if changed:return
        # Both explicit paths remain inside this isolated test directory.
        moved=destination.with_name('原暂存目录')
        assert destination.parent==moved.parent==root.parent
        destination.rename(moved)
        destination.mkdir();changed.append(True)
    with gate.capture(op) as permit,pytest.raises(BackupFilesError,match='被替换'):
        copy_inventory(root,destination,inventory,snapshot_permit=permit,progress=replace)
    assert changed and inventory_tree(root)==inventory
