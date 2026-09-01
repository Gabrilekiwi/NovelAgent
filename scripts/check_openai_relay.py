#!/usr/bin/env python3
"""Probe the configured OpenAI-compatible relay without loading app runtime.

The script reads only ``.env`` from the repository root.  It intentionally
does not import NovelAgent modules, mutate process environment variables,
honour proxy environment variables, follow redirects, write artifacts, or
perform retries.  This makes it suitable for isolating relay connectivity
from the application's provider workflow.

The default probe sends one small, non-streaming chat request and may incur a
small model charge.  Use ``--skip-chat`` for a no-generation check that stops
after the authenticated model-list endpoint.
"""

from __future__ import annotations

import argparse
import json
import socket
import ssl
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, parse, request

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
SAFE_RESPONSE_HEADERS = {"cf-ray", "server", "via", "x-request-id", "openai-request-id"}


@dataclass(frozen=True)
class RelayConfig:
    base_url: str
    api_key: str
    model: str


class NoRedirect(request.HTTPRedirectHandler):
    """Do not forward the configured bearer token to an unexpected host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout",
        type=float,
        default=90.0,
        help="Network timeout per request in seconds (default: 90).",
    )
    parser.add_argument(
        "--skip-chat",
        action="store_true",
        help="Only call GET /models; do not create a billable model request.",
    )
    return parser.parse_args()


def load_config() -> RelayConfig:
    values = dotenv_values(ENV_PATH)
    base_url = str(values.get("OPENAI_BASE_URL") or "").strip().rstrip("/")
    api_key = str(values.get("OPENAI_API_KEY") or "").strip()
    model = str(values.get("OPENAI_MODEL") or "").strip()
    if not base_url or not api_key or not model:
        missing = [
            name
            for name, value in {
                "OPENAI_BASE_URL": base_url,
                "OPENAI_API_KEY": api_key,
                "OPENAI_MODEL": model,
            }.items()
            if not value
        ]
        raise ValueError(f"Missing required .env value(s): {', '.join(missing)}")
    parsed = parse.urlparse(base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("OPENAI_BASE_URL must be a plain https URL without query or fragment")
    return RelayConfig(base_url=base_url, api_key=api_key, model=model)


def safe_headers(headers: Any) -> dict[str, str]:
    return {
        str(name).lower(): str(value)
        for name, value in headers.items()
        if str(name).lower() in SAFE_RESPONSE_HEADERS
    }


def clipped_text(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace").strip().replace("\n", " ")
    return text[:500]


def request_json(
    opener: request.OpenerDirector,
    *,
    method: str,
    url: str,
    api_key: str,
    timeout: float,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": "NovelAgent-relay-probe/1.0",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=headers, method=method)
    started = time.monotonic()
    try:
        with opener.open(req, timeout=timeout) as response:
            raw = response.read()
            elapsed_ms = round((time.monotonic() - started) * 1000)
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                return {
                    "ok": False,
                    "status": int(response.status),
                    "elapsed_ms": elapsed_ms,
                    "kind": "invalid_json_response",
                    "detail": str(exc),
                    "headers": safe_headers(response.headers),
                    "body_preview": clipped_text(raw),
                }
            return {
                "ok": 200 <= int(response.status) < 300,
                "status": int(response.status),
                "elapsed_ms": elapsed_ms,
                "headers": safe_headers(response.headers),
                "body": body,
            }
    except error.HTTPError as exc:
        elapsed_ms = round((time.monotonic() - started) * 1000)
        return {
            "ok": False,
            "status": int(exc.code),
            "elapsed_ms": elapsed_ms,
            "kind": "http_error",
            "headers": safe_headers(exc.headers),
            "body_preview": clipped_text(exc.read()),
        }
    except (error.URLError, TimeoutError, OSError, ssl.SSLError) as exc:
        elapsed_ms = round((time.monotonic() - started) * 1000)
        return {
            "ok": False,
            "elapsed_ms": elapsed_ms,
            "kind": type(exc).__name__,
            "detail": str(exc),
        }


def model_list_summary(result: dict[str, Any], configured_model: str) -> dict[str, Any]:
    summary = {key: value for key, value in result.items() if key != "body"}
    body = result.get("body")
    models = body.get("data") if isinstance(body, dict) else None
    identifiers = [item.get("id") for item in models if isinstance(item, dict) and isinstance(item.get("id"), str)] if isinstance(models, list) else []
    summary["model_count"] = len(identifiers)
    summary["configured_model_listed"] = configured_model in identifiers
    return summary


def chat_summary(result: dict[str, Any]) -> dict[str, Any]:
    summary = {key: value for key, value in result.items() if key != "body"}
    body = result.get("body")
    if not isinstance(body, dict):
        summary["response_contract_valid"] = False
        return summary
    choices = body.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else None
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    usage = body.get("usage")
    summary.update(
        {
            "response_contract_valid": isinstance(content, str) and bool(content.strip()),
            "response_text": content if isinstance(content, str) else None,
            "response_model": body.get("model") if isinstance(body.get("model"), str) else None,
            "response_id": body.get("id") if isinstance(body.get("id"), str) else None,
            "usage": usage if isinstance(usage, dict) else None,
        }
    )
    return summary


def main() -> int:
    args = parse_args()
    if not 1 <= args.timeout <= 300:
        raise ValueError("--timeout must be between 1 and 300 seconds")
    config = load_config()
    parsed = parse.urlparse(config.base_url)
    host = parsed.hostname
    assert host is not None
    port = parsed.port or 443

    report: dict[str, Any] = {
        "probe": "openai_compatible_relay",
        "environment_isolation": {
            "reads": ".env only",
            "imports_project_runtime": False,
            "proxy_environment_used": False,
            "redirects_followed": False,
            "writes_artifacts": False,
        },
        "configuration": {
            "base_url": config.base_url,
            "model": config.model,
            "api_key_configured": True,
            "timeout_seconds": args.timeout,
        },
    }
    try:
        started = time.monotonic()
        addresses = sorted({entry[4][0] for entry in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
        report["dns"] = {"ok": True, "elapsed_ms": round((time.monotonic() - started) * 1000), "addresses": addresses}
    except socket.gaierror as exc:
        report["dns"] = {"ok": False, "kind": type(exc).__name__, "detail": str(exc)}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 3

    opener = request.build_opener(
        request.ProxyHandler({}),
        NoRedirect(),
        request.HTTPSHandler(context=ssl.create_default_context()),
    )
    model_result = request_json(
        opener,
        method="GET",
        url=f"{config.base_url}/models",
        api_key=config.api_key,
        timeout=args.timeout,
    )
    report["models"] = model_list_summary(model_result, config.model)
    if args.skip_chat:
        report["chat_completion"] = {"skipped": True, "reason": "--skip-chat"}
        report["ok"] = bool(model_result.get("ok"))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 4

    chat_result = request_json(
        opener,
        method="POST",
        url=f"{config.base_url}/chat/completions",
        api_key=config.api_key,
        timeout=args.timeout,
        payload={
            "model": config.model,
            "messages": [{"role": "user", "content": "Return exactly: OK"}],
            "temperature": 0,
            "max_tokens": 4,
            "stream": False,
        },
    )
    report["chat_completion"] = chat_summary(chat_result)
    report["ok"] = bool(chat_result.get("ok")) and bool(report["chat_completion"].get("response_contract_valid"))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 5


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        print(json.dumps({"ok": False, "kind": "configuration_error", "detail": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(2)
