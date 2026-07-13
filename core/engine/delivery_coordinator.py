from __future__ import annotations

from typing import Any, Mapping

from core.delivery import DeliveryError, DeliveryQueue


class DeliveryCoordinator:
    """Routes explicit inspect, resolve, and reconcile operations through one queue facade."""

    def __init__(self, queue: DeliveryQueue, *, adapters: Mapping[str, Any], worker_id: str) -> None:
        self.queue = queue
        self.adapters = dict(adapters)
        self.worker_id = worker_id

    def inspect(self, job_id: str) -> dict[str, Any]:
        return {"ok": True, "command": "inspect_delivery", "inspection": self.queue.inspect(job_id)}

    def resolve_confirmed_absent(self, job_id: str) -> dict[str, Any]:
        job = self.queue.load(job_id)
        if job["target_type"] != "notion":
            raise DeliveryError("--confirmed-absent resolution is only valid for Notion delivery")
        adapter = self.adapters.get("notion")
        if adapter is None:
            raise DeliveryError("Notion delivery credentials are not configured")
        resolved = self.queue.resolve_confirmed_absent(job_id, worker_id=self.worker_id, adapter=adapter)
        return {"ok": True, "command": "resolve_delivery", "job": resolved}

    def reconcile(self, *, run_id: str | None = None) -> dict[str, Any]:
        report = self.queue.reconcile(adapters=self.adapters, worker_id=self.worker_id, run_id=run_id)
        return {
            "ok": bool(report["required_succeeded"]),
            "command": "reconcile_deliveries",
            **report,
        }


__all__ = ["DeliveryCoordinator"]
