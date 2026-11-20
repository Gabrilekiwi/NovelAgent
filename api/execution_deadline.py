"""Per-request SDK timeout and retry checks from the live chapter interval.

The operation service owns/persists the interval. This adapter never starts a
fresh clock on retry or after author waiting. SDK timeouts constrain blocking
I/O; streaming callbacks additionally check accumulated execution time.
"""
from decimal import Decimal, localcontext
import math

from core.chapter_execution_time import ChapterTimeError, ChapterTimeExceeded


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

    def remaining(self):
        if self.failed:
            raise ChapterTimeError('request deadline clock was previously invalid')
        now=float(self.policy.clock())
        if not math.isfinite(now) or now<self.last_observed:
            self.failed=True
            raise ChapterTimeError('request deadline clock moved backwards or is invalid')
        self.last_observed=now
        with localcontext() as context:
            context.prec=64
            remaining=Decimal(str(self.policy.deadline_seconds))-Decimal(str(now-self.started))
        active=self.runtime.remaining_active_seconds()
        if active is None:
            raise ChapterTimeError('request deadline requires a live chapter interval')
        remaining=min(remaining,active)
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

    def prepare(self):
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
