"""Partition the normal pytest collection by module, without splitting shared fixtures."""
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


class ModuleShard:
    def __init__(self, index, count):
        if not 0 <= index < count:
            raise ValueError("invalid CI shard")
        self.index, self.count = index, count

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, config, items):
        modules = sorted({item.path.relative_to(ROOT).as_posix() for item in items})
        included = set(modules[self.index::self.count])
        keep, drop = [], []
        for item in items:
            (keep if item.path.relative_to(ROOT).as_posix() in included else drop).append(item)
        config.hook.pytest_deselected(items=drop)
        items[:] = keep
        print(f"CI shard {self.index + 1}/{self.count}: {len(keep)} items in {len(included)} modules")


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    index, count = map(int, sys.argv[1:3])
    raise SystemExit(pytest.main(["-q", "--tb=short", "--durations=12", *sys.argv[3:]],
                                plugins=[ModuleShard(index, count)]))
