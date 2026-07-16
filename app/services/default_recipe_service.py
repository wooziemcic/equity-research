from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from app import config


BUNDLED_RECIPE_PATH = config.PROJECT_ROOT / "app" / "resources" / "recipes" / "common_equity_v1.json"
BUNDLED_RECIPE_SCHEMA_PATH = BUNDLED_RECIPE_PATH.with_suffix(".schema.json")


class BundledRecipeError(ValueError):
    """Raised when the bundled normalized recipe cannot be trusted."""


def _canonical_checksum(payload: dict[str, Any]) -> str:
    unsigned = deepcopy(payload)
    unsigned.pop("checksum", None)
    encoded = json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_bundled_recipe(resource_path: Path | str = BUNDLED_RECIPE_PATH) -> dict[str, Any]:
    """Validate schema, checksum, slot count, supplemental rows, and numbering gap."""
    path = Path(resource_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        schema = json.loads(BUNDLED_RECIPE_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundledRecipeError("The bundled Common Equity recipe resource is unavailable or invalid.") from exc
    errors = sorted(Draft202012Validator(schema).iter_errors(payload), key=lambda item: list(item.path))
    if errors:
        raise BundledRecipeError(f"Bundled recipe schema validation failed: {errors[0].message}")
    if payload["checksum"] != _canonical_checksum(payload):
        raise BundledRecipeError("Bundled recipe checksum validation failed.")
    slots = payload["slots"]
    if len(slots) != 28:
        raise BundledRecipeError("The bundled recipe must contain exactly 28 slots.")
    numbered = {slot["order_number"] for slot in slots if slot["order_number"] is not None and slot["suborder"] == 0}
    if 22 in numbered or not {21, 23}.issubset(numbered):
        raise BundledRecipeError("The approved recipe numbering gap was not preserved.")
    supplemental = {(slot["display_name"], slot["suborder"]) for slot in slots if slot["suborder"]}
    if supplemental != {("Sell-Side Downgrade", 1), ("Latest Earnings Call Audio", 1)}:
        raise BundledRecipeError("The approved supplemental recipe rows were not preserved.")
    return payload


def bootstrap_default_common_equity_recipe(
    connection: sqlite3.Connection,
    *,
    resource_path: Path | str = BUNDLED_RECIPE_PATH,
) -> dict[str, Any]:
    """Create the approved active default only when no Common Equity recipe exists."""
    active = connection.execute(
        "SELECT * FROM package_recipes WHERE security_type='Common Equity' AND status='ACTIVE' ORDER BY version DESC LIMIT 1"
    ).fetchone()
    if active:
        return {"status": "ACTIVE_RECIPE_PRESERVED", "created": 0, "recipe_id": active["recipe_id"]}
    existing_count = int(connection.execute(
        "SELECT COUNT(*) FROM package_recipes WHERE security_type='Common Equity'"
    ).fetchone()[0])
    if existing_count:
        return {"status": "ADMIN_RECOVERY_REQUIRED", "created": 0, "recipe_id": None}

    payload = validate_bundled_recipe(resource_path)
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    checksum = payload["checksum"]
    recipe_id = f"RCP-BUNDLED-{checksum[:16].upper()}"
    connection.execute(
        """INSERT INTO package_recipes(
            recipe_id, recipe_name, recipe_type, security_type, version, description,
            source_workbook_name, source_workbook_hash, source_sheet, importer_version,
            status, created_at, created_by, approved_at, approved_by, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'BUNDLED_RESOURCE', ?, 'ACTIVE', ?, 'SYSTEM', ?, 'SYSTEM', ?)""",
        (
            recipe_id, payload["recipe_name"], payload["recipe_type"], payload["security_type"],
            payload["recipe_version"], payload["description"], Path(resource_path).name, checksum,
            payload["importer_version"], now, now,
            "System bootstrap from checksum-validated non-confidential normalized resource.",
        ),
    )
    stored_slots: list[dict[str, Any]] = []
    for index, slot in enumerate(payload["slots"], start=1):
        slot_id = f"SLOT-BUNDLED-{index:02d}-{hashlib.sha256(slot['normalized_slot_type'].encode()).hexdigest()[:10].upper()}"
        connection.execute(
            """INSERT INTO research_slots(
                slot_id, recipe_id, order_number, suborder, display_name, normalized_slot_type,
                section_code, section_name, required_level, long_applicable, short_applicable,
                conditional_rule, preferred_sources_json, fallback_sources_json, instructions,
                minimum_documents, maximum_documents, freshness_rule, anchor_rule,
                allowed_document_types_json, expected_output_format, auto_search_enabled,
                manual_upload_allowed, analyst_review_required, default_status, enabled,
                source_sheet, source_row, source_coordinates_json, raw_import_json, import_warning, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      'BUNDLED_RESOURCE', ?, '{}', '{}', NULL, ?)""",
            (
                slot_id, recipe_id, slot["order_number"], slot["suborder"], slot["display_name"],
                slot["normalized_slot_type"], slot["section_code"], slot["section_name"], slot["required_level"],
                None if slot["long_applicable"] is None else int(slot["long_applicable"]),
                None if slot["short_applicable"] is None else int(slot["short_applicable"]),
                slot["conditional_rule"], json.dumps(slot["preferred_sources"]), json.dumps(slot["fallback_sources"]),
                slot["instructions"], slot["minimum_documents"], slot["maximum_documents"], slot["freshness_rule"],
                slot["anchor_rule"], json.dumps(slot["allowed_document_types"]), slot["expected_output_format"],
                0, int(slot["manual_upload_allowed"]), int(slot["analyst_review_required"]),
                slot["default_status"], int(slot["enabled"]), index, now,
            ),
        )
        stored_slots.append({"slot_id": slot_id, **slot})
    snapshot = {"recipe": {key: value for key, value in payload.items() if key != "slots"}, "slots": stored_slots}
    connection.execute(
        "INSERT INTO recipe_approvals VALUES (?, ?, 'SYSTEM', ?, ?, ?, ?)",
        (f"RAPP-{secrets.token_hex(8).upper()}", recipe_id, now, checksum, payload["recipe_version"], json.dumps(snapshot, sort_keys=True)),
    )
    connection.execute(
        "INSERT INTO phase6a_audit_events VALUES (?, NULL, ?, NULL, NULL, 'SYSTEM_RECIPE_BOOTSTRAPPED', ?, 'SYSTEM', ?)",
        (
            f"P6B-{secrets.token_hex(8).upper()}", recipe_id,
            json.dumps({"checksum": checksum, "slot_count": len(stored_slots), "initialization_source": "BUNDLED_RESOURCE"}, sort_keys=True),
            now,
        ),
    )
    return {"status": "BOOTSTRAPPED", "created": 1, "recipe_id": recipe_id, "checksum": checksum, "slot_count": len(stored_slots)}


def initialize_default_common_equity_recipe(*, db_path: Path | str = config.DATABASE_PATH) -> dict[str, Any]:
    """Run the idempotent default initialization for an administrator action."""
    from app.utils import database

    database.initialize_database(db_path)
    with database.get_connection(db_path) as connection:
        return bootstrap_default_common_equity_recipe(connection)


def bundled_recipe_status(*, db_path: Path | str = config.DATABASE_PATH) -> dict[str, Any]:
    """Return safe deployment and validation details for Recipe Administration."""
    from app.utils import database

    payload = validate_bundled_recipe()
    database.initialize_database(db_path)
    with database.get_connection(db_path) as connection:
        row = connection.execute(
            """SELECT r.*, COUNT(s.slot_id) AS slot_count
               FROM package_recipes r LEFT JOIN research_slots s ON s.recipe_id=r.recipe_id
               WHERE r.security_type='Common Equity' AND r.status='ACTIVE'
               GROUP BY r.recipe_id ORDER BY r.version DESC LIMIT 1"""
        ).fetchone()
    return {
        "validation_result": "VALID",
        "bundled_checksum": payload["checksum"],
        "active_recipe": dict(row) if row else None,
        "database_environment": config.DATABASE_ENVIRONMENT,
    }
