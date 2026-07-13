from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from app import config
from app.services.workspace_service import ensure_inside, sanitize_filename


def ensure_processed_inside(target: Path) -> Path:
    return ensure_inside(config.PROCESSED_DIR, target)


def processing_run_workspace(version_id: str, processing_run_id: str) -> Path:
    root = config.PROCESSED_DIR / sanitize_filename(version_id) / sanitize_filename(processing_run_id)
    ensure_processed_inside(root)
    for child in ("documents", "chunks", "evidence", "indexes"):
        (root / child).mkdir(parents=True, exist_ok=True)
    return root


def document_workspace(version_id: str, processing_run_id: str, version_document_id: str) -> Path:
    root = processing_run_workspace(version_id, processing_run_id) / "documents" / sanitize_filename(version_document_id)
    ensure_processed_inside(root)
    for child in ("pages", "sheets", "tables"):
        (root / child).mkdir(parents=True, exist_ok=True)
    return root


def relative_processed_path(path: Path) -> str:
    resolved = ensure_processed_inside(path)
    return resolved.relative_to(config.PROCESSED_DIR.resolve()).as_posix()


def processed_path(relative_path: str) -> Path:
    path = config.PROCESSED_DIR / relative_path
    return ensure_processed_inside(path)


def atomic_write_bytes(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    target = ensure_processed_inside(path)
    handle, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(handle, "wb") as temp_file:
            temp_file.write(content)
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.remove(temp_name)
    return target


def atomic_write_text(path: Path, content: str) -> Path:
    return atomic_write_bytes(path, content.encode("utf-8"))


def atomic_write_json(path: Path, payload: dict[str, Any] | list[Any]) -> Path:
    encoded = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True)
    return atomic_write_text(path, encoded)
