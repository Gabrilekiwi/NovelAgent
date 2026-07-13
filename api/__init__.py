"""External service adapters used by NovelAgent v1.0."""

from api.claude_client import polish_chapter
from api.contracts import ModelCallError, ModelOutputError, TextContract, detect_mojibake, validate_text_output
from api.notion_client import NotionClientError, create_database_page, query_database_pages
from api.openai_client import chat_completion
from api.retry import RetryOperationError, RetryPolicy, retry_policy_for_profile

__all__ = [
    "ModelCallError",
    "ModelOutputError",
    "NotionClientError",
    "RetryOperationError",
    "RetryPolicy",
    "TextContract",
    "chat_completion",
    "create_database_page",
    "detect_mojibake",
    "polish_chapter",
    "query_database_pages",
    "retry_policy_for_profile",
    "validate_text_output",
]
