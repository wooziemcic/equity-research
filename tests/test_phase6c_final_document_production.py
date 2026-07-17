from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from datetime import date
from pathlib import Path

import fitz
import pytest

from app.services.company_facts_service import build_company_facts, list_selected_facts
from app.services.final_delivery_service import _qa_zip, _zip_entry_from_file
from app.services.finalization_service import confirm_waiver, create_waiver, evaluate_readiness, is_final_locked, start_finalization
from app.services.package_recipe_service import (
    assign_document,
    create_package_from_active_recipe,
    list_slot_instances,
    mark_slot,
    update_assignment,
)
from app.services.sec_document_production_service import (
    clean_sec_html,
    extract_section_pdfs,
    qa_reader_pdf,
    render_sec_reader_pdfs,
    set_section_extraction_override,
)
from app.utils import database


def _package(tmp_path: Path) -> tuple[Path, dict]:
    db_path = tmp_path / "phase6c.db"
    database.initialize_database(db_path)
    package = create_package_from_active_recipe(
        {"ticker": "ACME", "company_name": "Acme Corp.", "cik": "1", "exchange": "NYSE", "resolution_status": "RESOLVED"},
        research_cutoff=date(2026, 7, 16), compilation_date=date(2026, 7, 17),
        compiled_by="Unit Analyst", created_by="Unit Analyst", db_path=db_path,
    )
    with database.get_connection(db_path) as connection:
        connection.execute(
            """INSERT INTO earnings_cycle_anchors(
               anchor_id, package_id, fiscal_year, fiscal_quarter, reporting_period_end,
               earnings_release_date, filing_date, anchor_source, confidence, validation_status,
               created_at, updated_at, approved_at, approved_by
               ) VALUES ('ANCHOR-6C', ?, 2026, 'Q1', '2026-03-31', '2026-05-12', '2026-05-12',
               'UNIT_TEST', 'HIGH', 'CONFIRMED', ?, ?, ?, 'Unit Analyst')""",
            (package["package_id"], database.utc_now_iso(), database.utc_now_iso(), database.utc_now_iso()),
        )
    return db_path, package


def _slot(db_path: Path, package_id: str, normalized_type: str) -> dict:
    return next(row for row in list_slot_instances(package_id, db_path=db_path) if row["normalized_slot_type"] == normalized_type)


def _html_filing() -> bytes:
    return b"""<html><head><title>Acme Corp 2025 Form 10-K</title><script>bad()</script></head><body>
    <nav>Navigation</nav><h1>Item 1. Business</h1><p>""" + b"Acme business operations and customers. " * 80 + b"""</p>
    <h2>Item 1A. Risk Factors</h2><p>""" + b"Material competition and execution risks. " * 80 + b"""</p>
    <h2>Item 2. Properties</h2><p>Facilities.</p>
    <h2>Item 7. Management's Discussion and Analysis</h2><h3>Liquidity and Capital Resources</h3>
    <p>""" + b"Cash resources, liquidity, debt and capital spending. " * 80 + b"""</p>
    <h3>Critical Accounting Policies</h3><p>Estimates.</p>
    <h2>Item 8. Financial Statements</h2><table><tr><th>Revenue</th><th>2025</th></tr><tr><td>Total</td><td>$100</td></tr></table>
    <h2>Item 9. Changes in and Disagreements</h2><p>None.</p>
    </body></html>"""


def _approved_filing(db_path: Path, package: dict, tmp_path: Path) -> dict:
    path = tmp_path / "acme-10k.htm"
    path.write_bytes(_html_filing())
    document = database.create_document_record({
        "document_id": database.generate_document_id("DOC-6C"), "package_id": package["package_id"], "ticker": package["ticker"],
        "category": "SEC Filing", "document_type": "10-K", "title": "Acme 2025 10-K", "source_name": "SEC",
        "source_url": "https://www.sec.gov/Archives/acme-10k.htm", "source_domain": "www.sec.gov", "form_type": "10-K",
        "normalized_form_family": "10-K", "accession_number": "0000000001-26-000001", "report_period": "2025-12-31",
        "publication_date": "2026-02-15", "document_date": "2026-02-15", "local_filename": path.name,
        "original_filename": path.name, "local_path": str(path), "mime_type": "text/html", "file_size_bytes": path.stat().st_size,
        "sha256_hash": hashlib.sha256(path.read_bytes()).hexdigest(), "collection_method": "PUBLIC_DISCOVERY",
        "collection_status": "DOWNLOADED", "is_public": 1,
    }, db_path=db_path)
    full_slot = _slot(db_path, package["package_id"], "most_recent_10_q_and_10_k")
    assign_document(full_slot["package_slot_instance_id"], document["document_id"], actor="Unit Analyst",
                    assignment_source="PUBLIC_DISCOVERY", db_path=db_path)
    liquidity = _slot(db_path, package["package_id"], "liquidity_and_capital_resources")
    now = database.utc_now_iso()
    with database.get_connection(db_path) as connection:
        connection.execute(
            """INSERT INTO slot_document_assignments(
               assignment_id, package_slot_instance_id, package_id, document_id, assignment_source,
               final_slot_id, matched_tokens_json, assignment_status, selected_for_package,
               assigned_at, assigned_by, approved_at, approved_by
               ) VALUES (?, ?, ?, ?, 'UNIT_TEST_SECTION', ?, '[]', 'APPROVED', 1, ?, 'Unit Analyst', ?, 'Unit Analyst')""",
            (database.generate_document_id("ASG-6C"), liquidity["package_slot_instance_id"], package["package_id"],
             document["document_id"], liquidity["slot_id"], now, now),
        )
    return document


def test_phase6c_schema_is_additive_and_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "schema.db"
    database.initialize_database(db_path)
    database.initialize_database(db_path)
    with database.get_connection(db_path) as connection:
        version = connection.execute("SELECT schema_value FROM schema_metadata WHERE schema_key='database_schema_version'").fetchone()[0]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert version == "6C.0"
    assert {"finalization_runs", "finalization_stage_status", "company_facts_responses", "normalized_financial_facts",
            "final_analysis_snapshots", "final_package_manifests", "final_zip_outputs", "final_package_locks"} <= tables


def test_sec_reader_pdf_and_actual_section_pdf(tmp_path: Path) -> None:
    db_path, package = _package(tmp_path)
    source = _approved_filing(db_path, package, tmp_path)
    readers = render_sec_reader_pdfs(package["package_id"], db_path=db_path)
    reader = next(row for row in readers if row["artifact_type"] == "SEC_READER_PDF")
    reader_path = Path(reader["generated_path"])
    assert reader["conversion_status"] == "READER_PDF_GENERATED"
    assert qa_reader_pdf(reader_path, source_sha256=source["sha256_hash"], required_text=["Item 1. Business"])["passed"]
    with fitz.open(reader_path) as document:
        reader_text = "".join(page.get_text() for page in document)
    assert "Navigation" not in reader_text
    sections = extract_section_pdfs(package["package_id"], db_path=db_path)
    section = next(row for row in sections if row["artifact_type"] == "FILING_SECTION_PDF")
    with fitz.open(section["generated_path"]) as document:
        text = "".join(page.get_text() for page in document)
    assert "Cash resources, liquidity" in text
    assert "Acme business operations" not in text


def test_proxy_compensation_headings_and_section_override(tmp_path: Path) -> None:
    db_path, package = _package(tmp_path)
    path = tmp_path / "acme-proxy.htm"
    path.write_bytes(b"""<html><head><title>Acme Corp DEF 14A</title></head><body>
    <div><span style="font-weight:bold">EXECUTIVE COMPENSATION</span></div>
    <p>Compensation discussion and analysis for named executive officers.</p>
    <table><tr><th>Summary Compensation Table</th><th>2025</th></tr><tr><td>CEO</td><td>$1</td></tr></table>
    <p>Outstanding Equity Awards at fiscal year-end.</p>
    <div>2025 DIRECTOR COMPENSATION TABLE</div><table><tr><td>Director</td><td>$1</td></tr></table>
    <h2>Security Ownership</h2><p>Owners.</p>
    </body></html>""")
    document = database.create_document_record({
        "document_id": database.generate_document_id("DOC-PROXY"), "package_id": package["package_id"], "ticker": package["ticker"],
        "category": "SEC Filing", "document_type": "DEF 14A", "title": "Acme Proxy", "source_name": "SEC",
        "source_url": "https://www.sec.gov/Archives/acme-proxy.htm", "source_domain": "www.sec.gov", "form_type": "DEF 14A",
        "normalized_form_family": "DEF 14A", "accession_number": "0000000001-26-000002", "report_period": "2025-12-31",
        "publication_date": "2026-04-15", "document_date": "2026-04-15", "local_filename": path.name,
        "original_filename": path.name, "local_path": str(path), "mime_type": "text/html", "file_size_bytes": path.stat().st_size,
        "sha256_hash": hashlib.sha256(path.read_bytes()).hexdigest(), "collection_method": "PUBLIC_DISCOVERY",
        "collection_status": "DOWNLOADED", "is_public": 1,
    }, db_path=db_path)
    exec_slot = _slot(db_path, package["package_id"], "executive_compensation_information")
    assign_document(exec_slot["package_slot_instance_id"], document["document_id"], actor="Unit Analyst",
                    assignment_source="PUBLIC_DISCOVERY", db_path=db_path)
    render_sec_reader_pdfs(package["package_id"], db_path=db_path)
    sections = extract_section_pdfs(package["package_id"], db_path=db_path)
    section = next(row for row in sections if row["artifact_type"] == "FILING_SECTION_PDF")
    qa = json.loads(section["qa_result_json"])
    assert qa["confidence"] == "HIGH"
    with fitz.open(section["generated_path"]) as pdf:
        text = "".join(page.get_text() for page in pdf)
    assert "Summary Compensation Table" in text
    assert "Security Ownership" not in text

    with database.get_connection(db_path) as connection:
        reference = connection.execute(
            "SELECT * FROM package_artifacts WHERE package_id=? AND artifact_type='FILING_SECTION_REFERENCE' LIMIT 1",
            (package["package_id"],),
        ).fetchone()
    set_section_extraction_override(
        package["package_id"], "PV-SECTION", reference["artifact_id"], reference["source_section"],
        start_text="EXECUTIVE COMPENSATION", end_text="Security Ownership",
        reason="Include complete compensation section for analyst review.", actor="Unit Analyst", db_path=db_path,
    )
    override_sections = extract_section_pdfs(package["package_id"], package_version_id="PV-SECTION", db_path=db_path)
    override = next(row for row in override_sections if row["artifact_type"] == "FILING_SECTION_PDF")
    override_qa = json.loads(override["qa_result_json"])
    assert override_qa["confidence"] == "OVERRIDE_CONFIRMED"


def test_invalid_or_empty_sec_html_is_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        clean_sec_html(b"")
    with pytest.raises(ValueError, match="enough readable"):
        clean_sec_html(b"<html><script>alert(1)</script><p>x</p></html>")


def test_company_facts_period_unit_amendment_conflict_and_derived_lineage(tmp_path: Path) -> None:
    db_path, package = _package(tmp_path)
    annual = {"start": "2025-01-01", "end": "2025-12-31", "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2026-02-10", "accn": "A", "frame": "CY2025"}
    amended = {**annual, "form": "10-K/A", "filed": "2026-03-01", "accn": "B"}
    quarter = {"start": "2026-01-01", "end": "2026-03-31", "fy": 2026, "fp": "Q1", "form": "10-Q", "filed": "2026-05-12", "accn": "C", "frame": "CY2026Q1"}
    ytd = {**quarter, "start": "2026-01-01", "end": "2026-06-30", "fp": "Q2", "frame": None, "filed": "2026-07-10", "accn": "D"}
    payload = {"entityName": "Acme Corp.", "facts": {"us-gaap": {
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
            {**annual, "val": 100.0}, {**amended, "val": 110.0}, {**quarter, "val": 30.0}, {**ytd, "val": 75.0},
            {**annual, "val": 999.0, "filed": "2026-08-01", "accn": "AFTER_CUTOFF"},
        ]}},
        "GrossProfit": {"units": {"USD": [{**amended, "val": 44.0}, {**quarter, "val": 12.0}]}},
        "Assets": {"units": {"USD": [{"end": "2026-03-31", "val": 250.0, "fy": 2026, "fp": "Q1", "form": "10-Q", "filed": "2026-05-12", "accn": "C", "frame": "CY2026Q1I"}]}},
    }}}
    summary = build_company_facts(package["package_id"], "PV-FACTS", payload=payload, db_path=db_path)
    selected = list_selected_facts("PV-FACTS", db_path=db_path)
    annual_revenue = next(row for row in selected if row["normalized_metric"] == "revenue" and row["period_end"] == "2025-12-31")
    assert annual_revenue["value"] == 110.0
    assert annual_revenue["form"] == "10-K/A"
    assert all(row["value"] != 999.0 for row in selected)
    assert any(row["normalized_metric"] == "gross_margin" and json.loads(row["source_fact_ids_json"]) for row in selected)
    assert summary["conflict_count"] >= 1
    assert not any(row["period_end"] == "2026-06-30" and row["normalized_metric"] == "revenue" for row in selected)


def test_readiness_waiver_states_and_database_lock_guard(tmp_path: Path) -> None:
    db_path, package = _package(tmp_path)
    required_slots = [slot for slot in list_slot_instances(package["package_id"], db_path=db_path) if slot["requirement_snapshot"] == "REQUIRED"]
    for slot in required_slots:
        mark_slot(slot["package_slot_instance_id"], "NOT_AVAILABLE", reason="Not available after documented review.", actor="Validator", db_path=db_path)
    run = start_finalization(package["package_id"], actor="Unit Analyst", db_path=db_path)
    pending = create_waiver(run["finalization_run_id"], required_slots[0]["package_slot_instance_id"],
                            reason="Validation found no official material.", actor="Validator", confirmed=False, db_path=db_path)
    assert not evaluate_readiness(package["package_id"], package_version_id=run["package_version_id"], db_path=db_path).ready
    confirm_waiver(pending["waiver_id"], actor="Unit Analyst", db_path=db_path)
    for slot in required_slots[1:]:
        create_waiver(run["finalization_run_id"], slot["package_slot_instance_id"],
                      reason="Analyst confirmed unavailable.", actor="Unit Analyst", db_path=db_path)
    assert evaluate_readiness(package["package_id"], package_version_id=run["package_version_id"], db_path=db_path).ready
    with database.get_connection(db_path) as connection:
        connection.execute(
            "INSERT INTO final_package_locks VALUES ('LOCK-TEST', ?, ?, 'SNAP-TEST', 'm', 'w', 'a', 'Unit Analyst', ?, '{}')",
            (package["package_id"], run["package_version_id"], database.utc_now_iso()),
        )
    assert is_final_locked(package["package_id"], db_path=db_path)
    slot = list_slot_instances(package["package_id"], db_path=db_path)[0]
    with pytest.raises(sqlite3.IntegrityError, match="Final package is locked"):
        mark_slot(slot["package_slot_instance_id"], "RESTORE", reason="", actor="Unit Analyst", db_path=db_path)


def test_zip_writer_is_flat_deterministic_and_path_safe(tmp_path: Path) -> None:
    source = tmp_path / "file.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture")
    archive_path = tmp_path / "package.zip"
    entry = "ACME Equity Research Package/ACME 10K FY25 2.10.26.pdf"
    with zipfile.ZipFile(archive_path, "w") as archive:
        _zip_entry_from_file(archive, entry, source)
    assert _qa_zip(archive_path, expected_paths=[entry])["passed"]
    with pytest.raises(ValueError, match="Unsafe ZIP"):
        with zipfile.ZipFile(tmp_path / "unsafe.zip", "w") as archive:
            _zip_entry_from_file(archive, "../escape.pdf", source)
