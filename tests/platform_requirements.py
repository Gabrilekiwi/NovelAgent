"""Host contracts for tests that must acquire a real Windows recovery target."""
import os

import pytest


def require_windows_recovery():
    if os.name != "nt":
        pytest.skip("Real restore destinations and certification require a Windows local drive")
