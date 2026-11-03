"""Coordinate auxiliary file writes with containing project snapshots."""
from contextlib import contextmanager
import os
from pathlib import Path

from core.engine.persistence import persistence_state_lock
from core.engine.safe_paths import assert_safe_local_tree


def _write_targets(paths, *, lock_output_paths):
    targets = set()
    for raw in paths:
        path = Path(raw).absolute()
        assert_safe_local_tree(path.parent)
        # Atomic publication replaces the leaf entry. Keep its lexical name so
        # a leaf link cannot hide the identity of the containing project.
        path = path.parent.resolve(strict=False) / path.name
        if lock_output_paths:
            targets.add(path)
        for parent in path.parents:
            if os.path.lexists(parent / '.novelagent/project.json'):
                assert_safe_local_tree(parent / '.novelagent')
                targets.add(parent)
    return targets


@contextmanager
def project_output_lock(*paths, lock_output_paths=True):
    """Lock actual paths and containing identified projects without creating output.

    Identity files are markers only; never trust paths from their contents.
    Queue operations can retain their existing local lock protocol by selecting
    only containing projects. Synchronous nested owners borrow active locks.
    """
    # Share the current machine ledger with reviewed publication. Import at
    # operation time because ownership itself uses persistence primitives.
    from workbench.book_ownership import book_output_write_lock
    targets = _write_targets(paths, lock_output_paths=lock_output_paths)
    with book_output_write_lock(*paths), persistence_state_lock(targets):
        if _write_targets(paths, lock_output_paths=lock_output_paths) != targets:
            raise RuntimeError('output project boundary changed while locking')
        yield
