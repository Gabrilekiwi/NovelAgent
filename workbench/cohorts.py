from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
import hashlib
import json
import math
import statistics
import uuid

from core.config import get_config
from core.runtime_paths import RuntimePaths
from core.story_project.activation import evaluate_story_state_activation
from core.story_project.identity import load_project_identity
from core.story_project.paths import resolve_outline
from core.story_project.semantic_parser import parse_story_project_semantic_state
from workbench.database import WorkbenchDatabase
from workbench.jobs import (
    JobStore,
    verify_approved_outline_binding,
    verify_committed_chapter,
)
from workbench.projects import ProjectRegistry


SCORE_DIMENSIONS = (
    "plot_progression",
    "continuity",
    "character_consistency",
    "world_consistency",
    "prose_quality",
    "pacing",
    "dialogue",
    "ending_hook",
    "overall_usability",
)


class CohortError(RuntimeError):
    """A production-validation cohort is incomplete, stale, or invalid."""


@dataclass(frozen=True)
class CohortRecord:
    id: str
    project_id: str
    start_chapter: int
    end_chapter: int
    status: str
    weekly_budget: float
    baseline: dict[str, Any]
    session_job_id: str | None
    result: dict[str, Any] | None
    revision: int
    created_at: str
    updated_at: str
    closed_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "start_chapter": self.start_chapter,
            "end_chapter": self.end_chapter,
            "status": self.status,
            "weekly_budget": self.weekly_budget,
            "baseline": _json_copy(self.baseline),
            "session_job_id": self.session_job_id,
            "result": _json_copy(self.result),
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "closed_at": self.closed_at,
        }


@dataclass(frozen=True)
class ChapterEvaluation:
    cohort_id: str
    chapter: int
    scores: dict[str, int]
    edit_minutes: float
    actual_cost: float
    severe_defects: tuple[str, ...]
    notes: str
    revision: int
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "cohort_id": self.cohort_id,
            "chapter": self.chapter,
            "scores": dict(self.scores),
            "edit_minutes": self.edit_minutes,
            "actual_cost": self.actual_cost,
            "severe_defects": list(self.severe_defects),
            "notes": self.notes,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def build_official_launch_declaration(
    *,
    passed_count: int,
    consecutive_ten_chapter_ready: bool,
) -> dict[str, Any]:
    """Describe official launch without using the historical two-cohort gate."""

    return {
        "criterion": "author_self_serve_workbench",
        "summary": (
            "正式上线以作者能否在工作台独立完成写作、处理待办并提交正文为准，"
            "不再以连续两轮十章人工评分验收作为门槛。"
        ),
        "badge_label": "上线不以十章验收轮次为准",
        "uses_two_cohort_gate": False,
        "cohort_trials": {
            "passed_count": int(passed_count),
            "consecutive_ten_chapter_ready": bool(consecutive_ten_chapter_ready),
            "required_for_official_launch": False,
        },
    }


class CohortService:
    """Optional ten-chapter acceptance trials and their historical ledger."""

    def __init__(
        self,
        *,
        database: WorkbenchDatabase,
        projects: ProjectRegistry,
        jobs: JobStore,
        repo_root: str | Path,
    ) -> None:
        self.database = database
        self.projects = projects
        self.jobs = jobs
        self.repo_root = Path(repo_root).resolve(strict=True)
        self.database.initialize_component(self._initialize)

    def create(
        self,
        project_id: str,
        *,
        start_chapter: int,
        end_chapter: int,
        weekly_budget: float,
        generation_config: Mapping[str, Any],
    ) -> CohortRecord:
        if (
            isinstance(start_chapter, bool)
            or isinstance(end_chapter, bool)
            or not isinstance(start_chapter, int)
            or not isinstance(end_chapter, int)
            or start_chapter < 1
            or end_chapter - start_chapter + 1 != 10
        ):
            raise CohortError("a production cohort must contain exactly ten contiguous chapters")
        if isinstance(weekly_budget, bool) or not isinstance(weekly_budget, (int, float)) or weekly_budget <= 0:
            raise CohortError("weekly_budget must be a positive number")
        project = self.projects.get(project_id)
        identity = load_project_identity(project.root)
        if identity is None or identity.story_state_mode != "strict":
            raise CohortError(
                "production cohorts require an activated strict StoryProject; shadow runs remain experimental"
            )
        try:
            semantic_state = parse_story_project_semantic_state(
                project.root,
                project.next_chapter,
                project_identity=identity,
            )
            activation = evaluate_story_state_activation(identity, semantic_state)
        except Exception as exc:  # noqa: BLE001 - convert the strict preflight into a product error.
            raise CohortError(
                f"current StoryProject no longer satisfies its strict calibration: {exc}"
            ) from exc
        if not (
            activation.get("configured_mode") == "strict"
            and activation.get("effective_mode") == "strict"
            and activation.get("authoritative") is True
            and activation.get("ready_for_next_step") is True
        ):
            raise CohortError(
                "current StoryProject no longer satisfies its strict calibration"
            )
        if start_chapter != project.next_chapter:
            raise CohortError(
                f"production cohort must start at canonical next chapter {project.next_chapter}"
            )
        safe_generation_config = _safe_generation_config(generation_config)
        if safe_generation_config.get("allow_shadow") is not False:
            raise CohortError("production cohorts prohibit shadow downgrade")
        outline_binding = self._approved_outline_binding(
            project.id, project.root, start_chapter, end_chapter
        )
        baseline = build_cohort_baseline(
            repo_root=self.repo_root,
            project_root=project.root,
            start_chapter=start_chapter,
            end_chapter=end_chapter,
            generation_config=safe_generation_config,
            outline_binding=outline_binding,
        )
        cohort_id = f"cohort_{uuid.uuid4().hex}"
        timestamp = _utc_now()
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM cohorts WHERE project_id = ? AND status = 'active' LIMIT 1",
                (str(project_id),),
            ).fetchone()
            if existing is not None:
                raise CohortError(f"project already has active cohort {existing['id']}")
            connection.execute(
                """
                INSERT INTO cohorts(
                    id, project_id, start_chapter, end_chapter, status,
                    weekly_budget, baseline_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)
                """,
                (
                    cohort_id,
                    str(project_id),
                    start_chapter,
                    end_chapter,
                    float(weekly_budget),
                    _canonical_json(baseline),
                    timestamp,
                    timestamp,
                ),
            )
        return self.get(cohort_id)

    def bind_session(self, cohort_id: str, job_id: str) -> CohortRecord:
        """Bind exactly one held generation job before it can reach the worker."""

        cohort = self.get(cohort_id)
        job = self.jobs.get(job_id)
        if cohort.status != "active":
            raise CohortError("only an active cohort can bind a production session")
        if job.project_id != cohort.project_id or job.kind != "generation_session":
            raise CohortError("cohort session belongs to another project or job kind")
        if (job.start_chapter, job.end_chapter) != (
            cohort.start_chapter,
            cohort.end_chapter,
        ):
            raise CohortError("cohort session range does not match the frozen cohort")
        if job.config.get("cohort_id") != cohort.id:
            raise CohortError("generation job is not bound to this cohort")
        expected = cohort.baseline["generation_config"]
        actual = {
            "quality_policy": job.config.get("quality_policy"),
            "llm_validator": job.config.get("llm_validator"),
            "review_gate": "needs_revision",
            "review_auto_repair": True,
            "review_repair_max_attempts": 1,
            "allow_shadow": job.config.get("allow_shadow"),
        }
        if actual != expected:
            raise CohortError("generation job differs from the cohort configuration")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT session_job_id FROM cohorts WHERE id = ?", (str(cohort_id),)
            ).fetchone()
            if row is None:
                raise CohortError(f"unknown cohort: {cohort_id}")
            if row["session_job_id"] not in {None, str(job_id)}:
                raise CohortError("cohort is already bound to another production session")
            connection.execute(
                """
                UPDATE cohorts SET session_job_id = ?, updated_at = ?, revision = revision + 1
                WHERE id = ? AND status = 'active' AND session_job_id IS NULL
                """,
                (str(job_id), _utc_now(), str(cohort_id)),
            )
        return self.get(cohort_id)

    def production_ready(self, project_id: str) -> bool:
        """Return whether two contiguous closed cohorts both passed.

        This is the historical ten-chapter trial ledger, not the official
        launch declaration. Official launch is author self-serve writing
        in the workbench; see official_launch_declaration().
        """

        closed = [item for item in reversed(self.list_for_project(project_id)) if item.closed_at]
        if len(closed) < 2:
            return False
        first, second = closed[-2:]
        return (
            first.status == "passed"
            and second.status == "passed"
            and second.start_chapter == first.end_chapter + 1
        )

    def official_launch_declaration(self, project_id: str) -> dict[str, Any]:
        items = self.list_for_project(project_id)
        return build_official_launch_declaration(
            passed_count=sum(1 for item in items if item.status == "passed"),
            consecutive_ten_chapter_ready=self.production_ready(project_id),
        )

    def _approved_outline_binding(
        self,
        project_id: str,
        project_root: Path,
        start_chapter: int,
        end_chapter: int,
    ) -> dict[str, Any]:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM outline_batches
                WHERE project_id = ? AND status = 'approved'
                  AND start_chapter = ? AND end_chapter = ?
                ORDER BY approved_at DESC LIMIT 1
                """,
                (str(project_id), int(start_chapter), int(end_chapter)),
            ).fetchone()
        if row is None:
            raise CohortError("production cohort requires one exact approved ten-chapter batch")
        binding = {
            "outline_batch_id": str(row["id"]),
            "outline_manifest_path": str(row["approval_manifest_path"] or ""),
            "outline_proposal_sha256": str(row["proposal_sha256"]),
            "outline_receipt_sha256": str(row["approval_receipt_sha256"] or ""),
        }
        for chapter in range(start_chapter, end_chapter + 1):
            verification = verify_approved_outline_binding(
                project_root, chapter, binding
            )
            if not verification["ok"]:
                raise CohortError(
                    f"approved outline binding failed for chapter {chapter}: "
                    + ", ".join(verification["errors"])
                )
        return binding

    def get(self, cohort_id: str) -> CohortRecord:
        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM cohorts WHERE id = ?", (str(cohort_id),)).fetchone()
        if row is None:
            raise CohortError(f"unknown cohort: {cohort_id}")
        return _cohort_from_row(row)

    def list_for_project(self, project_id: str) -> tuple[CohortRecord, ...]:
        self.projects.get(project_id)
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM cohorts WHERE project_id = ? ORDER BY created_at DESC",
                (str(project_id),),
            ).fetchall()
        return tuple(_cohort_from_row(row) for row in rows)

    def record_evaluation(
        self,
        cohort_id: str,
        chapter: int,
        *,
        scores: Mapping[str, Any],
        edit_minutes: float,
        actual_cost: float,
        severe_defects: Sequence[str],
        notes: str = "",
        expected_revision: int = 0,
    ) -> ChapterEvaluation:
        cohort = self.get(cohort_id)
        if cohort.status != "active":
            raise CohortError("only an active cohort can accept evaluations")
        if chapter < cohort.start_chapter or chapter > cohort.end_chapter:
            raise CohortError("chapter is outside the cohort")
        checked_scores = _validate_scores(scores)
        minutes = _non_negative_number("edit_minutes", edit_minutes)
        cost = _non_negative_number("actual_cost", actual_cost)
        defects = tuple(_required_short_text("severe_defect", item, 500) for item in severe_defects)
        clean_notes = str(notes or "").strip()
        if len(clean_notes) > 10_000:
            raise CohortError("evaluation notes exceed 10000 characters")
        timestamp = _utc_now()
        with self.database.transaction() as connection:
            current = connection.execute(
                """
                SELECT * FROM chapter_evaluations
                WHERE cohort_id = ? AND chapter = ?
                """,
                (str(cohort_id), int(chapter)),
            ).fetchone()
            if current is None:
                if int(expected_revision) != 0:
                    raise CohortError("new evaluation expected_revision must be 0")
                connection.execute(
                    """
                    INSERT INTO chapter_evaluations(
                        cohort_id, chapter, scores_json, edit_minutes, actual_cost,
                        severe_defects_json, notes, revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        str(cohort_id),
                        int(chapter),
                        _canonical_json(checked_scores),
                        minutes,
                        cost,
                        _canonical_json(list(defects)),
                        clean_notes,
                        timestamp,
                        timestamp,
                    ),
                )
            else:
                if int(current["revision"]) != int(expected_revision):
                    raise CohortError(
                        f"evaluation revision conflict: current {current['revision']}"
                    )
                updated = connection.execute(
                    """
                    UPDATE chapter_evaluations SET scores_json = ?, edit_minutes = ?,
                        actual_cost = ?, severe_defects_json = ?, notes = ?,
                        revision = revision + 1, updated_at = ?
                    WHERE cohort_id = ? AND chapter = ? AND revision = ?
                    """,
                    (
                        _canonical_json(checked_scores),
                        minutes,
                        cost,
                        _canonical_json(list(defects)),
                        clean_notes,
                        timestamp,
                        str(cohort_id),
                        int(chapter),
                        int(expected_revision),
                    ),
                )
                if updated.rowcount != 1:
                    raise CohortError("evaluation changed concurrently")
        return self.get_evaluation(cohort_id, chapter)

    def get_evaluation(self, cohort_id: str, chapter: int) -> ChapterEvaluation:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM chapter_evaluations WHERE cohort_id = ? AND chapter = ?",
                (str(cohort_id), int(chapter)),
            ).fetchone()
        if row is None:
            raise CohortError(f"chapter {chapter} has no evaluation in {cohort_id}")
        return _evaluation_from_row(row)

    def evaluations(self, cohort_id: str) -> tuple[ChapterEvaluation, ...]:
        self.get(cohort_id)
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM chapter_evaluations WHERE cohort_id = ?
                ORDER BY chapter
                """,
                (str(cohort_id),),
            ).fetchall()
        return tuple(_evaluation_from_row(row) for row in rows)

    def evaluate(self, cohort_id: str) -> dict[str, Any]:
        cohort = self.get(cohort_id)
        project = self.projects.get(cohort.project_id)
        current_baseline = build_cohort_baseline(
            repo_root=self.repo_root,
            project_root=project.root,
            start_chapter=cohort.start_chapter,
            end_chapter=cohort.end_chapter,
            generation_config=cohort.baseline["generation_config"],
            outline_binding=cohort.baseline.get("outline_batch") or {},
        )
        drift = _baseline_differences(cohort.baseline, current_baseline)
        outline_binding_errors: list[dict[str, Any]] = []
        frozen_binding = cohort.baseline.get("outline_batch")
        if isinstance(frozen_binding, Mapping) and frozen_binding:
            for chapter in range(cohort.start_chapter, cohort.end_chapter + 1):
                verification = verify_approved_outline_binding(
                    project.root, chapter, frozen_binding
                )
                if not verification["ok"]:
                    outline_binding_errors.append(
                        {
                            "chapter": chapter,
                            "errors": list(verification["errors"]),
                        }
                    )
        else:
            outline_binding_errors.append(
                {"chapter": None, "errors": ["approved_outline_binding_missing"]}
            )
        if outline_binding_errors and "approved_outline_binding_invalid" not in drift:
            drift.append("approved_outline_binding_invalid")
        evaluations = self.evaluations(cohort.id)
        by_chapter = {item.chapter: item for item in evaluations}
        run_results, run_ids = _cohort_run_results(
            self.database,
            project.root,
            session_job_id=cohort.session_job_id,
            start_chapter=cohort.start_chapter,
            end_chapter=cohort.end_chapter,
        )
        job_findings = _cohort_job_findings(
            self.database,
            session_job_id=cohort.session_job_id,
            start_chapter=cohort.start_chapter,
            end_chapter=cohort.end_chapter,
        )
        committed: dict[int, bool] = {}
        repair_counts: dict[int, int | None] = {}
        strict_chapters: list[int] = []
        uncertain_chapters = list(job_findings["uncertain_chapters"])
        internal_error_chapters = list(job_findings["internal_error_chapters"])
        for chapter, result in run_results.items():
            if result is None:
                committed[chapter] = False
                repair_counts[chapter] = None
                continue
            verification = verify_committed_chapter(project.root, chapter, result)
            committed[chapter] = bool(verification["ok"])
            run = result.get("run") if isinstance(result.get("run"), dict) else {}
            repairs = result.get("repair_attempts", run.get("repair_attempts"))
            repair_counts[chapter] = int(repairs) if isinstance(repairs, int) else None
            if _result_used_strict_story_state(result):
                strict_chapters.append(chapter)
            if run.get("status") == "failed" or isinstance(run.get("error"), dict):
                internal_error_chapters.append(chapter)
        uncertain_chapters = sorted(set(uncertain_chapters))
        internal_error_chapters = sorted(set(internal_error_chapters))

        scores = [score for item in evaluations for score in item.scores.values()]
        overall_usable = sum(
            1 for item in evaluations if item.scores["overall_usability"] >= 4
        )
        edit_times = [item.edit_minutes for item in evaluations]
        severe = [
            {"chapter": item.chapter, "defects": list(item.severe_defects)}
            for item in evaluations
            if item.severe_defects
        ]
        first_chapters = range(cohort.start_chapter, cohort.start_chapter + 3)
        last_chapters = range(cohort.end_chapter - 2, cohort.end_chapter + 1)
        first_average = _chapter_score_average(by_chapter, first_chapters)
        last_average = _chapter_score_average(by_chapter, last_chapters)
        quality_drop = (
            round(first_average - last_average, 4)
            if first_average is not None and last_average is not None
            else None
        )
        manual_chapters = _manual_intervention_chapters(
            self.database,
            start_chapter=cohort.start_chapter,
            end_chapter=cohort.end_chapter,
            session_job_id=cohort.session_job_id,
        )
        total_cost = round(sum(item.actual_cost for item in evaluations), 6)
        completed_count = sum(committed.values())
        known_repairs = [value for value in repair_counts.values() if value is not None]
        first_pass_rate = (
            sum(1 for value in known_repairs if value == 0) / 10
            if len(known_repairs) == 10
            else 0.0
        )
        criteria = {
            "baseline_unchanged": not drift,
            "ten_of_ten_committed_with_receipt": completed_count == 10,
            "all_chapters_used_strict_state": len(strict_chapters) == 10,
            "no_uncertain_or_internal_error": not uncertain_chapters and not internal_error_chapters,
            "no_severe_continuity_defect": not severe,
            "first_pass_rate_at_least_70_percent": first_pass_rate >= 0.7,
            "automatic_repair_at_most_once_each": len(known_repairs) == 10
            and all(value <= 1 for value in known_repairs),
            "all_ten_human_evaluations_present": len(evaluations) == 10,
            "human_score_mean_at_least_3_8": bool(scores) and statistics.fmean(scores) >= 3.8,
            "eight_chapters_overall_usability_at_least_4": overall_usable >= 8,
            "median_edit_minutes_at_most_20": len(edit_times) == 10
            and statistics.median(edit_times) <= 20,
            "p90_edit_minutes_at_most_30": len(edit_times) == 10
            and _nearest_rank_percentile(edit_times, 0.9) <= 30,
            "manual_intervention_at_most_two_chapters": len(manual_chapters) <= 2,
            "late_quality_drop_at_most_0_3": quality_drop is not None and quality_drop <= 0.3,
            "weekly_budget_not_exceeded": len(evaluations) == 10 and total_cost <= cohort.weekly_budget,
        }
        return {
            "cohort_id": cohort.id,
            "status": cohort.status,
            "passed": all(criteria.values()),
            "invalidated": bool(drift),
            "invalidation_reasons": drift,
            "criteria": criteria,
            "metrics": {
                "committed_chapters": completed_count,
                "evaluated_chapters": len(evaluations),
                "first_pass_rate": round(first_pass_rate, 4),
                "mean_human_score": round(statistics.fmean(scores), 4) if scores else None,
                "overall_usability_ge_4": overall_usable,
                "median_edit_minutes": statistics.median(edit_times) if edit_times else None,
                "p90_edit_minutes": _nearest_rank_percentile(edit_times, 0.9) if edit_times else None,
                "manual_intervention_chapters": manual_chapters,
                "quality_first_three": first_average,
                "quality_last_three": last_average,
                "quality_drop": quality_drop,
                "actual_cost": total_cost,
                "weekly_budget": cohort.weekly_budget,
                "uncertain_chapters": uncertain_chapters,
                "internal_error_chapters": internal_error_chapters,
                "severe_defects": severe,
                "repair_attempts": repair_counts,
                "strict_chapters": strict_chapters,
                "run_ids": run_ids,
                "outline_binding_errors": outline_binding_errors,
            },
            "evaluations": [item.to_dict() for item in evaluations],
        }

    def close(self, cohort_id: str, *, expected_revision: int) -> CohortRecord:
        cohort = self.get(cohort_id)
        if cohort.status != "active":
            return cohort
        if cohort.revision != int(expected_revision):
            raise CohortError("cohort revision conflict")
        result = self.evaluate(cohort_id)
        status = "invalidated" if result["invalidated"] else "passed" if result["passed"] else "failed"
        timestamp = _utc_now()
        with self.database.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE cohorts SET status = ?, result_json = ?, revision = revision + 1,
                    updated_at = ?, closed_at = ? WHERE id = ? AND status = 'active' AND revision = ?
                """,
                (
                    status,
                    _canonical_json(result),
                    timestamp,
                    timestamp,
                    str(cohort_id),
                    int(expected_revision),
                ),
            )
            if updated.rowcount != 1:
                raise CohortError("cohort changed concurrently")
        return self.get(cohort_id)

    def _initialize(self) -> None:
        with self.database.transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cohorts (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    start_chapter INTEGER NOT NULL,
                    end_chapter INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    weekly_budget REAL NOT NULL,
                    baseline_json TEXT NOT NULL,
                    session_job_id TEXT,
                    result_json TEXT,
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    closed_at TEXT,
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_cohorts_project
                ON cohorts(project_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS chapter_evaluations (
                    cohort_id TEXT NOT NULL,
                    chapter INTEGER NOT NULL,
                    scores_json TEXT NOT NULL,
                    edit_minutes REAL NOT NULL,
                    actual_cost REAL NOT NULL,
                    severe_defects_json TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (cohort_id, chapter),
                    FOREIGN KEY (cohort_id) REFERENCES cohorts(id) ON DELETE CASCADE
                );
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(cohorts)").fetchall()
            }
            if "session_job_id" not in columns:
                connection.execute("ALTER TABLE cohorts ADD COLUMN session_job_id TEXT")


def build_cohort_baseline(
    *,
    repo_root: str | Path,
    project_root: str | Path,
    start_chapter: int,
    end_chapter: int,
    generation_config: Mapping[str, Any],
    outline_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(project_root).resolve(strict=True)
    outlines: list[dict[str, Any]] = []
    for chapter in range(start_chapter, end_chapter + 1):
        resolution = resolve_outline(root, chapter)
        if resolution.conflict or resolution.path is None:
            raise CohortError(f"chapter {chapter} does not have one unique frozen outline")
        outlines.append(
            {
                "chapter": chapter,
                "path": resolution.path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(resolution.path.read_bytes()).hexdigest(),
            }
        )
    config = get_config()
    identity = load_project_identity(root)
    activation = identity.activation if identity is not None else None
    provider = {
        "openai_model": config.openai_model,
        "openai_base_url_sha256": _secret_safe_fingerprint(config.openai_base_url),
        "openai_max_output_tokens": config.openai_max_output_tokens,
        "openai_validation_max_output_tokens": config.openai_validation_max_output_tokens,
        "openai_revalidation_max_output_tokens": config.openai_revalidation_max_output_tokens,
        "openai_stream": config.openai_stream,
        "claude_model": config.claude_model,
        "claude_base_url_sha256": _secret_safe_fingerprint(config.claude_base_url),
        "claude_max_tokens": config.claude_max_tokens,
        "claude_stream": config.claude_stream,
        "provider_max_attempts": config.provider_max_attempts,
        "provider_retry_deadline_seconds": config.provider_retry_deadline_seconds,
    }
    safe_generation_config = _safe_generation_config(generation_config)
    return {
        "schema_version": "1.0",
        "code_fingerprint": _code_fingerprint(Path(repo_root)),
        "book_id": identity.book_id if identity else None,
        "story_state_mode": identity.story_state_mode if identity else "missing",
        "activation_profile": {
            key: activation.get(key)
            for key in (
                "parser_version",
                "semantic_schema_version",
                "layout_profile_version",
                "calibration_report_sha256",
            )
            if isinstance(activation, dict) and key in activation
        },
        "provider": provider,
        "generation_config": safe_generation_config,
        "outline_batch": _json_copy(dict(outline_binding or {})),
        "outlines": outlines,
    }


def _baseline_differences(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    for field, label in (
        ("code_fingerprint", "code_changed"),
        ("book_id", "project_identity_changed"),
        ("story_state_mode", "story_state_mode_changed"),
        ("activation_profile", "activation_profile_changed"),
        ("provider", "provider_or_model_changed"),
        ("generation_config", "generation_config_changed"),
        ("outline_batch", "approved_outline_binding_changed"),
        ("outlines", "outline_changed"),
    ):
        if before.get(field) != after.get(field):
            reasons.append(label)
    return reasons


def _code_fingerprint(root: Path) -> str:
    included_roots = (
        "api",
        "core",
        "modules",
        "prompts",
        "rules",
        "schemas",
        "workbench",
        ".github",
    )
    files: list[Path] = []
    for name in included_roots:
        base = root / name
        if base.is_dir():
            files.extend(
                path
                for path in base.rglob("*")
                if path.is_file()
                and "__pycache__" not in path.parts
                and path.suffix.casefold() not in {".pyc", ".db", ".log"}
            )
    files.extend(
        path
        for name in ("main.py", "requirements.txt", "requirements-dev.txt")
        for path in [root / name]
        if path.is_file()
    )
    digest = hashlib.sha256()
    for path in sorted(set(files), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        raw = path.read_bytes()
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.hexdigest()


def _cohort_run_results(
    database: WorkbenchDatabase,
    root: Path,
    *,
    session_job_id: str | None,
    start_chapter: int,
    end_chapter: int,
) -> tuple[dict[int, dict[str, Any] | None], dict[int, str]]:
    results: dict[int, dict[str, Any] | None] = {
        chapter: None for chapter in range(start_chapter, end_chapter + 1)
    }
    run_ids: dict[int, str] = {}
    if not session_job_id:
        return results, run_ids
    with database.connection() as connection:
        rows = connection.execute(
            """
            SELECT payload_json FROM job_events
            WHERE job_id = ? AND event_type = 'chapter.committed'
            ORDER BY sequence
            """,
            (str(session_job_id),),
        ).fetchall()
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError):
            continue
        chapter = payload.get("chapter")
        run_id = payload.get("run_id")
        if (
            isinstance(chapter, int)
            and start_chapter <= chapter <= end_chapter
            and isinstance(run_id, str)
            and run_id
        ):
            run_ids[chapter] = run_id
    for chapter, run_id in run_ids.items():
        results[chapter] = _result_for_run_id(root, run_id, expected_chapter=chapter)
    return results, run_ids


def _result_for_run_id(
    root: Path, run_id: str, *, expected_chapter: int
) -> dict[str, Any] | None:
    run_dir = RuntimePaths.for_story_project(root).run_dir
    if not run_dir.is_dir():
        return None
    for path in run_dir.glob("chapter_*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            run = value.get("run") if isinstance(value, dict) else None
            if (
                isinstance(run, dict)
                and run.get("id") == run_id
                and int(run.get("chapter_index") or 0) == expected_chapter
            ):
                return value
        except (OSError, ValueError, TypeError):
            continue
    return None


def _cohort_job_ids(
    database: WorkbenchDatabase, session_job_id: str | None
) -> tuple[str, ...]:
    if not session_job_id:
        return ()
    identifiers = {str(session_job_id)}
    with database.connection() as connection:
        rows = connection.execute(
            "SELECT id, config_json FROM jobs WHERE kind = 'locked_chapter_recovery'"
        ).fetchall()
    for row in rows:
        try:
            config = json.loads(str(row["config_json"]))
        except (TypeError, ValueError):
            continue
        if config.get("parent_job_id") == str(session_job_id):
            identifiers.add(str(row["id"]))
    return tuple(sorted(identifiers))


def _cohort_job_findings(
    database: WorkbenchDatabase,
    *,
    session_job_id: str | None,
    start_chapter: int,
    end_chapter: int,
) -> dict[str, list[int]]:
    job_ids = _cohort_job_ids(database, session_job_id)
    if not job_ids:
        return {"uncertain_chapters": [], "internal_error_chapters": []}
    placeholders = ",".join("?" for _ in job_ids)
    with database.connection() as connection:
        rows = connection.execute(
            f"""
            SELECT event.event_type, event.payload_json, job.next_chapter
            FROM job_events AS event
            JOIN jobs AS job ON job.id = event.job_id
            WHERE event.job_id IN ({placeholders})
            """,
            job_ids,
        ).fetchall()
    uncertain: set[int] = set()
    internal: set[int] = set()
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError):
            payload = {}
        chapter = payload.get("chapter")
        if not isinstance(chapter, int):
            try:
                chapter = int(row["next_chapter"])
            except (TypeError, ValueError):
                chapter = None
        if not isinstance(chapter, int) or not start_chapter <= chapter <= end_chapter:
            continue
        event_type = str(row["event_type"])
        if event_type in {"chapter.needs_recovery", "job.cancelled_provider_uncertain"}:
            uncertain.add(chapter)
        error = payload.get("error") if isinstance(payload.get("error"), dict) else payload
        code = str(error.get("code") or "") if isinstance(error, dict) else ""
        if event_type == "job.failed" or code in {
            "workbench_worker_error",
            "engine_result_unreadable",
            "committed_chapter_state_head_unreadable",
        }:
            internal.add(chapter)
    return {
        "uncertain_chapters": sorted(uncertain),
        "internal_error_chapters": sorted(internal),
    }


def _result_used_strict_story_state(result: Mapping[str, Any]) -> bool:
    run = result.get("run") if isinstance(result.get("run"), Mapping) else {}
    story = run.get("story_project") if isinstance(run.get("story_project"), Mapping) else {}
    semantic = story.get("semantic_state") if isinstance(story.get("semantic_state"), Mapping) else {}
    identity = story.get("project_identity") if isinstance(story.get("project_identity"), Mapping) else {}
    return (
        story.get("mode") == "strict"
        and semantic.get("configured_mode") == "strict"
        and semantic.get("effective_mode") == "strict"
        and semantic.get("downgraded") is not True
        and identity.get("story_state_mode") == "strict"
    )


def _manual_intervention_chapters(
    database: WorkbenchDatabase,
    *,
    start_chapter: int,
    end_chapter: int,
    session_job_id: str | None,
) -> list[int]:
    job_ids = _cohort_job_ids(database, session_job_id)
    if not job_ids:
        return []
    placeholders = ",".join("?" for _ in job_ids)
    with database.connection() as connection:
        rows = connection.execute(
            f"""
            SELECT e.payload_json FROM job_events e
            WHERE e.job_id IN ({placeholders})
              AND e.event_type IN (
                'chapter.needs_revision', 'chapter.needs_recovery',
                'job.cancelled_provider_uncertain', 'draft.validation_stale'
              )
            """,
            job_ids,
        ).fetchall()
    chapters: set[int] = set()
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"]))
            chapter = payload.get("chapter")
            if isinstance(chapter, int) and start_chapter <= chapter <= end_chapter:
                chapters.add(chapter)
        except (TypeError, ValueError):
            continue
    return sorted(chapters)


def _chapter_score_average(
    evaluations: Mapping[int, ChapterEvaluation], chapters: Sequence[int] | range
) -> float | None:
    items = [evaluations[chapter] for chapter in chapters if chapter in evaluations]
    if len(items) != len(list(chapters)):
        return None
    return round(statistics.fmean(score for item in items for score in item.scores.values()), 4)


def _nearest_rank_percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("percentile requires at least one value")
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def _validate_scores(value: Mapping[str, Any]) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != set(SCORE_DIMENSIONS):
        raise CohortError("scores must contain exactly the nine production dimensions")
    result: dict[str, int] = {}
    for dimension in SCORE_DIMENSIONS:
        score = value[dimension]
        if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
            raise CohortError(f"score {dimension} must be an integer from 1 to 5")
        result[dimension] = score
    return result


def _safe_generation_config(value: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "quality_policy",
        "llm_validator",
        "review_gate",
        "review_auto_repair",
        "review_repair_max_attempts",
        "allow_shadow",
    }
    if not isinstance(value, Mapping) or set(value) != allowed:
        raise CohortError("generation_config must contain exactly the frozen production fields")
    result = json.loads(json.dumps(dict(value), ensure_ascii=False, allow_nan=False, sort_keys=True))
    expected = {
        "quality_policy": "strict",
        "llm_validator": True,
        "review_gate": "needs_revision",
        "review_auto_repair": True,
        "review_repair_max_attempts": 1,
        "allow_shadow": False,
    }
    if result != expected:
        raise CohortError("production cohorts require the strict fixed generation policy")
    return result


def _secret_safe_fingerprint(value: Any) -> str | None:
    text = str(value or "").strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None


def _non_negative_number(field: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or not math.isfinite(value):
        raise CohortError(f"{field} must be a finite non-negative number")
    return float(value)


def _required_short_text(field: str, value: Any, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CohortError(f"{field} must be non-empty text")
    text = value.strip()
    if len(text) > maximum:
        raise CohortError(f"{field} is too long")
    return text


def _cohort_from_row(row: Any) -> CohortRecord:
    return CohortRecord(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        start_chapter=int(row["start_chapter"]),
        end_chapter=int(row["end_chapter"]),
        status=str(row["status"]),
        weekly_budget=float(row["weekly_budget"]),
        baseline=json.loads(str(row["baseline_json"])),
        session_job_id=str(row["session_job_id"]) if row["session_job_id"] else None,
        result=json.loads(str(row["result_json"])) if row["result_json"] else None,
        revision=int(row["revision"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        closed_at=str(row["closed_at"]) if row["closed_at"] else None,
    )


def _evaluation_from_row(row: Any) -> ChapterEvaluation:
    return ChapterEvaluation(
        cohort_id=str(row["cohort_id"]),
        chapter=int(row["chapter"]),
        scores=json.loads(str(row["scores_json"])),
        edit_minutes=float(row["edit_minutes"]),
        actual_cost=float(row["actual_cost"]),
        severe_defects=tuple(json.loads(str(row["severe_defects_json"]))),
        notes=str(row["notes"]),
        revision=int(row["revision"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _json_copy(value: Any) -> Any:
    if value is None:
        return None
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "SCORE_DIMENSIONS",
    "ChapterEvaluation",
    "CohortError",
    "CohortRecord",
    "CohortService",
    "build_cohort_baseline",
]
