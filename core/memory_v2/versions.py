from __future__ import annotations


LEGACY_REDUCER_VERSION = "memory-reducer-2.1"
CURRENT_REDUCER_VERSION = "memory-reducer-2.2"
SUPPORTED_REDUCER_VERSIONS = frozenset({LEGACY_REDUCER_VERSION, CURRENT_REDUCER_VERSION})


class UnsupportedMemoryVersionError(ValueError):
    pass


def require_supported_reducer_version(value: object) -> str:
    if not isinstance(value, str) or value not in SUPPORTED_REDUCER_VERSIONS:
        raise UnsupportedMemoryVersionError(f"unsupported memory reducer_version: {value}")
    return value


__all__ = [
    "CURRENT_REDUCER_VERSION",
    "LEGACY_REDUCER_VERSION",
    "SUPPORTED_REDUCER_VERSIONS",
    "UnsupportedMemoryVersionError",
    "require_supported_reducer_version",
]
