from __future__ import annotations

from typing import Any

__all__ = [
    "AgentExecutor",
    "DeliveryCoordinator",
    "PersistenceCoordinator",
    "PersistenceBackend",
    "LegacyV1PersistenceBackend",
    "PersistenceV2Backend",
    "PersistenceV2Target",
    "PersistenceV2Transaction",
    "QualityCoordinator",
    "RootRegistryService",
    "remap_roots",
    "StoryProjectContextService",
    "build_loop_session_record",
    "build_run_report",
    "run_loop",
    "run_once",
    "run_preflight",
    "verify_publication_receipt",
    "reconcile_pending_persistence_v2",
    "gc_persistence_v2",
]


def __getattr__(name: str) -> Any:
    if name in {"AgentExecutor", "run_loop", "run_once"}:
        from core.engine.executor import AgentExecutor, run_loop, run_once

        return {
            "AgentExecutor": AgentExecutor,
            "run_loop": run_loop,
            "run_once": run_once,
        }[name]
    if name in {
        "DeliveryCoordinator",
        "PersistenceCoordinator",
        "QualityCoordinator",
        "StoryProjectContextService",
    }:
        from core.engine.delivery_coordinator import DeliveryCoordinator
        from core.engine.persistence_coordinator import PersistenceCoordinator
        from core.engine.quality_coordinator import QualityCoordinator
        from core.engine.story_project_context import StoryProjectContextService

        return {
            "DeliveryCoordinator": DeliveryCoordinator,
            "PersistenceCoordinator": PersistenceCoordinator,
            "QualityCoordinator": QualityCoordinator,
            "StoryProjectContextService": StoryProjectContextService,
        }[name]
    if name in {
        "PersistenceBackend",
        "LegacyV1PersistenceBackend",
        "PersistenceV2Backend",
    }:
        from core.engine.persistence_backends import (
            LegacyV1PersistenceBackend,
            PersistenceBackend,
            PersistenceV2Backend,
        )

        return {
            "PersistenceBackend": PersistenceBackend,
            "LegacyV1PersistenceBackend": LegacyV1PersistenceBackend,
            "PersistenceV2Backend": PersistenceV2Backend,
        }[name]
    if name in {"RootRegistryService", "remap_roots"}:
        from core.engine.root_registry import RootRegistryService, remap_roots

        return {
            "RootRegistryService": RootRegistryService,
            "remap_roots": remap_roots,
        }[name]
    if name == "run_preflight":
        from core.engine.preflight import run_preflight

        return run_preflight
    if name == "build_run_report":
        from core.engine.report import build_run_report

        return build_run_report
    if name == "build_loop_session_record":
        from core.engine.run_record import build_loop_session_record

        return build_loop_session_record
    if name in {
        "PersistenceV2Target",
        "PersistenceV2Transaction",
        "verify_publication_receipt",
        "reconcile_pending_persistence_v2",
        "gc_persistence_v2",
    }:
        from core.engine.persistence_v2 import (
            PersistenceV2Target,
            PersistenceV2Transaction,
            gc_persistence_v2,
            reconcile_pending_persistence_v2,
            verify_publication_receipt,
        )

        return {
            "PersistenceV2Target": PersistenceV2Target,
            "PersistenceV2Transaction": PersistenceV2Transaction,
            "verify_publication_receipt": verify_publication_receipt,
            "reconcile_pending_persistence_v2": reconcile_pending_persistence_v2,
            "gc_persistence_v2": gc_persistence_v2,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
