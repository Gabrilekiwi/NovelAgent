"""Compose archive evidence, preserving the distinction from writable recovery.

Every check reads the same completed package. No caller-supplied check results,
source directories, runtime constructors or prepared execution plans are used.
"""
import json
from pathlib import Path

from core.story_project.identity import validate_project_identity
from core.story_project.reviewed_identity import validate_reviewed_identity
from core.story_project.reviewed_state_document import digest
from workbench.backup_archive import BackupArchiveError
from workbench.backup_archive_auxiliary import inspect_archived_auxiliary
from workbench.backup_archive_database import inspect_archived_database
from workbench.backup_archive_executions import inspect_archived_executions
from workbench.backup_archive_history import verify_archived_persistence_history
from workbench.backup_archive_jobs import inspect_archived_jobs
from workbench.backup_archive_legacy_state import inspect_archived_legacy_state
from workbench.backup_archive_references import inspect_archived_references
from workbench.backup_archive_reviewed import verify_archived_reviewed_state
from workbench.backup_archive_sources import inspect_archived_sources
from workbench.backup_archive_usage import inspect_archived_usage
from workbench.backup_archive_view import verified_archive_view
from workbench.backup_version import fingerprint_software


PROTOCOL = "target_archive_data_verification_v1"


def _implementation():
    # Explicit verifier installation, independent of the archive's source build.
    return fingerprint_software(Path(__file__).resolve().parents[1])


def verify_archived_target(package, *, project_id, book_id, temporary_root=None):
    """Return repeatable data evidence and the remaining recovery requirements."""
    implementation = _implementation()
    with verified_archive_view(package) as view:
        verified = view.verified
        manifest = view.manifest
        preview = manifest["preview"]
        scope = preview["scope"]
        if preview["project_id"] != project_id or preview["book_id"] != book_id:
            raise BackupArchiveError("离线汇总目标与归档不同。")
        blockers, continuation, checks = [], [], {}

        def block(check, code, reference="story_project"):
            item = {"check": check, "code": code, "reference": reference}
            if item not in blockers:
                blockers.append(item)

        # A failed source boundary cannot be erased by subsequently passing a
        # subset of offline checks. Diagnostic archives retain their blockers.
        for issue in scope["blockers"]:
            block("source_scope", issue["code"], issue.get("reference", "story_project"))
        if scope["scope_checks_passed"] is not True:
            block("source_scope", "source_scope_not_verified")
        if preview["runtime_config"]["job_execution_contract"] != "workbench_execution_v1":
            block("compatibility", "unsupported_job_execution_contract")

        identity_raw = view.read_root("story_project", ".novelagent/project.json")
        identity = None
        identity_version = None
        try:
            value = json.loads(identity_raw)
            identity_version = value.get("schema_version")
            identity = (validate_reviewed_identity(value) if identity_version == "3.0"
                        else validate_project_identity(value))
            if identity.book_id != book_id:
                raise ValueError("book identity differs")
            authority = identity.authority
            if (identity.ephemeral or identity.story_state_mode != "strict"
                    or authority["mode"] != "legacy_markdown_v1"
                    or authority["minimum_writer_contract"] > 2):
                block("identity", "unsupported_identity_execution_contract")
            tree = scope["inventories"]["story_project"]
            receipts = ".novelagent/authority/receipts"
            # The normal legacy reader rejects every receipt entry, including
            # incomplete activation. Empty directories do not imply activation.
            if (receipts in tree["files"] or any(name.startswith(receipts + "/")
                    for name in set(tree["files"]) | set(tree["directories"]))):
                block("identity", "legacy_writer_forbidden_after_event_receipt", receipts)
        except (ValueError, KeyError, TypeError, AttributeError):
            block("identity", "archived_identity_unverified")

        def run(name, checker, passed_field, *, database=False):
            args = {"project_id": project_id, "book_id": book_id}
            if database:
                args["temporary_root"] = temporary_root
            try:
                result = checker(package, **args)
                if (result["archive_id"] != verified["archive_id"]
                        or result["archive_receipt_sha256"] != verified["receipt_sha256"]
                        or result["project_id"] != project_id or result["book_id"] != book_id):
                    raise BackupArchiveError("离线组件核验了不同的归档或目标。")
                checks[name] = result
                for issue in result.get("blockers", []):
                    block(name, issue["code"], issue.get("reference", "story_project"))
                if result[passed_field] is not True:
                    block(name, "component_not_verified")
                for issue in result.get("continuation_blockers", []):
                    continuation.append({"check": name, **issue})
            except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                # Do not expose arbitrary SQLite contents, source paths or
                # exception messages in a summary of a damaged archive.
                block(name, "component_evidence_unverified")

        run("database", inspect_archived_database, "database_verified", database=True)
        run("persistence", verify_archived_persistence_history, "history_verified")
        run("references", inspect_archived_references, "references_verified", database=True)
        run("auxiliary", inspect_archived_auxiliary, "auxiliary_settled")
        run("usage", inspect_archived_usage, "model_and_budgets_verified")
        run("executions", inspect_archived_executions, "executions_verified")
        run("jobs", inspect_archived_jobs, "jobs_verified", database=True)
        if identity is not None and identity_version == "3.0":
            run("reviewed_state", verify_archived_reviewed_state, "reviewed_state_verified")
            run("sources", inspect_archived_sources, "source_snapshot_verified")
        elif identity is not None:
            run("legacy_state", inspect_archived_legacy_state, "legacy_state_verified")

        database = checks.get("database")
        if database:
            for table in database["unclassified_tables"]:
                block("database", "unclassified_database_table", table)
        if _implementation() != implementation:
            raise BackupArchiveError("离线核验期间验证器磁盘版本发生变化。")

        result = {
            "schema_version": PROTOCOL,
            "archive_id": verified["archive_id"],
            "archive_receipt_sha256": verified["receipt_sha256"],
            "archive_binding": {
                "manifest_sha256": manifest["manifest_sha256"],
                "preview_sha256": preview["preview_sha256"],
                "scope_sha256": scope["scope_sha256"],
                "database_logical_sha256": manifest["database"]["logical_sha256"],
                "source_software_sha256": preview["software"]["fingerprint_sha256"],
                "source_config_sha256": digest(preview["runtime_config"]),
                "archive_purpose": verified["archive_purpose"],
                "original_recoverable": verified["recoverable"],
            },
            "verifier": {
                "protocol": PROTOCOL,
                "implementation_sha256": implementation["fingerprint_sha256"],
                "implementation_scope": implementation["scope"],
                "loaded_modules_match_proven": False,
            },
            "project_id": project_id,
            "book_id": book_id,
            "identity": identity.to_dict() if identity is not None else None,
            "identity_sha256": digest(identity_raw),
            "checks": checks,
            "blockers": sorted(blockers, key=lambda x: (x["check"], x["code"], x["reference"])),
            "continuation_blockers": sorted(continuation, key=lambda x: (x["check"], x["code"], x["reference"])),
            "offline_data_verified": not blockers,
            "database_policy": database["restore_policy"] if database else None,
            "execution_policy": {
                "start_worker": False, "auto_import": False, "provider_calls_allowed": False,
                "replay_prepared_plans": False, "resume_jobs_automatically": False,
                "require_fresh_preparation": True, "write_activation_allowed": False,
            },
            "ownership_requirement": {"archived_evidence": scope.get("machine_ownership"),
                                      "current_machine_handoff_required": True},
            "restore_plan_ready": False,
            "recoverable": False,
            "write_activation_allowed": False,
        }
        result["verification_sha256"] = digest(result)
    return result
