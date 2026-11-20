from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.quality import evaluate_chapter_quality
from core.review.pipeline import run_review_pipeline


def evaluate(text: str, *, evidence: bool = False) -> dict:
    snapshot = json.loads(Path('tests/fixtures/chapter_quality/snapshot.json').read_text(encoding='utf-8'))
    return evaluate_chapter_quality(chapter_text=text, snapshot=snapshot, include_prose_evidence=evidence)


def meta_check(report: dict) -> dict:
    return next(check for check in report['checks'] if check['code'] == 'no_meta_output')


@pytest.mark.parametrize('text', [
    '他将第53章留下的材料检查记录逐项对齐。',
    '她检查了第五十三章埋下的线索。',
    '他核对第 53 章中交代过的安排。',
])
def test_narrative_chapter_provenance_is_advisory(text):
    check = meta_check(evaluate(text, evidence=True))
    assert check['status'] == 'warning'
    assert check['severity'] == 'low'
    assert check['evidence']['matched_markers']
    assert 'prose_excerpt' in check['evidence']


@pytest.mark.parametrize('text', [
    '第53章 材料检查\n\n他将留下的材料检查记录逐项对齐。',
    '第五十三章\n\n她翻到手册的第53章，又把书合上。',
    '“请读第53章。”她把书递给他。',
    '他将三天前留下的材料检查记录逐项对齐。',
])
def test_titles_and_ordinary_book_references_are_not_meta_output(text):
    assert meta_check(evaluate(text))['status'] == 'pass'


def test_existing_explicit_meta_failure_is_preserved():
    check = meta_check(evaluate('作为AI，我将第53章留下的线索重新整理。'))
    assert check['status'] == 'fail'
    assert check['severity'] == 'critical'


@pytest.mark.parametrize('text', [
    '“以下是今晚的值班名单。”她把纸递了过去。',
    '他低声分析：“门外只有两个人。”',
    '她最后总结：“我们得先离开这里。”',
])
def test_ordinary_dialogue_is_not_rejected_as_an_output_preamble(text):
    assert meta_check(evaluate(text))['status'] == 'pass'


@pytest.mark.parametrize('text', [
    '以下是续写的正文：\n他推开了门。',
    '以下是修改后的章节：\n他推开了门。',
    '分析：需要先承接上一章。\n接着展开冲突。',
    '总结：本章完成了任务。',
])
def test_actual_output_preambles_still_fail(text):
    assert meta_check(evaluate(text))['status'] == 'fail'


def test_chapter_reference_does_not_schedule_a_repair(tmp_path):
    fixture = Path('tests/fixtures/chapter_quality')
    text = (fixture / 'good_chapter.md').read_text(encoding='utf-8')
    text += '\n\n他将第53章留下的材料检查记录逐项对齐。'
    report = run_review_pipeline(
        chapter_text=text,
        snapshot=json.loads((fixture / 'snapshot.json').read_text(encoding='utf-8')),
        previous_chapter_text=(fixture / 'previous_chapter.md').read_text(encoding='utf-8'),
        include_prose_evidence=True,
        output_dir=tmp_path,
    )
    quality = json.loads((tmp_path / 'chapter_quality_report.json').read_text(encoding='utf-8'))
    plan = json.loads((tmp_path / 'rule_repair_plan.json').read_text(encoding='utf-8'))
    assert meta_check(quality)['status'] == 'warning'
    assert not any(task['rule_code'] == 'prose_only_no_meta_output'
                   for task in plan['tasks'])
    assert report['quality_decision']['accepted']
    findings = [item for item in report['quality_decision']['findings']
                if set(item['codes']) & {'no_meta_output', 'prose_only_no_meta_output'}]
    assert findings
    assert all(item['severity'] == 'warning' for item in findings)
