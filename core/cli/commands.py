from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.config import get_config
from core.delivery import DeliveryError, DeliveryQueue, FileDeliveryAdapter, NotionDeliveryAdapter
from core.engine.delivery_coordinator import DeliveryCoordinator
from core.engine.root_registry import load_root_registry
from core.engine.safe_paths import RootBinding
from core.runtime_paths import RuntimePaths


def delivery_command_requested(args: argparse.Namespace) -> bool:
    requested = [
        bool(getattr(args, "reconcile_deliveries", False)),
        bool(getattr(args, "inspect_delivery", None)),
        bool(getattr(args, "resolve_delivery", None)),
    ]
    if sum(requested) > 1:
        raise ValueError("choose only one delivery command")
    if getattr(args, "confirmed_absent", False) and not getattr(args, "resolve_delivery", None):
        raise ValueError("--confirmed-absent requires --resolve-delivery JOB_ID")
    if getattr(args, "resolve_delivery", None) and not getattr(args, "confirmed_absent", False):
        raise ValueError("--resolve-delivery requires --confirmed-absent")
    if getattr(args, "run_id", None) and not getattr(args, "reconcile_deliveries", False):
        raise ValueError("--run-id is only valid with --reconcile-deliveries")
    return any(requested)


def run_delivery_command(
    args: argparse.Namespace,
    *,
    story_runtime_paths: RuntimePaths | None,
) -> dict:
    queue = DeliveryQueue(args.delivery_dir)
    if args.inspect_delivery:
        return DeliveryCoordinator(queue, adapters={}, worker_id=args.delivery_worker_id).inspect(
            args.inspect_delivery
        )
    coordinator = DeliveryCoordinator(
        queue,
        adapters=delivery_adapters_from_args(args, story_runtime_paths=story_runtime_paths),
        worker_id=args.delivery_worker_id,
    )
    if args.resolve_delivery:
        return coordinator.resolve_confirmed_absent(args.resolve_delivery)
    return coordinator.reconcile(run_id=args.run_id)


def delivery_adapters_from_args(
    args: argparse.Namespace,
    *,
    story_runtime_paths: RuntimePaths | None,
) -> dict:
    paths = story_runtime_paths or RuntimePaths.legacy_default()
    story_root = getattr(args, "_resolved_story_project_root", None) or Path.cwd()
    root_map = paths.root_map(story_root)
    root_map["delivery_store"] = Path(args.delivery_dir).resolve()
    registry_root = Path(
        getattr(args, "persistence_dir", None) or paths.persistence_dir
    ).absolute()
    registry_path = registry_root / "root_registry.json"
    root_bindings: dict[str, RootBinding] = {}
    if registry_path.exists():
        try:
            registry = load_root_registry(registry_path)
        except Exception as exc:
            raise DeliveryError(
                f"cannot load safe file-delivery root registry: {type(exc).__name__}: {exc}"
            ) from exc
        root_bindings = {
            root_id: RootBinding(
                root_id=root_id,
                root_uuid=str(binding["root_uuid"]),
                path=Path(str(binding["path"])),
            )
            for root_id, binding in registry["roots"].items()
        }
    adapters: dict = {
        "file": FileDeliveryAdapter(
            root_map=root_map,
            root_bindings=root_bindings,
        )
    }
    config = get_config()
    if config.notion_api_key and config.notion_database_id:
        database_schema = None
        if args.notion_delivery_schema:
            database_schema = json.loads(Path(args.notion_delivery_schema).read_text(encoding="utf-8"))
            if not isinstance(database_schema, dict):
                raise ValueError("--notion-delivery-schema must contain a JSON object")
        adapters["notion"] = NotionDeliveryAdapter(
            database_id=config.notion_database_id,
            api_key=config.notion_api_key,
            database_schema=database_schema,
        )
    return adapters


__all__ = ["delivery_adapters_from_args", "delivery_command_requested", "run_delivery_command"]
