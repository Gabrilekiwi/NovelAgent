from __future__ import annotations

import json
import http.client
import socket
import ssl
from typing import Any, Callable
from urllib import error, parse, request

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
    config = get_config()
    data = json.dumps(body).encode("utf-8")
    req = request.Request(url, data=data, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=max(1, int(config.notion_timeout_seconds))) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        raise NotionClientError(_format_http_error(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - normalize network/client failures.
        fallback = _direct_ip_transport(
            url,
            headers,
            data,
            timeout=max(1, int(config.notion_timeout_seconds)),
        )
        if fallback["ok"]:
            return fallback["payload"]
        raise NotionClientError(f"{exc}; direct IP fallback failed: {fallback['error']}") from exc


def _format_http_error(exc: error.HTTPError) -> str:
    try:
        detail = exc.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - best-effort diagnostics only.
        detail = ""
    detail = detail[:500]
    return f"Notion API HTTP {exc.code}: {detail}" if detail else f"Notion API HTTP {exc.code}"


def _direct_ip_transport(
    url: str,
    headers: dict[str, str],
    data: bytes,
    *,
    timeout: int,
) -> dict[str, Any]:
    parsed = parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return {"ok": False, "error": "direct IP fallback only supports HTTPS URLs"}
    host = parsed.hostname
    port = parsed.port or 443
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"

    errors: list[str] = []
    for address in _resolve_hosts(host, port):
        try:
            payload = _https_json_request(
                connect_host=address,
                server_host=host,
                port=port,
                target=target,
                headers=headers,
                data=data,
                timeout=timeout,
            )
            return {"ok": True, "payload": payload}
        except Exception as exc:  # noqa: BLE001 - try the next resolved address.
            errors.append(f"{address}: {type(exc).__name__}: {exc}")
    return {"ok": False, "error": "; ".join(errors) or f"no resolved addresses for {host}"}


def _resolve_hosts(host: str, port: int) -> list[str]:
    addresses: list[str] = []
    seen: set[str] = set()
    for family, socktype, proto, _canonname, sockaddr in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        del family, socktype, proto
        address = str(sockaddr[0])
        if address in seen:
            continue
        seen.add(address)
        addresses.append(address)
    return addresses


class _ResolvedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, connect_host: str, *, server_host: str, **kwargs: Any) -> None:
        self._connect_host = connect_host
        super().__init__(server_host, **kwargs)

    def connect(self) -> None:
        sock = self._create_connection(
            (self._connect_host, self.port),
            self.timeout,
            self.source_address,
        )
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _https_json_request(
    *,
    connect_host: str,
    server_host: str,
    port: int,
    target: str,
    headers: dict[str, str],
    data: bytes,
    timeout: int,
) -> dict[str, Any]:
    request_headers = dict(headers)
    request_headers["Host"] = server_host
    connection = _ResolvedHTTPSConnection(
        connect_host,
        server_host=server_host,
        port=port,
        timeout=timeout,
        context=ssl.create_default_context(),
    )
    try:
        connection.request("POST", target, body=data, headers=request_headers)
        response = connection.getresponse()
        payload = response.read().decode("utf-8")
    finally:
        connection.close()
    if response.status < 200 or response.status >= 300:
        raise NotionClientError(f"Notion API HTTP {response.status}: {payload[:500]}")
    return json.loads(payload)
