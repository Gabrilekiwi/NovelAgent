from __future__ import annotations


def format_loop_progress_event(event: dict) -> str:
    name = event.get("event")
    if name == "loop_start":
        return f"Loop progress: starting {event.get('requested_steps')} steps"
    if name == "step_start":
        return f"Loop progress: step {event.get('step')}/{event.get('requested_steps')} started"
    if name == "step_end":
        return (
            f"Loop progress: step {event.get('step')}/{event.get('requested_steps')} "
            f"{event.get('status')} committed={str(bool(event.get('committed'))).lower()} "
            f"duration_ms={event.get('duration_ms')} run={event.get('run_id')}"
        )
    if name == "step_failed":
        return (
            f"Loop progress: step {event.get('step')}/{event.get('requested_steps')} failed "
            f"duration_ms={event.get('duration_ms')} error={event.get('error_type')}: {event.get('message')}"
        )
    if name == "loop_end":
        return (
            f"Loop progress: finished {event.get('completed_steps')}/{event.get('requested_steps')} "
            f"reason={event.get('stopped_reason')}"
        )
    return ""


def format_delivery_command_summary(result: dict) -> str:
    command = result.get("command") or "delivery"
    if command == "reconcile_deliveries":
        return "\n".join(
            [
                f"Delivery reconcile: {'OK' if result.get('ok') else 'BLOCKED'}",
                f"Attempted: {result.get('attempted', 0)}",
                f"Required deliveries succeeded: {bool(result.get('required_succeeded'))}",
            ]
        )
    job = result.get("job") or result.get("inspection", {}).get("job") or {}
    return "\n".join(
        [
            f"Delivery command: {command}",
            f"Job: {job.get('job_id', '-')}",
            f"State: {job.get('state', '-')}",
        ]
    )


def format_persistence_reconcile_summary(result: dict) -> str:
    return "\n".join(
        [
            f"Persistence reconcile: {'OK' if result.get('ok') else 'FAILED'}",
            f"Transactions: {result.get('transaction_count', 0)}",
            f"Published runs: {len(result.get('published_run_ids') or [])}",
            f"Recovery required: {len(result.get('recovery_required') or [])}",
            f"Publish errors: {len(result.get('publish_errors') or [])}",
        ]
    )


def format_locked_chapter_recovery_summary(result: dict) -> str:
    if not result.get("ok"):
        error = result.get("error") if isinstance(result.get("error"), dict) else {}
        return f"Locked chapter recovery failed: {error.get('message') or 'unknown error'}"

    status = str(result.get("status") or "recovered")
    action = str(result.get("action") or "none")
    chapter = result.get("chapter_index")
    if status == "not_locked":
        return f"Locked chapter recovery: NOT NEEDED\n- chapter: {chapter}\n- result: no locked execution found"

    action_labels = {
        "repair_draft": "reuse the complete draft and enter validation/repair",
        "resume_scenes": "keep the trustworthy scenes and generate only the missing scenes",
        "reset": "discard the failed chapter attempt and start the chapter fresh",
    }
    lines = [
        f"Locked chapter recovery: {'ALREADY READY' if status == 'already_recovered' else 'READY'}",
        f"- chapter: {chapter}",
        f"- decision: {action_labels.get(action, action)}",
    ]
    if action == "resume_scenes":
        lines.extend(
            [
                f"- reusable scenes: {result.get('reusable_scene_count', 0)}/{result.get('expected_scene_count', 0)}",
                f"- next scene: {result.get('next_scene_index')}",
            ]
        )
    lines.extend(
        [
            "- model calls made: 0",
            "- next step: rerun the original chapter-generation command",
            f"- checkpoint: {result.get('checkpoint_path') or '-'}",
        ]
    )
    return "\n".join(lines)


__all__ = [
    "format_delivery_command_summary",
    "format_locked_chapter_recovery_summary",
    "format_loop_progress_event",
    "format_persistence_reconcile_summary",
]
