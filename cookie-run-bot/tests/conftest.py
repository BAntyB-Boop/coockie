import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def wait_until(pred, timeout=5.0, interval=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(interval)
    return pred()


@pytest.fixture
def tmp_store(tmp_path):
    from backend.config import ConfigStore
    return ConfigStore(path=tmp_path / "config.json", profiles_dir=tmp_path / "profiles", persist=True)
