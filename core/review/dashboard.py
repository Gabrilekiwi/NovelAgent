from __future__ import annotations

from html import escape
import os
from pathlib import Path
from typing import Any

from core.review.index import load_review_index
from core.schema import validate_schema


DEFAULT_TITLE = "NovelAgent Review Dashboard"


def build_review_dashboard(
    *,
    review_index: dict[str, Any],
    title: str = DEFAULT_TITLE,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    index = validate_schema(review_index, "review_index.schema.json")
    html = _build_html(index=index, title=title, output_path=Path(output_path) if output_path is not None else None)
    output_text = str(output_path) if output_path is not None else None
    if output_path is not None:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")
    return {
        "html": html,
        "metadata": {
            "kind": "review_dashboard",
            "chars": len(html),
            "entry_count": int(index["summary"]["entry_count"]),
            "latest_run_id": index.get("latest_run_id"),
            "output_path": output_text,
        },
    }


def build_review_dashboard_from_index(
    *,
    review_output_dir: str | Path,
    output_path: str | Path | None = None,
    title: str = DEFAULT_TITLE,
) -> dict[str, Any]:
    output = Path(output_path) if output_path is not None else Path(review_output_dir) / "dashboard.html"
    index = load_review_index(review_output_dir=review_output_dir)
    return build_review_dashboard(review_index=index, title=title, output_path=output)


def _build_html(*, index: dict[str, Any], title: str, output_path: Path | None) -> str:
    entries = list(index.get("entries", []))
    latest = entries[0] if entries else None
    summary = index["summary"]
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="zh-CN">',
            "<head>",
            '  <meta charset="utf-8">',
            '  <meta name="viewport" content="width=device-width, initial-scale=1">',
            f"  <title>{escape(title)}</title>",
            f"  <style>{_css()}</style>",
            "</head>",
            "<body>",
            "  <main>",
            _header(index=index, title=title),
            _summary_cards(summary),
            _latest_review(latest, output_path),
            _review_table(entries, output_path),
            "  </main>",
            "</body>",
            "</html>",
            "",
        ]
    )


def _header(*, index: dict[str, Any], title: str) -> str:
    latest = index.get("latest_run_id") or "none"
    return "\n".join(
        [
            '    <header class="page-header">',
            f"      <h1>{escape(title)}</h1>",
            f"      <p>Updated at: {escape(str(index.get('updated_at') or ''))}</p>",
            f"      <p>Review output dir: {escape(str(index.get('review_output_dir') or ''))}</p>",
            f"      <p>Latest run: {escape(str(latest))}</p>",
            "    </header>",
        ]
    )


def _summary_cards(summary: dict[str, Any]) -> str:
    cards = [
        ("Total Reviews", summary.get("entry_count")),
        ("Pass", summary.get("pass_count")),
        ("Warning", summary.get("warning_count")),
        ("Needs Revision", summary.get("needs_revision_count")),
        ("Blocked", summary.get("blocked_count")),
        ("Error", summary.get("error_count")),
        ("Gate Fail", summary.get("gate_fail_count")),
    ]
    items = [
        f'      <section class="card"><span>{escape(label)}</span><strong>{escape(str(value))}</strong></section>'
        for label, value in cards
    ]
    return "\n".join(['    <section class="summary-grid">', *items, "    </section>"])


def _latest_review(entry: dict[str, Any] | None, output_path: Path | None) -> str:
    if not entry:
        return "\n".join(
            [
                '    <section class="panel">',
                "      <h2>Latest Review</h2>",
                "      <p>No review entries found.</p>",
                "    </section>",
            ]
        )
    rows = [
        ("Run ID", entry.get("run_id")),
        ("Created At", entry.get("created_at")),
        ("Chapter", entry.get("chapter_index")),
        ("Status", entry.get("review_status")),
        ("Decision", entry.get("review_decision")),
        ("Quality Score", entry.get("quality_score")),
        ("Rule Score", entry.get("rule_score")),
        ("Repair Tasks", entry.get("repair_task_count")),
        ("Blocking Tasks", entry.get("blocking_task_count")),
        ("Gate Status", entry.get("gate_status")),
        ("Gate Exit Code", entry.get("gate_exit_code")),
    ]
    details = [f"        <dt>{escape(label)}</dt><dd>{escape(_display(value))}</dd>" for label, value in rows]
    links = [
        ("Human Report", entry.get("human_report_path"), "human report"),
        ("Repair Prompt", entry.get("repair_prompt_path"), "repair prompt"),
        ("Summary JSON", entry.get("summary_path"), "summary JSON"),
    ]
    link_rows = [
        f"        <dt>{escape(label)}</dt><dd>{_link(value, text, output_path)}</dd>"
        for label, value, text in links
    ]
    return "\n".join(
        [
            '    <section class="panel">',
            "      <h2>Latest Review</h2>",
            "      <dl class=\"latest-grid\">",
            *details,
            *link_rows,
            "      </dl>",
            "    </section>",
        ]
    )


def _review_table(entries: list[dict[str, Any]], output_path: Path | None) -> str:
    if not entries:
        return "\n".join(
            [
                '    <section class="panel">',
                "      <h2>Recent Reviews</h2>",
                "      <p>No review entries found.</p>",
                "    </section>",
            ]
        )
    rows = [_table_row(entry, output_path) for entry in entries]
    return "\n".join(
        [
            '    <section class="panel">',
            "      <h2>Recent Reviews</h2>",
            '      <div class="table-wrap">',
            "        <table>",
            "          <thead>",
            "            <tr>",
            "              <th>Created At</th>",
            "              <th>Run ID</th>",
            "              <th>Chapter</th>",
            "              <th>Status</th>",
            "              <th>Decision</th>",
            "              <th>Quality</th>",
            "              <th>Rule</th>",
            "              <th>Repair Tasks</th>",
            "              <th>Blocking</th>",
            "              <th>Gate</th>",
            "              <th>Human Report</th>",
            "              <th>Repair Prompt</th>",
            "              <th>Summary</th>",
            "            </tr>",
            "          </thead>",
            "          <tbody>",
            *rows,
            "          </tbody>",
            "        </table>",
            "      </div>",
            "    </section>",
        ]
    )


def _table_row(entry: dict[str, Any], output_path: Path | None) -> str:
    classes = []
    if entry.get("review_status") == "blocked":
        classes.append("status-blocked")
    if entry.get("gate_status") == "fail":
        classes.append("gate-fail")
    class_attr = f' class="{" ".join(classes)}"' if classes else ""
    cells = [
        (_display(entry.get("created_at")), False),
        (_display(entry.get("run_id")), False),
        (_display(entry.get("chapter_index")), False),
        (_display(entry.get("review_status")), False),
        (_display(entry.get("review_decision")), False),
        (_display(entry.get("quality_score")), False),
        (_display(entry.get("rule_score")), False),
        (_display(entry.get("repair_task_count")), False),
        (_display(entry.get("blocking_task_count")), False),
        (f"{_display(entry.get('gate_status'))} / {_display(entry.get('gate_exit_code'))}", False),
        (_link(entry.get("human_report_path"), "human", output_path), True),
        (_link(entry.get("repair_prompt_path"), "prompt", output_path), True),
        (_link(entry.get("summary_path"), "summary", output_path), True),
    ]
    rendered = []
    for cell, is_html in cells:
        if is_html:
            rendered.append(f"              <td>{cell}</td>")
        else:
            rendered.append(f"              <td>{escape(str(cell))}</td>")
    return "\n".join([f"            <tr{class_attr}>", *rendered, "            </tr>"])


def _link(value: Any, text: str, output_path: Path | None) -> str:
    if value is None:
        return "-"
    raw = str(value)
    href = _href(raw, output_path)
    return f'<a href="{escape(href, quote=True)}">{escape(text)}</a>'


def _href(path_text: str, output_path: Path | None) -> str:
    if output_path is None:
        return path_text.replace("\\", "/")
    try:
        target = Path(path_text)
        if not target.is_absolute():
            target = Path.cwd() / target
        base = output_path.parent
        if not base.is_absolute():
            base = Path.cwd() / base
        return Path(os.path.relpath(target.resolve(), base.resolve())).as_posix()
    except (OSError, ValueError):
        return path_text.replace("\\", "/")


def _display(value: Any) -> str:
    return "-" if value is None else str(value)


def _css() -> str:
    return """
body { margin: 0; font-family: Arial, sans-serif; color: #182026; background: #f6f7f9; }
main { max-width: 1180px; margin: 0 auto; padding: 28px; }
.page-header { margin-bottom: 20px; }
h1 { margin: 0 0 12px; font-size: 28px; letter-spacing: 0; }
h2 { margin: 0 0 14px; font-size: 20px; letter-spacing: 0; }
p { margin: 4px 0; }
.summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin: 20px 0; }
.card, .panel { background: #fff; border: 1px solid #d9dee5; border-radius: 6px; }
.card { padding: 14px; }
.card span { display: block; color: #5c6875; font-size: 13px; }
.card strong { display: block; margin-top: 8px; font-size: 24px; }
.panel { padding: 18px; margin: 16px 0; }
.latest-grid { display: grid; grid-template-columns: 160px minmax(0, 1fr); gap: 8px 14px; margin: 0; }
dt { color: #5c6875; }
dd { margin: 0; word-break: break-word; }
.table-wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; min-width: 980px; }
th, td { border-bottom: 1px solid #e5e9ef; padding: 9px 10px; text-align: left; vertical-align: top; font-size: 13px; }
th { background: #f0f3f7; color: #303943; }
tr.status-blocked { background: #fff1f0; }
tr.gate-fail { outline: 2px solid #b42318; outline-offset: -2px; }
a { color: #1f5fbf; text-decoration: none; }
a:hover { text-decoration: underline; }
""".strip()


__all__ = [
    "DEFAULT_TITLE",
    "build_review_dashboard",
    "build_review_dashboard_from_index",
]
