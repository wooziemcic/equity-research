from __future__ import annotations

import pytest

from app import config


@pytest.fixture(autouse=True)
def deterministic_unit_test_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep legacy deterministic unit fixtures explicit; production defaults require OpenAI."""
    monkeypatch.setattr(config, "OPENAI_REQUIRED", False)
    monkeypatch.setattr(config, "EXTERNAL_LLM_EXTRACTION_ENABLED", False)
    monkeypatch.setattr(config, "EXTERNAL_NARRATIVE_MODEL_ENABLED", False)
    monkeypatch.setattr(config, "MEMO_SYNTHESIS_REQUIRED", False)
