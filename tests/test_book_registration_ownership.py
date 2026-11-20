"""Fence real registry/migration/EA writes before control files are created."""
import json
from pathlib import Path
import uuid

import pytest

from core.engine import root_registry as registry_module
from core.engine.root_registry import RootRegistryService
from core.engine.project_root_remap import remap_story_project_roots
from core.story_project.authority_persistence import (
    event_authority_write_operation, reconcile_event_authority_persistence,
)
from core.story_project.migration import (
    inspect_story_project_runtime_migration, migrate_story_project_runtime,
)
from tests.test_book_persistence_ownership import legacy, suspend, inventory, child_cannot_take_ownership
from workbench import book_ownership as m


def registry_case(c, *, embedded=False):
    runtime=c['root']/'.novelagent/runtime' if embedded else c['base']/'external-runtime'
    runtime.mkdir(parents=True)
    export=c['base']/'export'; export.mkdir()
    roots=dict(story_project=c['root'],runtime=runtime,export=export)
    service=RootRegistryService(runtime/'persistence')
    return service,roots


@pytest.mark.parametrize('entry',['new','existing','remap'])
def test_revoked_registration_cannot_create_arm_or_update_control_files(legacy,entry):
    c=legacy;service,roots=registry_case(c)
    if entry!='new':record=service.ensure(roots)
    if entry=='remap':(c['base']/'new-export').mkdir()
    suspend(c);before=inventory(c)
    with pytest.raises(m.BookOwnershipError):
        if entry=='remap':
            service.remap({'export':c['base']/'new-export'},expected_revision=record['revision'],
                          expected_registry_digest=record['registry_digest'])
        else:service.ensure(roots)
    assert inventory(c)==before
    if entry=='new':assert not service.transaction_root.exists()
    assert (c['base']/'new-export').exists() == (entry=='remap')


def test_existing_registry_still_checks_original_book_when_new_map_omits_it(legacy):
    c=legacy;service,roots=registry_case(c);service.ensure(roots)
    suspend(c);before=inventory(c)
    with pytest.raises(m.BookOwnershipError):service.ensure({'runtime':roots['runtime']})
    assert inventory(c)==before


def test_active_registration_holds_machine_gate_before_lock_file_creation(legacy,monkeypatch):
    c=legacy;service,roots=registry_case(c);seen=[]
    original=registry_module._arm_persistence_lock
    def observe(path):
        child_cannot_take_ownership(c);seen.append(path)
        return original(path)
    monkeypatch.setattr(registry_module,'_arm_persistence_lock',observe)
    record=service.ensure(roots)
    assert seen==[service.transaction_root] and service.load()==record


def test_active_remap_retains_book_ownership_through_registry_replace(legacy,monkeypatch):
    c=legacy;service,roots=registry_case(c);record=service.ensure(roots)
    destination=c['base']/'new-export';destination.mkdir();seen=[]
    original=registry_module._atomic_replace_from_bytes
    def observe(path,content):
        child_cannot_take_ownership(c);seen.append(path)
        return original(path,content)
    monkeypatch.setattr(registry_module,'_atomic_replace_from_bytes',observe)
    updated=service.remap({'export':destination},expected_revision=record['revision'],
                         expected_registry_digest=record['registry_digest'])
    assert seen==[service.registry_path]
    assert updated['roots']['export']['path']==str(destination)
    assert updated['registry_id']==record['registry_id']


def migration_source(c):
    source=c['base']/'old-runtime';(source/'runs').mkdir(parents=True)
    (source/'runs/chapter_1_fixture.json').write_text(json.dumps({'run':{
        'id':'chapter_1_fixture','story_project':{'root':str(c['root']),
        'book_id':c['identity'].book_id}}}),encoding='utf-8')
    (source/'snapshot.json').write_text('{}')
    return source


@pytest.mark.parametrize('lost_identity',[False,True])
def test_migration_checks_book_before_identity_creation_and_staging(legacy,lost_identity):
    c=legacy;source=migration_source(c)
    suspend(c)
    if lost_identity:(c['root']/'.novelagent/project.json').unlink()
    before=inventory(c)
    with pytest.raises(m.BookOwnershipError):
        migrate_story_project_runtime(source_runtime=source,story_project_root=c['root'])
    assert inventory(c)==before and not (c['root']/'.novelagent/runtime').exists()


def test_migration_inspection_remains_readonly_after_revocation(legacy):
    c=legacy;source=migration_source(c);suspend(c);before=inventory(c)
    assert inspect_story_project_runtime_migration(source_runtime=source,story_project_root=c['root'])['ok']
    assert inventory(c)==before


def test_active_migration_keeps_machine_gate_and_original_source(legacy):
    c=legacy;source=migration_source(c)
    before={str(p.relative_to(source)):p.read_bytes() for p in source.rglob('*') if p.is_file()}
    def migration_id():
        child_cannot_take_ownership(c)
        return uuid.uuid4()
    result=migrate_story_project_runtime(source_runtime=source,story_project_root=c['root'],
                                        migration_id_factory=migration_id)
    assert result['ok'] and Path(result['manifest_path']).exists()
    assert {str(p.relative_to(source)):p.read_bytes() for p in source.rglob('*') if p.is_file()}==before


@pytest.mark.parametrize('entry',['operation','reconcile'])
def test_ea_control_plane_rejects_before_creating_global_directories(legacy,entry):
    c=legacy;suspend(c);before=inventory(c)
    with pytest.raises(m.BookOwnershipError):
        if entry=='operation':
            with event_authority_write_operation(c['root'],expected_book_id=c['identity'].book_id,
                                                 writer_kind='chapter'):
                pytest.fail('revoked writer entered EA operation')
        else:reconcile_event_authority_persistence(c['root'],expected_book_id=c['identity'].book_id)
    assert inventory(c)==before and not (c['root']/'.novelagent/runtime').exists()


def test_active_ea_operation_keeps_machine_gate_through_yield(legacy):
    c=legacy
    with event_authority_write_operation(c['root'],expected_book_id=c['identity'].book_id,
                                         writer_kind='chapter'):
        child_cannot_take_ownership(c)


def test_moved_enrolled_book_cannot_use_project_remap_to_reacquire_ownership(legacy):
    c=legacy;service,roots=registry_case(c,embedded=True);record=service.ensure(roots)
    moved=c['base']/'moved-book'
    assert c['root'].is_relative_to(Path.cwd()/'.tmp') and moved.is_relative_to(Path.cwd()/'.tmp')
    c['root'].rename(moved)
    before=inventory(c)
    request={'story_project':{'path':str(moved),'root_uuid':record['roots']['story_project']['root_uuid']}}
    with pytest.raises(m.BookOwnershipError):
        remap_story_project_roots(new_story_project=moved,
            control_plane=moved/'.novelagent/runtime/persistence',requested=request,
            expected_revision=record['revision'],expected_registry_digest=record['registry_digest'])
    assert inventory(c)==before and not (moved/'.novelagent/root-remap').exists()
