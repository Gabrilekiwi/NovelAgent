from __future__ import annotations

from pathlib import Path

from tests.test_workbench_app import _client, _create_project, _unlock
from workbench.cohorts import build_official_launch_declaration


STATIC = Path(__file__).resolve().parents[1] / "workbench" / "static"

BADGE = "\u4e0a\u7ebf\u4e0d\u4ee5\u5341\u7ae0\u9a8c\u6536\u8f6e\u6b21\u4e3a\u51c6"
SUMMARY_NEEDLE = "\u4e0d\u518d\u4ee5\u8fde\u7eed\u4e24\u8f6e\u5341\u7ae0\u4eba\u5de5\u8bc4\u5206\u9a8c\u6536\u4f5c\u4e3a\u95e8\u69db"
OLD_HTML_GATE = "\u4e24\u4e2a\u72ec\u7acb cohort \u8fde\u7eed\u901a\u8fc7\uff0c\u624d\u6807\u8bb0\u4e3a\u201c\u81ea\u7528\u751f\u4ea7\u7248\u201d\u3002"
OPTIONAL_LEDGER = "\u53ef\u9009\u9a8c\u6536\u8bb0\u5f55"
NOT_LAUNCH_GATE = "\u4e0d\u518d\u4f5c\u4e3a\u6b63\u5f0f\u4e0a\u7ebf\u95e8\u69db"
AUTHOR_PATH = "\u4f5c\u8005\u80fd\u5426\u5728\u5de5\u4f5c\u53f0\u72ec\u7acb\u5b8c\u6210\u5199\u4f5c"
OPTIONAL_GATES = "\u53ef\u9009\u8bd5\u9a8c\u95e8\u69db"
OLD_BADGE = "\u81ea\u7528\u751f\u4ea7\u7248\u5df2\u8fbe\u6807"
OLD_COUNT_BADGE = "/ 2 \u8f6e\u901a\u8fc7"

NOT_OFFICIAL_CONTINUE = "这不会续写正式正文"
USE_SESSION_PATH = "要继续当前作品，请使用上方的「预检并开始」"
SESSION_CTA = "预检并开始"
OLD_COHORT_PRIMARY_CTA = "冻结基线并开始 Cohort"
COHORT_SECONDARY_CTA = "冻结基线并开始可选试验"
OPTIONAL_TRIAL_SUMMARY = "可选：开始一轮十章试验记录"


def test_official_launch_declaration_does_not_use_or_fake_the_two_cohort_gate() -> None:
    not_ready = build_official_launch_declaration(
        passed_count=0,
        consecutive_ten_chapter_ready=False,
    )
    historically_ready = build_official_launch_declaration(
        passed_count=2,
        consecutive_ten_chapter_ready=True,
    )

    for payload in (not_ready, historically_ready):
        assert payload["criterion"] == "author_self_serve_workbench"
        assert payload["uses_two_cohort_gate"] is False
        assert payload["cohort_trials"]["required_for_official_launch"] is False
        assert payload["badge_label"] == BADGE
        assert SUMMARY_NEEDLE in payload["summary"]
        assert "declared_ready" not in payload
        assert "self_use_production_ready" not in payload

    assert not_ready["cohort_trials"]["passed_count"] == 0
    assert not_ready["cohort_trials"]["consecutive_ten_chapter_ready"] is False
    assert historically_ready["cohort_trials"]["passed_count"] == 2
    assert historically_ready["cohort_trials"]["consecutive_ten_chapter_ready"] is True


def test_production_api_keeps_historical_cohort_flag_and_exposes_official_launch(
    tmp_path: Path,
) -> None:
    client, workspace = _client(tmp_path)
    _unlock(client)
    project = _create_project(client, workspace, name="official-launch-presentation")

    production = client.get(f"/api/projects/{project['id']}/production")
    assert production.status_code == 200, production.text
    payload = production.json()

    assert payload["self_use_production_ready"] is False
    launch = payload["official_launch"]
    assert launch["criterion"] == "author_self_serve_workbench"
    assert launch["uses_two_cohort_gate"] is False
    assert launch["badge_label"] == BADGE
    assert launch["cohort_trials"]["passed_count"] == 0
    assert launch["cohort_trials"]["consecutive_ten_chapter_ready"] is False
    assert launch["cohort_trials"]["required_for_official_launch"] is False
    assert "declared_ready" not in launch


def test_production_page_does_not_present_two_cohort_gate_as_official_launch() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")

    assert OLD_HTML_GATE not in html
    assert OPTIONAL_LEDGER in html
    assert NOT_LAUNCH_GATE in html
    assert AUTHOR_PATH in html
    assert BADGE in html
    assert f'aria-label="{OPTIONAL_GATES}"' in html
    assert "status-badge status-idle" in html
    assert "data-product-readiness" in html

    assert OLD_COUNT_BADGE not in spa
    assert OLD_BADGE not in spa
    assert 'payload.self_use_production_ready ? "strict" : "experimental"' not in spa
    assert f'officialLaunch.badge_label || "{BADGE}"' in spa
    assert "payload.official_launch" in spa


def test_production_page_keeps_chapter_writing_as_primary_and_demotes_cohort_create() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    spa = (STATIC / "workbench-spa.js").read_text(encoding="utf-8")
    css = (STATIC / "workbench.css").read_text(encoding="utf-8")

    session_button = f'button class="button button-primary button-wide" type="submit">{SESSION_CTA}</button>'
    assert session_button in html
    assert "data-session-form" in html

    details_token = '<details class="cohort-create-details" data-cohort-create-details>'
    details_open = html.find(details_token)
    assert details_open != -1
    details_tag = html[details_open:html.find(">", details_open) + 1]
    assert "open" not in details_tag
    assert OPTIONAL_TRIAL_SUMMARY in html
    assert NOT_OFFICIAL_CONTINUE in html
    assert USE_SESSION_PATH in html
    assert f'button class="button button-secondary" type="submit">{COHORT_SECONDARY_CTA}</button>' in html
    assert OLD_COHORT_PRIMARY_CTA not in html
    assert 'button class="button button-primary" type="submit">冻结基线并开始' not in html

    create_form_idx = html.find("data-cohort-create-form")
    start_input_idx = html.find('name="start_chapter"', create_form_idx)
    start_input_tag = html[start_input_idx:html.find(">", start_input_idx) + 1]
    assert "value=" not in start_input_tag

    assert "createForm).value = payload.project?.next_chapter" not in spa
    assert "never prefill the official next chapter" in spa

    assert ".cohort-panel { margin-top: 22px; border-top: 1px solid var(--line); }" in css
    assert ".cohort-panel { margin-top: 22px; border-top: 3px solid var(--jade); }" not in css
    assert ".cohort-create-details" in css
