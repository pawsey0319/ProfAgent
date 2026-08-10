from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture
def project_root() -> Path:
    return ROOT


@pytest.fixture
def offline_settings(project_root: Path):
    from profagent.config import Settings

    return Settings(
        root_dir=project_root,
        cpa_text_enabled=False,
        cpa_api_key=None,
        dense_enabled=False,
        dense_force_failure=False,
        catalog_enabled=True,
        catalog_force_failure=False,
        vision_force_failure=False,
    )
