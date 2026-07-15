from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping
import re
import uuid

from core.delivery_intents import validate_file_delivery_profile

from core.autonomy.common import (
    AutonomyContractError,
    canonical_hash,
    load_json_object,
    positive_int,
    required_text,
    safe_id,
    validate_mapping,
)


_PROFILE_KINDS = (
    "story_projects",
    "provider_models",
    "file_deliveries",
    "budgets",
    "quality_policies",
)
_DEFAULT_FIELDS = {
    "story_projects": "story_project",
    "provider_models": "provider_model",
    "file_deliveries": "file_delivery",
    "budgets": "budget",
    "quality_policies": "quality_policy",
}
_FORBIDDEN_FIELD_FRAGMENTS = (
    "api_key",
    "apikey",
    "credential",
    "password",
    "secret",
    "access_token",
    "auth_token",
    "bearer_token",
    "environment",
    "env_var",
    "notion",
)


class TrustedProfilesError(AutonomyContractError):
    pass


@dataclass(frozen=True)
class TrustedProfiles:
    payload: dict[str, Any]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TrustedProfiles":
        payload = _validate_profiles(value)
        return cls(copy.deepcopy(payload))

    @classmethod
    def load(cls, path: str | Path) -> "TrustedProfiles":
        return cls.from_dict(load_json_object(path))

    @property
    def profile_set_id(self) -> str:
        return str(self.payload["profile_set_id"])

    @property
    def profile_set_hash(self) -> str:
        return canonical_hash(self.payload)

    def default_id(self, kind: str) -> str:
        normalized = _profile_kind(kind)
        return str(self.payload["defaults"][_DEFAULT_FIELDS[normalized]])

    def get(self, kind: str, profile_id: str | None = None) -> dict[str, Any]:
        normalized = _profile_kind(kind)
        selected = profile_id or self.default_id(normalized)
        for profile in self.payload[normalized]:
            if profile["profile_id"] == selected:
                return copy.deepcopy(profile)
        raise TrustedProfilesError(
            "trusted_profile_unknown", f"unknown {normalized} profile: {selected!r}"
        )

    def public_snapshot(self, kind: str, profile_id: str | None = None) -> dict[str, Any]:
        normalized = _profile_kind(kind)
        profile = self.get(normalized, profile_id)
        snapshot = copy.deepcopy(profile)
        # The trusted delivery configuration owns the actual local template;
        # an executable plan carries only the bound profile identity.
        snapshot.pop("path_template", None)
        snapshot["profile_hash"] = canonical_hash(profile)
        return snapshot

    def assert_snapshot(self, kind: str, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        normalized = _profile_kind(kind)
        if not isinstance(snapshot, Mapping):
            raise TrustedProfilesError(
                "trusted_profile_snapshot_invalid", f"{normalized} snapshot must be an object"
            )
        profile_id = str(snapshot.get("profile_id") or "")
        expected = self.public_snapshot(normalized, profile_id)
        if dict(snapshot) != expected:
            raise TrustedProfilesError(
                "trusted_profile_snapshot_drift",
                f"trusted {normalized} profile changed after plan preview: {profile_id!r}",
            )
        return expected

    def file_delivery_runtime_profile(
        self, profile_id: str | None = None, *, book_id: str
    ) -> dict[str, Any]:
        """Resolve a trusted relative template into the File Delivery contract.

        The returned value contains no physical path.  Operator-owned root
        mapping remains a separate runtime capability.
        """

        profile = self.get("file_deliveries", profile_id)
        template = str(profile["path_template"]).replace("\\", "/")
        template = template.replace("{book_id}", safe_id("book_id", book_id))
        path = PurePosixPath(template)
        directory = str(path.parent)
        filename = path.name
        if directory in {"", "."}:
            directory = "exports"
        if any(token in directory for token in ("{run_id}", "{chapter_index}")):
            raise TrustedProfilesError(
                "trusted_delivery_template_unsafe",
                "run/chapter placeholders must be in the export filename",
            )
        return validate_file_delivery_profile(
            {
                "schema_version": "1.0",
                "profile_id": profile["profile_id"],
                "root_id": f"external:{profile['profile_id']}",
                "root_uuid": profile["root_uuid"],
                "relative_directory": directory,
                "filename_template": filename,
            }
        )


def _validate_profiles(value: Mapping[str, Any]) -> dict[str, Any]:
    _reject_sensitive_fields(value)
    payload = validate_mapping(value, "trusted_profiles.schema.json", "TrustedProfiles")
    safe_id("profile_set_id", payload["profile_set_id"])
    indexes: dict[str, set[str]] = {}
    for kind in _PROFILE_KINDS:
        identifiers: set[str] = set()
        for profile in payload[kind]:
            identifier = safe_id("profile_id", profile["profile_id"])
            if identifier in identifiers:
                raise TrustedProfilesError(
                    "trusted_profile_duplicate", f"duplicate {kind} profile: {identifier!r}"
                )
            identifiers.add(identifier)
        indexes[kind] = identifiers
    for kind, default_field in _DEFAULT_FIELDS.items():
        selected = safe_id(default_field, payload["defaults"][default_field])
        if selected not in indexes[kind]:
            raise TrustedProfilesError(
                "trusted_profile_default_unknown",
                f"default {default_field} is not defined: {selected!r}",
            )

    for profile in payload["story_projects"]:
        safe_id("book_id", profile["book_id"])
        safe_id("root_uuid", profile["root_uuid"])
    for profile in payload["provider_models"]:
        profile.setdefault("endpoint_type", "official")
        required_text("provider", profile["provider"])
        if profile["endpoint_type"] not in {"official", "openai_compatible"}:
            raise TrustedProfilesError(
                "trusted_provider_endpoint_invalid",
                "provider endpoint_type must be official or openai_compatible",
            )
        required_text("model", profile["model"])
        positive_int("max_output_tokens", profile["max_output_tokens"])
    for profile in payload["file_deliveries"]:
        if profile["target_kind"] != "file":
            raise TrustedProfilesError(
                "trusted_delivery_external_forbidden", "only File Delivery profiles are allowed"
            )
        safe_id("root_uuid", profile["root_uuid"])
        try:
            parsed_uuid = uuid.UUID(str(profile["root_uuid"]))
        except ValueError as exc:
            raise TrustedProfilesError(
                "trusted_delivery_root_uuid_invalid",
                "File Delivery root_uuid must be a canonical UUID",
            ) from exc
        if str(parsed_uuid) != profile["root_uuid"]:
            raise TrustedProfilesError(
                "trusted_delivery_root_uuid_invalid",
                "File Delivery root_uuid must be lowercase canonical UUID text",
            )
        template = required_text("path_template", profile["path_template"]).replace("\\", "/")
        pure = PurePosixPath(template)
        if (
            pure.is_absolute()
            or any(part in {"", ".", ".."} for part in pure.parts)
            or str(pure) != template
            or pure.suffix.lower() != ".json"
        ):
            raise TrustedProfilesError(
                "trusted_delivery_template_unsafe",
                "File Delivery template must be a safe relative JSON path",
            )
        fields = set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", template))
        if not fields.issubset({"book_id", "run_id", "chapter_index"}):
            raise TrustedProfilesError(
                "trusted_delivery_template_unsafe",
                "File Delivery template contains an unsupported placeholder",
            )
        if "{run_id}" not in template or "{chapter_index}" not in template:
            raise TrustedProfilesError(
                "trusted_delivery_template_unsafe",
                "File Delivery template must contain {run_id} and {chapter_index}",
            )
        if not profile["requires_run_id"] or not profile["requires_chapter_id"]:
            raise TrustedProfilesError(
                "trusted_delivery_template_unsafe", "delivery uniqueness flags must be enabled"
            )
    for profile in payload["budgets"]:
        for field in (
            "max_chapters",
            "max_model_calls",
            "max_input_tokens",
            "max_output_tokens",
            "max_wall_seconds",
        ):
            positive_int(field, profile[field])
    for profile in payload["quality_policies"]:
        if int(profile["minimum_score"]) != 0:
            raise TrustedProfilesError(
                "trusted_quality_score_unsupported",
                "v1 quality profiles use severity policy only; minimum_score must be 0",
            )
    return payload


def _profile_kind(kind: str) -> str:
    normalized = str(kind)
    aliases = {
        "story_project": "story_projects",
        "provider_model": "provider_models",
        "file_delivery": "file_deliveries",
        "budget": "budgets",
        "quality_policy": "quality_policies",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in _PROFILE_KINDS:
        raise TrustedProfilesError("trusted_profile_kind_invalid", f"unsupported profile kind: {kind}")
    return normalized


def _reject_sensitive_fields(value: Any, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key).lower()
            if key == "token" or any(
                fragment in key for fragment in _FORBIDDEN_FIELD_FRAGMENTS
            ):
                joined = ".".join(path + (str(raw_key),))
                raise TrustedProfilesError(
                    "trusted_profile_sensitive_field", f"sensitive or external field is forbidden: {joined}"
                )
            _reject_sensitive_fields(child, path + (str(raw_key),))
    elif isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        for index, child in enumerate(value):
            _reject_sensitive_fields(child, path + (str(index),))


__all__ = ["TrustedProfiles", "TrustedProfilesError"]
