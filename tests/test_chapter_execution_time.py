from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys

import pytest

from core.chapter_budget_contract import freeze_chapter_budget
from core.chapter_execution_time import (ChapterExecutionClock, ChapterTimeError,
    ChapterTimeExceeded, ChapterTimeUnknown)
from core.engine.persistence import PersistenceLockError


class FakeClock:
    def __init__(self):
        self.wall = datetime(2026,9,10,tzinfo=timezone.utc)
        self.ns = 0

    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.ns += int(Decimal(str(seconds))*1_000_000_000)

    def ledger(self, root, limit='10'):
        return ChapterExecutionClock(root,budget(limit),wall_clock=lambda:self.wall,monotonic_ns=lambda:self.ns)


def budget(limit='10'):
    return freeze_chapter_budget(book_id='book-test',chapter=2,max_provider_calls=10,
        max_input_tokens=1000,max_output_tokens=1000,max_active_seconds=limit)


def test_author_wait_and_restart_do_not_consume_active_budget(tmp_path):
    now = FakeClock()
    store = now.ledger(tmp_path)
    store.initialize()
    with store.active('generate',awaiting_author_after=True) as interval:
        now.advance(2.25)
        assert interval.remaining_seconds() == Decimal('7.75')
    now.advance(3*24*3600)
    restarted = now.ledger(tmp_path)
    before = restarted.report()
    assert before['active_seconds'] == '2.25'
    assert before['author_wait_seconds'] == str(3*24*3600)
    assert before['other_inactive_seconds'] == '0'
    with restarted.active('facts',awaiting_author_after=True):
        now.advance(1.5)
    after = restarted.report()
    assert after['active_seconds'] == '3.75'
    assert after['author_wait_seconds'] == str(3*24*3600)
    assert after['active_time_complete']
    assert after['end_to_end_seconds'] == '259203.75'


def test_accumulated_execution_limit_survives_new_instance(tmp_path):
    now=FakeClock(); store=now.ledger(tmp_path); store.initialize()
    with store.active('generate'):
        now.advance(6)
    with now.ledger(tmp_path).active('facts') as interval:
        now.advance(4)
        with pytest.raises(ChapterTimeExceeded):interval.remaining_seconds()
    before=(tmp_path/'time.json').read_bytes()
    with pytest.raises(ChapterTimeExceeded):
        with now.ledger(tmp_path).active('repair'):pytest.fail('budget must block before work')
    assert (tmp_path/'time.json').read_bytes()==before
    assert store.report()['active_limit_reached']


def test_failure_measures_active_time_and_ends_interval(tmp_path):
    now=FakeClock(); store=now.ledger(tmp_path); store.initialize()
    with pytest.raises(RuntimeError,match='provider failed'):
        with store.active('facts',awaiting_author_after=True):
            now.advance(3)
            raise RuntimeError('provider failed')
    assert store.report()['active_seconds']=='3'
    assert store.report()['active_status']=='settled'


def test_wall_clock_rollback_does_not_rewrite_measured_duration(tmp_path):
    now=FakeClock(); store=now.ledger(tmp_path); store.initialize()
    with store.active('generation'):
        now.ns+=2_000_000_000
        now.wall-=timedelta(hours=1)
    report=store.report()
    assert report['active_seconds']=='2'
    assert report['wall_time_status']=='unknown'
    assert report['end_to_end_seconds'] is None and report['author_wait_seconds'] is None


def test_monotonic_rollback_leaves_unknown_interval_and_blocks(tmp_path):
    now=FakeClock();now.ns=5_000_000_000
    store=now.ledger(tmp_path);store.initialize()
    with pytest.raises(ChapterTimeUnknown):
        with store.active('generation'):
            now.ns-=1
    report=store.report()
    assert report['active_status']=='unknown'
    assert not report['active_time_complete']
    with pytest.raises(ChapterTimeUnknown):
        with store.active('repair'):pytest.fail('unknown duration must block')


def test_reads_do_not_create_or_repair_ledger(tmp_path):
    root=tmp_path/'clock';store=ChapterExecutionClock(root,budget())
    with pytest.raises(ChapterTimeError):store.report()
    assert not root.exists()
    store.initialize()
    before=(root/'time.json').read_bytes()
    store.report();store.report()
    assert (root/'time.json').read_bytes()==before
    (root/'time.json').write_text('{}',encoding='utf-8')
    with pytest.raises(ChapterTimeError):store.initialize()
    assert (root/'time.json').read_text()=='{}'


def test_intermediate_clock_rollback_cannot_refund_or_later_settle(tmp_path):
    now=FakeClock();now.ns=1_000_000_000
    store=now.ledger(tmp_path);store.initialize()
    with pytest.raises(ChapterTimeUnknown):
        with store.active('generation') as handle:
            now.ns=2_000_000_000
            assert handle.remaining_seconds()==Decimal('9')
            now.ns=1_500_000_000
            with pytest.raises(ChapterTimeUnknown):handle.remaining_seconds()
            # Catching the error and a recovered clock cannot erase uncertainty.
            now.ns=3_000_000_000
            with pytest.raises(ChapterTimeUnknown):handle.remaining_seconds()
    assert store.report()['active_status']=='unknown'
    assert not store.report()['active_time_complete']


@pytest.mark.parametrize('change',['book','chapter','limit'])
def test_frozen_binding_prevents_changing_limit_or_chapter(tmp_path,change):
    store=ChapterExecutionClock(tmp_path,budget());store.initialize()
    args=dict(book_id='book-other' if change=='book' else 'book-test',chapter=3 if change=='chapter' else 2,
        max_provider_calls=10,max_input_tokens=1000,max_output_tokens=1000,max_active_seconds='20' if change=='limit' else '10')
    other=ChapterExecutionClock(tmp_path,freeze_chapter_budget(**args))
    with pytest.raises(ChapterTimeError):other.report()
    with pytest.raises(ChapterTimeError):other.initialize()


def test_live_lock_and_process_death_have_distinct_report_states(tmp_path):
    ChapterExecutionClock(tmp_path,budget()).initialize()
    code='''import json,sys
from core.chapter_execution_time import ChapterExecutionClock
store=ChapterExecutionClock(sys.argv[1],json.loads(sys.argv[2]))
with store.active('generation',awaiting_author_after=True):
    print('held',flush=True)
    sys.stdin.read()
'''
    child=subprocess.Popen([sys.executable,'-B','-c',code,str(tmp_path),json.dumps(budget())],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        assert child.stdout.readline().strip()=='held'
        store=ChapterExecutionClock(tmp_path,budget())
        assert store.report()['active_status']=='running'
        with pytest.raises(PersistenceLockError):
            with store.active('parallel'):pytest.fail('parallel clock must be excluded')
        child.kill();child.wait(timeout=10)
        before=(tmp_path/'time.json').read_bytes()
        report=store.report()
        assert report['active_status']=='unknown' and not report['active_time_complete']
        with pytest.raises(ChapterTimeUnknown):
            with ChapterExecutionClock(tmp_path,budget()).active('restart'):pytest.fail('no automatic zeroing')
        assert (tmp_path/'time.json').read_bytes()==before
    finally:
        if child.poll() is None:child.kill();child.wait(timeout=10)
        child.stdin.close();child.stdout.close();child.stderr.close()


def test_non_author_inactive_time_is_reported_separately(tmp_path):
    now=FakeClock();store=now.ledger(tmp_path);store.initialize()
    with store.active('local_check'):
        now.advance(1)
    now.advance(8)
    report=store.report()
    assert report['other_inactive_seconds']=='8' and report['author_wait_seconds']=='0'
    assert report['active_seconds']=='1'


def test_closed_handle_cannot_supply_new_request_budget(tmp_path):
    now=FakeClock();store=now.ledger(tmp_path);store.initialize()
    with store.active('generation') as handle:now.advance(1)
    with pytest.raises(ChapterTimeError):handle.remaining_seconds()


def test_last_interval_finishes_between_read_and_activity_probe(tmp_path,monkeypatch):
    now=FakeClock();store=now.ledger(tmp_path);store.initialize()
    manager=store.active('generation');manager.__enter__()
    def finish():
        now.advance(1)
        manager.__exit__(None,None,None)
        return False
    monkeypatch.setattr(store,'_active',finish)
    report=store.report()
    assert report['active_status']=='settled' and report['active_seconds']=='1'
