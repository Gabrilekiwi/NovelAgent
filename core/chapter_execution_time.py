"""Durable active intervals for a frozen chapter budget.

Only an explicit operation writes. Reads never start, close, or repair an
interval. A process death leaves an unknown interval and blocks further work.
The caller must already own the book and inject this clock into its execution
and request-timeout boundaries; this component does not grant write authority.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import errno
import json
import os
from pathlib import Path
import re
import time
import uuid

from core.chapter_budget_contract import validate_chapter_budget
from core.chapter_budget_lock import chapter_budget_lock
from core.engine.persistence import (PersistenceLockError, atomic_create_json,
                                     atomic_write_json, persistence_run_lock)
from core.engine.safe_paths import SafePathError, assert_safe_local_tree
from core.memory_v2.canonical import canonical_json_hash
from core.story_project.reviewed_state_document import read_project_bytes


class ChapterTimeError(RuntimeError):
    code = 'chapter_time_evidence_invalid'
    failure_category = 'local_budget'
    retryable = False


class ChapterTimeUnknown(ChapterTimeError):
    code = 'chapter_active_time_unknown'


class ChapterTimeExceeded(ChapterTimeError):
    code = 'chapter_active_time_exceeded'


def _hash(value):
    return canonical_json_hash(value, exclude_environment_fields=False)


def _stamp(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ChapterTimeError('clock must return a timezone-aware datetime')
    return value.astimezone(timezone.utc).isoformat()


def _date(value):
    try:
        parsed = datetime.fromisoformat(value)
        if _stamp(parsed) != value:
            raise ValueError('non-canonical timestamp')
        return parsed
    except (ValueError, TypeError) as exc:
        raise ChapterTimeError('invalid interval timestamp') from exc


def _seconds(ns):
    with localcontext() as context:
        context.prec = 64
        raw = format(Decimal(ns) / Decimal(1_000_000_000), 'f')
    return raw.rstrip('0').rstrip('.') if '.' in raw else raw


def _wall_ns(start, end):
    delta = end - start
    return ((delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds) * 1000


def _tick(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ChapterTimeUnknown('monotonic clock did not return nonnegative nanoseconds')
    return value


class ActiveChapterInterval:
    def __init__(self, owner, *, interval_id, spent_ns, started_ns):
        self.owner, self.interval_id = owner, interval_id
        self.spent_ns, self.started_ns = spent_ns, started_ns
        self.last_observed_ns = started_ns
        self.clock_failed = False
        self.closed = False

    def elapsed_ns(self):
        if self.closed:
            raise ChapterTimeError('active interval handle is already closed')
        if self.clock_failed:
            raise ChapterTimeUnknown('active interval already observed an invalid monotonic clock')
        try:
            observed = _tick(self.owner.monotonic_ns())
        except Exception:
            self.clock_failed = True
            raise
        if observed < self.last_observed_ns:
            self.clock_failed = True
            raise ChapterTimeUnknown('monotonic clock moved backwards')
        self.last_observed_ns = observed
        return observed - self.started_ns

    def remaining_seconds(self):
        """For the next Provider timeout; includes this still-running interval."""
        with localcontext() as context:
            context.prec = 64
            remaining = self.owner.limit - Decimal(self.spent_ns + self.elapsed_ns()) / Decimal(1_000_000_000)
        if remaining <= 0:
            raise ChapterTimeExceeded('chapter active execution time exhausted')
        return remaining


class ChapterExecutionClock:
    def __init__(self, root, budget, *, wall_clock=None, monotonic_ns=None, _local_recovery=False):
        self.root = assert_safe_local_tree(Path(root))
        self.budget = validate_chapter_budget(budget)
        self.binding = {key:self.budget[key] for key in ('book_id', 'chapter', 'budget_sha256')}
        self.limit = Decimal(self.budget['max_active_seconds'])
        self.wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self.monotonic_ns = monotonic_ns or time.monotonic_ns
        self.path = self.root / 'time.json'
        self.lock_root = self.root / 'time-lock'
        self._is_local_recovery = _local_recovery

    def _recovery_clock(self):
        return ChapterExecutionClock(self.root/'local-recovery',self.budget,
            wall_clock=self.wall_clock,monotonic_ns=self.monotonic_ns,_local_recovery=True)

    def _recovery_marker(self):
        value={'schema_version':'chapter_local_recovery_v1','binding':self.binding}
        return {**value,'record_sha256':_hash(value)}

    def _has_recovery(self):
        if self._is_local_recovery:return False
        marker=self.root/'local-recovery.json'
        if not os.path.lexists(marker) and not os.path.lexists(self.root/'local-recovery'):return False
        try:value=json.loads(read_project_bytes(self.root,'local-recovery.json'))
        except (OSError,ValueError,SafePathError) as exc:
            raise ChapterTimeError('local recovery initialization evidence is missing') from exc
        if value!=self._recovery_marker():raise ChapterTimeError('local recovery binding differs')
        self._recovery_clock()._load()
        return True

    @contextmanager
    def local_recovery(self,operation_id):
        """Measure local-only completion without closing an unknown original interval.

        The parent scope must prohibit Provider dispatch. Creating the directory
        marks first initialization; an existing directory with missing evidence
        is never silently initialized again.
        """
        if self._is_local_recovery:raise ChapterTimeError('nested local recovery is forbidden')
        clock=self._recovery_clock()
        with chapter_budget_lock(self.root/'recovery-lock'):
            if not self._has_recovery():
                atomic_create_json(self.root/'local-recovery.json',self._recovery_marker())
                clock.root.mkdir()
                clock.initialize()
            clock._load()
            with clock.active(operation_id,awaiting_author_after=True) as interval:
                yield interval

    def initialize(self):
        """Only used together with first-time budget creation, never on resume."""
        with chapter_budget_lock(self.lock_root):
            if self.path.exists():
                self._load()
                return
            self._save({'schema_version':'chapter_execution_time_v1', 'binding':self.binding,
                        'intervals':[]}, create=True)

    def _save(self, value, *, create=False):
        assert_safe_local_tree(self.root)
        if not create:
            read_project_bytes(self.root, 'time.json')
        value = {k:v for k,v in value.items() if k != 'record_sha256'}
        value['record_sha256'] = _hash(value)
        (atomic_create_json if create else atomic_write_json)(self.path, value)

    def _load(self):
        try:
            value = json.loads(read_project_bytes(self.root, 'time.json'))
        except (OSError, ValueError, SafePathError) as exc:
            raise ChapterTimeError('chapter time ledger is missing or unreadable') from exc
        return self._validate_record(value)

    def _validate_record(self, value):
        if (not isinstance(value, dict)
                or set(value) != {'schema_version','binding','intervals','record_sha256'}
                or value['schema_version'] != 'chapter_execution_time_v1'
                or value['binding'] != self.binding
                or value['record_sha256'] != _hash({k:v for k,v in value.items() if k != 'record_sha256'})
                or not isinstance(value['intervals'], list) or len(value['intervals']) > 10000):
            raise ChapterTimeError('chapter time ledger binding or hash differs')
        ids = set()
        for index, row in enumerate(value['intervals']):
            if (not isinstance(row, dict) or set(row) != {'interval_id','operation_id','started_at','ended_at','elapsed_ns','awaiting_author_after'}
                    or not isinstance(row['interval_id'],str) or not re.fullmatch('[0-9a-f]{32}', row['interval_id'])
                    or row['interval_id'] in ids
                    or not isinstance(row['operation_id'],str) or not re.fullmatch('[a-zA-Z0-9_.:-]{1,192}', row['operation_id'])
                    or not isinstance(row['awaiting_author_after'],bool)):
                raise ChapterTimeError('invalid active interval')
            ids.add(row['interval_id'])
            _date(row['started_at'])
            if row['ended_at'] is None:
                if row['elapsed_ns'] is not None or index != len(value['intervals'])-1 or row['awaiting_author_after']:
                    raise ChapterTimeError('invalid unclosed interval')
            else:
                _date(row['ended_at'])
                ns = row['elapsed_ns']
                if isinstance(ns,bool) or not isinstance(ns,int) or not 0 <= ns <= 10**24:
                    raise ChapterTimeError('invalid measured active duration')
        return value

    def _active(self):
        try:
            (self.lock_root / '.persistence.lock').lstat()
        except FileNotFoundError:
            return False
        try:
            with persistence_run_lock(self.lock_root, require_existing_lock=True):
                return False
        except PersistenceLockError as exc:
            cause = exc.__cause__
            if str(exc).startswith('persistence state is locked:') and isinstance(cause,OSError) and cause.errno in {errno.EACCES, errno.EAGAIN}:
                return True
            raise

    def report(self):
        value = self._load()
        intervals = value['intervals']
        live = False
        if intervals and intervals[-1]['ended_at'] is None:
            live = self._active()
            if not live:
                value = self._load()  # Writer may have finished during the lock probe.
                intervals = value['intervals']
        open_row = intervals[-1] if intervals and intervals[-1]['ended_at'] is None else None
        recovery_rows=[]
        recovery_status='settled'
        if self._has_recovery():
            recovery_clock=self._recovery_clock()
            recovery_report=recovery_clock.report()
            recovery_rows=recovery_clock._load()['intervals']
            recovery_status=recovery_report['active_status']
        intervals=sorted([*intervals,*recovery_rows],key=lambda row:_date(row['started_at']))
        measured = sum(row['elapsed_ns'] or 0 for row in intervals)
        now = _date(_stamp(self.wall_clock()))
        wait_ns, other_ns = 0, 0
        wall_unknown = bool(open_row and recovery_rows) or recovery_status=='unknown'
        last_stamp = None
        for index, row in enumerate(intervals):
            start = _date(row['started_at'])
            if last_stamp is not None and start < last_stamp:
                wall_unknown = True
            if row['ended_at'] is not None:
                end = _date(row['ended_at'])
                if end < start:
                    wall_unknown = True
                next_start = _date(intervals[index+1]['started_at']) if index+1 < len(intervals) else now
                gap = _wall_ns(end, next_start)
                if gap < 0:
                    wall_unknown = True
                elif row['awaiting_author_after']:
                    wait_ns += gap
                else:
                    other_ns += gap
                last_stamp = end
            else:
                last_stamp = start
        total_ns = _wall_ns(_date(intervals[0]['started_at']), now) if intervals else 0
        wall_unknown = wall_unknown or total_ns < 0 or (last_stamp is not None and now < last_stamp)
        return {'binding':dict(self.binding), 'interval_count':len(intervals),
                'active_seconds':_seconds(measured), 'active_time_complete':open_row is None and recovery_status=='settled',
                'active_status':'running' if open_row and live else 'unknown' if open_row or recovery_status=='unknown' else recovery_status,
                'local_recovery_active_seconds':_seconds(sum(row['elapsed_ns'] or 0 for row in recovery_rows)),
                'local_recovery_status':recovery_status,
                'open_interval_id':open_row['interval_id'] if open_row else None,
                'author_wait_seconds':None if wall_unknown else _seconds(wait_ns),
                'other_inactive_seconds':None if wall_unknown else _seconds(other_ns),
                'end_to_end_seconds':None if wall_unknown else _seconds(total_ns),
                'wall_time_status':'unknown' if wall_unknown else 'observed',
                'active_limit_seconds':self.budget['max_active_seconds'],
                'active_limit_reached':Decimal(_seconds(measured)) >= self.limit}

    @contextmanager
    def active(self, operation_id, *, awaiting_author_after=False):
        if not isinstance(operation_id,str) or not re.fullmatch('[a-zA-Z0-9_.:-]{1,192}', operation_id):
            raise ChapterTimeError('operation identity must not contain a path or payload')
        if not isinstance(awaiting_author_after,bool):
            raise ChapterTimeError('author wait state must be explicit')
        with chapter_budget_lock(self.lock_root):
            value = self._load()
            rows = value['intervals']
            if rows and rows[-1]['ended_at'] is None:
                raise ChapterTimeUnknown('previous active interval has no measured ending; explicit recovery required')
            spent = sum(row['elapsed_ns'] for row in rows)
            if self._has_recovery():
                recovery=self._recovery_clock()._load()['intervals']
                if any(row['elapsed_ns'] is None for row in recovery):
                    raise ChapterTimeUnknown('previous local recovery has no measured ending')
                spent+=sum(row['elapsed_ns'] for row in recovery)
            if not self._is_local_recovery and Decimal(_seconds(spent)) >= self.limit:
                raise ChapterTimeExceeded('chapter active execution time exhausted')
            if len(rows) >= 10000:
                raise ChapterTimeError('chapter time ledger interval limit reached')
            interval_id = uuid.uuid4().hex
            handle = ActiveChapterInterval(self,interval_id=interval_id,spent_ns=spent,started_ns=_tick(self.monotonic_ns()))
            rows.append({'interval_id':interval_id, 'operation_id':operation_id,
                         'started_at':_stamp(self.wall_clock()),'ended_at':None,'elapsed_ns':None,'awaiting_author_after':False})
            self._save(value)
            try:
                yield handle
            finally:
                elapsed = handle.elapsed_ns()
                current = self._load()
                if current != {**value, 'record_sha256':_hash({k:v for k,v in value.items() if k != 'record_sha256'})}:
                    raise ChapterTimeError('chapter time ledger changed while interval was active')
                current['intervals'][-1].update(ended_at=_stamp(self.wall_clock()),elapsed_ns=elapsed,
                                                awaiting_author_after=awaiting_author_after)
                self._save(current)
                handle.closed = True
