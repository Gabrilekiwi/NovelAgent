"""External service adapters used by NovelAgent v1.0."""

from api.claude_client import polish_chapter
from api.contracts import ModelCallError, ModelOutputError, TextContract, validate_text_output
from api.notion_client import NotionClientError, create_database_page, query_database_pages
from api.openai_client import chat_completion

__all__ = [
    "ModelCallError",
    "ModelOutputError",
    "NotionClientError",
    "TextContract",
    "chat_completion",
    "create_database_page",
    "polish_chapter",
    "query_database_pages",
    "validate_text_output",
]
