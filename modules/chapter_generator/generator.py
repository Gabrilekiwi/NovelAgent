from __future__ import annotations

from pathlib import Path

from api.contracts import CHAPTER_CONTRACT, validate_text_output
from api.openai_client import chat_completion

_PROMPT_PATH = Path("prompts/chapter_prompt.md")
_DRY_RUN_CHAPTER = (
    "Continue from shelter, the first alarm sounded just as the shelter lights dimmed. The protagonist stood before the sealed gate "
    "and saw that the route once marked safe had been cut off by a new infection zone. She had to choose "
    "between rescuing a teammate and protecting the serum sample, and that choice pushed the team into open conflict."
)


def _load_prompt() -> str:
    if _PROMPT_PATH.exists():
        return _PROMPT_PATH.read_text(encoding="utf-8")
    return (
        "You are a professional long-form fiction chapter writer. Generate continuous prose that advances plot, "
        "preserves continuity, and creates meaningful conflict."
    )


def generate_chapter(input_pack: str, *, dry_run: bool = False) -> str:
    if dry_run:
        output = _DRY_RUN_CHAPTER
    else:
        output = chat_completion(
            [
                {"role": "system", "content": _load_prompt()},
                {"role": "user", "content": input_pack},
            ],
            stage="chapter_generation",
        )

    return validate_text_output(output, CHAPTER_CONTRACT)
