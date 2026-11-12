from pathlib import Path

import pytest

from engine import config

ROOT = Path(__file__).resolve().parent.parent
_orig_local_path_for = config.local_path_for


@pytest.fixture(autouse=True)
def _ignore_owner_local_config(monkeypatch, tmp_path_factory):
    """Тесты читают config.yaml проекта без личного config.local.yaml владельца."""
    nowhere = tmp_path_factory.getbasetemp() / "no-such-dir" / "config.local.yaml"

    def local_path_for(path):
        if Path(path).resolve().parent == ROOT:
            return nowhere
        return _orig_local_path_for(path)

    monkeypatch.setattr(config, "local_path_for", local_path_for)
