"""Actual saved planning history must be inventoried, checked, and kept offline."""
from pathlib import Path
import copy
import json
import os

import pytest

from tests.test_backup_archive import archive_case,legacy,software_tree,capture
from tests.test_backup_archive_auxiliary import tmp_path
from tests.test_backup_archive_history import absent_source
from tests.test_autonomy_plans import trusted_profiles,NOW
from core.autonomy.profiles import TrustedProfiles
from core.autonomy.plans import build_source_snapshot,compile_instruction_plan
from core.autonomy.session import AutonomySessionStore
from workbench.backup_auxiliary_history import inspect_archived_auxiliary_history
from workbench.backup_files import inventory_tree


def plan_for(book_id):
    payload=copy.deepcopy(trusted_profiles().payload);payload['story_projects'][0]['book_id']=book_id
    profiles=TrustedProfiles.from_dict(payload)
    source=build_source_snapshot(book_id=book_id,root_uuid=payload['story_projects'][0]['root_uuid'],authority_epoch=2,
        authority_head_event_hash='2'*64,canonical_next_chapter=2,source_digest='1'*64,captured_at=NOW)
    return profiles,compile_instruction_plan('write 2 chapters',trusted_profiles=profiles,source_snapshot=source,created_at=NOW)


def inspect(c,package):
    return inspect_archived_auxiliary_history(package,project_id=c['project'].id,book_id=c['project'].book_id)


@pytest.mark.parametrize('layout',['canonical','custom','uppercase'])
def test_actual_preview_only_store_is_verified_with_source_absent(archive_case,layout,monkeypatch):
    if layout=='uppercase' and os.name!='nt':pytest.skip('case-insensitive Windows directory identity')
    c=archive_case;profiles,plan=plan_for(c['project'].book_id)
    root=c['root']/('.novelagent/runtime/autonomy' if layout=='canonical' else 'custom-preview')
    if layout=='uppercase':(root/'PLANS').mkdir(parents=True)
    store=AutonomySessionStore(root,trusted_profiles=profiles,reconcile_on_open=False)
    store.save_preview(plan)
    package=Path(capture(c)['path']);before=inventory_tree(package)
    def forbidden(*args,**kwargs):pytest.fail('offline history opened a live autonomy store')
    monkeypatch.setattr(AutonomySessionStore,'__init__',forbidden)
    with absent_source(c['root']):first=inspect(c,package);second=inspect(c,package)
    assert first==second and first['complete_history_certified'],first['blockers']
    assert first['histories'][0]['plans'][0]['plan_hash']==plan['plan_hash']
    assert not first['recoverable'] and not first['write_activation_allowed']
    assert inventory_tree(package)==before


@pytest.mark.parametrize('damage',['bad_json','other_book','wrong_filename','unknown_sibling'])
def test_plan_history_damage_is_rejected_without_repair(archive_case,damage):
    c=archive_case;profiles,plan=plan_for('another-book' if damage=='other_book' else c['project'].book_id)
    store=AutonomySessionStore(c['root']/'custom-preview',trusted_profiles=profiles,reconcile_on_open=False)
    path=store.save_preview(plan)
    if damage=='bad_json':path.write_text('{broken')
    elif damage=='wrong_filename':path.rename(path.parent/('plan_'+'0'*24+'-'+'0'*16+'.json'))
    elif damage=='unknown_sibling':(store.root/'unknown-control.json').write_text('{}')
    package=Path(capture(c)['path']);before=inventory_tree(c['root'])
    with absent_source(c['root']):result=inspect(c,package)
    assert not result['complete_history_certified'] and result['blockers']
    assert inventory_tree(c['root'])==before


def test_author_plans_are_not_discovered_from_directory_name_alone(archive_case):
    c=archive_case;folder=c['root']/'author-notes/plans';folder.mkdir(parents=True)
    (folder/'chapter-ideas.json').write_text(json.dumps({'ideas':['author notes']}))
    package=Path(capture(c)['path']);result=inspect(c,package)
    assert result['complete_history_certified'] and result['histories']==[]
