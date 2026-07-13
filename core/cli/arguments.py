from __future__ import annotations

import argparse


RUNTIME_PATH_OPTIONS = {
    "snapshot": "--snapshot",
    "run_dir": "--run-dir",
    "persistence_dir": "--persistence-dir",
    "delivery_dir": "--delivery-dir",
    "chapter_dir": "--chapter-dir",
    "review_output_dir": "--review-output-dir",
    "memory_outbox": "--memory-outbox",
    "memory_v2_out": "--memory-v2-out",
}


def option_was_provided(argv: list[str], option: str) -> bool:
    return any(token == option or token.startswith(f"{option}=") for token in argv)


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def review_repair_attempts(value: str) -> int:
    parsed = positive_int(value)
    if parsed > 3:
        raise argparse.ArgumentTypeError("must be between 1 and 3")
    return parsed


def apply_notion_shortcuts(args: argparse.Namespace) -> argparse.Namespace:
    if getattr(args, "notion_memory", False) or getattr(args, "notion_sync", False):
        args.memory_source = "notion"
    if getattr(args, "notion_sync", False):
        args.memory_writeback = "notion"
        args.notion_readback = True
    return args


def parse_arguments(
    parser: argparse.ArgumentParser,
    argv: list[str],
) -> argparse.Namespace:
    args = parser.parse_args(argv)
    args._runtime_path_explicit = {
        name: option_was_provided(argv, option)
        for name, option in RUNTIME_PATH_OPTIONS.items()
    }
    return args


__all__ = [
    "RUNTIME_PATH_OPTIONS",
    "apply_notion_shortcuts",
    "option_was_provided",
    "parse_arguments",
    "positive_int",
    "review_repair_attempts",
]
