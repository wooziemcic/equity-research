from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

from app import config
from app.utils import database


def archive_manual_test_drafts(
    package_ids: list[str],
    *,
    reason: str = "Archived manual test draft",
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    """Archive explicitly selected unlocked drafts while retaining their full audit history."""
    archived: list[str] = []
    skipped: dict[str, str] = {}
    for package_id in dict.fromkeys(package_ids):
        try:
            package = database.archive_draft_package(package_id, reason=reason, db_path=db_path)
            database.create_audit_event(
                event_id=f"AUD-ADMIN-{secrets.token_hex(6).upper()}",
                package_id=package_id,
                event_type="MANUAL_TEST_DRAFT_ARCHIVED",
                event_details_json=json.dumps(
                    {"reason": reason, "archived_at": package.get("archived_at")}, sort_keys=True
                ),
                actor="admin",
                db_path=db_path,
            )
            archived.append(package_id)
        except ValueError as exc:
            skipped[package_id] = str(exc)
    return {"archived": archived, "skipped": skipped}
