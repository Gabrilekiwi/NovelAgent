from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
import hashlib
from importlib import metadata as importlib_metadata
import json
from pathlib import Path, PurePosixPath
import platform
import re
import subprocess
from types import MappingProxyType
from typing import Any, ClassVar


EXECUTION_PROVENANCE_SCHEMA_VERSION = "1.0"
EXECUTION_PROVENANCE_CANONICAL_JSON_ALGORITHM = (
    "novelagent-execution-provenance-canonical-json-v1"
)
CODE_BUNDLE_ALGORITHM = "novelagent-code-bundle-v1"
HASH_ALGORITHM = "sha256"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{7,64}$")
_REQUIREMENT_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_NORMALIZE_DISTRIBUTION_RE = re.compile(r"[-_.]+")
_UNSAFE_FIELD_RE = re.compile(
    r"(?:^|_)(?:"
    r"authorization|proxy_authorization|api_?key|secret|password|passwd|"
    r"access_token|refresh_token|session_token|id_token|private_key|"
    r"credentials?|cookies?|set_cookie|env|environ|environment|"
    r"environment_variables|headers|http_headers"
    r")(?:_|$)",
    flags=re.IGNORECASE,
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{6,}", re.IGNORECASE),
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{8,}\b", re.IGNORECASE),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{12,}\b", re.IGNORECASE),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{8,}\b", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{12,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(
        r"\b(?:authorization|proxy-authorization|api_?key|client_secret|"
        r"access_token|refresh_token|password)\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
)
_ENV_ASSIGNMENT_RE = re.compile(r"(?m)^\s*[A-Za-z_][A-Za-z0-9_]*\s*=\s*.*$")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\[^\\/\s]+[\\/])"
)
_POSIX_ABSOLUTE_PATH_RE = re.compile(r"(?:^|[\s='\"(])/(?!/)")
_HOME_PATH_RE = re.compile(r"(?:^|[\s='\"(])~[\\/]")
_FILE_URI_RE = re.compile(r"\bfile:(?://)?[\\/]", re.IGNORECASE)

_JSON_SCALARS = (str, int, float, bool, type(None))
_AUTO_GIT = object()


class ExecutionProvenanceError(ValueError):
    """Base error for invalid or unsafe execution provenance."""


class UnsafeProvenanceError(ExecutionProvenanceError):
    """Raised when provenance input could disclose sensitive local data."""


class ProvenanceCaptureError(ExecutionProvenanceError):
    """Raised when deterministic provenance evidence cannot be captured."""


@dataclass(frozen=True)
class ExecutionProvenance:
    """Versioned, immutable evidence describing one execution environment.

    The record intentionally has no timestamp, host name, user name, branch,
    environment dump, raw Git status, or dirty diff.  Its stable identity is
    ``provenance_hash``, computed over every field except the hash itself.
    """

    code_bundle_hash: str
    code_file_count: int
    git_commit: str | None
    git_dirty: bool
    prompt_hashes: Mapping[str, str]
    schema_hashes: Mapping[str, str]
    python_version: str
    python_implementation: str
    dependency_versions: Mapping[str, str]
    provider: str
    model: str
    config: Mapping[str, Any] = field(default_factory=dict)
    feature_flags: Mapping[str, bool] = field(default_factory=dict)

    schema_version: ClassVar[str] = EXECUTION_PROVENANCE_SCHEMA_VERSION
    canonical_json_algorithm: ClassVar[str] = (
        EXECUTION_PROVENANCE_CANONICAL_JSON_ALGORITHM
    )
    hash_algorithm: ClassVar[str] = HASH_ALGORITHM
    code_bundle_algorithm: ClassVar[str] = CODE_BUNDLE_ALGORITHM

    def __post_init__(self) -> None:
        _require_sha256(self.code_bundle_hash, "code bundle hash")
        if (
            isinstance(self.code_file_count, bool)
            or not isinstance(self.code_file_count, int)
            or self.code_file_count < 0
        ):
            raise ExecutionProvenanceError(
                "code file count must be a non-negative integer"
            )
        commit = _normalize_git_commit(self.git_commit)
        if not isinstance(self.git_dirty, bool):
            raise ExecutionProvenanceError("git dirty state must be a boolean")

        prompt_hashes = _normalize_file_hashes(self.prompt_hashes, "prompt")
        schema_hashes = _normalize_file_hashes(self.schema_hashes, "schema")
        dependencies = _normalize_string_mapping(
            self.dependency_versions,
            kind="dependency",
        )
        config = _normalize_public_config(self.config)
        flags = _normalize_feature_flags(self.feature_flags)

        python_version = _public_text(self.python_version, "Python version")
        python_implementation = _public_text(
            self.python_implementation,
            "Python implementation",
        )
        provider = _public_text(self.provider, "provider")
        model = _public_text(self.model, "model")

        object.__setattr__(self, "git_commit", commit)
        object.__setattr__(self, "prompt_hashes", MappingProxyType(prompt_hashes))
        object.__setattr__(self, "schema_hashes", MappingProxyType(schema_hashes))
        object.__setattr__(
            self,
            "dependency_versions",
            MappingProxyType(dependencies),
        )
        object.__setattr__(self, "config", _freeze_mapping(config))
        object.__setattr__(self, "feature_flags", MappingProxyType(flags))
        object.__setattr__(self, "python_version", python_version)
        object.__setattr__(self, "python_implementation", python_implementation)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "model", model)

        # Scan the complete public payload as a final fail-closed boundary.  No
        # unsafe value is ever copied into a returned provenance record.
        _assert_safe_public_value(self._payload_dict())

    @property
    def provenance_hash(self) -> str:
        return hashlib.sha256(self.canonical_payload_bytes()).hexdigest()

    def canonical_payload_bytes(self) -> bytes:
        """Canonical bytes covered by ``provenance_hash``."""

        return _canonical_json_bytes(self._payload_dict())

    def canonical_json_bytes(self) -> bytes:
        """Canonical bytes for the complete, hash-bearing record."""

        return _canonical_json_bytes(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload_dict()
        payload["provenance_hash"] = self.provenance_hash
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExecutionProvenance":
        return _provenance_from_mapping(value)

    def _payload_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "canonical_json_algorithm": self.canonical_json_algorithm,
            "hash_algorithm": self.hash_algorithm,
            "code": {
                "bundle_algorithm": self.code_bundle_algorithm,
                "bundle_hash": self.code_bundle_hash,
                "file_count": self.code_file_count,
                "git": {
                    "commit": self.git_commit,
                    "dirty": self.git_dirty,
                },
            },
            "assets": {
                "prompts": [
                    {"path": path, "sha256": digest}
                    for path, digest in self.prompt_hashes.items()
                ],
                "schemas": [
                    {"path": path, "sha256": digest}
                    for path, digest in self.schema_hashes.items()
                ],
            },
            "runtime": {
                "python": {
                    "implementation": self.python_implementation,
                    "version": self.python_version,
                },
                "dependencies": [
                    {"name": name, "version": version}
                    for name, version in self.dependency_versions.items()
                ],
            },
            "model": {
                "provider": self.provider,
                "model": self.model,
            },
            "config": [
                {"name": name, "value": _thaw_json(value)}
                for name, value in self.config.items()
            ],
            "feature_flags": [
                {"name": name, "enabled": enabled}
                for name, enabled in self.feature_flags.items()
            ],
        }


def build_execution_provenance(
    *,
    code_bundle_hash: str,
    code_file_count: int,
    git_commit: str | None,
    git_dirty: bool,
    prompt_hashes: Mapping[str, str],
    schema_hashes: Mapping[str, str],
    dependency_versions: Mapping[str, str],
    provider: str,
    model: str,
    config: Mapping[str, Any] | None = None,
    feature_flags: Mapping[str, bool] | None = None,
    python_version: str | None = None,
    python_implementation: str | None = None,
) -> ExecutionProvenance:
    """Build provenance exclusively from explicit, public inputs."""

    return ExecutionProvenance(
        code_bundle_hash=code_bundle_hash,
        code_file_count=code_file_count,
        git_commit=git_commit,
        git_dirty=git_dirty,
        prompt_hashes=prompt_hashes,
        schema_hashes=schema_hashes,
        python_version=python_version or platform.python_version(),
        python_implementation=(
            python_implementation or platform.python_implementation()
        ),
        dependency_versions=dependency_versions,
        provider=provider,
        model=model,
        config=config or {},
        feature_flags=feature_flags or {},
    )


def capture_execution_provenance(
    repository_root: str | Path,
    *,
    provider: str,
    model: str,
    config: Mapping[str, Any] | None = None,
    feature_flags: Mapping[str, bool] | None = None,
    dependency_versions: Mapping[str, str] | None = None,
    dependency_names: Iterable[str] | None = None,
    code_files: Iterable[str | Path] | None = None,
    prompt_files: Iterable[str | Path] | None = None,
    schema_files: Iterable[str | Path] | None = None,
    git_commit: str | None | object = _AUTO_GIT,
    git_dirty: bool | object = _AUTO_GIT,
) -> ExecutionProvenance:
    """Capture deterministic provenance without recording local paths or env.

    Absolute paths may be used as private inputs to locate files, but only
    normalized repository-relative logical paths can enter the returned record.
    Dependency discovery is restricted to named direct requirements; it never
    enumerates the process environment or every installed distribution.
    """

    root = _resolve_repository_root(repository_root)
    selected_code_files = (
        _default_code_files(root)
        if code_files is None
        else _expand_file_inputs(root, code_files, suffixes=frozenset({".py"}))
    )
    selected_prompt_files = (
        _default_asset_files(root, "prompts", suffix=".md")
        if prompt_files is None
        else _expand_file_inputs(root, prompt_files, suffixes=frozenset({".md"}))
    )
    selected_schema_files = (
        _default_asset_files(root, "schemas", suffix=".json")
        if schema_files is None
        else _expand_file_inputs(root, schema_files, suffixes=frozenset({".json"}))
    )

    code_hashes = _hash_files(root, selected_code_files)
    prompt_hashes = _hash_files(root, selected_prompt_files)
    schema_hashes = _hash_files(root, selected_schema_files)
    bundle_hash = _code_bundle_hash(code_hashes)

    auto_commit: str | None = None
    auto_dirty = True
    if git_commit is _AUTO_GIT or git_dirty is _AUTO_GIT:
        auto_commit, auto_dirty = _capture_git_state(root)
    resolved_commit = auto_commit if git_commit is _AUTO_GIT else git_commit
    resolved_dirty = auto_dirty if git_dirty is _AUTO_GIT else git_dirty

    resolved_dependencies = (
        collect_dependency_versions(
            dependency_names
            if dependency_names is not None
            else _direct_requirement_names(root)
        )
        if dependency_versions is None
        else dict(dependency_versions)
    )

    return build_execution_provenance(
        code_bundle_hash=bundle_hash,
        code_file_count=len(code_hashes),
        git_commit=resolved_commit,  # type: ignore[arg-type]
        git_dirty=resolved_dirty,  # type: ignore[arg-type]
        prompt_hashes=prompt_hashes,
        schema_hashes=schema_hashes,
        dependency_versions=resolved_dependencies,
        provider=provider,
        model=model,
        config=config,
        feature_flags=feature_flags,
    )


def collect_dependency_versions(names: Iterable[str]) -> dict[str, str]:
    """Return sorted versions for an explicit dependency name set."""

    result: dict[str, str] = {}
    for raw_name in names:
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ExecutionProvenanceError(
                "dependency names must be non-empty strings"
            )
        name = _NORMALIZE_DISTRIBUTION_RE.sub("-", raw_name.strip()).lower()
        _assert_safe_public_name(name)
        try:
            version = importlib_metadata.version(raw_name.strip())
        except importlib_metadata.PackageNotFoundError:
            version = "not-installed"
        result[name] = _public_text(version, "dependency version")
    return dict(sorted(result.items()))


def validate_execution_provenance(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate structure, safety, canonical ordering, and integrity hash."""

    return _provenance_from_mapping(value).to_dict()


def canonical_provenance_json_bytes(
    value: ExecutionProvenance | Mapping[str, Any],
) -> bytes:
    provenance = (
        value if isinstance(value, ExecutionProvenance) else _provenance_from_mapping(value)
    )
    return provenance.canonical_json_bytes()


def canonical_provenance_hash(
    value: ExecutionProvenance | Mapping[str, Any],
) -> str:
    provenance = (
        value if isinstance(value, ExecutionProvenance) else _provenance_from_mapping(value)
    )
    return provenance.provenance_hash


def _provenance_from_mapping(value: Mapping[str, Any]) -> ExecutionProvenance:
    if not isinstance(value, Mapping):
        raise ExecutionProvenanceError("execution provenance must be an object")
    raw = dict(value)
    _assert_safe_public_value(raw)
    _require_exact_keys(
        raw,
        {
            "schema_version",
            "canonical_json_algorithm",
            "hash_algorithm",
            "provenance_hash",
            "code",
            "assets",
            "runtime",
            "model",
            "config",
            "feature_flags",
        },
        "execution provenance",
    )
    if raw["schema_version"] != EXECUTION_PROVENANCE_SCHEMA_VERSION:
        raise ExecutionProvenanceError("unsupported execution provenance schema version")
    if (
        raw["canonical_json_algorithm"]
        != EXECUTION_PROVENANCE_CANONICAL_JSON_ALGORITHM
    ):
        raise ExecutionProvenanceError("unsupported provenance canonical JSON algorithm")
    if raw["hash_algorithm"] != HASH_ALGORITHM:
        raise ExecutionProvenanceError("unsupported provenance hash algorithm")
    _require_sha256(raw["provenance_hash"], "provenance hash")

    code = _require_object(raw["code"], "code")
    _require_exact_keys(
        code,
        {"bundle_algorithm", "bundle_hash", "file_count", "git"},
        "code",
    )
    if code["bundle_algorithm"] != CODE_BUNDLE_ALGORITHM:
        raise ExecutionProvenanceError("unsupported code bundle algorithm")
    git = _require_object(code["git"], "git")
    _require_exact_keys(git, {"commit", "dirty"}, "git")

    assets = _require_object(raw["assets"], "assets")
    _require_exact_keys(assets, {"prompts", "schemas"}, "assets")
    prompt_hashes = _file_hashes_from_entries(assets["prompts"], "prompts")
    schema_hashes = _file_hashes_from_entries(assets["schemas"], "schemas")

    runtime = _require_object(raw["runtime"], "runtime")
    _require_exact_keys(runtime, {"python", "dependencies"}, "runtime")
    python = _require_object(runtime["python"], "runtime python")
    _require_exact_keys(python, {"implementation", "version"}, "runtime python")
    dependencies = _dependencies_from_entries(runtime["dependencies"])

    model = _require_object(raw["model"], "model")
    _require_exact_keys(model, {"provider", "model"}, "model")
    config = _config_from_entries(raw["config"])
    flags = _flags_from_entries(raw["feature_flags"])

    provenance = build_execution_provenance(
        code_bundle_hash=code["bundle_hash"],
        code_file_count=code["file_count"],
        git_commit=git["commit"],
        git_dirty=git["dirty"],
        prompt_hashes=prompt_hashes,
        schema_hashes=schema_hashes,
        python_version=python["version"],
        python_implementation=python["implementation"],
        dependency_versions=dependencies,
        provider=model["provider"],
        model=model["model"],
        config=config,
        feature_flags=flags,
    )
    if provenance.provenance_hash != raw["provenance_hash"]:
        raise ExecutionProvenanceError("execution provenance hash mismatch")
    if provenance.to_dict() != raw:
        raise ExecutionProvenanceError("execution provenance is not canonical")
    return provenance


def _file_hashes_from_entries(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, list):
        raise ExecutionProvenanceError(f"{label} must be an array")
    result: dict[str, str] = {}
    for item in value:
        entry = _require_object(item, f"{label} entry")
        _require_exact_keys(entry, {"path", "sha256"}, f"{label} entry")
        path = entry["path"]
        if not isinstance(path, str) or path in result:
            raise ExecutionProvenanceError(f"{label} paths must be unique strings")
        result[path] = entry["sha256"]
    return result


def _dependencies_from_entries(value: Any) -> dict[str, str]:
    if not isinstance(value, list):
        raise ExecutionProvenanceError("dependencies must be an array")
    result: dict[str, str] = {}
    for item in value:
        entry = _require_object(item, "dependency entry")
        _require_exact_keys(entry, {"name", "version"}, "dependency entry")
        name = entry["name"]
        if not isinstance(name, str) or name in result:
            raise ExecutionProvenanceError(
                "dependency names must be unique strings"
            )
        result[name] = entry["version"]
    return result


def _config_from_entries(value: Any) -> dict[str, Any]:
    if not isinstance(value, list):
        raise ExecutionProvenanceError("config must be an array")
    result: dict[str, Any] = {}
    for item in value:
        entry = _require_object(item, "config entry")
        _require_exact_keys(entry, {"name", "value"}, "config entry")
        name = entry["name"]
        if not isinstance(name, str) or name in result:
            raise ExecutionProvenanceError("config names must be unique strings")
        result[name] = entry["value"]
    return result


def _flags_from_entries(value: Any) -> dict[str, bool]:
    if not isinstance(value, list):
        raise ExecutionProvenanceError("feature flags must be an array")
    result: dict[str, bool] = {}
    for item in value:
        entry = _require_object(item, "feature flag entry")
        _require_exact_keys(entry, {"name", "enabled"}, "feature flag entry")
        name = entry["name"]
        if not isinstance(name, str) or name in result:
            raise ExecutionProvenanceError(
                "feature flag names must be unique strings"
            )
        result[name] = entry["enabled"]
    return result


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ExecutionProvenanceError(f"{label} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ExecutionProvenanceError(f"{label} keys must be strings")
    return dict(value)


def _require_exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    label: str,
) -> None:
    if set(value) != expected:
        raise ExecutionProvenanceError(f"{label} fields do not match the contract")


def _resolve_repository_root(value: str | Path) -> Path:
    try:
        root = Path(value).resolve(strict=True)
    except (OSError, RuntimeError, TypeError) as exc:
        raise ProvenanceCaptureError("repository root is unavailable") from exc
    if not root.is_dir():
        raise ProvenanceCaptureError("repository root must be a directory")
    return root


def _default_code_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for package_name in ("api", "core", "modules", "workflows"):
        package_root = root / package_name
        if package_root.is_dir():
            files.extend(
                path
                for path in package_root.rglob("*.py")
                if path.is_file()
            )
    files.extend(path for path in root.glob("*.py") if path.is_file())
    return _validated_file_list(root, files, suffixes=frozenset({".py"}))


def _default_asset_files(root: Path, directory: str, *, suffix: str) -> list[Path]:
    asset_root = root / directory
    if not asset_root.is_dir():
        return []
    return _validated_file_list(
        root,
        (path for path in asset_root.rglob(f"*{suffix}") if path.is_file()),
        suffixes=frozenset({suffix}),
    )


def _expand_file_inputs(
    root: Path,
    values: Iterable[str | Path],
    *,
    suffixes: frozenset[str],
) -> list[Path]:
    files: list[Path] = []
    for value in values:
        try:
            candidate = Path(value)
            if not candidate.is_absolute():
                # Accept either a repository-relative logical path (``core/x.py``)
                # or a path assembled from a relative repository root
                # (``.tmp/repo/core/x.py``).  Both are reduced to a confined
                # absolute locator before any public record is constructed.
                cwd_candidate = candidate.resolve(strict=False)
                try:
                    cwd_candidate.relative_to(root)
                    already_rooted = candidate.exists()
                except ValueError:
                    already_rooted = False
                candidate = cwd_candidate if already_rooted else root / candidate
            if candidate.is_symlink():
                raise ProvenanceCaptureError("symbolic provenance inputs are rejected")
            if candidate.is_dir():
                files.extend(path for path in candidate.rglob("*") if path.is_file())
            else:
                files.append(candidate)
        except (OSError, RuntimeError, TypeError) as exc:
            if isinstance(exc, ProvenanceCaptureError):
                raise
            raise ProvenanceCaptureError("provenance file input is unavailable") from exc
    return _validated_file_list(root, files, suffixes=suffixes)


def _validated_file_list(
    root: Path,
    values: Iterable[Path],
    *,
    suffixes: frozenset[str],
) -> list[Path]:
    resolved_by_logical_path: dict[str, Path] = {}
    for value in values:
        try:
            if value.is_symlink():
                raise ProvenanceCaptureError("symbolic provenance inputs are rejected")
            path = value.resolve(strict=True)
            if not path.is_file() or path.suffix.lower() not in suffixes:
                raise ProvenanceCaptureError("provenance input has an invalid file type")
            try:
                relative = path.relative_to(root)
            except ValueError as exc:
                raise ProvenanceCaptureError(
                    "provenance inputs must remain inside the repository"
                ) from exc
            logical_path = _normalize_logical_path(relative.as_posix())
            resolved_by_logical_path[logical_path] = path
        except (OSError, RuntimeError) as exc:
            if isinstance(exc, ProvenanceCaptureError):
                raise
            raise ProvenanceCaptureError("provenance file input is unavailable") from exc
    return [resolved_by_logical_path[key] for key in sorted(resolved_by_logical_path)]


def _hash_files(root: Path, files: Iterable[Path]) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in files:
        try:
            logical_path = _normalize_logical_path(path.relative_to(root).as_posix())
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ProvenanceCaptureError("unable to hash provenance input") from exc
        result[logical_path] = digest.hexdigest()
    return dict(sorted(result.items()))


def _code_bundle_hash(file_hashes: Mapping[str, str]) -> str:
    manifest = {
        "algorithm": CODE_BUNDLE_ALGORITHM,
        "files": [
            {"path": path, "sha256": digest}
            for path, digest in sorted(file_hashes.items())
        ],
    }
    return hashlib.sha256(_canonical_json_bytes(manifest)).hexdigest()


def _capture_git_state(root: Path) -> tuple[str | None, bool]:
    try:
        commit_process = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="strict",
            check=False,
        )
    except (OSError, UnicodeError) as exc:
        raise ProvenanceCaptureError("Git metadata capture is unavailable") from exc
    if commit_process.returncode != 0:
        # A source archive or unborn repository has no commit to claim.  Treat
        # it conservatively as dirty while the code bundle still identifies it.
        return None, True
    commit = _normalize_git_commit(commit_process.stdout.strip())
    try:
        status_process = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=normal"],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError as exc:
        raise ProvenanceCaptureError("Git dirty-state capture is unavailable") from exc
    if status_process.returncode != 0:
        raise ProvenanceCaptureError("Git dirty-state capture failed")
    # Paths are used only to reduce the result to a boolean and are never
    # decoded, returned, logged, or included in the provenance hash.
    dirty = bool(status_process.stdout.strip())
    return commit, dirty


def _direct_requirement_names(root: Path) -> tuple[str, ...]:
    requirements = root / "requirements.txt"
    if not requirements.is_file():
        return ()
    try:
        lines = requirements.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ProvenanceCaptureError("direct dependency manifest is unreadable") from exc
    names: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "-")):
            continue
        match = _REQUIREMENT_NAME_RE.match(stripped)
        if match:
            names.add(match.group(1))
    return tuple(sorted(names, key=str.lower))


def _normalize_file_hashes(
    value: Mapping[str, str],
    kind: str,
) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ExecutionProvenanceError(f"{kind} hashes must be an object")
    result: dict[str, str] = {}
    for raw_path, digest in value.items():
        if not isinstance(raw_path, str):
            raise ExecutionProvenanceError(
                f"{kind} hash paths must be strings"
            )
        path = _normalize_logical_path(raw_path)
        if path in result:
            raise ExecutionProvenanceError(f"duplicate {kind} logical path")
        result[path] = _require_sha256(digest, f"{kind} file hash")
    return dict(sorted(result.items()))


def _normalize_logical_path(value: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ExecutionProvenanceError("logical paths must be non-empty strings")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or normalized.startswith("//")
        or re.match(r"^[A-Za-z]:", normalized)
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise UnsafeProvenanceError("absolute or escaping paths are not recordable")
    _assert_safe_public_text(normalized)
    return path.as_posix()


def _normalize_git_commit(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _GIT_COMMIT_RE.fullmatch(value.strip().lower()):
        raise ExecutionProvenanceError("Git commit must be a hexadecimal object id")
    return value.strip().lower()


def _normalize_string_mapping(
    value: Mapping[str, str],
    *,
    kind: str,
) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ExecutionProvenanceError(f"{kind} versions must be an object")
    result: dict[str, str] = {}
    for raw_name, raw_version in value.items():
        name = _public_name(raw_name, f"{kind} name")
        if name in result:
            raise ExecutionProvenanceError(f"duplicate {kind} name")
        result[name] = _public_text(raw_version, f"{kind} version")
    return dict(sorted(result.items()))


def _normalize_public_config(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ExecutionProvenanceError("config must be an object")
    result: dict[str, Any] = {}
    for raw_name, raw_value in value.items():
        name = _public_name(raw_name, "config name")
        if name in result:
            raise ExecutionProvenanceError("duplicate config name")
        _assert_safe_public_name(name)
        result[name] = _normalize_json_value(raw_value)
    return dict(sorted(result.items()))


def _normalize_feature_flags(value: Mapping[str, bool]) -> dict[str, bool]:
    if not isinstance(value, Mapping):
        raise ExecutionProvenanceError("feature flags must be an object")
    result: dict[str, bool] = {}
    for raw_name, enabled in value.items():
        name = _public_name(raw_name, "feature flag name")
        _assert_safe_public_name(name)
        if not isinstance(enabled, bool):
            raise ExecutionProvenanceError("feature flag values must be booleans")
        result[name] = enabled
    return dict(sorted(result.items()))


def _normalize_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, child in value.items():
            key = _public_name(raw_key, "config field name")
            _assert_safe_public_name(key)
            result[key] = _normalize_json_value(child)
        return dict(sorted(result.items()))
    if isinstance(value, (list, tuple)):
        return [_normalize_json_value(child) for child in value]
    if not isinstance(value, _JSON_SCALARS):
        raise ExecutionProvenanceError("config values must be JSON-compatible")
    if isinstance(value, float) and not (float("-inf") < value < float("inf")):
        raise ExecutionProvenanceError("config numbers must be finite")
    if isinstance(value, str):
        _assert_safe_public_text(value)
    return value


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(
        {key: _freeze_json(child) for key, child in sorted(value.items())}
    )


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, list):
        return tuple(_freeze_json(child) for child in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(child) for child in value]
    return value


def _public_name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExecutionProvenanceError(f"{label} must be a non-empty string")
    name = value.strip()
    _assert_safe_public_text(name)
    return name


def _public_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExecutionProvenanceError(f"{label} must be a non-empty string")
    text = value.strip()
    _assert_safe_public_text(text)
    return text


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ExecutionProvenanceError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _assert_safe_public_name(value: str) -> None:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    if _UNSAFE_FIELD_RE.search(normalized):
        raise UnsafeProvenanceError("sensitive fields are not recordable")


def _assert_safe_public_text(value: str) -> None:
    if _WINDOWS_ABSOLUTE_PATH_RE.search(value):
        raise UnsafeProvenanceError("local absolute paths are not recordable")
    if _POSIX_ABSOLUTE_PATH_RE.search(value):
        raise UnsafeProvenanceError("local absolute paths are not recordable")
    if _HOME_PATH_RE.search(value) or _FILE_URI_RE.search(value):
        raise UnsafeProvenanceError("local absolute paths are not recordable")
    if any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS):
        raise UnsafeProvenanceError("credential material is not recordable")
    if len(_ENV_ASSIGNMENT_RE.findall(value)) >= 2:
        raise UnsafeProvenanceError("environment snapshots are not recordable")


def _assert_safe_public_value(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ExecutionProvenanceError("provenance object keys must be strings")
            _assert_safe_public_name(key)
            _assert_safe_public_text(key)
            _assert_safe_public_value(child)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _assert_safe_public_value(child)
        return
    if isinstance(value, str):
        _assert_safe_public_text(value)
        return
    if not isinstance(value, _JSON_SCALARS):
        raise ExecutionProvenanceError("provenance must contain JSON-compatible values")
    if isinstance(value, float) and not (float("-inf") < value < float("inf")):
        raise ExecutionProvenanceError("provenance numbers must be finite")


def _canonical_json_bytes(value: Any) -> bytes:
    _assert_safe_public_value(value)
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ExecutionProvenanceError(
            "provenance is not canonical JSON compatible"
        ) from exc
    return rendered.encode("utf-8")


__all__ = [
    "CODE_BUNDLE_ALGORITHM",
    "EXECUTION_PROVENANCE_CANONICAL_JSON_ALGORITHM",
    "EXECUTION_PROVENANCE_SCHEMA_VERSION",
    "ExecutionProvenance",
    "ExecutionProvenanceError",
    "HASH_ALGORITHM",
    "ProvenanceCaptureError",
    "UnsafeProvenanceError",
    "build_execution_provenance",
    "canonical_provenance_hash",
    "canonical_provenance_json_bytes",
    "capture_execution_provenance",
    "collect_dependency_versions",
    "validate_execution_provenance",
]
