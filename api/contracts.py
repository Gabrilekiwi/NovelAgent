from __future__ import annotations

import copy
from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Any, Mapping


MODEL_ENDPOINT_OFFICIAL = "official"
MODEL_ENDPOINT_OPENAI_COMPATIBLE = "openai_compatible"
MODEL_ENDPOINT_UNKNOWN = "unknown"


class ModelResponse(str):
    """Provider-neutral result metadata for one successful model response.

    Provider clients may migrate to this contract independently.  Existing
    string-returning call sites intentionally remain compatible until that
    migration is wired into the executor.
    """

    def __new__(
        cls,
        text: str,
        usage: Mapping[str, Any] | None = None,
        finish_reason: str | None = None,
        request_id: str | None = None,
        actual_model: str | None = None,
        endpoint_type: str = MODEL_ENDPOINT_UNKNOWN,
    ) -> "ModelResponse":
        if not isinstance(text, str):
            raise TypeError("ModelResponse.text must be a string")
        if usage is not None and not isinstance(usage, Mapping):
            raise TypeError("ModelResponse.usage must be a mapping or None")
        for name, value in (
            ("finish_reason", finish_reason),
            ("request_id", request_id),
            ("actual_model", actual_model),
        ):
            if value is not None and not isinstance(value, str):
                raise TypeError(f"ModelResponse.{name} must be a string or None")
        if not isinstance(endpoint_type, str) or not endpoint_type.strip():
            raise ValueError("ModelResponse.endpoint_type must be a non-empty string")
        instance = str.__new__(cls, text)
        object.__setattr__(
            instance,
            "usage",
            MappingProxyType(copy.deepcopy(dict(usage or {}))),
        )
        object.__setattr__(instance, "finish_reason", finish_reason)
        object.__setattr__(instance, "request_id", request_id)
        object.__setattr__(instance, "actual_model", actual_model)
        object.__setattr__(instance, "endpoint_type", endpoint_type.strip())
        object.__setattr__(instance, "_model_response_frozen", True)
        return instance

    @property
    def text(self) -> str:
        return str(self)

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_model_response_frozen", False):
            raise AttributeError("ModelResponse is immutable")
        object.__setattr__(self, name, value)

    def to_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "usage": copy.deepcopy(dict(self.usage or {})),
            "finish_reason": self.finish_reason,
            "request_id": self.request_id,
            "actual_model": self.actual_model,
            "endpoint_type": self.endpoint_type,
        }
        if include_text:
            result["text"] = self.text
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ModelResponse":
        if not isinstance(value, Mapping):
            raise TypeError("ModelResponse value must be a mapping")
        return cls(
            text=value.get("text"),
            usage=value.get("usage"),
            finish_reason=value.get("finish_reason"),
            request_id=value.get("request_id"),
            actual_model=value.get("actual_model"),
            endpoint_type=value.get("endpoint_type", MODEL_ENDPOINT_UNKNOWN),
        )


def coerce_model_response(
    value: ModelResponse | str,
    *,
    usage: Mapping[str, Any] | None = None,
    finish_reason: str | None = None,
    request_id: str | None = None,
    actual_model: str | None = None,
    endpoint_type: str = MODEL_ENDPOINT_UNKNOWN,
) -> ModelResponse:
    """Accept legacy string mocks while normalizing real provider results."""

    if isinstance(value, ModelResponse):
        return value
    if not isinstance(value, str):
        raise TypeError("model response must be ModelResponse or string")
    return ModelResponse(
        value,
        usage=usage,
        finish_reason=finish_reason,
        request_id=request_id,
        actual_model=actual_model,
        endpoint_type=endpoint_type,
    )


def model_response_text(value: ModelResponse | str) -> str:
    if not isinstance(value, str):
        raise TypeError("model response must be ModelResponse or string")
    return str(value)


class ModelCallError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        provider: str,
        stage: str,
        model: str | None = None,
        cause: BaseException | None = None,
        failure_category: str | None = None,
        retryable: bool | None = None,
        attempts: int | None = None,
        elapsed_ms: int | None = None,
        attempt_history: list[dict[str, object]] | None = None,
        retry_stop_reason: str | None = None,
        partial_content_received: bool = False,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.stage = stage
        self.model = model
        self.cause_type = type(cause).__name__ if cause is not None else None
        self.failure_category = failure_category or classify_model_failure(self.cause_type, message)
        self.retryable = is_retryable_failure(self.failure_category) if retryable is None else bool(retryable)
        self.attempts = attempts
        self.elapsed_ms = elapsed_ms
        self.attempt_history = [dict(item) for item in (attempt_history or [])]
        self.retry_stop_reason = retry_stop_reason
        self.partial_content_received = bool(partial_content_received)

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "provider": self.provider,
            "stage": self.stage,
            "model": self.model,
            "cause_type": self.cause_type,
            "message": str(self),
        }
        if self.failure_category:
            data["failure_category"] = self.failure_category
            data["retryable"] = self.retryable
        if self.attempts is not None:
            data["attempts"] = self.attempts
        if self.elapsed_ms is not None:
            data["elapsed_ms"] = self.elapsed_ms
        if self.attempt_history:
            data["attempt_history"] = [dict(item) for item in self.attempt_history]
        if self.retry_stop_reason:
            data["retry_stop_reason"] = self.retry_stop_reason
        if self.partial_content_received:
            data["partial_content_received"] = True
        return data


def classify_model_failure(cause_type: str | None, message: str) -> str:
    combined = f"{cause_type or ''} {message}".lower()
    if "authentication" in combined or "permission" in combined or "api key" in combined or " 401" in combined or " 403" in combined:
        return "authentication"
    if "schema" in combined:
        return "schema"
    if "output contract" in combined or "outputerror" in combined:
        return "output_contract"
    if "timeout" in combined or "timed out" in combined:
        return "timeout"
    if (
        "connection" in combined
        or "connect" in combined
        or "urlerror" in combined
        or "gaierror" in combined
        or "connectionreset" in combined
        or "brokenpipe" in combined
        or "network is unreachable" in combined
    ):
        return "connection"
    if "rate limit" in combined or "ratelimit" in combined or " 429" in combined:
        return "rate_limit"
    if (
        "temporarily unavailable" in combined
        or "internalservererror" in combined
        or "server error" in combined
        or " 500" in combined
        or " 502" in combined
        or " 503" in combined
        or " 504" in combined
    ):
        return "transient_provider_error"
    if "bad request" in combined or "invalid configuration" in combined or " 400" in combined:
        return "configuration"
    return "provider_error"


def is_retryable_failure(failure_category: str | None) -> bool:
    return failure_category in {"connection", "timeout", "transient_provider_error", "rate_limit"}


class ModelOutputError(ValueError):
    pass


_LATIN_MOJIBAKE_CHARS = frozenset(
    "ÃÂâ€™€œ€œ¢£¤¥¦§¨©ª«¬®¯°±²³´µ¶·¸¹º»¼½¾¿"
    "åæçèéêëìíîïðñòóôõöøùúûüýþÿ"
)
_MOJIBAKE_FRAGMENTS = (
    "ç¼",
    "é—",
    "é",
    "æ¶",
    "î†",
    "î‡",
    "â‚¬",
    "â”",
    "鈥",
    "锛岄",
    "銆?",
    "榛戞",
    "湀闆",
    "嗗競",
    "鐨勭",
    "鍦ㄤ",
    "涓€",
    "绔欏",
    "闄嗙",
    "浣犲",
    "濡傛",
    "璇风",
)


@dataclass(frozen=True)
class TextContract:
    name: str
    min_chars: int = 1
    min_cjk_ratio: float = 0.0
    forbidden_prefixes: tuple[str, ...] = ("{", "[")
    forbidden_meta_prefixes: tuple[str, ...] = (
        "```",
        "as an ai",
        "error:",
        "here is ",
        "here's ",
        "i cannot",
        "i can't",
        "i'm sorry",
        "i’m sorry",
        "note:",
        "chapter:",
        "polished chapter:",
        "repaired chapter:",
        "draft:",
        "output:",
        "sorry,",
        "warning:",
    )
    forbidden_meta_fragments: tuple[str, ...] = (
        "如果你希望我",
        "请确认",
        "请告诉我",
        "待润色的原稿",
        "真正想让我处理",
        "我把这一章从头看到尾",
        "按我的职责",
        "你交给我的稿子",
        "你贴出的这段",
        "一旦你确认",
        "please confirm",
        "would you like me",
        "the text you provided",
        "如果你希望",
        "请确认",
        "请告诉我",
        "待润色的原稿",
    )


CHAPTER_CONTRACT = TextContract(name="chapter", min_chars=1)
POLISH_CONTRACT = TextContract(name="polished_chapter", min_chars=1)
REPAIR_CONTRACT = TextContract(name="repaired_chapter", min_chars=1)


def validate_text_output(value: object, contract: TextContract) -> str:
    if not isinstance(value, str):
        raise ModelOutputError(f"{contract.name} output must be a string")

    text = value.strip()
    if not text:
        raise ModelOutputError(f"{contract.name} output is empty")

    if len(text) < contract.min_chars:
        raise ModelOutputError(
            f"{contract.name} output is too short: {len(text)} < {contract.min_chars}"
        )

    mojibake = detect_mojibake(text)
    if mojibake["reject"]:
        raise ModelOutputError(
            f"{contract.name} output looks mojibake-corrupted: {mojibake['reason']}"
        )

    if text.startswith(contract.forbidden_prefixes):
        raise ModelOutputError(f"{contract.name} output looks like structured data, not prose")

    lowered = text.lower()
    matched_prefix = next(
        (prefix for prefix in contract.forbidden_meta_prefixes if lowered.startswith(prefix)),
        None,
    )
    if matched_prefix:
        raise ModelOutputError(
            f"{contract.name} output looks like assistant commentary, not prose: starts with {matched_prefix!r}"
        )

    matched_fragment = next(
        (fragment for fragment in contract.forbidden_meta_fragments if fragment in lowered or fragment in text),
        None,
    )
    if matched_fragment:
        raise ModelOutputError(
            f"{contract.name} output looks like assistant commentary, not prose: contains {matched_fragment!r}"
        )

    markdown_marker = _markdown_wrapper_marker(text)
    if markdown_marker:
        raise ModelOutputError(
            f"{contract.name} output looks like Markdown formatting, not prose: starts with {markdown_marker!r}"
        )

    if contract.min_cjk_ratio > 0:
        cjk_ratio = _cjk_ratio(text)
        if cjk_ratio < contract.min_cjk_ratio:
            raise ModelOutputError(
                f"{contract.name} output is not predominantly Simplified Chinese prose: "
                f"CJK ratio {cjk_ratio:.2f} < {contract.min_cjk_ratio:.2f}"
            )

    return text


def validate_polished_output(value: object, source_text: str, *, language: str | None = None) -> str:
    text = validate_text_output(value, POLISH_CONTRACT)
    _validate_polish_completeness(text, source_text)
    if _requires_simplified_chinese(language) or _cjk_ratio(source_text) >= 0.35:
        _validate_simplified_chinese_ratio(text, "polished_chapter output did not preserve Simplified Chinese prose")
    return text


def validate_language_output(value: object, contract: TextContract, *, language: str | None = None) -> str:
    text = validate_text_output(value, contract)
    if _requires_simplified_chinese(language):
        _validate_simplified_chinese_ratio(
            text,
            f"{contract.name} output did not match configured Simplified Chinese language",
        )
    return text


def _markdown_wrapper_marker(text: str) -> str | None:
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if not first_line:
        return None
    if re.match(r"^#{1,6}\s+\S", first_line):
        return first_line[:20]
    if re.match(r"^(-{3,}|\*{3,}|_{3,})$", first_line):
        return first_line
    if re.match(r"^chapter\s+\d+\s*$", first_line, flags=re.IGNORECASE):
        return first_line
    if re.match(r"^\*\*[^*]+\*\*\s*$", first_line):
        return first_line[:20]
    return None


def _validate_polish_completeness(text: str, source_text: str) -> None:
    source = str(source_text or "").strip()
    output = str(text or "").strip()
    if len(source) >= 1200 and len(output) < int(len(source) * 0.65):
        raise ModelOutputError(
            "polished_chapter output appears truncated or over-compressed: "
            f"{len(output)} chars from {len(source)} source chars"
        )
    if len(output) >= 120 and not _ends_like_complete_prose(output):
        raise ModelOutputError("polished_chapter output appears truncated: missing terminal prose punctuation")


def _ends_like_complete_prose(text: str) -> bool:
    stripped = text.rstrip()
    if not stripped:
        return False
    terminal = stripped[-1]
    terminal_punctuation = {
        ".",
        "!",
        "?",
        "\u3002",
        "\uff01",
        "\uff1f",
        "\u2026",
        '"',
        "'",
        "\u201d",
        "\u2019",
        "\uff09",
        "\u300d",
        "\u300f",
        "\u3011",
        "\u300b",
    }
    return terminal in terminal_punctuation


def _cjk_ratio(text: str) -> float:
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin_count = len(re.findall(r"[A-Za-z]", text))
    denominator = cjk_count + latin_count
    if denominator == 0:
        return 0.0
    return cjk_count / denominator


def _requires_simplified_chinese(language: str | None) -> bool:
    normalized = str(language or "").strip().lower().replace("_", "-")
    return normalized in {"zh", "zh-cn", "zh-hans", "chinese", "simplified-chinese"}


def _validate_simplified_chinese_ratio(text: str, message: str) -> None:
    cjk_ratio = _cjk_ratio(text)
    if cjk_ratio < 0.35:
        raise ModelOutputError(f"{message}: CJK ratio {cjk_ratio:.2f} < 0.35")


def detect_mojibake(text: str) -> dict[str, object]:
    normalized = str(text or "")
    visible = re.sub(r"\s+", "", normalized)
    visible_len = len(visible)
    if visible_len == 0:
        return {
            "looks_corrupted": False,
            "reject": False,
            "reason": "",
            "latin_marker_count": 0,
            "marker_fragments": [],
            "marker_density": 0.0,
        }

    latin_marker_count = sum(1 for char in visible if char in _LATIN_MOJIBAKE_CHARS)
    replacement_count = visible.count("\ufffd")
    fragment_hits = [fragment for fragment in _MOJIBAKE_FRAGMENTS if fragment in normalized]
    marker_count = latin_marker_count + replacement_count + sum(len(fragment) for fragment in fragment_hits)
    marker_density = marker_count / visible_len

    if replacement_count:
        return _mojibake_result(
            True,
            True,
            "replacement character present",
            latin_marker_count,
            replacement_count,
            fragment_hits,
            marker_density,
        )

    if fragment_hits and (latin_marker_count >= 2 or len(fragment_hits) >= 2):
        return _mojibake_result(
            True,
            latin_marker_count >= 2,
            "known mojibake fragments present",
            latin_marker_count,
            replacement_count,
            fragment_hits,
            marker_density,
        )

    if latin_marker_count >= 6 and marker_density >= 0.01:
        return _mojibake_result(
            True,
            True,
            "latin-1 mojibake marker density too high",
            latin_marker_count,
            replacement_count,
            fragment_hits,
            marker_density,
        )

    return _mojibake_result(
        False,
        False,
        "",
        latin_marker_count,
        replacement_count,
        fragment_hits,
        marker_density,
    )


def _mojibake_result(
    looks_corrupted: bool,
    reject: bool,
    reason: str,
    latin_marker_count: int,
    replacement_count: int,
    fragment_hits: list[str],
    marker_density: float,
) -> dict[str, object]:
    return {
        "looks_corrupted": bool(looks_corrupted),
        "reject": bool(reject),
        "reason": reason,
        "latin_marker_count": int(latin_marker_count),
        "replacement_count": int(replacement_count),
        "marker_fragments": fragment_hits[:8],
        "marker_density": round(marker_density, 4),
    }
