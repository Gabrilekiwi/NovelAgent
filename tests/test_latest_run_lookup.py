import json
import os
from pathlib import Path
from unittest.mock import patch

from workbench.app import _latest_run_for_chapter, _run_chapter_index, _run_file_signature
import pytest


def test_latest_run_skips_corrupt_and_other_chapters_without_reading_older(tmp_path):
    runs = tmp_path / '.novelagent/runtime/runs'
    runs.mkdir(parents=True)
    values = [('old', {'run': {'chapter_index': 3, 'id': 'old'}}),
              ('match', {'run': {'chapter_index': 3, 'id': 'match'}}),
              ('other', {'run': {'chapter_index': 4}}), ('broken', None)]
    for index, (name, value) in enumerate(values):
        p = runs / f'chapter_{name}.json'
        p.write_text(json.dumps(value) if value else '{', encoding='utf-8')
        os.utime(p, ns=(1000000000 + index, 1000000000 + index))
    original = Path.read_text
    reads = []
    def read(p, *args, **kwargs):
        reads.append(p.name)
        return original(p, *args, **kwargs)
    with patch.object(Path, 'read_text', read):
        result = _latest_run_for_chapter(tmp_path, 3)
    assert result['run']['id'] == 'match'
    assert 'chapter_old.json' not in reads
    assert _latest_run_for_chapter(tmp_path, 99) is None


def test_missing_lookup_reuses_metadata_and_detects_changed_records(tmp_path):
    runs = tmp_path / '.novelagent/runtime/runs'
    runs.mkdir(parents=True)
    path = runs / 'chapter_arbitrary.json'
    path.write_text(json.dumps({'run': {'chapter_index': 3}}), encoding='utf-8')
    assert _latest_run_for_chapter(tmp_path, 9) is None
    with patch.object(Path, 'read_text', side_effect=AssertionError('unchanged record reread')):
        assert _latest_run_for_chapter(tmp_path, 9) is None
    path.write_text(json.dumps({'run': {'chapter_index': 9, 'id': 'edited'}}), encoding='utf-8')
    assert _latest_run_for_chapter(tmp_path, 9)['run']['id'] == 'edited'
    replacement = runs / 'replacement.tmp'
    replacement.write_text(json.dumps({'run': {'chapter_index': 8}}), encoding='utf-8')
    previous_time = path.stat().st_mtime_ns
    os.utime(replacement, ns=(previous_time, previous_time))
    os.replace(replacement, path)
    assert _latest_run_for_chapter(tmp_path, 9) is None
    assert _latest_run_for_chapter(tmp_path, 8)['run']['chapter_index'] == 8
    fresh = runs / 'chapter_new.json'
    fresh.write_text('{', encoding='utf-8')
    assert _latest_run_for_chapter(tmp_path, 9) is None
    fresh.write_text(json.dumps({'run': {'chapter_index': 9}}), encoding='utf-8')
    assert _latest_run_for_chapter(tmp_path, 9)['run']['chapter_index'] == 9
    fresh.unlink()
    assert _latest_run_for_chapter(tmp_path, 9) is None


def test_matching_payload_is_reread_and_not_shared(tmp_path):
    runs = tmp_path / '.novelagent/runtime/runs'
    runs.mkdir(parents=True)
    path = runs / 'chapter_one.json'
    path.write_text(json.dumps({'run': {'chapter_index': 1, 'id': 'original'}}), encoding='utf-8')
    result = _latest_run_for_chapter(tmp_path, 1)
    result['run']['id'] = 'mutated by caller'
    assert _latest_run_for_chapter(tmp_path, 1)['run']['id'] == 'original'


def test_replacement_with_same_size_and_timestamp_invalidates_metadata(tmp_path):
    runs = tmp_path / '.novelagent/runtime/runs'
    runs.mkdir(parents=True)
    path = runs / 'chapter_one.json'
    path.write_text('{"run":{"chapter_index":1}}', encoding='utf-8')
    assert _latest_run_for_chapter(tmp_path, 2) is None
    previous = path.stat()
    replacement = runs / 'replacement.tmp'
    replacement.write_text('{"run":{"chapter_index":2}}', encoding='utf-8')
    os.utime(replacement, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    os.replace(replacement, path)
    assert _latest_run_for_chapter(tmp_path, 2)['run']['chapter_index'] == 2


def test_record_changed_during_read_is_not_cached(tmp_path):
    path = (tmp_path / 'chapter_one.json').absolute()
    path.write_text('{"run":{"chapter_index":1}}', encoding='utf-8')
    signature = _run_file_signature(path)
    original = Path.read_text
    def changing_read(p, *args, **kwargs):
        value = original(p, *args, **kwargs)
        p.write_text('{"run":{"chapter_index":22}}', encoding='utf-8')
        return value
    with patch.object(Path, 'read_text', changing_read):
        with pytest.raises(OSError, match='changed during lookup'):
            _run_chapter_index(path, signature)
    assert _run_chapter_index(path, _run_file_signature(path)) == 22
