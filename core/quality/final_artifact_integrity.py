from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import json
import re
from typing import Any, Iterable

from core.schema import validate_schema


FINAL_ARTIFACT_GATE_VERSION = "2.0.1"
FINAL_ARTIFACT_SCHEMA_VERSION = "1.0"
_SAFE_CANONICALIZATION_POLICY = "rstrip_terminal_whitespace_then_single_newline_v1"
_BLOCKING = "blocking"
_WARNING = "warning"
_PARAGRAPH_SEPARATOR_RE = re.compile(r"\n[ \t]*\n+")
_TITLE_RE = re.compile(
    r"^(?:第[零一二三四五六七八九十百千0-9]+章(?:\s+.*)?|chapter\s+\d+(?:\s+.*)?)$",
    flags=re.IGNORECASE,
)
_DIALOGUE_RE = re.compile(r"^[“\"「『]")
_NORMALIZE_RE = re.compile(r"[\W_]+", flags=re.UNICODE)


@dataclass(frozen=True)
class FinalArtifactIntegrityConfig:
    gate_version: str = FINAL_ARTIFACT_GATE_VERSION
    min_exact_paragraph_chars: int = 48
    min_exact_line_chars: int = 36
    min_near_duplicate_chars: int = 60
    min_source_comparison_chars: int = 32
    char_ngram_size: int = 3
    near_sequence_threshold: float = 0.76
    near_jaccard_threshold: float = 0.52
    near_combined_threshold: float = 0.68
    append_length_ratio_threshold: float = 1.35
    append_prefix_ratio_threshold: float = 0.85
    append_source_retained_ratio_threshold: float = 0.82
    opening_ending_similarity_threshold: float = 0.80
    opening_ending_min_chars: int = 72
    summary_chars: int = 96

    def __post_init__(self) -> None:
        positive_ints = (
            "min_exact_paragraph_chars",
            "min_exact_line_chars",
            "min_near_duplicate_chars",
            "min_source_comparison_chars",
            "char_ngram_size",
            "opening_ending_min_chars",
            "summary_chars",
        )
        for field in positive_ints:
            if int(getattr(self, field)) < 1:
                raise ValueError(f"{field} must be positive")
        thresholds = (
            "near_sequence_threshold",
            "near_jaccard_threshold",
            "near_combined_threshold",
            "append_prefix_ratio_threshold",
            "append_source_retained_ratio_threshold",
            "opening_ending_similarity_threshold",
        )
        for field in thresholds:
            value = float(getattr(self, field))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{field} must be between 0 and 1")
        if float(self.append_length_ratio_threshold) <= 1.0:
            raise ValueError("append_length_ratio_threshold must be greater than 1")


class FinalArtifactIntegrityError(ValueError):
    def __init__(self, report: dict[str, Any]) -> None:
        self.report = validate_schema(report, "final_artifact_integrity.schema.json")
        codes = ", ".join(
            str(item.get("code"))
            for item in self.report.get("findings", [])
            if item.get("blocking")
        )
        super().__init__(f"final artifact integrity gate rejected artifact: {codes or 'unknown'}")


class FinalArtifactIntegrityGate:
    def __init__(self, config: FinalArtifactIntegrityConfig | None = None) -> None:
        self.config = config or FinalArtifactIntegrityConfig()

    def evaluate(
        self,
        *,
        artifact_text: str,
        stage: str,
        source_text: str | None = None,
        scene_events: Iterable[dict[str, Any]] | None = None,
        scene_drafts: list[dict[str, Any]] | None = None,
        scene_spans: list[dict[str, Any]] | None = None,
        expected_artifact_sha256: str | None = None,
    ) -> dict[str, Any]:
        text = str(artifact_text or "")
        units = _paragraph_units(text)
        findings: list[dict[str, Any]] = []
        findings.extend(self._exact_duplicates(text, units))
        findings.extend(self._near_duplicates(units))
        findings.extend(self._duplicate_opening_and_ending(text, units))
        event_findings = self._duplicate_events(scene_events or ())
        findings.extend(event_findings)

        transition_metrics: dict[str, Any] = {}
        if source_text is not None:
            transition_metrics, transition_findings = self._transition_integrity(
                source_text=str(source_text),
                output_text=text,
                stage=stage,
            )
            findings.extend(transition_findings)

        if scene_drafts is not None or scene_spans is not None:
            findings.extend(
                self._scene_evidence(
                    artifact_text=text,
                    scene_drafts=scene_drafts or [],
                    scene_spans=scene_spans or [],
                )
            )

        artifact_sha256 = _sha256_text(text)
        if expected_artifact_sha256 is not None and artifact_sha256 != expected_artifact_sha256:
            findings.append(
                _finding(
                    code="final_artifact_hash_mismatch",
                    severity=_BLOCKING,
                    message="The candidate artifact bytes do not match the artifact accepted by the final gate.",
                    locations=[],
                    summary="writeback SHA-256 differs from accepted artifact SHA-256",
                    evidence={
                        "expected_artifact_sha256": expected_artifact_sha256,
                        "actual_artifact_sha256": artifact_sha256,
                    },
                )
            )

        findings = _deduplicate_findings(findings)
        report = {
            "schema_version": FINAL_ARTIFACT_SCHEMA_VERSION,
            "gate_version": self.config.gate_version,
            "stage": str(stage),
            "artifact_sha256": artifact_sha256,
            "artifact_chars": len(text),
            "artifact_bytes": len(text.encode("utf-8")),
            "accepted": not any(item["blocking"] for item in findings),
            "findings": findings,
            "metrics": {
                "paragraph_count": len(units),
                "event_count": len(list(_event_items(scene_events or ()))),
                "blocking_count": sum(1 for item in findings if item["blocking"]),
                "warning_count": sum(1 for item in findings if not item["blocking"]),
                **transition_metrics,
            },
            "config": asdict(self.config),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return validate_schema(report, "final_artifact_integrity.schema.json")

    def require_accepted(self, report: dict[str, Any]) -> dict[str, Any]:
        validated = validate_schema(report, "final_artifact_integrity.schema.json")
        if not validated["accepted"]:
            raise FinalArtifactIntegrityError(validated)
        return validated

    def _exact_duplicates(
        self,
        text: str,
        paragraphs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        findings.extend(
            _duplicate_unit_findings(
                paragraphs,
                code="duplicate_scene_text",
                minimum_chars=self.config.min_exact_paragraph_chars,
                summary_chars=self.config.summary_chars,
                unit_kind="paragraph",
            )
        )
        line_units = _line_units(text)
        findings.extend(
            _duplicate_unit_findings(
                line_units,
                code="duplicate_scene_text",
                minimum_chars=self.config.min_exact_line_chars,
                summary_chars=self.config.summary_chars,
                unit_kind="line",
            )
        )
        return findings

    def _near_duplicates(self, paragraphs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        for left_index, left in enumerate(paragraphs):
            left_text = str(left["text"])
            left_normalized = _normalize_text(left_text)
            if not _eligible_near_unit(left_text, left_normalized, self.config):
                continue
            for right in paragraphs[left_index + 1 :]:
                right_text = str(right["text"])
                right_normalized = _normalize_text(right_text)
                if not _eligible_near_unit(right_text, right_normalized, self.config):
                    continue
                if left_normalized == right_normalized:
                    continue
                sequence = SequenceMatcher(None, left_normalized, right_normalized, autojunk=False).ratio()
                jaccard = _jaccard(
                    _ngrams(left_normalized, self.config.char_ngram_size),
                    _ngrams(right_normalized, self.config.char_ngram_size),
                )
                combined = (sequence * 0.62) + (jaccard * 0.38)
                blocking = (
                    sequence >= self.config.near_sequence_threshold
                    or (
                        jaccard >= self.config.near_jaccard_threshold
                        and combined >= self.config.near_combined_threshold
                    )
                )
                if not blocking:
                    continue
                findings.append(
                    _finding(
                        code="near_duplicate_scene_text",
                        severity=_BLOCKING,
                        message="Two substantial prose ranges are deterministic near-duplicates.",
                        locations=[_location(left), _location(right)],
                        similarity={
                            "sequence_matcher": round(sequence, 6),
                            "char_ngram_jaccard": round(jaccard, 6),
                            "combined": round(combined, 6),
                        },
                        summary=(
                            f"{_summary(left_text, self.config.summary_chars)} | "
                            f"{_summary(right_text, self.config.summary_chars)}"
                        ),
                        evidence={"unit_kind": "paragraph"},
                    )
                )
        return findings

    def _transition_integrity(
        self,
        *,
        source_text: str,
        output_text: str,
        stage: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        source = _normalize_text(source_text)
        output = _normalize_text(output_text)
        source_chars = len(source_text)
        output_chars = len(output_text)
        length_ratio = output_chars / max(1, source_chars)
        common_prefix = _common_prefix_length(source, output)
        prefix_ratio = common_prefix / max(1, len(source))
        matcher = SequenceMatcher(None, source, output, autojunk=False)
        matched_source_chars = sum(block.size for block in matcher.get_matching_blocks())
        retained_ratio = min(1.0, matched_source_chars / max(1, len(source)))
        exact_source_contained = bool(source and source in output)
        source_position = output.find(source) if exact_source_contained else -1
        source_prefix_chars = source_position if source_position >= 0 else 0
        source_suffix_chars = (
            len(output) - source_position - len(source)
            if source_position >= 0
            else 0
        )
        source_prefix_ratio = source_prefix_chars / max(1, len(source))
        source_suffix_ratio = source_suffix_chars / max(1, len(source))
        outside_source = (
            output[:source_position] + output[source_position + len(source) :]
            if source_position >= 0
            else ""
        )
        outside_source_similarity = (
            SequenceMatcher(
                None,
                source,
                outside_source,
                autojunk=False,
            ).ratio()
            if outside_source
            else 0.0
        )
        source_at_append_boundary = bool(
            exact_source_contained
            and (
                (source_prefix_ratio <= 0.15 and source_suffix_ratio >= 0.30)
                or (
                    source_prefix_ratio >= 0.30
                    and source_suffix_ratio <= 0.15
                    and outside_source_similarity >= 0.34
                )
            )
        )
        suspect = False
        if len(source) >= self.config.min_source_comparison_chars:
            suspect = (
                (
                    source_at_append_boundary
                    and length_ratio >= self.config.append_length_ratio_threshold
                )
                or (
                    prefix_ratio >= self.config.append_prefix_ratio_threshold
                    and length_ratio >= self.config.append_length_ratio_threshold
                )
            )
        metrics = {
            "source_chars": source_chars,
            "output_chars": output_chars,
            "length_ratio": round(length_ratio, 6),
            "source_prefix_retained_ratio": round(prefix_ratio, 6),
            "source_subsequence_ratio": round(retained_ratio, 6),
            "source_exactly_contained": exact_source_contained,
            "source_prefix_chars": source_prefix_chars,
            "source_suffix_chars": source_suffix_chars,
            "outside_source_similarity": round(outside_source_similarity, 6),
            "source_at_append_boundary": source_at_append_boundary,
            "suspected_append_instead_of_replace": suspect,
        }
        if not suspect:
            return metrics, []
        normalized_stage = str(stage).strip().lower()
        code = (
            "polish_append_instead_of_replace"
            if normalized_stage == "polish"
            else "repair_append_instead_of_replace"
            if normalized_stage == "repair"
            else "append_instead_of_replace"
        )
        return metrics, [
            _finding(
                code=code,
                severity=_BLOCKING,
                message="The transformation output appears to retain the source and append another version.",
                locations=[
                    {"kind": "source", "start_char": 0, "end_char": source_chars},
                    {"kind": "output", "start_char": 0, "end_char": output_chars},
                ],
                summary=f"{normalized_stage or 'transform'} output retained too much source text",
                evidence=metrics,
            )
        ]

    def _duplicate_opening_and_ending(
        self,
        text: str,
        paragraphs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        titles: dict[str, list[dict[str, Any]]] = {}
        for line in _line_units(text):
            normalized = _normalize_text(str(line["text"]))
            if normalized and _TITLE_RE.match(str(line["text"]).strip()):
                titles.setdefault(normalized, []).append(line)
        for units in titles.values():
            if len(units) > 1:
                findings.append(
                    _finding(
                        code="duplicate_opening",
                        severity=_BLOCKING,
                        message="The chapter title appears more than once.",
                        locations=[_location(item) for item in units[:2]],
                        summary=_summary(str(units[0]["text"]), self.config.summary_chars),
                        evidence={"title_occurrences": len(units)},
                    )
                )

        eligible = [
            item
            for item in paragraphs
            if len(_normalize_text(str(item["text"]))) >= self.config.opening_ending_min_chars
        ]
        if len(eligible) < 2:
            return findings
        opening = eligible[0]
        for candidate in eligible[1:]:
            similarity = _sequence_similarity(str(opening["text"]), str(candidate["text"]))
            if similarity >= self.config.opening_ending_similarity_threshold:
                findings.append(
                    _finding(
                        code="duplicate_opening",
                        severity=_BLOCKING,
                        message="A second substantial opening-like range repeats the chapter opening.",
                        locations=[_location(opening), _location(candidate)],
                        similarity={"sequence_matcher": round(similarity, 6)},
                        summary=_summary(str(opening["text"]), self.config.summary_chars),
                        evidence={},
                    )
                )
                break
        ending = eligible[-1]
        for candidate in eligible[:-1]:
            similarity = _sequence_similarity(str(ending["text"]), str(candidate["text"]))
            if similarity >= self.config.opening_ending_similarity_threshold:
                findings.append(
                    _finding(
                        code="duplicate_ending_hook",
                        severity=_BLOCKING,
                        message="The final pressure or hook substantially repeats an earlier ending-like range.",
                        locations=[_location(candidate), _location(ending)],
                        similarity={"sequence_matcher": round(similarity, 6)},
                        summary=_summary(str(ending["text"]), self.config.summary_chars),
                        evidence={},
                    )
                )
                break
        return findings

    def _duplicate_events(
        self,
        scene_events: Iterable[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        events = list(_event_items(scene_events))
        findings: list[dict[str, Any]] = []
        for left_index, left in enumerate(events):
            for right_index, right in enumerate(events[left_index + 1 :], start=left_index + 1):
                overlap = _event_overlap(left, right)
                if not overlap["duplicate"]:
                    continue
                findings.append(
                    _finding(
                        code="duplicate_scene_event",
                        severity=_BLOCKING,
                        message="A completed structured event is declared again in a later scene.",
                        locations=[
                            {
                                "kind": "event",
                                "index": left_index,
                                "event_id": str(left.get("event_id") or ""),
                            },
                            {
                                "kind": "event",
                                "index": right_index,
                                "event_id": str(right.get("event_id") or ""),
                            },
                        ],
                        similarity={"event_overlap": overlap["score"]},
                        summary=(
                            f"{left.get('type') or 'event'}: "
                            f"{', '.join(str(item) for item in left.get('objects') or [])}"
                        ),
                        evidence=overlap,
                    )
                )
        return findings

    def _scene_evidence(
        self,
        *,
        artifact_text: str,
        scene_drafts: list[dict[str, Any]],
        scene_spans: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        problems: list[dict[str, Any]] = []
        draft_sequence = [
            item
            for item in scene_drafts
            if isinstance(item, dict)
        ]
        drafts = {
            int(item.get("index") or 0): str(item.get("text") or "")
            for item in draft_sequence
            if int(item.get("index") or 0) > 0
        }
        if not draft_sequence or not scene_spans:
            problems.append(
                {
                    "reason": "scene_evidence_missing",
                    "draft_count": len(scene_drafts),
                    "span_count": len(scene_spans),
                }
            )
        if len(drafts) != len(draft_sequence):
            problems.append(
                {
                    "reason": "scene_draft_index_invalid_or_duplicate",
                    "draft_count": len(draft_sequence),
                    "unique_index_count": len(drafts),
                }
            )

        previous_end = 0
        span_indexes: list[int] = []
        for position, span in enumerate(scene_spans):
            if not isinstance(span, dict):
                problems.append({"position": position, "reason": "span_not_object"})
                continue
            index = int(span.get("index") or 0)
            start = int(span.get("start_char") or 0)
            end = int(span.get("end_char") or 0)
            draft = drafts.get(index)
            span_indexes.append(index)
            if position == 0 and start != 0:
                problems.append(
                    {
                        "position": position,
                        "scene_index": index,
                        "start_char": start,
                        "reason": "scene_coverage_does_not_start_at_zero",
                    }
                )
            elif position > 0 and (
                start != previous_end + 2
                or artifact_text[previous_end:start] != "\n\n"
            ):
                problems.append(
                    {
                        "position": position,
                        "scene_index": index,
                        "previous_end_char": previous_end,
                        "start_char": start,
                        "separator": artifact_text[
                            max(0, previous_end):max(0, min(start, len(artifact_text)))
                        ],
                        "reason": "scene_separator_or_coverage_gap_mismatch",
                    }
                )
            if (
                index < 1
                or draft is None
                or start < previous_end
                or end <= start
                or end > len(artifact_text)
                or artifact_text[start:end] != draft
                or int(span.get("chars") or 0) != len(draft)
            ):
                problems.append(
                    {
                        "position": position,
                        "scene_index": index,
                        "start_char": start,
                        "end_char": end,
                        "draft_chars": len(draft) if draft is not None else None,
                        "actual_excerpt": artifact_text[max(0, start) : max(0, min(end, len(artifact_text)))],
                        "reason": "span_or_text_mismatch",
                    }
                )
            previous_end = max(previous_end, end)
        if scene_spans and previous_end != len(artifact_text):
            problems.append(
                {
                    "reason": "scene_coverage_does_not_reach_artifact_end",
                    "last_end_char": previous_end,
                    "artifact_chars": len(artifact_text),
                }
            )
        if len(scene_spans) != len(scene_drafts):
            problems.append(
                {
                    "reason": "scene_span_count_mismatch",
                    "draft_count": len(scene_drafts),
                    "span_count": len(scene_spans),
                }
            )
        if len(set(span_indexes)) != len(span_indexes) or set(span_indexes) != set(drafts):
            problems.append(
                {
                    "reason": "scene_span_index_set_mismatch",
                    "draft_indexes": sorted(drafts),
                    "span_indexes": span_indexes,
                }
            )
        reconstructed = "\n\n".join(
            str(item.get("text") or "")
            for item in draft_sequence
        )
        if reconstructed != artifact_text:
            problems.append(
                {
                    "reason": "scene_reconstruction_mismatch",
                    "reconstructed_sha256": _sha256_text(reconstructed),
                    "artifact_sha256": _sha256_text(artifact_text),
                    "reconstructed_chars": len(reconstructed),
                    "artifact_chars": len(artifact_text),
                }
            )
        if not problems:
            return []
        return [
            _finding(
                code="stale_scene_evidence",
                severity=_BLOCKING,
                message="Scene drafts or spans no longer align with the final artifact bytes.",
                locations=[
                    {
                        "kind": "scene_span",
                        "index": item.get("scene_index", item.get("position")),
                        "start_char": item.get("start_char"),
                        "end_char": item.get("end_char"),
                    }
                    for item in problems[:4]
                ],
                summary="final chapter changed without rebuilding scene evidence",
                evidence={"problems": problems[:8]},
            )
        ]


def build_integrity_stage_record(
    *,
    stage: str,
    input_text: str | None,
    output_text: str,
    report: dict[str, Any],
) -> dict[str, Any]:
    validated = validate_schema(report, "final_artifact_integrity.schema.json")
    source = None if input_text is None else str(input_text)
    output = str(output_text)
    return {
        "stage": str(stage),
        "input_sha256": _sha256_text(source) if source is not None else None,
        "output_sha256": _sha256_text(output),
        "input_chars": len(source) if source is not None else 0,
        "output_chars": len(output),
        "integrity_findings": [
            {
                "code": item["code"],
                "severity": item["severity"],
                "blocking": item["blocking"],
                "summary": item["summary"],
            }
            for item in validated["findings"]
        ],
        "accepted": bool(validated["accepted"]),
        "artifact_sha256": validated["artifact_sha256"],
        "gate_version": validated["gate_version"],
        "timestamp": validated["timestamp"],
    }


def merge_integrity_report_into_validation(
    validation: dict[str, Any],
    report: dict[str, Any],
) -> dict[str, Any]:
    base = dict(validation)
    validated_report = validate_schema(report, "final_artifact_integrity.schema.json")
    problems = [_integrity_problem(item) for item in validated_report["findings"]]
    checks = [dict(item) for item in base.get("checks") or [] if isinstance(item, dict)]
    if not checks and isinstance(base.get("problems"), list):
        checks.append(
            {
                "name": "custom_validator",
                "ok": bool(base.get("ok")),
                "problems": [
                    _normalize_external_problem(item, validation_ok=bool(base.get("ok")))
                    for item in base["problems"]
                    if isinstance(item, dict)
                ],
            }
        )
    else:
        checks = [
            {
                **check,
                "problems": [
                    _normalize_external_problem(
                        item,
                        validation_ok=bool(check.get("ok")),
                    )
                    for item in check.get("problems") or []
                    if isinstance(item, dict)
                ],
            }
            for check in checks
        ]
    checks = [item for item in checks if item.get("name") != "final_artifact_integrity"]
    checks.append(
        {
            "name": "final_artifact_integrity",
            "ok": not any(item["blocking"] for item in problems),
            "problems": problems,
            "artifact_sha256": validated_report["artifact_sha256"],
            "gate_version": validated_report["gate_version"],
            "stage": validated_report["stage"],
        }
    )
    all_problems = [
        problem
        for check in checks
        for problem in check.get("problems", [])
        if isinstance(problem, dict)
    ]
    executed = [str(item) for item in base.get("executed_checks") or []]
    if "final_artifact_integrity" not in executed:
        executed.append("final_artifact_integrity")
    base.update(
        {
            "ok": not any(item.get("blocking") for item in all_problems),
            "requested_focus": [
                str(item) for item in base.get("requested_focus") or []
            ],
            "executed_checks": executed,
            "skipped_checks": [
                str(item) for item in base.get("skipped_checks") or []
            ],
            "checks": checks,
            "problems": all_problems,
            "blocking_problem_count": sum(1 for item in all_problems if item.get("blocking")),
            "warning_count": sum(1 for item in all_problems if not item.get("blocking")),
            "severity_counts": _severity_counts(all_problems),
            "deterministic_repair_count": sum(
                1 for item in all_problems if item.get("repair_action") != "manual_review"
            ),
            "manual_review_count": sum(
                1 for item in all_problems if item.get("repair_action") == "manual_review"
            ),
            "repair_action_counts": _repair_action_counts(all_problems),
        }
    )
    return validate_schema(base, "validation_result.schema.json")


def _normalize_external_problem(
    problem: dict[str, Any],
    *,
    validation_ok: bool,
) -> dict[str, Any]:
    normalized = dict(problem)
    blocking = bool(normalized.get("blocking", not validation_ok))
    normalized.update(
        {
            "code": str(normalized.get("code") or "custom_validation_problem"),
            "message": str(normalized.get("message") or "Custom validator reported a problem."),
            "validator": str(normalized.get("validator") or "custom"),
            "severity": str(
                normalized.get("severity")
                or ("critical" if blocking else "medium")
            ),
            "blocking": blocking,
            "category": str(
                normalized.get("category")
                or ("blocking" if blocking else "warning")
            ),
            "repair_hint": str(
                normalized.get("repair_hint")
                or "Inspect the custom validator evidence before commit."
            ),
            "repair_action": str(
                normalized.get("repair_action") or "manual_review"
            ),
            "repair_parameters": (
                dict(normalized.get("repair_parameters"))
                if isinstance(normalized.get("repair_parameters"), dict)
                else {}
            ),
            "evidence": [
                dict(item)
                for item in normalized.get("evidence") or []
                if isinstance(item, dict)
            ],
        }
    )
    return normalized


def merge_integrity_reports_for_artifact(
    report: dict[str, Any],
    prior_reports: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    return _merge_integrity_reports(
        report,
        prior_reports,
        equivalent_before_sha256=None,
        canonicalization_transition=None,
    )


def merge_integrity_reports_for_canonicalized_artifact(
    report: dict[str, Any],
    prior_reports: Iterable[dict[str, Any]],
    *,
    before_artifact_text: str,
    before_artifact_sha256: str,
    canonicalized_artifact_text: str,
) -> dict[str, Any]:
    """Merge reports across the one explicitly supported canonicalization.

    The caller must bind the supplied pre-canonicalization bytes to their hash,
    supply the post-canonicalization bytes, and provide a report bound to those
    post-canonicalization bytes. Different-SHA findings are inherited only
    after all three bindings and the canonicalization rule have been verified.
    """

    if not isinstance(before_artifact_text, str):
        raise TypeError("before_artifact_text must be a string")
    if not isinstance(canonicalized_artifact_text, str):
        raise TypeError("canonicalized_artifact_text must be a string")

    declared_before_sha256 = str(before_artifact_sha256)
    actual_before_sha256 = _sha256_text(before_artifact_text)
    if declared_before_sha256 != actual_before_sha256:
        raise ValueError(
            "before_artifact_sha256 does not match before_artifact_text"
        )

    expected_canonicalized_text = _canonicalize_artifact_text(before_artifact_text)
    if canonicalized_artifact_text != expected_canonicalized_text:
        raise ValueError(
            "canonicalized_artifact_text is not the supported safe canonicalization "
            "of before_artifact_text"
        )

    validated_report = validate_schema(
        report,
        "final_artifact_integrity.schema.json",
    )
    after_artifact_sha256 = _sha256_text(canonicalized_artifact_text)
    if validated_report["artifact_sha256"] != after_artifact_sha256:
        raise ValueError(
            "report artifact_sha256 does not match canonicalized_artifact_text"
        )
    if (
        validated_report["artifact_chars"] != len(canonicalized_artifact_text)
        or validated_report["artifact_bytes"]
        != len(canonicalized_artifact_text.encode("utf-8"))
    ):
        raise ValueError(
            "report artifact size does not match canonicalized_artifact_text"
        )

    transition = {
        "policy": _SAFE_CANONICALIZATION_POLICY,
        "before_artifact_sha256": actual_before_sha256,
        "after_artifact_sha256": after_artifact_sha256,
    }
    return _merge_integrity_reports(
        validated_report,
        prior_reports,
        equivalent_before_sha256=actual_before_sha256,
        canonicalization_transition=transition,
    )


def _merge_integrity_reports(
    report: dict[str, Any],
    prior_reports: Iterable[dict[str, Any]],
    *,
    equivalent_before_sha256: str | None,
    canonicalization_transition: dict[str, str] | None,
) -> dict[str, Any]:
    merged = dict(validate_schema(report, "final_artifact_integrity.schema.json"))
    findings = [dict(item) for item in merged["findings"]]
    matching_stages: list[str] = []
    equivalent_stages: list[str] = []
    equivalent_prior_hashes: list[str] = []
    for prior in prior_reports:
        if not isinstance(prior, dict):
            continue
        validated = validate_schema(prior, "final_artifact_integrity.schema.json")
        prior_sha256 = str(validated["artifact_sha256"])
        if prior_sha256 == merged["artifact_sha256"]:
            matching_stages.append(str(validated["stage"]))
        elif (
            equivalent_before_sha256 is not None
            and prior_sha256 == equivalent_before_sha256
        ):
            equivalent_stages.append(str(validated["stage"]))
            equivalent_prior_hashes.append(prior_sha256)
        else:
            continue
        findings.extend(dict(item) for item in validated["findings"])
    findings = _deduplicate_findings(findings)
    metrics = dict(merged["metrics"])
    metrics.update(
        {
            "blocking_count": sum(1 for item in findings if item["blocking"]),
            "warning_count": sum(1 for item in findings if not item["blocking"]),
            "matching_prior_stages": sorted(set(matching_stages)),
        }
    )
    if canonicalization_transition is not None:
        metrics.update(
            {
                "canonicalization_transition": dict(canonicalization_transition),
                "equivalent_prior_stages": sorted(set(equivalent_stages)),
                "equivalent_prior_artifact_sha256s": sorted(
                    set(equivalent_prior_hashes)
                ),
            }
        )
    merged["findings"] = findings
    merged["metrics"] = metrics
    merged["accepted"] = not any(item["blocking"] for item in findings)
    return validate_schema(merged, "final_artifact_integrity.schema.json")


def _integrity_problem(finding: dict[str, Any]) -> dict[str, Any]:
    evidence = [
        {"kind": "summary", "value": str(finding["summary"])},
        {
            "kind": "integrity_evidence",
            "value": json.dumps(finding.get("evidence") or {}, ensure_ascii=False, sort_keys=True),
        },
    ]
    return {
        "code": str(finding["code"]),
        "message": str(finding["message"]),
        "validator": "final_artifact_integrity",
        "severity": "critical" if finding["blocking"] else "medium",
        "blocking": bool(finding["blocking"]),
        "category": "blocking" if finding["blocking"] else "warning",
        "repair_hint": "Reject the artifact or regenerate the affected stage with the reported evidence.",
        "repair_action": "manual_review",
        "repair_parameters": {},
        "evidence": evidence,
    }


def _duplicate_unit_findings(
    units: list[dict[str, Any]],
    *,
    code: str,
    minimum_chars: int,
    summary_chars: int,
    unit_kind: str,
) -> list[dict[str, Any]]:
    by_normalized: dict[str, list[dict[str, Any]]] = {}
    for unit in units:
        raw = str(unit["text"])
        normalized = _normalize_text(raw)
        if len(normalized) < minimum_chars:
            continue
        if _DIALOGUE_RE.match(raw.strip()) and len(normalized) < minimum_chars * 2:
            continue
        by_normalized.setdefault(normalized, []).append(unit)
    findings: list[dict[str, Any]] = []
    for duplicates in by_normalized.values():
        if len(duplicates) < 2:
            continue
        findings.append(
            _finding(
                code=code,
                severity=_BLOCKING,
                message=f"The same substantial {unit_kind} appears more than once.",
                locations=[_location(item) for item in duplicates[:2]],
                similarity={"exact": 1.0},
                summary=_summary(str(duplicates[0]["text"]), summary_chars),
                evidence={"unit_kind": unit_kind, "occurrences": len(duplicates)},
            )
        )
    return findings


def _paragraph_units(text: str) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    cursor = 0
    for match in _PARAGRAPH_SEPARATOR_RE.finditer(text):
        _append_unit(units, text, cursor, match.start(), "paragraph")
        cursor = match.end()
    _append_unit(units, text, cursor, len(text), "paragraph")
    return units


def _line_units(text: str) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    cursor = 0
    for line in text.splitlines(keepends=True):
        end = cursor + len(line.rstrip("\r\n"))
        _append_unit(units, text, cursor, end, "line")
        cursor += len(line)
    if not text.splitlines(keepends=True) and text:
        _append_unit(units, text, 0, len(text), "line")
    return units


def _append_unit(
    units: list[dict[str, Any]],
    text: str,
    raw_start: int,
    raw_end: int,
    kind: str,
) -> None:
    raw = text[raw_start:raw_end]
    if not raw.strip():
        return
    leading = len(raw) - len(raw.lstrip())
    trailing = len(raw.rstrip())
    start = raw_start + leading
    end = raw_start + trailing
    units.append(
        {
            "kind": kind,
            "index": len(units),
            "start_char": start,
            "end_char": end,
            "text": text[start:end],
        }
    )


def _normalize_text(value: str) -> str:
    return _NORMALIZE_RE.sub("", str(value or "").casefold())


def _eligible_near_unit(
    raw: str,
    normalized: str,
    config: FinalArtifactIntegrityConfig,
) -> bool:
    if len(normalized) < config.min_near_duplicate_chars:
        return False
    if _DIALOGUE_RE.match(raw.strip()) and len(normalized) < config.min_near_duplicate_chars * 2:
        return False
    return True


def _ngrams(value: str, size: int) -> set[str]:
    if len(value) < size:
        return {value} if value else set()
    return {value[index : index + size] for index in range(len(value) - size + 1)}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _event_items(value: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    for item in value:
        if not isinstance(item, dict):
            continue
        nested = item.get("events")
        if isinstance(nested, list):
            yield from _event_items(nested)
        elif item.get("type"):
            yield item


def _event_overlap(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    left_type = str(left.get("type") or "")
    right_type = str(right.get("type") or "")
    left_subjects = {str(item) for item in left.get("subjects") or [] if str(item)}
    right_subjects = {str(item) for item in right.get("subjects") or [] if str(item)}
    left_objects = {str(item) for item in left.get("objects") or [] if str(item)}
    right_objects = {str(item) for item in right.get("objects") or [] if str(item)}
    same_location = bool(left.get("location")) and left.get("location") == right.get("location")
    same_status = str(left.get("status") or "") == str(right.get("status") or "")
    type_match = bool(left_type) and left_type == right_type
    subject_overlap = bool(left_subjects & right_subjects)
    object_overlap = bool(left_objects & right_objects)
    score = sum((type_match, subject_overlap, object_overlap, same_location, same_status)) / 5
    return {
        "duplicate": bool(
            type_match
            and same_status
            and (subject_overlap or not left_subjects or not right_subjects)
            and (object_overlap or not left_objects or not right_objects)
            and (same_location or not left.get("location") or not right.get("location"))
        ),
        "score": round(score, 6),
        "type_match": type_match,
        "subject_overlap": sorted(left_subjects & right_subjects),
        "object_overlap": sorted(left_objects & right_objects),
        "same_location": same_location,
        "same_status": same_status,
    }


def _finding(
    *,
    code: str,
    severity: str,
    message: str,
    locations: list[dict[str, Any]],
    summary: str,
    evidence: dict[str, Any],
    similarity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "blocking": severity == _BLOCKING,
        "message": message,
        "locations": locations,
        "similarity": similarity or {},
        "summary": summary,
        "evidence": evidence,
    }


def _location(unit: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": str(unit.get("kind") or "text"),
        "index": int(unit.get("index") or 0),
        "start_char": int(unit.get("start_char") or 0),
        "end_char": int(unit.get("end_char") or 0),
    }


def _summary(value: str, limit: int) -> str:
    compact = re.sub(r"\s+", " ", str(value or "")).strip()
    return compact if len(compact) <= limit else compact[: max(1, limit - 1)] + "…"


def _common_prefix_length(left: str, right: str) -> int:
    count = 0
    for left_char, right_char in zip(left, right):
        if left_char != right_char:
            break
        count += 1
    return count


def _sequence_similarity(left: str, right: str) -> float:
    return SequenceMatcher(
        None,
        _normalize_text(left),
        _normalize_text(right),
        autojunk=False,
    ).ratio()


def _deduplicate_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for finding in findings:
        identity = json.dumps(
            {
                "code": finding["code"],
                "locations": finding.get("locations") or [],
                "summary": finding.get("summary") or "",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        unique.setdefault(hashlib.sha256(identity.encode("utf-8")).hexdigest(), finding)
    return sorted(
        unique.values(),
        key=lambda item: (
            0 if item["blocking"] else 1,
            item["code"],
            json.dumps(item.get("locations") or [], ensure_ascii=False, sort_keys=True),
        ),
    )


def _severity_counts(problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    order = ("critical", "high", "medium", "low")
    return [
        {"severity": severity, "count": sum(1 for item in problems if item.get("severity") == severity)}
        for severity in order
        if any(item.get("severity") == severity for item in problems)
    ]


def _repair_action_counts(problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    actions = sorted({str(item.get("repair_action") or "manual_review") for item in problems})
    return [
        {
            "action": action,
            "count": sum(1 for item in problems if item.get("repair_action") == action),
        }
        for action in actions
    ]


def _sha256_text(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _canonicalize_artifact_text(value: str) -> str:
    return str(value).rstrip() + "\n"


__all__ = [
    "FINAL_ARTIFACT_GATE_VERSION",
    "FinalArtifactIntegrityConfig",
    "FinalArtifactIntegrityError",
    "FinalArtifactIntegrityGate",
    "build_integrity_stage_record",
    "merge_integrity_reports_for_canonicalized_artifact",
    "merge_integrity_reports_for_artifact",
    "merge_integrity_report_into_validation",
]
