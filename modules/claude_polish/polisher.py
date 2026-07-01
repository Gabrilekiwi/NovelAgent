from __future__ import annotations

from api.contracts import POLISH_CONTRACT, validate_text_output
from api.claude_client import polish_chapter as polish_with_claude


def polish_chapter(chapter_text: str, *, dry_run: bool = False) -> str:
    output = polish_with_claude(chapter_text, dry_run=dry_run)
    return validate_text_output(output, POLISH_CONTRACT)
