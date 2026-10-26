from pathlib import Path

import pytest

from energy_agent.config import Settings
from energy_agent.data import create_demo, dataset_context


@pytest.fixture
def settings(tmp_path: Path):
    path = tmp_path / "energy.sqlite"
    create_demo(path)
    return Settings(path)


@pytest.fixture
def context(settings):
    return dataset_context(settings.db_path)
