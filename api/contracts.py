from __future__ import annotations

from dataclasses import dataclass
import re


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
        return data


def classify_model_failure(cause_type: str | None, message: str) -> str:
    combined = f"{cause_type or ''} {message}".lower()
    if "timeout" in combined or "timed out" in combined:
        return "timeout"
    if "connection" in combined or "connect" in combined:
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
    if "authentication" in combined or "permission" in combined or "api key" in combined:
        return "configuration"
    return "provider_error"


def is_retryable_failure(failure_category: str | None) -> bool:
    return failure_category in {"connection", "timeout", "transient_provider_error", "rate_limit"}


class ModelOutputError(ValueError):
    pass


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
