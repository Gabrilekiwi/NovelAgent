"""Per-user machine book ownership, independent of configurable workbench DBs.

Enrollment follows verified onboarding/upgrade. Commit never silently enrolls,
rebinds or recreates lost ownership. T7 will provide the verified restore handoff.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import stat
import threading
import time
import uuid

from core.engine.persistence import atomic_create_json, atomic_write_json, persistence_state_lock, PersistenceLockError, PersistenceLockBusy
from core.engine.root_registry import directory_identity
from core.engine.safe_paths import FILE_ATTRIBUTE_REPARSE_POINT, assert_safe_local_tree
from core.story_project.reviewed_identity import load_verified_reviewed_identity
from core.story_project.reviewed_state_document import digest, read_project_bytes
from workbench.instance import InstanceLease, InstanceAlreadyRunning
from workbench.writer_identity import validate_writer_identity, load_verified_writer_identity

_locks_guard = threading.Lock()
_locks = {}
_machine_scopes = ContextVar('novelagent_machine_book_scopes', default=())
WITNESS_PATH = '.novelagent/write-registration.json'
OWNERSHIP_V2 = 'machine_book_ownership_v2'
OWNERSHIP_V3 = 'machine_book_ownership_v3'
_RECORD_KEYS = frozenset({'version', 'generation', 'state', 'binding', 'record_sha256'})


class BookOwnershipError(ValueError):
    code = "book_ownership_invalid"


def default_machine_store():
    # Deliberately unrelated to the workbench database/data-directory override.
    # Isolated release gates may clear HOME/USERPROFILE; honor an explicit
    # store override and otherwise require a resolvable machine data root.
    override = os.environ.get("NOVELAGENT_MACHINE_BOOK_STORE")
    if override:
        return Path(override)
    if os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"])
    elif os.environ.get("XDG_DATA_HOME"):
        base = Path(os.environ["XDG_DATA_HOME"])
    else:
        home = os.environ.get("USERPROFILE") or os.environ.get("HOME")
        if not home:
            try:
                home = str(Path.home())
            except RuntimeError as exc:
                raise BookOwnershipError(
                    "machine book store requires LOCALAPPDATA, HOME, or NOVELAGENT_MACHINE_BOOK_STORE"
                ) from exc
        base = Path(home) / (".local/share" if os.name != "nt" else "AppData/Local")
    return base / "NovelAgent/machine-books"


def _lineage(path):
    path = assert_safe_local_tree(Path(path).absolute())
    return {str(p): directory_identity(p) for p in (*reversed(path.parents), path)}


def _file_identity(path):
    assert_safe_local_tree(path.parent)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT):
        raise BookOwnershipError("unsafe database file")
    return {"device": int(info.st_dev), "inode": int(info.st_ino)}


@contextmanager
def _machine_gate(book_id, store, *, wait_timeout=0):
    # Use the same deterministic OS-backed state lock even before the first
    # ownership record exists. An unenrolled legacy writer must serialize with
    # its first restore handoff without creating a machine ledger of its own.
    store = assert_safe_local_tree(Path(store).absolute())
    directory = assert_safe_local_tree(store / digest(book_id.encode('utf-8')))
    path = directory / 'ownership.json'
    # Reads may briefly wait; writes keep the same nonblocking admission in
    # this process and across processes. Both use the same per-book mutex.
    # The OS state lock still fences other processes. Borrowing an OS handle
    # is thread-bound; concurrent readers must not borrow each other's handle
    # or classify its ordinary contention as damaged restore qualification.
    key = os.path.normcase(str(directory.absolute()))
    with _locks_guard:
        mutex = _locks.setdefault(key, threading.RLock())
    deadline = time.monotonic() + wait_timeout
    if not mutex.acquire(timeout=wait_timeout):
        raise PersistenceLockBusy(f'machine ownership is busy: {directory}')
    try:
        with ExitStack() as stack:
            while True:
                try:
                    stack.enter_context(persistence_state_lock((path,)))
                    break
                except PersistenceLockBusy:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise
                    time.sleep(min(.05, remaining))
            token = _machine_scopes.set((*_machine_scopes.get(), (book_id, Path(store).absolute())))
            try:
                yield
            finally:
                _machine_scopes.reset(token)
    finally:
        mutex.release()


def _record(directory, book_id):
    path = directory/'ownership.json'
    if not os.path.lexists(path):
        return None
    _file_identity(path)
    if path.stat().st_size > 1024**2:
        raise BookOwnershipError('machine ownership record exceeds supported size')
    try:
        value = json.loads(read_project_bytes(directory, path.name))
        version = value.get('version') if isinstance(value, dict) else None
        expected = _RECORD_KEYS | ({'registration_id', 'witness_sha256'} if version in {OWNERSHIP_V2,OWNERSHIP_V3} else set())
        if version==OWNERSHIP_V3: expected=expected|{'restore_qualification_sha256'}
        if (not isinstance(value, dict)
                or set(value) != expected
                or version not in {'machine_book_ownership_v1', OWNERSHIP_V2, OWNERSHIP_V3}
                or type(value['generation']) is not int or value['generation'] < 1
                or not isinstance(value['state'], str)
                or not isinstance(value['binding'], dict) or value['binding'].get('book_id') != book_id
                or value['record_sha256'] != digest({k:v for k,v in value.items() if k != 'record_sha256'})):
            raise BookOwnershipError('machine ownership record is invalid')
        if version in {OWNERSHIP_V2,OWNERSHIP_V3}:
            _registration_id(value['registration_id'])
            if not _sha256(value['witness_sha256']):
                raise BookOwnershipError('machine ownership witness hash is invalid')
        if version==OWNERSHIP_V3 and not _sha256(value['restore_qualification_sha256']):
            raise BookOwnershipError('restored ownership qualification hash is invalid')
        return value
    except (OSError, TypeError, ValueError) as exc:
        raise BookOwnershipError('machine ownership record cannot be verified') from exc


def _sha256(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _registration_id(value):
    if not isinstance(value, str) or len(value) != 32 or any(c not in '0123456789abcdef' for c in value):
        raise BookOwnershipError('book registration identifier is invalid')


def _witness(root, book_id):
    path = root / WITNESS_PATH
    if not os.path.lexists(path):
        return None
    _file_identity(path)
    if path.stat().st_size > 64 * 1024:
        raise BookOwnershipError('book registration witness exceeds supported size')
    try:
        value = json.loads(read_project_bytes(root, WITNESS_PATH))
        if (not isinstance(value, dict)
                or set(value) != {'version', 'book_id', 'registration_id', 'initial_binding_sha256', 'witness_sha256'}
                or value['version'] != 'machine_book_witness_v1' or value['book_id'] != book_id
                or not _sha256(value['initial_binding_sha256'])
                or value['witness_sha256'] != digest({k:v for k,v in value.items() if k != 'witness_sha256'})):
            raise BookOwnershipError('book registration witness is invalid')
        _registration_id(value['registration_id'])
        return value
    except (OSError, TypeError, ValueError) as exc:
        raise BookOwnershipError('book registration witness cannot be verified') from exc


def _new_witness(book_id, registration_id, binding):
    value = dict(version='machine_book_witness_v1', book_id=book_id,
                 registration_id=registration_id, initial_binding_sha256=digest(binding))
    value['witness_sha256'] = digest(value)
    return value


def _require_witness(root, book_id, record):
    witness = _witness(root, book_id)
    if record is None:
        if witness is not None:
            raise BookOwnershipError('machine ownership is missing for a previously registered book')
    elif record['version'] in {OWNERSHIP_V2,OWNERSHIP_V3}:
        if (witness is None or witness['registration_id'] != record['registration_id']
                or witness['witness_sha256'] != record['witness_sha256']):
            raise BookOwnershipError('machine ownership and book registration witness differ')
    elif witness is not None:
        raise BookOwnershipError('old ownership cannot bypass a book registration witness')
    return witness


@contextmanager
def _ownership_machine_gate(book_id, store, directory, *, wait_timeout=0):
    with ExitStack() as stack:
        try:
            stack.enter_context(_machine_gate(book_id, store, wait_timeout=wait_timeout))
        except PersistenceLockError as exc:
            # Keep the existing public acquisition failure used by API callers
            # and external process probes, regardless of which OS lock is busy.
            raise InstanceAlreadyRunning(directory) from exc
        yield


def _output_books(paths):
    roots = {}
    for raw in paths:
        path = Path(raw).absolute()
        assert_safe_local_tree(path.parent)
        for parent in (path, *path.parents):
            marker = parent/'.novelagent/project.json'
            if not os.path.lexists(marker):
                if os.path.lexists(parent / WITNESS_PATH):
                    raise BookOwnershipError('registered output book identity is missing')
                continue
            _file_identity(marker)
            if marker.stat().st_size > 1024**2:
                raise BookOwnershipError('output project identity exceeds supported size')
            try:
                value = json.loads(read_project_bytes(parent, '.novelagent/project.json'))
                book_id = value['book_id']
                if not isinstance(book_id, str) or not book_id.strip():
                    raise ValueError('missing book id')
            except (OSError, KeyError, TypeError, ValueError) as exc:
                raise BookOwnershipError('output project identity cannot be verified') from exc
            roots[parent] = book_id
    return roots


def _require_output_owner(root, book_id, record):
    _require_witness(root, book_id, record)
    if record is None:
        return  # Existing unenrolled flows retain their current write contract.
    binding = record['binding']
    try:
        database = Path(binding['database'])
        if (record['state'] != 'active' or binding['root'] != str(root)
                or binding['root_lineage'] != _lineage(root)
                or binding['database_parent_lineage'] != _lineage(database.parent)
                or binding['database_file_identity'] != _file_identity(database)
                or binding['identity_sha256'] != digest(read_project_bytes(root, '.novelagent/project.json'))):
            raise BookOwnershipError('output book registration or physical ownership is no longer active')
        from workbench.restore_qualification import require_qualified_record
        require_qualified_record(record)
        from workbench.registered_identity_upgrade import require_identity_upgrade_record
        require_identity_upgrade_record(record)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise BookOwnershipError('output book registration or physical ownership is no longer active') from exc


@contextmanager
def book_output_write_lock(*paths, book_id=None):
    """Fence supported writers against this user's current ledger.

    Borrowing follows persistence_state_lock's thread/handle validation. The
    context only carries an existing test-injected store; it never grants a
    permit or bypasses an OS lock in another thread/process.
    """
    roots = _output_books(paths)
    if book_id is not None and (not isinstance(book_id, str) or not book_id.strip()):
        raise BookOwnershipError('write operation has an invalid book identity')
    identities = set(roots.values())
    if book_id is not None:
        if identities and book_id not in identities:
            raise BookOwnershipError('write operation identity differs from its actual project')
        identities.add(book_id)
    stores = {}
    for identity in identities:
        selected = {store for known,store in _machine_scopes.get() if known == identity}
        if len(selected) > 1:
            raise BookOwnershipError('conflicting machine stores for one output book')
        stores[identity] = next(iter(selected)) if selected else default_machine_store()
    with ExitStack() as stack:
        records = {}
        for identity, store in sorted(stores.items(), key=lambda item: os.path.normcase(str(item[1]/digest(item[0].encode('utf-8'))))):
            stack.enter_context(_machine_gate(identity, store))
            directory = store/digest(identity.encode('utf-8'))
            records[identity] = (directory, _record(directory, identity))
        if _output_books(paths) != roots:
            raise BookOwnershipError('output book identity changed while locking')
        if book_id is not None and records[book_id][1] is not None and book_id not in roots.values():
            # An external runtime plus an ID is not proof of the physical book
            # that owns the operation. Never infer it from the ledger alone.
            raise BookOwnershipError('enrolled writing requires its actual project root')
        for root, book_id in roots.items():
            _require_output_owner(root, book_id, records[book_id][1])
        from workbench.restore_execution_policy import require_restored_execution_paths
        require_restored_execution_paths(*paths)
        yield
        for book_id, (directory, record) in records.items():
            if _record(directory, book_id) != record:
                raise BookOwnershipError('machine ownership changed during output writing')


class BookOwnership:
    def __init__(self, book_id, *, machine_store=None, fault_injector=None):
        if not isinstance(book_id, str) or not book_id.strip():
            raise BookOwnershipError("book identity is required")
        self.book_id = book_id
        self.store = assert_safe_local_tree(default_machine_store() if machine_store is None else machine_store)
        self.directory = self.store / digest(book_id.encode("utf-8"))
        self.record_path = self.directory / "ownership.json"
        self._lease = None
        self._locked_lineage = None
        self.fault_injector = fault_injector

    @contextmanager
    def locked(self, *, wait_timeout=0):
        with _ownership_machine_gate(self.book_id, self.store, self.directory, wait_timeout=wait_timeout):
            if self._lease is not None:
                raise BookOwnershipError("nested book ownership lock")
            assert_safe_local_tree(self.directory).mkdir(parents=True, exist_ok=True)
            lineage = _lineage(self.directory)
            lease = InstanceLease(self.directory).acquire()
            self._lease, self._locked_lineage = lease, lineage
            try:
                self._held()
                yield self
            finally:
                self._lease = self._locked_lineage = None
                lease.close()

    def _held(self):
        if self._lease is None:
            raise BookOwnershipError("machine book lock is required")
        self._lease.require_held()
        if _lineage(self.directory) != self._locked_lineage:
            raise BookOwnershipError("machine ownership directory changed while locked")

    def _binding(self, *, root, database, project_id, connection):
        self._held()
        root = assert_safe_local_tree(Path(root).absolute())
        row = connection.execute("SELECT id, book_id, root FROM projects WHERE id = ?", (project_id,)).fetchone()
        databases = {r[1]: r[2] for r in connection.execute("PRAGMA database_list")}
        if (not connection.in_transaction or Path(databases.get("main", "")).resolve() != database.path
                or row is None or row["book_id"] != self.book_id or Path(row["root"]).absolute() != root):
            raise BookOwnershipError("ownership does not match the locked database registration")
        raw = read_project_bytes(root, ".novelagent/project.json")
        identity = validate_writer_identity(json.loads(raw.decode("utf-8-sig")))
        if identity.book_id != self.book_id:
            raise BookOwnershipError("ownership identity differs from registered book")
        return {"book_id": self.book_id, "root": str(root), "root_lineage": _lineage(root),
            "database": str(database.path), "database_parent_lineage": _lineage(database.path.parent),
            "database_file_identity": _file_identity(database.path), "project_id": project_id,
            "identity_sha256": digest(raw)}

    def enroll(self, *, root, database, project_id, connection):
        binding = self._binding(root=root, database=database, project_id=project_id, connection=connection)
        root = Path(binding['root'])
        load_verified_reviewed_identity(root)
        return self._enroll_verified(binding, database=database, project_id=project_id, connection=connection)

    def _enroll_verified_legacy(self, *, root, database, project_id, connection):
        """Used only by the explicit maintenance/archive-bound legacy service."""
        binding = self._binding(root=root, database=database, project_id=project_id, connection=connection)
        proof = load_verified_writer_identity(root)
        if proof['identity']['schema_version'] not in {'1.0', '2.0'}:
            raise BookOwnershipError('legacy registration requires the original supported Strict format')
        return self._enroll_verified(binding, database=database, project_id=project_id, connection=connection)

    def _enroll_verified(self, binding, *, database, project_id, connection):
        root = Path(binding['root'])
        record = _record(self.directory, self.book_id)
        witness = _witness(root, self.book_id)
        if record is None and witness is not None:
            raise BookOwnershipError('machine ownership is missing for a previously registered book')
        if record is not None:
            if record['binding'] != binding or record['state'] not in {'active', 'enrolling'}:
                raise BookOwnershipError('book registration, generation or physical ownership is no longer active')
            if record['version'] in {OWNERSHIP_V2,OWNERSHIP_V3} and record['state'] == 'active':
                return self.require(root=root, database=database, project_id=project_id, connection=connection)
            if record['version'] != OWNERSHIP_V2:
                # A matching old active record can be migrated only by explicit
                # enrollment. Lost or suspended records are never reconstructed.
                self.require(root=root, database=database, project_id=project_id, connection=connection)
        if record is None or record['version'] != OWNERSHIP_V2:
            registration_id = uuid.uuid4().hex
            expected = _new_witness(self.book_id, registration_id, binding)
            pending = dict(version=OWNERSHIP_V2, generation=record['generation'] if record else 1,
                           state='enrolling', binding=binding, registration_id=registration_id,
                           witness_sha256=expected['witness_sha256'])
            pending['record_sha256'] = digest(pending)
            self._held()
            (atomic_write_json if record is not None else atomic_create_json)(self.record_path, pending)
            record = pending
            self._fault('after_enrollment_intent', record)
        expected = _new_witness(self.book_id, record['registration_id'], binding)
        if expected['witness_sha256'] != record['witness_sha256']:
            raise BookOwnershipError('enrollment intent differs from the original binding')
        if witness is not None and witness != expected:
            raise BookOwnershipError('enrollment witness differs from the original intent')
        if witness is None:
            self._held()
            atomic_create_json(root / WITNESS_PATH, expected)
            self._fault('after_enrollment_witness', record)
        # Recheck both records and the physical DB registration before enabling.
        self._held()
        if (_record(self.directory, self.book_id) != record
                or self._binding(root=root, database=database, project_id=project_id, connection=connection) != binding):
            raise BookOwnershipError('enrollment binding changed before activation')
        _require_witness(root, self.book_id, record)
        active = {**record, 'state': 'active'}
        active['record_sha256'] = digest({k:v for k,v in active.items() if k != 'record_sha256'})
        atomic_write_json(self.record_path, active)
        self._fault('after_enrollment_active', active)
        return self.require(root=root, database=database, project_id=project_id, connection=connection)

    def _fault(self, phase, record):
        if self.fault_injector is not None:
            self.fault_injector(phase, record)

    def require(self, *, root, database, project_id, connection):
        binding = self._binding(root=root, database=database, project_id=project_id, connection=connection)
        record = _record(self.directory, self.book_id)
        _require_witness(Path(binding['root']), self.book_id, record)
        if record is None:
            raise BookOwnershipError("book has not been enrolled for reviewed writing")
        if record['state'] != 'active' or record['binding'] != binding:
            raise BookOwnershipError("book registration, generation or physical ownership is no longer active")
        from workbench.restore_qualification import require_qualified_record
        require_qualified_record(record,machine_store=self.store)
        from workbench.registered_identity_upgrade import require_identity_upgrade_record
        require_identity_upgrade_record(record,machine_store=self.store)
        return record
