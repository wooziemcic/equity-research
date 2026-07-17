from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app import config
from app.services.package_discovery_service import backfill_earnings_release_date, get_earnings_anchor
from app.services.package_naming_service import generate_package_display_filename
from app.services.package_recipe_service import list_assignments, list_slot_instances
from app.utils import database


DERIVED_SECTIONS = {
    "liquidity_and_capital_resources": "Liquidity and Capital Resources",
    "description_of_business_and_risk": "Business and Risk Factors",
    "executive_compensation_information": "Executive Compensation",
    "financial_statements_from_latest_filing": "Financial Statements",
}
FULL_FILING_SLOT = "most_recent_10_q_and_10_k"
SEC_FORMS = {"10-K", "10-Q", "8-K", "DEF 14A"}
PHASE6C_GENERATED_TYPES = {"SEC_READER_PDF", "FILING_SECTION_PDF", "FINAL_RECOMMENDATION", "FINAL_CHECKLIST"}


def _token(*parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:20].upper()
    return f"ART-{digest}"


def _loads(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _candidate_metadata(document_id: str, *, db_path: Path | str) -> dict[str, Any]:
    with database.get_connection(db_path) as connection:
        row = connection.execute(
            """SELECT metadata_json FROM discovered_candidates
               WHERE downloaded_document_id=? ORDER BY updated_at DESC LIMIT 1""",
            (document_id,),
        ).fetchone()
    return _loads(row["metadata_json"]).get("source_metadata", {}) if row else {}


def _document_form(document: dict[str, Any], *, db_path: Path | str) -> tuple[str, dict[str, Any]]:
    metadata = _candidate_metadata(document["document_id"], db_path=db_path)
    form = str(
        document.get("form_type")
        or document.get("normalized_form_family")
        or metadata.get("form_type")
        or ""
    ).upper()
    if not form:
        title = str(document.get("title") or "").upper()
        form = next((candidate for candidate in ("DEF 14A", "10-K", "10-Q", "8-K") if candidate in title), "")
    return form, metadata


def _conversion_status(document: dict[str, Any], artifact_type: str) -> str:
    if artifact_type == "FILING_SECTION_REFERENCE":
        return "SECTION_PDF_PENDING_PHASE6C"
    if artifact_type == "FULL_FILING" and str(document.get("mime_type") or "").startswith("text/html"):
        return "READER_PDF_PENDING_PHASE6C"
    if artifact_type in {"LICENSED_UPLOAD", "INTERNAL_UPLOAD"}:
        return "MANUAL_FILE_READY"
    if str(document.get("mime_type") or "") == "application/pdf":
        return "ORIGINAL_PDF_READY"
    return "ORIGINAL_HTML_READY"


def _full_filing_filename(
    package: dict[str, Any], document: dict[str, Any], form: str, anchor: dict[str, Any] | None,
    existing_names: list[str],
) -> str:
    filing_anchor = dict(anchor or {})
    report_period = str(document.get("report_period") or "")
    if form == "10-K" and len(report_period) >= 4 and report_period[:4].isdigit():
        filing_anchor["fiscal_year"] = int(report_period[:4])
        filing_anchor["fiscal_quarter"] = None
    return generate_package_display_filename(
        ticker=package["ticker"],
        slot_type=FULL_FILING_SLOT,
        document={**document, "form_type": form},
        anchor=filing_anchor,
        existing_names=existing_names,
    )


def _artifact_filename(
    package: dict[str, Any], slot: dict[str, Any], document: dict[str, Any],
    anchor: dict[str, Any] | None, existing_names: list[str],
) -> str:
    return generate_package_display_filename(
        ticker=package["ticker"], slot_type=slot["normalized_slot_type"], document=document,
        anchor=anchor, existing_names=existing_names,
    )


def sync_package_artifacts(
    package_id: str,
    *,
    db_path: Path | str = config.DATABASE_PATH,
) -> list[dict[str, Any]]:
    """Project approved assignments into a flat logical working-package inventory."""
    database.initialize_database(db_path)
    package = database.get_package_by_package_id(package_id, db_path=db_path)
    if not package:
        return []
    slots = {row["package_slot_instance_id"]: row for row in list_slot_instances(package_id, db_path=db_path)}
    documents = {row["document_id"]: row for row in database.list_documents_by_package(package_id, db_path=db_path)}
    approved = [
        row for row in list_assignments(package_id, db_path=db_path)
        if row["assignment_status"] == "APPROVED" and row.get("selected_for_package")
    ]
    approved = [row for row in approved if row.get("collection_status") == config.DOCUMENT_STATUS_DOWNLOADED]
    backfill_earnings_release_date(package_id, db_path=db_path)
    anchor = get_earnings_anchor(package_id, db_path=db_path)
    by_document: dict[str, list[dict[str, Any]]] = {}
    for assignment in approved:
        by_document.setdefault(assignment["document_id"], []).append(assignment)

    desired: list[dict[str, Any]] = []
    used_names: list[str] = []
    for document_id, assignments in sorted(by_document.items()):
        document = documents.get(document_id)
        if not document or not document.get("sha256_hash"):
            continue
        form, metadata = _document_form(document, db_path=db_path)
        document = {
            **document,
            "form_type": form or document.get("form_type"),
            "accession_number": document.get("accession_number") or metadata.get("accession_number"),
            "report_period": document.get("report_period") or metadata.get("report_period"),
            "publication_date": document.get("publication_date") or metadata.get("filing_date"),
        }
        sec_source = form in SEC_FORMS or str(document.get("source_domain") or "").lower().endswith("sec.gov")
        if sec_source and form in SEC_FORMS:
            canonical = next(
                (row for row in assignments if slots[row["package_slot_instance_id"]]["normalized_slot_type"] == FULL_FILING_SLOT),
                assignments[0],
            )
            slot = slots[canonical["package_slot_instance_id"]]
            filename = _full_filing_filename(package, document, form, anchor, used_names)
            used_names.append(filename)
            desired.append({
                "artifact_id": _token(package_id, document_id, "FULL_FILING", form),
                "source_document_id": document_id,
                "package_id": package_id,
                "slot_instance_id": canonical["package_slot_instance_id"],
                "assignment_id": canonical["assignment_id"],
                "artifact_type": "FULL_FILING",
                "display_filename": filename,
                "purpose_label": f"Full {form} filing",
                "source_section": None,
                "working_package_inclusion": 1,
                "audit_package_inclusion": 1,
                "analysis_eligible": 1,
                "conversion_status": _conversion_status(document, "FULL_FILING"),
            })
        for assignment in assignments:
            slot = slots.get(assignment["package_slot_instance_id"])
            if not slot:
                continue
            slot_type = slot["normalized_slot_type"]
            if sec_source and slot_type in DERIVED_SECTIONS:
                artifact_type = "FILING_SECTION_REFERENCE"
                purpose = DERIVED_SECTIONS[slot_type]
                artifact_document = {**document, "form_type": form}
            elif sec_source:
                continue
            else:
                method = str(document.get("collection_method") or "").upper()
                artifact_type = "INTERNAL_UPLOAD" if "INTERNAL" in method else "LICENSED_UPLOAD" if "UPLOAD" in method else "OFFICIAL_WEB_PAGE" if str(document.get("mime_type") or "").startswith("text/html") else "OFFICIAL_DOCUMENT"
                purpose = slot["display_name_snapshot"]
                artifact_document = document
            filename = _artifact_filename(package, slot, artifact_document, anchor, used_names)
            used_names.append(filename)
            desired.append({
                "artifact_id": _token(package_id, assignment["assignment_id"], artifact_type, purpose),
                "source_document_id": document_id,
                "package_id": package_id,
                "slot_instance_id": assignment["package_slot_instance_id"],
                "assignment_id": assignment["assignment_id"],
                "artifact_type": artifact_type,
                "display_filename": filename,
                "purpose_label": purpose,
                "source_section": purpose if artifact_type == "FILING_SECTION_REFERENCE" else None,
                "working_package_inclusion": 1,
                "audit_package_inclusion": 1,
                "analysis_eligible": 1,
                "conversion_status": _conversion_status(document, artifact_type),
            })

    checklist_name = f"{package['ticker'].upper()} Cutler Checklist.xlsx"
    desired.append({
        "artifact_id": _token(package_id, "CHECKLIST"), "source_document_id": None,
        "package_id": package_id, "slot_instance_id": None, "assignment_id": None,
        "artifact_type": "CHECKLIST", "display_filename": checklist_name,
        "purpose_label": "Current Package Checklist", "source_section": None,
        "working_package_inclusion": 1, "audit_package_inclusion": 1,
        "analysis_eligible": 0, "conversion_status": "CHECKLIST_READY",
    })

    desired_ids = {row["artifact_id"] for row in desired}
    now = database.utc_now_iso()
    with database.get_connection(db_path) as connection:
        connection.execute(
            """UPDATE package_artifacts
               SET display_filename='__sync__' || artifact_id
               WHERE package_id=? AND artifact_status='CURRENT'
                 AND artifact_type NOT IN ('PRELIMINARY_RECOMMENDATION','SEC_READER_PDF','FILING_SECTION_PDF','FINAL_RECOMMENDATION','FINAL_CHECKLIST')""",
            (package_id,),
        )
        current = connection.execute(
            """SELECT artifact_id FROM package_artifacts
               WHERE package_id=? AND artifact_status='CURRENT'
                 AND artifact_type NOT IN ('PRELIMINARY_RECOMMENDATION','SEC_READER_PDF','FILING_SECTION_PDF','FINAL_RECOMMENDATION','FINAL_CHECKLIST')""",
            (package_id,),
        ).fetchall()
        stale_ids = [row["artifact_id"] for row in current if row["artifact_id"] not in desired_ids]
        if stale_ids:
            connection.executemany(
                "UPDATE package_artifacts SET artifact_status='SUPERSEDED', working_package_inclusion=0, analysis_eligible=0, superseded_at=? WHERE artifact_id=?",
                [(now, artifact_id) for artifact_id in stale_ids],
            )
        for row in desired:
            existing = connection.execute(
                "SELECT artifact_id FROM package_artifacts WHERE artifact_id=?", (row["artifact_id"],)
            ).fetchone()
            if existing:
                connection.execute(
                    """UPDATE package_artifacts SET assignment_id=?, slot_instance_id=?, display_filename=?,
                       working_package_inclusion=?, audit_package_inclusion=?, analysis_eligible=?,
                       conversion_status=?, artifact_status='CURRENT', superseded_at=NULL WHERE artifact_id=?""",
                    (row["assignment_id"], row["slot_instance_id"], row["display_filename"],
                     row["working_package_inclusion"], row["audit_package_inclusion"], row["analysis_eligible"],
                     row["conversion_status"], row["artifact_id"]),
                )
            else:
                connection.execute(
                    """INSERT INTO package_artifacts(
                       artifact_id, source_document_id, package_id, slot_instance_id, assignment_id,
                       artifact_type, display_filename, purpose_label, source_section,
                       working_package_inclusion, audit_package_inclusion, analysis_eligible,
                       conversion_status, artifact_status, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CURRENT', ?)""",
                    (row["artifact_id"], row["source_document_id"], row["package_id"], row["slot_instance_id"],
                     row["assignment_id"], row["artifact_type"], row["display_filename"], row["purpose_label"],
                     row["source_section"], row["working_package_inclusion"], row["audit_package_inclusion"],
                     row["analysis_eligible"], row["conversion_status"], now),
                )
        connection.execute(
            """UPDATE package_artifacts SET working_package_inclusion=0, analysis_eligible=0
               WHERE package_id=? AND artifact_id IN (
                 SELECT parent_artifact_id FROM package_artifacts
                 WHERE package_id=? AND artifact_status='CURRENT' AND qa_status='PASSED'
                   AND artifact_type IN ('SEC_READER_PDF','FILING_SECTION_PDF')
               )""",
            (package_id, package_id),
        )
    return list_package_artifacts(package_id, db_path=db_path)


def list_package_artifacts(
    package_id: str,
    *,
    include_audit_only: bool = False,
    db_path: Path | str = config.DATABASE_PATH,
) -> list[dict[str, Any]]:
    database.initialize_database(db_path)
    clauses = ["a.package_id=?", "a.artifact_status='CURRENT'"]
    if not include_audit_only:
        clauses.append("a.working_package_inclusion=1")
    with database.get_connection(db_path) as connection:
        rows = connection.execute(
            f"""SELECT a.*, d.source_name, d.source_institution, d.source_url, d.publication_date,
                       d.document_date, d.file_size_bytes, d.local_path, d.mime_type, d.sha256_hash,
                       psi.display_name_snapshot AS checklist_item, psi.order_number, psi.suborder
                FROM package_artifacts a
                LEFT JOIN documents d ON d.document_id=a.source_document_id
                LEFT JOIN package_slot_instances psi ON psi.package_slot_instance_id=a.slot_instance_id
                WHERE {' AND '.join(clauses)}
                ORDER BY COALESCE(psi.order_number, 999), COALESCE(psi.suborder, 0), a.artifact_type, a.display_filename""",
            (package_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def register_preliminary_report_artifact(
    package_id: str,
    report: dict[str, Any],
    *,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any] | None:
    path = Path(report.get("pdf_path") or "")
    if not path.is_file():
        return None
    artifact_id = _token(package_id, "PRELIMINARY_RECOMMENDATION", report["report_id"])
    now = database.utc_now_iso()
    with database.get_connection(db_path) as connection:
        connection.execute(
            """UPDATE package_artifacts SET artifact_status='SUPERSEDED',
               working_package_inclusion=0, superseded_at=?
               WHERE package_id=? AND artifact_type='PRELIMINARY_RECOMMENDATION'
                 AND artifact_status='CURRENT' AND artifact_id!=?""",
            (now, package_id, artifact_id),
        )
        connection.execute(
            """INSERT INTO package_artifacts(
               artifact_id, source_document_id, package_id, slot_instance_id, assignment_id,
               artifact_type, display_filename, purpose_label, source_section,
               working_package_inclusion, audit_package_inclusion, analysis_eligible,
               conversion_status, artifact_status, created_at
               ) VALUES (?, NULL, ?, NULL, NULL, 'PRELIMINARY_RECOMMENDATION', ?,
                         'Preliminary Package View', NULL, 1, 1, 0,
                         'PRELIMINARY_REPORT_READY', 'CURRENT', ?)
               ON CONFLICT(artifact_id) DO UPDATE SET
                 display_filename=excluded.display_filename,
                 working_package_inclusion=1,
                 audit_package_inclusion=1,
                 conversion_status='PRELIMINARY_REPORT_READY',
                 artifact_status='CURRENT',
                 superseded_at=NULL""",
            (artifact_id, package_id, path.name, now),
        )
        row = connection.execute("SELECT * FROM package_artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
    return dict(row) if row else None
