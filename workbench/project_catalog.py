from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence


# Official StoryProjects for validation live under books/ with this prefix.
# See scripts/prepare_chapter20_recovery_fixture.py and docs/product-validation.md.
ENGINEERING_FIXTURE_DIRECTORY_PREFIXES: tuple[str, ...] = ("_validation_",)


def is_engineering_fixture_root(root: str | Path) -> bool:
    """Return True when a StoryProject directory uses a validation/fixture name.

    This is a path convention, not a hardcoded book title. Tests and engineering
    can still import and open these projects by path or id.
    """
    name = Path(root).name
    return any(name.startswith(prefix) for prefix in ENGINEERING_FIXTURE_DIRECTORY_PREFIXES)


def resolve_active_book(repository: str | Path) -> Path | None:
    """Resolve the repository .active-book marker to a canonical path, if present."""
    marker = Path(repository) / ".active-book"
    if not marker.is_file():
        return None
    try:
        value = marker.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return None
    if not value:
        return None
    candidate = Path(value)
    path = candidate if candidate.is_absolute() else Path(repository) / candidate
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def should_auto_import_book(root: str | Path, *, active_book: Path | None = None) -> bool:
    """Whether startup auto-import should register this directory.

    An explicit .active-book target is always imported. Other books under the
    repository are imported unless they use a fixture/validation directory name.
    """
    try:
        canonical = Path(root).resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    if active_book is not None:
        try:
            if canonical == Path(active_book).resolve(strict=False):
                return True
        except (OSError, RuntimeError):
            pass
    return not is_engineering_fixture_root(canonical)


def annotate_project_catalog(item: Mapping[str, Any], record: Any) -> dict[str, Any]:
    payload = dict(item)
    payload["engineering_fixture"] = is_engineering_fixture_root(getattr(record, "root", ""))
    return payload


def select_author_bookshelf(
    records: Sequence[Any],
    *,
    repository: str | Path,
    include_fixtures: bool = False,
) -> tuple[Any, ...]:
    """Author-facing bookshelf order: hide fixtures by default, prefer .active-book."""
    if include_fixtures:
        visible = list(records)
    else:
        visible = [record for record in records if not is_engineering_fixture_root(getattr(record, "root", ""))]
    active = resolve_active_book(repository)
    if active is None or not visible:
        return tuple(visible)
    try:
        active_canonical = active.resolve(strict=False)
    except (OSError, RuntimeError):
        return tuple(visible)
    preferred: list[Any] = []
    rest: list[Any] = []
    for record in visible:
        root = getattr(record, "root", None)
        try:
            if root is not None and Path(root).resolve(strict=False) == active_canonical:
                preferred.append(record)
            else:
                rest.append(record)
        except (OSError, RuntimeError):
            rest.append(record)
    return tuple(preferred + rest)


__all__ = [
    "ENGINEERING_FIXTURE_DIRECTORY_PREFIXES",
    "annotate_project_catalog",
    "is_engineering_fixture_root",
    "resolve_active_book",
    "select_author_bookshelf",
    "should_auto_import_book",
]
