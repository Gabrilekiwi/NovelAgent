"""Exercise the public quality report, using prose instead of precomputed matches."""
import pytest

from core.quality import evaluate_chapter_quality
from core.state.snapshot import normalize_snapshot

PERMIT = "顾言为何拥有封闭区权限"
VALVE = "备用阀口至少四分钟，扫描空隙三分钟，缺口待复核，不能算可用水源"


def progress(text, threads, **extra):
    snapshot = normalize_snapshot({"chapter_index": 2, "story_state": {
        "pending_threads": threads, "last_scene_characters": ["顾言", "林澈"]}})
    snapshot.update(extra)
    report = evaluate_chapter_quality(chapter_text=text, snapshot=snapshot)
    return next(check for check in report["checks"] if check["code"] == "advances_open_threads_or_conflicts")


@pytest.mark.parametrize('thread, text', [
    (PERMIT, "控制室日志显示顾言在十分钟前刷卡进入封闭区。"),
    ("失踪的货箱被运到哪里", "码头签收单上写着失踪货箱的新编号。"),
    ("钟楼的铜钥匙是谁留下的", "她在钟楼地板里找到了刻有姓氏的铜钥匙。"),
    (VALVE, "原定的四分钟开阀，从这里就断了。"),
    ("运输队只有两天送药", "原定的两天送药，这个方案只能取消了。"),
    ("旧账簿", "她在旧账簿内页发现了一枚指印。"),
])
def test_finding_is_bound_to_one_thread_and_actual_prose(thread, text):
    result = progress(text, [thread])
    assert result['status'] == 'pass'
    witness = result['evidence']
    assert witness['thread'] == thread and witness['witness'] in text
    assert witness['observed_action'] in witness['witness']


@pytest.mark.parametrize('text', [
    "顾言为何拥有封闭区权限？", "她写下：顾言为何拥有封闭区权限。",
    "院子里下起了雨。", "顾言在封闭区发现了一只流浪猫。",
    "门卫确认顾言刚进厨房。另一本日志显示有人刷卡进入封闭区。",
    "顾言刷卡进入封闭区了吗？", "如果顾言刷卡进入封闭区，就能拿回东西。",
    "他怀疑顾言曾刷卡进入封闭区。", "她没有查到顾言进入封闭区的证据。",
])
def test_mentions_restatements_questions_and_speculation_stay_warnings(text):
    assert progress(text, [PERMIT])['status'] == 'warning'


@pytest.mark.parametrize('text', ["原定的四分钟开阀，顾言的鱼线就断了。",
    "如果原定的四分钟开阀，这个计划就取消了。", "原定的四分钟开门，这个计划取消了。",
    "原定的十四分钟开阀，这个计划取消了。", VALVE])
def test_plan_needs_the_same_quantity_object_and_an_actual_outcome(text):
    assert progress(text, [VALVE])['status'] == 'warning'


def test_metadata_cannot_substitute_for_a_thread_and_english_compatibility():
    assert progress('active', [], active_constraints=[{'status':'active'}])['status'] == 'skip'
    assert progress('active', [PERMIT], active_constraints=[{'status':'active'}])['status'] == 'warning'
    assert progress('They found the missing ledger.', ['missing ledger'])['status'] == 'pass'
