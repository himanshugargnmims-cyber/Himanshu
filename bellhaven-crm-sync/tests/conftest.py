import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Tests must never write into the real state/ dir (it holds the live run's audit snapshots)."""
    from bellhaven_sync import config
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "state" / "test.db")
