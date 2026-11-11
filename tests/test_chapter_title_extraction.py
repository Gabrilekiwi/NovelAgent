from __future__ import annotations

from pathlib import Path

from core.story_project.mapper import extract_chapter_title, _extract_title


def test_extract_title_from_canonical_outline_heading() -> None:
    path = Path("大纲/细纲_第064章.md")
    text = "# 第64章 三分钟之外\n\n核心事件：外围观察。\n"
    assert extract_chapter_title(text, path) == "三分钟之外"


def test_extract_title_keeps_chinese_numeral_published_title() -> None:
    path = Path("大纲/细纲_第001章.md")
    text = "# 第一章：断电后的第十二分钟\n"
    assert extract_chapter_title(text, path) == "第一章：断电后的第十二分钟"


def test_extract_title_prefers_heading_over_filename() -> None:
    path = Path("大纲/细纲_第064章_文件名标题.md")
    text = "# 第64章 三分钟之外\n"
    assert extract_chapter_title(text, path) == "三分钟之外"


def test_extract_title_from_filename_when_heading_missing() -> None:
    path = Path("大纲/细纲_第064章_三分钟之外.md")
    assert extract_chapter_title("核心事件：外围观察。\n", path) == "三分钟之外"


def test_canonical_outline_filename_is_not_a_title() -> None:
    path = Path("大纲/细纲_第064章.md")
    assert extract_chapter_title("", path) == ""
    assert extract_chapter_title("核心事件：外围观察。\n", path) == ""


def test_extract_title_from_prose_filename() -> None:
    path = Path("正文/第063章_第63章 一天的水线.md")
    assert extract_chapter_title("林夏重新核了一遍库存。\n", path) == "一天的水线"


def test_mapper_wrapper_keeps_stem_fallback() -> None:
    path = Path("大纲/细纲_第064章.md")
    assert _extract_title("", path) == "细纲_第064章"
