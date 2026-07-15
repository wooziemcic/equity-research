from __future__ import annotations

import os
import tempfile

import pytest


_TEST_DATABASE_DIRECTORY = tempfile.TemporaryDirectory(prefix="cutler-pytest-")
os.environ["DATABASE_ENVIRONMENT"] = "TEST"
os.environ["CUTLER_DATABASE_PATH"] = os.path.join(_TEST_DATABASE_DIRECTORY.name, "streamlit-tests.db")

from app import config


@pytest.fixture(scope="session", autouse=True)
def isolated_default_test_database() -> None:
    """Make every default database call explicit, temporary, and outside development storage."""
    yield
    _TEST_DATABASE_DIRECTORY.cleanup()


@pytest.fixture(autouse=True)
def deterministic_unit_test_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep legacy deterministic unit fixtures explicit; production defaults require OpenAI."""
    monkeypatch.setattr(config, "OPENAI_REQUIRED", False)
    monkeypatch.setattr(config, "EXTERNAL_LLM_EXTRACTION_ENABLED", False)
    monkeypatch.setattr(config, "EXTERNAL_NARRATIVE_MODEL_ENABLED", False)
    monkeypatch.setattr(config, "MEMO_SYNTHESIS_REQUIRED", False)
