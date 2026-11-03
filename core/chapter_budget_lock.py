"""Prepare budget locks without following an existing linked lock file."""
from contextlib import contextmanager
from pathlib import Path
import stat

from core.engine.persistence import PersistenceLockError, atomic_create_text, persistence_run_lock
from core.engine.safe_paths import assert_safe_local_tree, FILE_ATTRIBUTE_REPARSE_POINT


@contextmanager
def chapter_budget_lock(root):
    root=assert_safe_local_tree(Path(root))
    root.mkdir(parents=True,exist_ok=True)
    root=assert_safe_local_tree(root)
    path=root/'.persistence.lock'
    try:path.lstat()
    except FileNotFoundError:
        try:atomic_create_text(path,'\0')
        except FileExistsError:pass
    info=path.lstat()
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink!=1
            or getattr(info,'st_file_attributes',0)&FILE_ATTRIBUTE_REPARSE_POINT):
        raise PersistenceLockError('budget lock must be an ordinary unlinked file')
    with persistence_run_lock(root,require_existing_lock=True):
        yield
