"""A process-held observation lease for one exact execution, never a write permit.

The OS releases the lease on process exit. A remaining lock file alone says
nothing about liveness. Intent/receipt artifacts still determine recovery.
"""
from contextlib import contextmanager
import errno
import hashlib
from pathlib import Path
import re

from core.engine.persistence import PersistenceLockError, persistence_run_lock
from core.engine.safe_paths import assert_safe_local_tree


def _root(run_dir, execution_id):
    if not isinstance(execution_id, str) or not re.fullmatch(r"execution_[a-zA-Z0-9_-]{1,180}", execution_id):
        raise ValueError("invalid execution activity identity")
    # Keep observation artifacts in the existing non-authoritative lock area,
    # with a short path for Windows. Exact execution digest collision would at
    # most display busy and cannot authorize a model call or state transition.
    return assert_safe_local_tree(Path(run_dir) / '.provider-boundary-lock' / hashlib.sha256(execution_id.encode()).hexdigest()[:24])


@contextmanager
def execution_activity(run_dir, execution_id):
    """Called inside the existing Provider boundary for the execution lifetime."""
    from workbench.restore_execution_policy import require_restored_execution_paths
    require_restored_execution_paths(Path(run_dir)/'executions'/execution_id)
    with persistence_run_lock(_root(run_dir, execution_id)):
        yield


def execution_is_active(run_dir, execution_id):
    """Probe an existing lock without creating artifacts or guessing from a PID."""
    root = _root(run_dir, execution_id)
    try:
        (root / '.persistence.lock').lstat()
    except FileNotFoundError:
        return False
    try:
        with persistence_run_lock(root, require_existing_lock=True):
            return False
    except PersistenceLockError as exc:
        cause = exc.__cause__
        if str(exc).startswith('persistence state is locked:') and isinstance(cause, OSError) and cause.errno in {errno.EACCES, errno.EAGAIN}:
            return True
        raise
