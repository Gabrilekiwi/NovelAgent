"""Machine binding for an explicit, already prepared legacy format upgrade.

This is internal transaction machinery, not onboarding permission. Original
registered legacy books retain their witness. A durable fence prevents normal
writers and automatic recovery from using a partially changed identity. Only
the explicit continuation below may finish that exact original transaction.
"""
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3

from core.engine.persistence import atomic_create_json, atomic_write_json, persistence_run_lock
from core.story_project.reviewed_identity import UPGRADE_TRANSITION
from core.story_project.reviewed_state_document import digest, read_project_bytes
from workbench.backup_restore_roots import _read


PROTOCOL = 'registered_identity_upgrade_v1'
RESTORED_PROTOCOL = 'registered_restored_identity_upgrade_v1'


def _signed(value, key):
    result = {k:v for k,v in value.items() if k != key}
    return {**result, key:digest(result)}


def is_identity_upgrade(manifest):
    return any(d.get('transition') == UPGRADE_TRANSITION for d in
        manifest['immutable'].get('read_set_declared_writes', []))


def _details(transaction_root, manifest):
    from core.engine.persistence_v2 import _root_map_from_manifest
    from workbench.book_ownership import BookOwnershipError
    roots = _root_map_from_manifest(manifest)
    book = roots.get('story_project')
    if (book is None or roots != {'story_project':book,'runtime':book/'.novelagent/runtime'}
            or Path(transaction_root) != book/'.novelagent/runtime/reviewed/p'):
        raise BookOwnershipError('identity upgrade has a different physical root layout')
    targets = [t for t in manifest['immutable']['targets'] if t['phase']=='apply']
    identities = [t for t in targets if t['target_id']=='reviewed_identity']
    if (len(targets) != 2 or {t['target_id'] for t in targets} != {'reviewed_identity','reviewed_state'}
            or len(identities) != 1 or identities[0]['path_ref']['root_id'] != 'story_project'
            or identities[0]['path_ref']['relative_path'] != '.novelagent/project.json'
            or not identities[0]['before_exists']):
        raise BookOwnershipError('identity upgrade targets cannot be verified')
    return book, roots, identities[0]


def _name(identity_hash, suffix):
    from workbench.book_ownership import _sha256, BookOwnershipError
    if not _sha256(identity_hash):
        raise BookOwnershipError('identity upgrade hash is invalid')
    # Existing Windows machine-store parents can already be long. The full
    # digest is checked inside the record; a filename collision fails closed.
    codes = {'intent':'i','done':'d'}
    return 'iu-'+identity_hash[:16]+'-'+codes[suffix]+'.json'


def _binding_now(record):
    """Read the exact registered SQL row without initialization or repair."""
    from workbench import book_ownership as b
    from workbench.writer_identity import validate_writer_identity
    binding = record['binding']; root = Path(binding['root']); database = Path(binding['database'])
    raw = read_project_bytes(root, '.novelagent/project.json')
    identity = validate_writer_identity(json.loads(raw))
    if identity.book_id != binding['book_id']:
        raise b.BookOwnershipError('identity upgrade book changed')
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)) as connection:
        connection.execute('BEGIN')
        row = connection.execute('SELECT book_id, root FROM projects WHERE id=?',
            (binding['project_id'],)).fetchone()
    if row != (binding['book_id'], str(root)):
        raise b.BookOwnershipError('identity upgrade SQL registration changed')
    return {**binding, 'root_lineage':b._lineage(root),
            'database_parent_lineage':b._lineage(database.parent),
            'database_file_identity':b._file_identity(database), 'identity_sha256':digest(raw)}


def validate_identity_upgrade_intent(intent, identity_hash, *, depth=0):
    from workbench.book_ownership import BookOwnershipError, OWNERSHIP_V2, OWNERSHIP_V3
    try:
        restored = intent.get('schema_version') == RESTORED_PROTOCOL
        if (set(intent) != {'schema_version','source_record','pending_record','active_record',
                           'transaction_root','immutable','intent_sha256'} | (
                               {'source_restore_provenance'} if restored else set())
                or intent['schema_version'] not in {PROTOCOL,RESTORED_PROTOCOL}
                or depth > 16 or len(json.dumps(intent,ensure_ascii=False).encode('utf-8')) > 32*1024**2
                or intent != _signed(intent,'intent_sha256')):
            raise ValueError('intent fields or digest changed')
        source = intent['source_record']; target = intent['active_record']
        if (source['version'] != (OWNERSHIP_V3 if restored else OWNERSHIP_V2) or source['state'] != 'active'
                or type(source['generation']) is not int or source['generation'] < 1
                or source != _signed(source,'record_sha256')
                or intent['pending_record'] != _signed({**source,'state':'identity_upgrading'},'record_sha256')
                or target != _signed({**source, 'generation':source['generation']+1,
                    'binding':{**source['binding'],'identity_sha256':identity_hash}},'record_sha256')):
            raise ValueError('intent ownership relationship changed')
        if (not isinstance(intent['immutable'],dict)
                or intent['transaction_root'] != str(Path(source['binding']['root'])/'.novelagent/runtime/reviewed/p')):
            raise ValueError('immutable transaction missing')
        if restored:
            from workbench.backup_restore_provenance import validate_restore_provenance
            validate_restore_provenance(intent['source_restore_provenance'],
                root=Path(source['binding']['root']),book_id=source['binding']['book_id'],
                ownership=source,depth=depth+1)
        return intent
    except (OSError,ValueError,TypeError,KeyError) as exc:
        raise BookOwnershipError('original identity upgrade intent is missing or invalid') from exc


def _read_intent(directory, identity_hash):
    from workbench.book_ownership import BookOwnershipError
    try:
        return validate_identity_upgrade_intent(_read(directory,_name(identity_hash,'intent')),identity_hash)
    except (OSError,ValueError,TypeError,KeyError) as exc:
        raise BookOwnershipError('original identity upgrade intent is missing or invalid') from exc


def _manifest(intent):
    from core.engine.persistence_v2 import load_persistence_manifest_v2
    from workbench.book_ownership import BookOwnershipError
    root = Path(intent['transaction_root']); run = intent['immutable']['run_id']
    # run is validated by the real manifest loader; never accept a path locator
    # from an unvalidated control file before checking its finite filename.
    import re
    if not isinstance(run,str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}',run) is None:
        raise BookOwnershipError('identity upgrade transaction identifier is invalid')
    manifest = load_persistence_manifest_v2(root/'journals'/run/'manifest.json')
    if manifest['immutable'] != intent['immutable'] or not is_identity_upgrade(manifest):
        raise BookOwnershipError('original identity upgrade transaction changed')
    book, _, target = _details(root, manifest)
    if (str(book) != intent['source_record']['binding']['root']
            or target['before_sha256'] != intent['source_record']['binding']['identity_sha256']
            or target['after_sha256'] != intent['active_record']['binding']['identity_sha256']
            or manifest['immutable']['book_id'] != intent['source_record']['binding']['book_id']):
        raise BookOwnershipError('identity upgrade manifest differs from registered ownership')
    return manifest


def _completed(intent):
    from core.engine.persistence_v2 import verify_publication_receipt
    from core.story_project.reviewed_identity import verify_reviewed_identity_transition
    from workbench.book_ownership import BookOwnershipError
    manifest = _manifest(intent)
    if manifest['state'] != 'completed':
        raise BookOwnershipError('identity upgrade transaction is not completed')
    book, roots, _ = _details(Path(intent['transaction_root']),manifest)
    ref = manifest['immutable']['publication_receipt']['path_ref']
    receipt = _read(roots[ref['root_id']],ref['relative_path'])
    if not verify_publication_receipt(receipt,root_map=roots)['valid']:
        raise BookOwnershipError('original identity upgrade publication receipt cannot be verified')
    run = manifest['immutable']['run_id']; prefix = 'reviewed/upgrades/'+run
    verify_reviewed_identity_transition(
        old_identity_bytes=read_project_bytes(roots['runtime'],prefix+'-before.json'),
        new_identity_bytes=read_project_bytes(roots['runtime'],prefix+'-after.json'),
        baseline_document=read_project_bytes(roots['runtime'],'reviewed/states/'+run+'.md'))
    return identity_upgrade_completion(intent,receipt)


def identity_upgrade_completion(intent, receipt):
    return _signed({'schema_version':intent['schema_version']+'_completed','intent_sha256':intent['intent_sha256'],
        'active_record_sha256':intent['active_record']['record_sha256'],
        'publication_receipt_sha256':receipt['receipt_hash']},'receipt_sha256')


def identity_upgrade_publication_evidence(intent):
    _completed(intent)
    manifest = _manifest(intent)
    _, roots, _ = _details(Path(intent['transaction_root']),manifest)
    ref = manifest['immutable']['publication_receipt']['path_ref']
    return {'manifest':manifest,'publication_receipt':_read(roots[ref['root_id']],ref['relative_path'])}


def validate_identity_upgrade_publication(intent,evidence,completion,*,depth=0):
    """Verify frozen publication relationships without opening source paths."""
    from core.engine.persistence_v2 import (validate_persistence_manifest_v2,
        validate_publication_receipt,_assert_receipt_matches_manifest)
    from workbench.book_ownership import BookOwnershipError
    active=intent['active_record'];source=intent['source_record']
    validate_identity_upgrade_intent(intent,active['binding']['identity_sha256'],depth=depth)
    if set(evidence)!={'manifest','publication_receipt'}:
        raise BookOwnershipError('identity upgrade publication fields changed')
    manifest=validate_persistence_manifest_v2(evidence['manifest'])
    receipt=validate_publication_receipt(evidence['publication_receipt'])
    _assert_receipt_matches_manifest(receipt,manifest)
    book,_,target=_details(Path(intent['transaction_root']),manifest)
    if (manifest['state']!='completed' or manifest['immutable']!=intent['immutable']
            or str(book)!=source['binding']['root'] or manifest['immutable']['book_id']!=source['binding']['book_id']
            or target['before_sha256']!=source['binding']['identity_sha256']
            or target['after_sha256']!=active['binding']['identity_sha256']
            or target['before_sha256']==target['after_sha256']
            or completion!=identity_upgrade_completion(intent,receipt)):
        raise BookOwnershipError('identity upgrade publication or completion differs')
    return active


def require_identity_upgrade_record(record, *, machine_store=None):
    """A changed V2 binding needs its actual original upgrade and completion."""
    from workbench import book_ownership as b
    if record['version'] != b.OWNERSHIP_V2:
        return
    root = Path(record['binding']['root']); witness = b._require_witness(root,record['binding']['book_id'],record)
    if witness['initial_binding_sha256'] == digest(record['binding']):
        return
    from workbench.restore_qualification import machine_store_for
    store = machine_store_for(record['binding']['book_id'],machine_store)
    directory = store/digest(record['binding']['book_id'].encode('utf-8'))
    identity_hash = record['binding']['identity_sha256']
    try:
        intent = _read_intent(directory,identity_hash)
        if {k:v for k,v in record.items() if k not in {'generation','record_sha256'}} != {
                k:v for k,v in intent['active_record'].items() if k not in {'generation','record_sha256'}}:
            raise b.BookOwnershipError('identity upgrade belongs to another physical registration')
        if _read(directory,_name(identity_hash,'done')) != _completed(intent):
            raise b.BookOwnershipError('identity upgrade completion changed')
        if intent['active_record'] != record:
            from workbench.restore_rollback_ancestry import load_rollback_ancestry
            load_rollback_ancestry(directory,intent['active_record'],record,record['binding']['book_id'])
    except (OSError,ValueError,KeyError,TypeError) as exc:
        raise b.BookOwnershipError('registered identity upgrade qualification cannot be verified') from exc


@contextmanager
def registered_upgrade_scope(transaction_root, manifest, *, fault_injector=None, resume=False):
    from workbench import book_ownership as b
    if not is_identity_upgrade(manifest):
        yield False; return
    book, roots, target = _details(Path(transaction_root),manifest)
    book_id = manifest['immutable']['book_id']
    from workbench.restore_qualification import machine_store_for
    store = machine_store_for(book_id); directory = store/digest(book_id.encode('utf-8'))
    identity_hash = target['after_sha256']
    def fault(event):
        if fault_injector: fault_injector(event,None,None)
    with b._machine_gate(book_id,store):
        record = b._record(directory,book_id)
        if record is None:
            if resume: raise b.BookOwnershipError('registered upgrade ownership was lost')
            yield False; return
        if record['version'] not in {b.OWNERSHIP_V2,b.OWNERSHIP_V3}:
            raise b.BookOwnershipError('identity upgrade requires a supported registered owner')
        path = directory/_name(identity_hash,'intent')
        if not os.path.lexists(path):
            if resume or record['state'] != 'active':
                raise b.BookOwnershipError('original identity upgrade intent is missing')
            b._require_output_owner(book,book_id,record)
            if record['binding']['identity_sha256'] != target['before_sha256']:
                raise b.BookOwnershipError('upgrade before-identity differs from active registration')
            from core.engine.persistence_v2 import _validate_complete_staged_journal
            _validate_complete_staged_journal(Path(transaction_root)/'journals'/manifest['immutable']['run_id'],manifest)
            from workbench.writer_identity import load_verified_writer_identity
            load_verified_writer_identity(book)
            active = _signed({**record,'generation':record['generation']+1,
                'binding':{**record['binding'],'identity_sha256':identity_hash}},'record_sha256')
            extra = {}
            if record['version'] == b.OWNERSHIP_V3:
                from types import SimpleNamespace
                from workbench.backup_restore_provenance import capture_restore_provenance
                extra['source_restore_provenance'] = capture_restore_provenance(
                    SimpleNamespace(path=Path(record['binding']['database'])),record,machine_store=store)
            intent = _signed({'schema_version':RESTORED_PROTOCOL if extra else PROTOCOL,'source_record':record,
                'pending_record':_signed({**record,'state':'identity_upgrading'},'record_sha256'),
                'active_record':active,'transaction_root':str(transaction_root),
                'immutable':manifest['immutable'],**extra},'intent_sha256')
            atomic_create_json(path,intent); fault('after_identity_upgrade_intent')
        intent = _read_intent(directory,identity_hash)
        source, pending, active = (intent[k] for k in ('source_record','pending_record','active_record'))
        if record not in (source,pending,active):
            raise b.BookOwnershipError('identity upgrade does not own the current machine generation')
        if _manifest(intent) != manifest:
            raise b.BookOwnershipError('upgrade changed while acquiring its registered scope')
        if intent['schema_version'] == RESTORED_PROTOCOL:
            from workbench.restored_identity_upgrade import require_original_qualification
            require_original_qualification(intent,directory)
        b._require_witness(book,book_id,record)
        current = _binding_now(source)
        if current not in (source['binding'],active['binding']):
            raise b.BookOwnershipError('identity upgrade physical registration changed')
        if record == active:
            b._require_output_owner(book,book_id,record)
        elif record == source:
            if current != source['binding']:
                raise b.BookOwnershipError('unfenced identity upgrade cannot adopt changed data')
            from core.story_project.read_set import verify_story_project_read_set
            verify_story_project_read_set(manifest['immutable']['story_project_read_set'],phase='pre_apply')
            atomic_write_json(directory/'ownership.json',pending); fault('after_identity_upgrade_fence')
        elif not resume:
            raise b.BookOwnershipError('interrupted registered upgrade needs explicit continuation')
        yield True
        final = _manifest(intent)
        current_record = b._record(directory,book_id)
        if current_record not in (pending,active):
            raise b.BookOwnershipError('identity upgrade ownership changed during transaction')
        if final['state'] == 'completed':
            completion = _completed(intent)
            if _binding_now(source) != active['binding']:
                raise b.BookOwnershipError('completed upgrade identity differs from current registration')
            done = directory/_name(identity_hash,'done')
            if current_record == active:
                b._require_output_owner(book,book_id,active)
                return
            if os.path.lexists(done):
                if _read(directory,done.name) != completion:
                    raise b.BookOwnershipError('original identity upgrade completion differs')
            else:
                atomic_create_json(done,completion); fault('after_identity_upgrade_done')
            if intent['schema_version'] == RESTORED_PROTOCOL:
                from workbench.restored_identity_upgrade import publish_qualified_upgrade
                publish_qualified_upgrade(intent,directory,fault=fault)
            atomic_write_json(directory/'ownership.json',active); fault('after_identity_upgrade_active')
        elif final['state'] == 'rolled_back' and current_record == pending:
            if _binding_now(source) != source['binding']:
                raise b.BookOwnershipError('rolled-back upgrade did not preserve original identity')
            atomic_write_json(directory/'ownership.json',source)
        # Incomplete publication remains fenced for explicit continuation.


def resume_registered_identity_upgrade(transaction_root, run_id, *, fault_injector=None):
    """Continue only an existing original transaction; never scan or repair."""
    from core.engine.persistence_v2 import (PersistenceV2Transaction, load_persistence_manifest_v2,
        _root_map_from_manifest, _resolved_target_paths, _validate_pending_candidate,
        _rollback_pre_marker, _result)
    import re
    if not isinstance(run_id,str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}',run_id) is None:
        raise ValueError('invalid original identity upgrade run')
    root = Path(transaction_root).absolute()
    manifest = load_persistence_manifest_v2(root/'journals'/run_id/'manifest.json')
    tx = PersistenceV2Transaction(transaction_root=root,run_id=run_id,
        book_id=manifest['immutable']['book_id'],root_map=_root_map_from_manifest(manifest),
        fault_injector=fault_injector)
    with registered_upgrade_scope(root,manifest,fault_injector=fault_injector,resume=True):
        resolver = tx._resolver_for_manifest(manifest,require_same_revision=True)
        paths = _resolved_target_paths(manifest,phase='apply',resolver=resolver)
        with persistence_run_lock(root,state_paths=paths):
            if load_persistence_manifest_v2(tx.manifest_path) != manifest:
                raise ValueError('original upgrade changed during explicit continuation')
            if manifest['state']=='completed':
                return tx._complete_publication_locked()
            if manifest['state']=='rolled_back':
                return _result(manifest,receipt_valid=False)
            if tx.marker_path.exists():
                return tx._complete_publication_locked()
            _validate_pending_candidate(tx.journal_dir,manifest)
            return _rollback_pre_marker(transaction_root=root,journal_dir=tx.journal_dir,
                manifest_path=tx.manifest_path,manifest=manifest,pending_entry_path=tx.pending_entry_path,
                resolver=resolver,durability_hook=tx._durability_faults.run)
