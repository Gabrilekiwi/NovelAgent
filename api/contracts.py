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
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.stage = stage
        self.model = model
        self.cause_type = type(cause).__name__ if cause is not None else None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "provider": self.provider,
            "stage": self.stage,
            "model": self.model,
            "cause_type": self.cause_type,
            "message": str(self),
        }


class ModelOutputError(ValueError):
    pass


@dataclass(frozen=True)
class TextContract:
    name: str
    min_chars: int = 1
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

    markdown_marker = _markdown_wrapper_marker(text)
    if markdown_marker:
        raise ModelOutputError(
            f"{contract.name} output looks like Markdown formatting, not prose: starts with {markdown_marker!r}"
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
