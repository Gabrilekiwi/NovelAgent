from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence


# Official StoryProjects for validation live under books/ with this prefix.
# See scripts/prepare_chapter20_recovery_fixture.py and docs/product-validation.md.
ENGINEERING_FIXTURE_DIRECTORY_PREFIXES: tuple[str, ...] = ("_validation_",)

# A later edition directory is the earlier stem plus a rewrite/version suffix.
# Examples: 「书名_重写V2」, 「书名_V2」, 「书名_重写」.
_EDITION_SUFFIX_RE = re.compile(r"^(?P<stem>.+)[_-](?:重写(?:[Vv]?\d+)?|[Vv]\d+)$")


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


def edition_stem(root: str | Path) -> str:
    """Return the shared title stem used to group rewrite/version editions."""
    name = Path(root).name
    match = _EDITION_SUFFIX_RE.fullmatch(name)
    return match.group("stem") if match else name


def _canonical_root(root: str | Path | None) -> Path | None:
    if root is None or str(root).strip() == "":
        return None
    try:
        return Path(root).resolve(strict=False)
    except (OSError, RuntimeError):
        return Path(root)


def annotate_related_editions(
    items: Sequence[Mapping[str, Any]],
    *,
    repository: str | Path,
) -> list[dict[str, Any]]:
    """Label previous/current editions when similarly named books share a shelf.

    Detection uses directory-name stems such as 「书名」 plus 「书名_重写V2」,
    not a hardcoded title. The .active-book target is the current edition when
    it belongs to the family; otherwise the later named rewrite/version is used.
    Unrelated books and single-member families are left unlabeled.
    """
    annotated = [dict(item) for item in items]
    active = resolve_active_book(repository)
    active_canonical = _canonical_root(active) if active is not None else None

    for item in annotated:
        item["is_active_book"] = bool(
            active_canonical is not None and _canonical_root(item.get("root")) == active_canonical
        )
        item.setdefault("edition_role", None)
        item.setdefault("edition_label", None)
        item.setdefault("current_edition_id", None)
        item.setdefault("current_edition_title", None)

    clusters: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in annotated:
        root = item.get("root")
        if not root:
            continue
        clusters[edition_stem(root)].append(item)

    for stem, members in clusters.items():
        if len(members) < 2:
            continue
        names = {Path(member["root"]).name for member in members}
        has_suffix = any(Path(member["root"]).name != stem for member in members)
        if not has_suffix and stem not in names:
            continue

        current = next((member for member in members if member.get("is_active_book")), None)
        if current is None:
            suffixed = [member for member in members if Path(member["root"]).name != stem]
            current = sorted(suffixed, key=lambda member: Path(member["root"]).name)[-1] if suffixed else members[0]

        current_title = (
            current.get("title")
            or current.get("name")
            or Path(str(current.get("root") or "")).name
        )
        current_id = current.get("id")
        current_root = _canonical_root(current.get("root"))

        for member in members:
            same = (
                member is current
                or (current_id and member.get("id") == current_id)
                or (_canonical_root(member.get("root")) == current_root)
            )
            if same:
                member["edition_role"] = "current"
                member["edition_label"] = "当前作品"
                member["current_edition_id"] = current_id
                member["current_edition_title"] = current_title
            else:
                member["edition_role"] = "previous"
                member["edition_label"] = "上一版"
                member["current_edition_id"] = current_id
                member["current_edition_title"] = current_title
    return annotated


def annotate_project_with_related_editions(
    item: Mapping[str, Any],
    siblings: Sequence[Mapping[str, Any]],
    *,
    repository: str | Path,
) -> dict[str, Any]:
    """Annotate one project in the context of its author-visible siblings."""
    combined: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for candidate in (item, *siblings):
        key = str(candidate.get("id") or candidate.get("root") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        combined.append(candidate)
    annotated = annotate_related_editions(combined, repository=repository)
    target_id = item.get("id")
    target_root = _canonical_root(item.get("root"))
    for each in annotated:
        if (target_id and each.get("id") == target_id) or (
            target_root is not None and _canonical_root(each.get("root")) == target_root
        ):
            return each
    return annotated[0] if annotated else dict(item)


__all__ = [
    "ENGINEERING_FIXTURE_DIRECTORY_PREFIXES",
    "annotate_project_catalog",
    "annotate_project_with_related_editions",
    "annotate_related_editions",
    "edition_stem",
    "is_engineering_fixture_root",
    "resolve_active_book",
    "select_author_bookshelf",
    "should_auto_import_book",
]
