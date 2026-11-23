"""Per-request SDK timeout and retry checks from the live chapter interval.

The operation service owns/persists the interval. This adapter never starts a
fresh clock on retry or after author waiting. SDK timeouts constrain blocking
I/O; streaming callbacks additionally check accumulated execution time.
"""
from decimal import Decimal, localcontext
import math

from core.chapter_execution_time import ChapterTimeError, ChapterTimeExceeded


class StreamIdleExceeded(ChapterTimeExceeded):
    """No usable stream progress within the transport timeout."""


def interrupted_stream_reason(error):
    if isinstance(error, StreamIdleExceeded):
        return 'stream_idle_timeout'
    if isinstance(error, ChapterTimeExceeded):
        return 'execution_deadline_exhausted'
    return 'stream_interrupted'


class RequestExecutionDeadline:
    def __init__(self,runtime,policy,*,configured_timeout,extra_remaining=None):
        self.runtime=runtime
        self.policy=policy
        self.started=self.last_observed=float(policy.clock())
        if not math.isfinite(self.started):raise ChapterTimeError('request deadline clock is invalid')
        self.failed=False
        self.extra_remaining=extra_remaining
        self.configured_timeout=configured_timeout
        self.timeout=None
        self.last_stream_text=None

    def _observe_clock(self):
        if self.failed:
            raise ChapterTimeError('request deadline clock was previously invalid')
        now=float(self.policy.clock())
        if not math.isfinite(now) or now<self.last_observed:
            self.failed=True
            raise ChapterTimeError('request deadline clock moved backwards or is invalid')
        self.last_observed=now
        return now

    def _active_remaining(self):
        active=self.runtime.remaining_active_seconds()
        if active is None:
            raise ChapterTimeError('request deadline requires a live chapter interval')
        remaining=active
        if self.extra_remaining is not None:
            extra=self.extra_remaining()
            if extra is not None:
                if isinstance(extra,bool):raise ChapterTimeError('invalid extra execution time limit')
                extra=Decimal(str(extra))
                if not extra.is_finite():raise ChapterTimeError('invalid extra execution time limit')
                remaining=min(remaining,extra)
        if remaining<=0:
            raise ChapterTimeExceeded('Provider request active execution deadline exhausted')
        return remaining

    def remaining(self):
        now=self._observe_clock()
        with localcontext() as context:
            context.prec=64
            window=Decimal(str(self.policy.deadline_seconds))-Decimal(str(now-self.started))
        if window<=0:
            raise ChapterTimeExceeded('Provider dispatch/retry window exhausted')
        return min(window,self._active_remaining())

    def check_stream(self, received_text=False):
        # A retry window limits starting requests, not finishing a productive one.
        if self.last_stream_text is None:
            self.remaining()
        else:
            now=self._observe_clock()
            self._active_remaining()
            if self.configured_timeout>0 and now-self.last_stream_text>=self.configured_timeout:
                raise StreamIdleExceeded('Provider stream stopped producing text')
        if received_text:
            self.last_stream_text=self.last_observed

    def prepare(self):
        self.last_stream_text=None
        remaining=self.remaining()
        configured=Decimal(str(self.configured_timeout))
        if not configured.is_finite():raise ChapterTimeError('SDK timeout must be finite')
        if configured>0:remaining=min(remaining,configured)
        # Round down for the SDK float boundary, never above the measured limit.
        self.timeout=math.nextafter(float(remaining),0.0)
        if self.timeout<=0:raise ChapterTimeExceeded('remaining time is below SDK timeout precision')

    def retry_remaining(self):
        try:return float(self.remaining())
        except ChapterTimeExceeded:return 0.0
