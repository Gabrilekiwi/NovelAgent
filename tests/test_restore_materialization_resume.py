"""Explicit continuation of real process-death copies, with immutable archive."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_backup_archive_database import private_temporary_directory
from tests.test_backup_archive_executions import execution_archive
from tests.test_backup_restore_materialization import prepare,new_target,assert_original_bytes,REPO
from tests.test_backup_archive import capture,software_tree
from workbench.backup_files import inventory_tree
from workbench.backup_restore_materialization import materialize_archived_restore,RestoreMaterializationError


def interrupted(c,scratch,boundary):
    package=Path(capture(c)['path']);target=new_target();plan=prepare(c,package,target,scratch)
    saved=c['root'].parent/'resume-plan.json';saved.write_text(json.dumps(plan),encoding='utf-8')
    script='''import json,os,sys
from pathlib import Path
from workbench.backup_archive_view import ArchiveView
from workbench.backup_restore_materialization import materialize_archived_restore
plan=json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
original=ArchiveView.copy_member
def copy(self,name,output):
    target=Path(plan['root_plan']['container'])
    selected=(name=='workbench.sqlite' and Path(output.name)==Path(plan['root_plan']['database'])) if sys.argv[4]!='book_file' else (name.startswith('roots/') and Path(output.name).is_relative_to(target/'book'))
    if selected:
        raw=self.read_member(name)
        output.write(raw[:32] if sys.argv[4]!='complete_database' else raw)
        output.flush();os.fsync(output.fileno());os._exit(94)
    return original(self,name,output)
ArchiveView.copy_member=copy
materialize_archived_restore(sys.argv[1],plan=plan,temporary_root=sys.argv[3])
'''
    result=subprocess.run([sys.executable,'-B','-c',script,str(package),str(saved),str(scratch),boundary],
        cwd=REPO,capture_output=True,text=True,timeout=180)
    assert result.returncode==94,result.stderr
    return package,target,plan,saved


@pytest.mark.parametrize('boundary',['partial_database','complete_database','book_file'])
def test_actual_process_death_resumes_same_directory_in_new_process(execution_archive,private_temporary_directory,boundary):
    c=execution_archive;scratch=private_temporary_directory
    package,target,plan,saved=interrupted(c,scratch,boundary)
    before=inventory_tree(package)
    script='''import json,sys
from pathlib import Path
from workbench.backup_restore_materialization import materialize_archived_restore
plan=json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
result=materialize_archived_restore(sys.argv[1],plan=plan,temporary_root=sys.argv[3],resume=True)
print(json.dumps(result))
'''
    resumed=subprocess.run([sys.executable,'-B','-c',script,str(package),str(saved),str(scratch)],
        cwd=REPO,capture_output=True,text=True,timeout=180)
    assert resumed.returncode==0,resumed.stderr
    result=json.loads(resumed.stdout)
    assert_original_bytes(package,plan,result)
    complete=inventory_tree(target)
    assert materialize_archived_restore(package,plan=plan,temporary_root=scratch,resume=True)==result
    assert inventory_tree(target)==complete and inventory_tree(package)==before


@pytest.mark.parametrize('damage',['prefix','unexpected','lost_intent','replaced_container'])
def test_damaged_partial_copy_is_retained_and_never_resumed(execution_archive,private_temporary_directory,damage):
    package,target,plan,_=interrupted(execution_archive,private_temporary_directory,'partial_database')
    if damage=='prefix':(target/'workbench.sqlite').write_bytes(b'wrong prefix')
    elif damage=='unexpected':(target/'author-content.txt').write_text('retain me',encoding='utf-8')
    elif damage=='lost_intent':(target/'restore-intent.json').unlink()
    else:
        moved=target.with_name(target.name+'-original')
        assert target.resolve().is_relative_to(REPO/'.tmp') and moved.resolve().is_relative_to(REPO/'.tmp')
        target.rename(moved);target.mkdir()
        for name in ('restore-intent.json','restore-plan.json','workbench.sqlite'):
            (target/name).write_bytes((moved/name).read_bytes())
    before=inventory_tree(target)
    with pytest.raises((OSError,ValueError,RuntimeError)):
        materialize_archived_restore(package,plan=plan,temporary_root=private_temporary_directory,resume=True)
    assert inventory_tree(target)==before and not (target/'restore-materialization.json').exists()
