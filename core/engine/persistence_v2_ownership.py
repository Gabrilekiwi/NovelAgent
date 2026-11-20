"""Machine ownership for v2 mutations, including external recovery journals."""
from contextlib import ExitStack, contextmanager
import hashlib
import os
from pathlib import Path
import stat

from core.engine.safe_paths import FILE_ATTRIBUTE_REPARSE_POINT, assert_safe_local_tree


def _regular_bytes(path):
    assert_safe_local_tree(path.parent)
    info=path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, 'st_file_attributes', 0) & FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError('unsafe persistence ownership source')
    return path.read_bytes()


def _manifest_paths(manifest, registry=None):
    from core.engine.persistence_v2 import _root_map_from_manifest
    from core.path_refs import validate_path_ref
    roots=_root_map_from_manifest(manifest)
    paths=list(roots.values())
    if registry is not None and manifest['schema_version']=='2.1':
        roots={key:Path(value['path']) for key,value in registry['roots'].items()}
        paths.extend(roots.values())
    immutable=manifest['immutable']
    refs=[item['path_ref'] for item in immutable['targets']]
    refs.append(immutable['publication_receipt']['path_ref'])
    for value in refs:
        ref=validate_path_ref(value)
        paths.append(roots[ref.root_id]/ref.relative_path)
    return paths


def _registry(root):
    from core.engine.root_registry import validate_root_registry
    import json
    path=root/'root_registry.json'
    if not os.path.lexists(path):return None, None
    raw=_regular_bytes(path)
    return validate_root_registry(json.loads(raw)), hashlib.sha256(raw).hexdigest()


def prepare_write_scope(root, root_map, book_id, targets, refs, *, run_id=None):
    from workbench.book_ownership import book_output_write_lock
    from core.path_refs import validate_path_ref
    paths=[root, *root_map.values()]
    if run_id is not None: paths += [Path(root)/name/run_id for name in ('journals','staging')]
    for value in [*(target.path_ref for target in targets), *refs]:
        ref=validate_path_ref(value)
        paths.append(root_map[ref.root_id]/ref.relative_path)
    return book_output_write_lock(*paths, book_id=book_id)


@contextmanager
def manifest_write_scope(root, manifest, *, fault_injector=None):
    from workbench.registered_identity_upgrade import registered_upgrade_scope
    with registered_upgrade_scope(root, manifest, fault_injector=fault_injector) as handled:
        if handled:
            yield
            return
        with _ordinary_manifest_write_scope(root, manifest):
            yield


@contextmanager
def _ordinary_manifest_write_scope(root, manifest):
    from workbench.book_ownership import book_output_write_lock
    registry, _ = _registry(root)
    journals = [Path(root)/name/manifest['immutable']['run_id'] for name in ('journals','staging')]
    with book_output_write_lock(root, *journals, *_manifest_paths(manifest, registry),
                                book_id=manifest['immutable']['book_id']):
        yield


def recovery_facts(root):
    """Read only: gather every journal association before discovery can write.

    A valid current registry identifies incomplete/corrupt staged preparations.
    Old 2.0 journals without a registry carry their own immutable root map.
    Unknown external staging without either source cannot authorize cleanup.
    """
    from core.engine.persistence_v2 import validate_persistence_manifest_v2, PersistenceV2IntegrityError
    import json
    registry, registry_hash=_registry(root)
    from workbench.restore_qualification import restored_recovery_history
    retained_history = restored_recovery_history(root)
    paths=[root]
    if registry is not None:
        paths.extend(Path(item['path']) for item in registry['roots'].values())
    identities=set(); files={}; children={}
    from core.schema import validate_schema
    for state in ('pending','completed','rolled_back','recovery_required'):
        directory=root/'registry'/state
        assert_safe_local_tree(directory)
        entries=sorted(directory.glob('*.json')) if directory.exists() else []
        children['registry/'+state]=[p.name for p in entries]
        for path in entries:
            raw=_regular_bytes(path)
            files[str(path.relative_to(root))]=hashlib.sha256(raw).hexdigest()
            try:
                entry=validate_schema(json.loads(raw),'persistence_registry_entry.schema.json')
            except (ValueError, TypeError, KeyError, RuntimeError):
                continue
            identities.add(entry['book_id'])
    for name in ('staging','journals'):
        directory=root/name
        assert_safe_local_tree(directory)
        entries=sorted(directory.iterdir()) if directory.exists() else []
        children[name]=[p.name for p in entries]
        for entry in entries:
            if not entry.is_dir():continue
            assert_safe_local_tree(entry)
            path=entry/'manifest.json'
            if not os.path.lexists(path):continue
            raw=_regular_bytes(path)
            files[str(path.relative_to(root))]=hashlib.sha256(raw).hexdigest()
            try:
                manifest=validate_persistence_manifest_v2(json.loads(raw))
            except (ValueError, TypeError, KeyError, RuntimeError):
                continue  # The actual registry still fences damaged journals.
            identities.add(manifest['immutable']['book_id'])
            # Frozen restored terminal journals are history. Their original
            # physical roots may still exist, but are not this recovery's
            # write targets. Exact journal/registration bytes were checked above.
            if name != 'journals' or entry.name not in retained_history:
                paths.extend(_manifest_paths(manifest, registry))
    if registry is None and not identities and any(children.values()):
        raise PersistenceV2IntegrityError('recovery has no verifiable project association; refusing journal mutations')
    return dict(registry_sha256=registry_hash,files=files,children=children,
                paths=sorted({str(p) for p in paths}),book_ids=sorted(identities))


@contextmanager
def recovery_write_scope(root, *, expected_book_id=None):
    from workbench.book_ownership import book_output_write_lock
    from core.engine.persistence_v2 import PersistenceV2IntegrityError
    facts=recovery_facts(root)
    identities=set(facts['book_ids'])
    if expected_book_id is not None:identities.add(expected_book_id)
    with ExitStack() as stack:
        # Acquire actual containing books together in the established order.
        stack.enter_context(book_output_write_lock(*facts['paths']))
        for identity in sorted(identities):
            stack.enter_context(book_output_write_lock(*facts['paths'],book_id=identity))
        if recovery_facts(root)!=facts:
            raise PersistenceV2IntegrityError('recovery association changed while acquiring machine ownership')
        yield facts
