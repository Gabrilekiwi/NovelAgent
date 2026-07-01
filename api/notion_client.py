from __future__ import annotations

import json
from typing import Any, Callable
from urllib import request

from core.config import get_config


NOTION_VERSION = "2022-06-28"
NOTION_API_BASE = "https://api.notion.com/v1"

Transport = Callable[[str, dict[str, str], dict[str, Any]], dict[str, Any]]


class NotionClientError(RuntimeError):
    pass


def query_database_pages(
    *,
    database_id: str | None = None,
    api_key: str | None = None,
    page_size: int = 100,
    transport: Transport | None = None,
) -> list[dict[str, Any]]:
    config = get_config()
    database_id = database_id or config.notion_database_id
    api_key = api_key or config.notion_api_key
    if not database_id:
        raise NotionClientError("NOTION_DATABASE_ID or NOVELAGENT_NOTION_DATABASE_ID is required.")
    if not api_key:
        raise NotionClientError("NOTION_API_KEY is required.")

    url = f"{NOTION_API_BASE}/databases/{database_id}/query"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }
    caller = transport or _urllib_transport

    pages: list[dict[str, Any]] = []
    start_cursor: str | None = None
    while True:
        body: dict[str, Any] = {"page_size": page_size}
        if start_cursor:
            body["start_cursor"] = start_cursor

        payload = caller(url, headers, body)
        results = payload.get("results", [])
        if not isinstance(results, list):
            raise NotionClientError("Notion database query response missing results list.")
        pages.extend(page for page in results if isinstance(page, dict))

        if not payload.get("has_more"):
            break
        next_cursor = payload.get("next_cursor")
        if not isinstance(next_cursor, str) or not next_cursor:
            raise NotionClientError("Notion response has_more without next_cursor.")
        start_cursor = next_cursor

    return pages


def create_database_page(
    *,
    properties: dict[str, Any],
    database_id: str | None = None,
    api_key: str | None = None,
    transport: Transport | None = None,
) -> dict[str, Any]:
    config = get_config()
    database_id = database_id or config.notion_database_id
    api_key = api_key or config.notion_api_key
    if not database_id:
        raise NotionClientError("NOTION_DATABASE_ID or NOVELAGENT_NOTION_DATABASE_ID is required.")
    if not api_key:
        raise NotionClientError("NOTION_API_KEY is required.")
    if not isinstance(properties, dict) or not properties:
        raise NotionClientError("Notion page properties are required.")

    url = f"{NOTION_API_BASE}/pages"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }
    body = {
        "parent": {"database_id": database_id},
        "properties": properties,
    }
    caller = transport or _urllib_transport
    payload = caller(url, headers, body)
    if not isinstance(payload, dict):
        raise NotionClientError("Notion page create response must be an object.")
    return payload


def _urllib_transport(url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = request.Request(url, data=data, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - normalize network/client failures.
        raise NotionClientError(str(exc)) from exc
