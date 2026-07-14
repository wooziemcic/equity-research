from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from app import config

SAFE_FILENAME_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")


class WorkspaceError(ValueError):
    """Raised for unsafe package workspace operations."""


def sanitize_filename(name: str, *, default: str = "document") -> str:
    """Return a filesystem-safe filename while preserving extensions."""
    cleaned = SAFE_FILENAME_PATTERN.sub("_", name.strip()).strip("._")
    cleaned = re.sub(r"_+", "_", cleaned)
    return cleaned[:180] or default


def ensure_inside(base: Path, target: Path) -> Path:
    """Resolve and validate that target stays within base."""
    resolved_base = base.resolve()
    resolved_target = target.resolve()
    comparison_base = _without_windows_namespace(resolved_base)
    comparison_target = _without_windows_namespace(resolved_target)
    try:
        comparison_target.relative_to(comparison_base)
    except ValueError as exc:
        raise WorkspaceError("Refusing to write outside the configured data directory.") from exc
    return resolved_target


def _without_windows_namespace(path: Path) -> Path:
    """Normalize the optional extended-length prefix used by Windows APIs."""
    value = str(path)
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


def package_workspace(package_id: str) -> Path:
    """Create and return the package download workspace."""
    safe_package_id = sanitize_filename(package_id)
    if safe_package_id != package_id:
        raise WorkspaceError("Unsafe package id for workspace creation.")
    root = config.DOWNLOAD_DIR / safe_package_id
    ensure_inside(config.DOWNLOAD_DIR, root)
    for child in ("sec", "investor_relations", "metadata", "licensed"):
        (root / child).mkdir(parents=True, exist_ok=True)
    for child in config.LICENSED_SOURCE_TYPES:
        (root / "licensed" / child).mkdir(parents=True, exist_ok=True)
    return root


def source_directory(package_id: str, source: str) -> Path:
    """Return the package subdirectory for a document source."""
    if source not in {"sec", "investor_relations", "metadata", "licensed"}:
        raise WorkspaceError("Unsupported package workspace source.")
    directory = package_workspace(package_id) / source
    return ensure_inside(config.DOWNLOAD_DIR, directory)


def licensed_source_directory(package_id: str, source_type: str) -> Path:
    """Return a safe licensed-source directory for the package."""
    if source_type not in config.LICENSED_SOURCE_TYPES:
        raise WorkspaceError("Unsupported licensed source type.")
    directory = package_workspace(package_id) / "licensed" / source_type
    return ensure_inside(config.DOWNLOAD_DIR, directory)


def safe_licensed_document_path(package_id: str, source_type: str, filename: str) -> Path:
    """Return a safe path for an uploaded licensed document."""
    directory = licensed_source_directory(package_id, source_type)
    path = directory / sanitize_filename(filename)
    return ensure_inside(config.DOWNLOAD_DIR, path)


def safe_document_path(package_id: str, source: str, filename: str) -> Path:
    """Return a safe path for a downloaded document."""
    directory = source_directory(package_id, source)
    path = directory / sanitize_filename(filename)
    return ensure_inside(config.DOWNLOAD_DIR, path)


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Write bytes atomically so partial downloads are not considered complete."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ensure_inside(config.DOWNLOAD_DIR, path)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as temp_file:
            temp_file.write(content)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.remove(temp_name)


def write_metadata_json(package_id: str, filename: str, payload: dict[str, Any]) -> Path:
    """Write a structured metadata snapshot for a package."""
    path = safe_document_path(package_id, "metadata", sanitize_filename(filename))
    if path.suffix.lower() != ".json":
        path = path.with_suffix(".json")
    atomic_write_bytes(path, json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"))
    return path
