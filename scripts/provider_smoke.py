from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.claude_client import polish_chapter
from api.contracts import CHAPTER_CONTRACT, ModelCallError, validate_text_output
from api.openai_client import chat_completion
from api.notion_client import query_database_pages
from core.config import get_config
from core.director import ModelDirector, decide_next_step
from core.runtime_paths import init_runtime_state
from core.schema import validate_schema
from core.state.builder import build_snapshot_state_with_audit
from core.state.input_pack import build_input_pack
from core.state.memory import load_memory_context
from core.state.memory_writer import NotionMemoryWriter
from core.state.snapshot import load_snapshot


PROVIDERS = ("openai", "claude", "notion")
REQUIRED_CHECKS = (
    ("openai", "director"),
    ("openai", "chapter_generation"),
    ("claude", "polish"),
    ("notion", "read"),
    ("notion", "writeback"),
    ("notion", "readback"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run real provider smoke checks for NovelAgent.")
    parser.add_argument(
        "--providers",
        nargs="+",
        choices=PROVIDERS,
        default=list(PROVIDERS),
        help="Provider checks to run.",
    )
    parser.add_argument(
        "--work-dir",
        default=None,
        help="Output directory. Defaults to .tmp/runtime/provider_smoke/<timestamp>.",
    )
    parser.add_argument(
        "--steps",
        type=int,
        default=1,
        help="Maximum chapter generation steps for provider smoke. Currently capped to 1.",
    )
    parser.add_argument(
        "--max-input-chars",
        type=int,
        default=4000,
        help="Maximum input-pack characters sent to OpenAI chapter generation smoke.",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=800,
        help="Maximum OpenAI output tokens per smoke request.",
    )
    parser.add_argument(
        "--openai-max-retries",
        type=int,
        default=0,
        help="OpenAI SDK retries per request. Defaults to 0 so provider smoke retries stay explicit.",
    )
    parser.add_argument(
        "--openai-scene-limit",
        type=int,
        default=1,
        help="Maximum compact scene probes for the OpenAI chapter-generation smoke check. Currently capped to 1.",
    )
    parser.add_argument(
        "--openai-model",
        default=None,
        help="Override OPENAI_MODEL for OpenAI provider smoke.",
    )
    parser.add_argument(
        "--openai-base-url",
        default=None,
        help="Override OPENAI_BASE_URL for OpenAI provider smoke.",
    )
    parser.add_argument(
        "--no-openai-base-url",
        action="store_true",
        help="Clear OPENAI_BASE_URL for OpenAI provider smoke and use the SDK default endpoint.",
    )
    parser.add_argument(
        "--request-timeout",
        type=int,
        default=20,
        help="Per-request provider timeout in seconds.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=0,
        help="Retries for non-writing provider subchecks such as model calls and Notion reads.",
    )
    parser.add_argument(
        "--retry-delay-seconds",
        type=float,
        default=1.0,
        help="Delay between provider smoke retry attempts.",
    )
    parser.add_argument(
        "--claude-model",
        default=None,
        help="Override CLAUDE_MODEL for Claude provider smoke.",
    )
    parser.add_argument(
        "--claude-base-url",
        default=None,
        help="Override CLAUDE_BASE_URL for Claude provider smoke.",
    )
    parser.add_argument(
        "--claude-user-agent",
        default=None,
        help="Override CLAUDE_USER_AGENT for Claude provider smoke.",
    )
    parser.add_argument(
        "--claude-max-tokens",
        type=int,
        default=None,
        help="Override CLAUDE_MAX_TOKENS for Claude provider smoke.",
    )
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="Return success with skipped provider checks when required config is missing.",
    )
    parser.add_argument(
        "--require-all-checks",
        action="store_true",
        help="Return failure unless every Phase 4 required check passes.",
    )
    parser.add_argument(
        "--ignore-dotenv",
        action="store_true",
        help="Ignore local .env when checking whether provider credentials are configured.",
    )
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        help="Clear HTTP(S)/ALL proxy environment variables for this provider smoke process.",
    )
    parser.add_argument(
        "--notion-write",
        action="store_true",
        help="Actually create a Notion smoke memory item and read it back.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    work_dir = _work_dir(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    if args.ignore_dotenv:
        os.environ["NOVELAGENT_SKIP_DOTENV"] = "1"
    limits = _normalize_limits(args)
    report = {
        "id": f"provider_smoke_{_timestamp()}",
        "work_dir": str(work_dir),
        "request": {
            "providers": list(args.providers),
            "notion_write": bool(args.notion_write),
            "allow_missing": bool(args.allow_missing),
            "ignore_dotenv": bool(args.ignore_dotenv),
            "no_proxy": bool(args.no_proxy),
        },
        "limits": limits,
        "providers": {},
        "ok": True,
    }
    os.environ["OPENAI_TIMEOUT_SECONDS"] = str(limits["request_timeout"])
    os.environ["OPENAI_MAX_OUTPUT_TOKENS"] = str(limits["max_output_tokens"])
    os.environ["OPENAI_MAX_RETRIES"] = str(limits["openai_max_retries"])
    os.environ["CLAUDE_TIMEOUT_SECONDS"] = str(limits["request_timeout"])
    os.environ["NOTION_TIMEOUT_SECONDS"] = str(limits["request_timeout"])
    if args.no_proxy:
        _clear_proxy_env()
    if args.openai_model:
        os.environ["OPENAI_MODEL"] = str(args.openai_model)
    if args.no_openai_base_url:
        os.environ["OPENAI_BASE_URL"] = ""
    elif args.openai_base_url is not None:
        os.environ["OPENAI_BASE_URL"] = str(args.openai_base_url)
    if args.claude_model:
        os.environ["CLAUDE_MODEL"] = str(args.claude_model)
    if args.claude_base_url:
        os.environ["CLAUDE_BASE_URL"] = str(args.claude_base_url)
    if args.claude_user_agent:
        os.environ["CLAUDE_USER_AGENT"] = str(args.claude_user_agent)
    if args.claude_max_tokens is not None:
        os.environ["CLAUDE_MAX_TOKENS"] = str(limits["claude_max_tokens"])
    report["config_status"] = _config_status()

    runtime = init_runtime_state(
        snapshot_target=work_dir / "snapshot.json",
        memory_target=work_dir / "notion_memory.json",
        overwrite=True,
    )
    report["runtime"] = runtime

    for provider in args.providers:
        try:
            if provider == "openai":
                result = _smoke_openai(
                    work_dir,
                    max_input_chars=limits["max_input_chars"],
                    scene_limit=limits["openai_scene_limit"],
                    retries=limits["retries"],
                    retry_delay_seconds=limits["retry_delay_seconds"],
                )
            elif provider == "claude":
                result = _smoke_claude(retries=limits["retries"], retry_delay_seconds=limits["retry_delay_seconds"])
            else:
                result = _smoke_notion(
                    write=args.notion_write,
                    retries=limits["retries"],
                    retry_delay_seconds=limits["retry_delay_seconds"],
                )
        except MissingConfig as exc:
            result = {
                "ok": False,
                "status": "skipped" if args.allow_missing else "failed",
                "reason": str(exc),
                "missing_config_groups": exc.groups,
            }
            if not args.allow_missing:
                report["ok"] = False
        except Exception as exc:  # noqa: BLE001 - provider smoke must preserve diagnostics.
            result = _provider_failure_result(exc)
            report["ok"] = False
        report["providers"][provider] = result
        if not result.get("ok") and result.get("status") != "skipped":
            report["ok"] = False

    report_path = work_dir / "provider_smoke_report.json"
    report["report_path"] = str(report_path)
    report["required_checks"] = _required_check_summary(report["providers"])
    report["required_checks_ok"] = all(bool(item.get("ok")) for item in report["required_checks"])
    report["diagnostics"] = _build_diagnostics(report)
    validate_schema(report, "provider_smoke_report.schema.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    exit_ok = bool(report["ok"]) and (not args.require_all_checks or bool(report["required_checks_ok"]))
    raise SystemExit(0 if exit_ok else 1)


def _normalize_limits(args: argparse.Namespace) -> dict[str, Any]:
    claude_max_tokens = None
    if args.claude_max_tokens is not None:
        claude_max_tokens = max(1, int(args.claude_max_tokens))
    return {
        "steps": max(1, min(int(args.steps), 1)),
        "max_input_chars": max(1, int(args.max_input_chars)),
        "max_output_tokens": max(1, int(args.max_output_tokens)),
        "openai_max_retries": max(0, int(args.openai_max_retries)),
        "openai_scene_limit": max(1, int(args.openai_scene_limit)),
        "request_timeout": max(1, int(args.request_timeout)),
        "retries": max(0, int(args.retries)),
        "retry_delay_seconds": max(0.0, float(args.retry_delay_seconds)),
        "openai_model": args.openai_model,
        "openai_base_url": _base_url_limit_label(args),
        "claude_model": args.claude_model,
        "claude_base_url": _set_status(args.claude_base_url),
        "claude_user_agent": _set_status(args.claude_user_agent),
        "claude_max_tokens": claude_max_tokens,
        "require_all_checks": bool(args.require_all_checks),
    }


def _config_status() -> dict[str, Any]:
    config = get_config()
    return {
        "openai": {
            "configured": bool(config.openai_api_key),
            "api_key": _set_status(config.openai_api_key),
            "model": config.openai_model,
            "base_url": "custom" if config.openai_base_url else "sdk_default",
            "timeout_seconds": config.openai_timeout_seconds,
            "max_output_tokens": config.openai_max_output_tokens,
            "max_retries": config.openai_max_retries,
        },
        "claude": {
            "configured": bool(config.anthropic_api_key and config.claude_model),
            "api_key": _set_status(config.anthropic_api_key),
            "base_url": "custom" if config.claude_base_url else "sdk_default",
            "user_agent": _set_status(config.claude_user_agent),
            "model": config.claude_model,
            "model_status": _set_status(config.claude_model),
            "timeout_seconds": config.claude_timeout_seconds,
            "max_tokens": config.claude_max_tokens,
        },
        "notion": {
            "configured": bool(config.notion_api_key and config.notion_database_id),
            "api_key": _set_status(config.notion_api_key),
            "database_id": _set_status(config.notion_database_id),
            "timeout_seconds": config.notion_timeout_seconds,
        },
        "proxy": _proxy_status(),
    }


def _set_status(value: object) -> str:
    return "set" if str(value or "").strip() else "missing"


def _clear_proxy_env() -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ[name] = ""


def _proxy_status() -> dict[str, Any]:
    names = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy")
    endpoints: list[dict[str, Any]] = []
    status: dict[str, Any] = {
        "http_proxy": _set_status(os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")),
        "https_proxy": _set_status(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")),
        "all_proxy": _set_status(os.environ.get("ALL_PROXY") or os.environ.get("all_proxy")),
        "no_proxy": _set_status(os.environ.get("NO_PROXY") or os.environ.get("no_proxy")),
        "proxy_endpoints": endpoints,
    }
    for name in names:
        value = os.environ.get(name)
        if not str(value or "").strip() or name.lower() == "no_proxy":
            continue
        endpoints.append(_redacted_proxy_endpoint(name, str(value)))
    return status


def _redacted_proxy_endpoint(name: str, value: str) -> dict[str, Any]:
    parsed = urlparse(value if "://" in value else f"http://{value}")
    port: int | None = None
    try:
        port = parsed.port
    except ValueError:
        port = None
    return {
        "name": name,
        "scheme": parsed.scheme or None,
        "host": parsed.hostname,
        "port": port,
        "has_userinfo": bool(parsed.username or parsed.password),
    }


def _smoke_openai(
    work_dir: Path,
    *,
    max_input_chars: int,
    scene_limit: int = 1,
    retries: int = 0,
    retry_delay_seconds: float = 0.0,
) -> dict[str, Any]:
    config = get_config()
    if not config.openai_api_key:
        raise _missing_config("OpenAI", ["OPENAI_API_KEY"])

    snapshot = load_snapshot(work_dir / "snapshot.json")
    memory = load_memory_context(work_dir / "notion_memory.json", source="file")
    state_result = build_snapshot_state_with_audit(snapshot, memory)
    runtime_snapshot = state_result["snapshot"]
    memory["snapshot_builder_audit"] = state_result["audit"]

    checks: dict[str, Any] = {}
    decision: dict[str, Any] | None = None
    holder: dict[str, Any] = {}

    def run_director_check() -> dict[str, Any]:
        director_decision = ModelDirector(model=config.openai_model)(runtime_snapshot, memory)
        holder["decision"] = director_decision
        return {
            "ok": True,
            "status": "passed",
            "model": config.openai_model,
            "chapter_index": int(director_decision["chapter_index"]),
            "goal": director_decision.get("goal"),
            "workflow": director_decision.get("actions", []),
        }

    checks["director"] = _retry_provider_check(
        run_director_check,
        retries=retries,
        retry_delay_seconds=retry_delay_seconds,
    )
    if checks["director"].get("ok"):
        decision = holder.get("decision")
    else:
        decision = decide_next_step(runtime_snapshot, memory)

    input_pack = build_input_pack(runtime_snapshot, decision, memory)
    limited_input_pack = input_pack[:max(1, int(max_input_chars))]

    def run_chapter_generation_check() -> dict[str, Any]:
        chapter_text = _generate_smoke_chapter_scene(
            limited_input_pack,
            decision=decision,
            max_tokens=config.openai_max_output_tokens,
        )
        return {
            "ok": True,
            "status": "passed",
            "model": config.openai_model,
            "director_source": "model" if checks["director"].get("ok") else "rule_fallback",
            "generation_plan_source": "smoke_compact_scene",
            "scene_count": 1,
            "scene_limit": int(scene_limit),
            "merged_chars": len(chapter_text),
        }

    checks["chapter_generation"] = _retry_provider_check(
        run_chapter_generation_check,
        retries=retries,
        retry_delay_seconds=retry_delay_seconds,
    )

    ok = all(bool(check.get("ok")) for check in checks.values())
    return {
        "ok": ok,
        "status": "passed" if ok else "failed",
        "model": config.openai_model,
        "checks": checks,
        "director_goal": decision.get("goal"),
        "workflow": decision.get("actions", []),
        "max_input_chars": int(max_input_chars),
        "max_output_tokens": config.openai_max_output_tokens,
        "scene_limit": int(scene_limit),
    }


def _generate_smoke_chapter_scene(input_pack: str, *, decision: dict[str, Any], max_tokens: int) -> str:
    prompt = (
        "You are smoke-testing NovelAgent chapter generation. "
        "Write one compact fiction scene in 4-6 sentences. "
        "No heading, no bullet list, no explanation."
    )
    payload = {
        "chapter_goal": str(decision.get("goal") or "Advance the chapter conflict."),
        "required_beats": ["immediate pressure", "sealed gate or route cutoff", "serum sample remains central"],
        "input_pack_excerpt": input_pack,
    }
    text = chat_completion(
        [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        temperature=0.4,
        stage="chapter_generation",
        max_tokens=max(1, int(max_tokens)),
    )
    return validate_text_output(text, CHAPTER_CONTRACT)


def _retry_provider_check(fn, *, retries: int = 0, retry_delay_seconds: float = 0.0) -> dict[str, Any]:
    attempts = max(0, int(retries)) + 1
    retry_delay = max(0.0, float(retry_delay_seconds))
    result: dict[str, Any] | None = None
    for attempt in range(1, attempts + 1):
        try:
            result = fn()
            result["attempts"] = attempt
            return result
        except Exception as exc:  # noqa: BLE001 - provider smoke records the final failed attempt.
            result = _provider_failure_result(exc)
            result["attempts"] = attempt
            if attempt < attempts and retry_delay > 0:
                time.sleep(retry_delay)
    return result or {
        "ok": False,
        "status": "failed",
        "error_type": "RuntimeError",
        "message": "provider check did not run",
        "attempts": 0,
    }


def _required_check_summary(providers: dict[str, Any]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for provider, check in REQUIRED_CHECKS:
        provider_result = providers.get(provider)
        entry: dict[str, Any] = {
            "provider": provider,
            "check": check,
            "ok": False,
            "status": "not_requested",
            "requested": False,
        }
        if not isinstance(provider_result, dict):
            summary.append(entry)
            continue

        entry["requested"] = True
        checks = provider_result.get("checks")
        check_result = checks.get(check) if isinstance(checks, dict) else None
        if isinstance(check_result, dict):
            entry["ok"] = bool(check_result.get("ok"))
            entry["status"] = str(check_result.get("status") or "failed")
            if check_result.get("attempts") is not None:
                entry["attempts"] = check_result.get("attempts")
            if check_result.get("error_type"):
                entry["error_type"] = check_result.get("error_type")
            if check_result.get("message"):
                entry["message"] = check_result.get("message")
            if check_result.get("reason"):
                entry["reason"] = check_result.get("reason")
            if check_result.get("failure_category"):
                entry["failure_category"] = check_result.get("failure_category")
            if check_result.get("retryable") is not None:
                entry["retryable"] = bool(check_result.get("retryable"))
            summary.append(entry)
            continue

        entry["status"] = str(provider_result.get("status") or "failed")
        if provider_result.get("reason"):
            entry["reason"] = provider_result.get("reason")
        if provider_result.get("message"):
            entry["message"] = provider_result.get("message")
        if provider_result.get("error_type"):
            entry["error_type"] = provider_result.get("error_type")
        if provider_result.get("failure_category"):
            entry["failure_category"] = provider_result.get("failure_category")
        if provider_result.get("retryable") is not None:
            entry["retryable"] = bool(provider_result.get("retryable"))
        summary.append(entry)
    return summary


def _build_diagnostics(report: dict[str, Any]) -> dict[str, Any]:
    required_checks = report.get("required_checks") if isinstance(report.get("required_checks"), list) else []
    missing_config: set[str] = set()
    missing_config_groups: list[dict[str, Any]] = []
    failed_checks: list[dict[str, Any]] = []
    skipped_checks: list[dict[str, Any]] = []
    unrequested_checks: list[dict[str, Any]] = []

    providers = report.get("providers") if isinstance(report.get("providers"), dict) else {}
    for provider_name, provider_result in providers.items():
        if isinstance(provider_result, dict):
            missing_config_groups.extend(_provider_missing_config_groups(str(provider_name), provider_result))

    for item in required_checks:
        if not isinstance(item, dict) or item.get("ok"):
            continue
        diagnostic = {
            "provider": item.get("provider"),
            "check": item.get("check"),
            "status": item.get("status"),
        }
        for key in ("attempts", "error_type", "message", "reason", "failure_category", "retryable"):
            if item.get(key) is not None:
                diagnostic[key] = item.get(key)
        if item.get("status") == "skipped":
            skipped_checks.append(diagnostic)
        elif item.get("status") == "not_requested":
            unrequested_checks.append(diagnostic)
        else:
            failed_checks.append(diagnostic)
        reason = str(item.get("reason") or item.get("message") or "")
        missing_config.update(_extract_config_names(reason))
    for group in missing_config_groups:
        missing_config.update(str(name) for name in group.get("any_of", []) if name)

    if bool(report.get("required_checks_ok")):
        status = "passed"
    elif bool(report.get("ok")):
        status = "incomplete"
    else:
        status = "failed"

    return {
        "status": status,
        "missing_config": sorted(missing_config),
        "missing_config_groups": missing_config_groups,
        "failed_checks": failed_checks,
        "skipped_checks": skipped_checks,
        "unrequested_checks": unrequested_checks,
    }


def _provider_missing_config_groups(provider: str, provider_result: dict[str, Any]) -> list[dict[str, Any]]:
    groups = provider_result.get("missing_config_groups")
    if isinstance(groups, list):
        normalized = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            any_of = group.get("any_of")
            names = [str(name) for name in any_of if str(name).strip()] if isinstance(any_of, list) else []
            if not names:
                continue
            normalized.append(
                {
                    "provider": str(group.get("provider") or provider),
                    "requirement": str(group.get("requirement") or "provider_config"),
                    "any_of": names,
                    "status": str(group.get("status") or "missing"),
                }
            )
        return normalized
    return []


def _extract_config_names(text: str) -> list[str]:
    names = set(re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", text))
    return sorted(name for name in names if name.endswith("_KEY") or name.endswith("_ID") or name.endswith("_MODEL"))


def _provider_failure_result(exc: Exception) -> dict[str, Any]:
    failure_category = _failure_category(exc)
    result: dict[str, Any] = {
        "ok": False,
        "status": "failed",
        "error_type": type(exc).__name__,
        "message": str(exc),
        "failure_category": failure_category,
        "retryable": failure_category in {"connection", "timeout", "transient_provider_error"},
    }
    if isinstance(exc, ModelCallError):
        result["model_call"] = exc.to_dict()
    return result


def _failure_category(exc: Exception) -> str:
    message = str(exc).lower()
    cause_type = ""
    if isinstance(exc, ModelCallError):
        cause_type = str(exc.to_dict().get("cause_type") or "").lower()
    combined = f"{cause_type} {message}"
    if "timeout" in combined:
        return "timeout"
    if "connection" in combined or "connect" in combined:
        return "connection"
    if "temporarily unavailable" in combined or "internalservererror" in combined or "503" in combined:
        return "transient_provider_error"
    if "authentication" in combined or "permission" in combined or "api key" in combined:
        return "configuration"
    if isinstance(exc, ModelCallError):
        return "provider_error"
    return "unexpected"


def _base_url_limit_label(args: argparse.Namespace) -> str | None:
    if args.no_openai_base_url:
        return "sdk_default"
    if args.openai_base_url is not None:
        return "custom"
    return None


def _smoke_claude(*, retries: int = 0, retry_delay_seconds: float = 0.0) -> dict[str, Any]:
    config = get_config()
    missing = []
    if not config.anthropic_api_key:
        missing.append(["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"])
    if not config.claude_model:
        missing.append(["CLAUDE_MODEL", "ANTHROPIC_MODEL"])
    if missing:
        raise _missing_config("Claude", missing)
    sample = (
        "The shelter alarm forced the protagonist to choose between rescuing a teammate "
        "and protecting the serum sample."
    )
    def run_polish_check() -> dict[str, Any]:
        polished = polish_chapter(sample, dry_run=False)
        return {
            "ok": True,
            "status": "passed",
            "model": config.claude_model,
            "input_chars": len(sample),
            "output_chars": len(polished),
            "max_tokens": config.claude_max_tokens,
        }

    checks: dict[str, Any] = {
        "polish": _retry_provider_check(
            run_polish_check,
            retries=retries,
            retry_delay_seconds=retry_delay_seconds,
        ),
    }

    ok = all(bool(check.get("ok")) for check in checks.values())
    return {
        "ok": ok,
        "status": "passed" if ok else "failed",
        "model": config.claude_model,
        "checks": checks,
    }


def _smoke_notion(*, write: bool, retries: int = 0, retry_delay_seconds: float = 0.0) -> dict[str, Any]:
    config = get_config()
    missing = []
    if not config.notion_api_key:
        missing.append("NOTION_API_KEY")
    if not config.notion_database_id:
        missing.append(["NOTION_DATABASE_ID", "NOVELAGENT_NOTION_DATABASE_ID"])
    if missing:
        raise _missing_config("Notion", missing)

    def run_read_check() -> dict[str, Any]:
        pages = query_database_pages(page_size=1)
        return {
            "ok": True,
            "status": "passed",
            "read_count": len(pages),
        }

    checks: dict[str, Any] = {
        "read": _retry_provider_check(
            run_read_check,
            retries=retries,
            retry_delay_seconds=retry_delay_seconds,
        ),
    }

    if checks["read"].get("status") == "failed":
        checks["writeback"] = _provider_skip_result("notion read failed before writeback")
        checks["readback"] = _provider_skip_result("notion read failed before remote readback")
        return _notion_result(checks)

    if not write:
        checks["writeback"] = _provider_skip_result("pass --notion-write to create a smoke memory item")
        checks["readback"] = _provider_skip_result("pass --notion-write to create and read back a smoke memory item")
        return _notion_result(checks)

    smoke_id = f"provider_smoke:{_timestamp()}"
    writer = NotionMemoryWriter(verify_remote_readback=True, dedupe_existing=True)
    try:
        writeback = writer(
            [
                {
                    "id": smoke_id,
                    "type": "timeline_event",
                    "name": "provider_smoke",
                    "data": {"summary": "NovelAgent provider smoke writeback/readback check."},
                }
            ]
        )
        checks["writeback"] = {
            "ok": True,
            "status": "passed",
            "attempts": 1,
            "written": writeback.get("written"),
            "skipped": writeback.get("skipped"),
            "item_mappings": writeback.get("item_mappings", []),
        }
        verification = writeback.get("verification") if isinstance(writeback.get("verification"), dict) else {}
        checks["readback"] = {
            "ok": verification.get("status") == "verified",
            "status": "passed" if verification.get("status") == "verified" else "failed",
            "attempts": 1,
            "verification": verification,
        }
    except Exception as exc:  # noqa: BLE001 - provider smoke must preserve Notion writeback diagnostics.
        checks["writeback"] = _provider_failure_result(exc)
        checks["writeback"]["attempts"] = 1
        checks["readback"] = _provider_skip_result("writeback failed before remote readback")

    return _notion_result(checks)


def _notion_result(checks: dict[str, Any]) -> dict[str, Any]:
    failed = [check for check in checks.values() if check.get("status") == "failed"]
    return {
        "ok": not failed,
        "status": "failed" if failed else "passed",
        "checks": checks,
    }


def _provider_skip_result(reason: str) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "skipped",
        "reason": reason,
    }


def _work_dir(value: str | None) -> Path:
    if value:
        path = Path(value)
        return path if path.is_absolute() else ROOT / path
    return ROOT / ".tmp" / "runtime" / "provider_smoke" / _timestamp()


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


class MissingConfig(RuntimeError):
    def __init__(self, provider: str, groups: list[dict[str, Any]]) -> None:
        self.provider = provider
        self.groups = groups
        super().__init__(_format_missing_config(provider, groups))


def _missing_config(provider: str, names: Sequence[str | Sequence[str]]) -> MissingConfig:
    groups: list[dict[str, Any]] = []
    for index, raw_names in enumerate(names, start=1):
        if isinstance(raw_names, list):
            options = sorted({str(name) for name in raw_names if str(name).strip()})
        else:
            options = [str(raw_names)] if str(raw_names).strip() else []
        if not options:
            continue
        groups.append(
            {
                "provider": provider.lower(),
                "requirement": _requirement_name(options, fallback=f"config_{index}"),
                "any_of": options,
                "status": "missing",
            }
        )
    return MissingConfig(provider, groups)


def _requirement_name(options: list[str], *, fallback: str) -> str:
    option_set = set(options)
    if option_set == {"OPENAI_API_KEY"}:
        return "openai_api_key"
    if option_set == {"ANTHROPIC_API_KEY"} or option_set == {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}:
        return "anthropic_api_key"
    if option_set == {"CLAUDE_MODEL"} or option_set == {"CLAUDE_MODEL", "ANTHROPIC_MODEL"}:
        return "claude_model"
    if option_set == {"NOTION_API_KEY"}:
        return "notion_api_key"
    if option_set == {"NOTION_DATABASE_ID", "NOVELAGENT_NOTION_DATABASE_ID"}:
        return "notion_database_id"
    return fallback


def _format_missing_config(provider: str, groups: list[dict[str, Any]]) -> str:
    rendered = []
    for group in groups:
        names = [str(name) for name in group.get("any_of", []) if str(name).strip()]
        if not names:
            continue
        if len(names) == 1:
            rendered.append(names[0])
        else:
            rendered.append("one of " + ", ".join(names))
    return f"{provider} provider smoke requires: {'; '.join(rendered)}."


if __name__ == "__main__":
    main()
