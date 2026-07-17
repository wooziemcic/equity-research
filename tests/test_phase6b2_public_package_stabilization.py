from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from app.services.analysis_snapshot_service import (
    CorpusIsolationError,
    create_analysis_snapshot,
    finalize_analysis_snapshot,
    validate_analysis_snapshot,
    validate_snapshot_document_scope,
)
from app.services.numeric_claim_service import (
    validate_derived_numeric_claim,
    validate_reported_numeric_claim,
)
from app.services.package_artifact_service import register_preliminary_report_artifact, sync_package_artifacts
from app.services.package_assembly_service import package_contents
from app.services.package_discovery_service import (
    EarningsAnchor,
    RecipePlannerAgent,
    SlotQueryPlannerAgent,
    _store_anchor,
    discover_earnings_exhibit_99_1,
    get_earnings_anchor,
    run_all_public_slots,
    select_curated_sec_filings,
)
from app.services.package_naming_service import classify_upload_filename
from app.services.package_recipe_service import (
    assign_document,
    create_package_from_active_recipe,
    list_slot_instances,
    mark_slot,
    suggest_document_assignments,
    update_assignment,
)
from app.services.public_slot_status_service import (
    public_discovery_preview,
    public_slot_diagnostics,
    sync_public_slot_states,
)
from app.services.reporting.memo_quality import (
    InvestmentMemoDraft,
    MemoDraftItem,
    MemoEvidenceCandidate,
    _draft_failure,
    _remove_failed_content,
    _remove_unsupported_numeric_sentences,
)
from app.services.reporting.investment_report import memo_to_sections
from app.services.research_workflow_service import _existing_analysis_run
from app.services.upload_service import UploadCandidate, prepare_batch_review, store_reviewed_upload_batch
from app.utils import database


@pytest.fixture()
def phase6b2_package(tmp_path: Path) -> tuple[Path, dict, Path]:
    db_path = tmp_path / "phase6b2.db"
    database.initialize_database(db_path)
    package = create_package_from_active_recipe(
        {
            "ticker": "QXO", "company_name": "QXO, Inc.", "cik": "0001236275",
            "exchange": "NYSE", "fiscal_year_end": "1231", "resolution_status": "RESOLVED",
        },
        research_cutoff=date(2026, 7, 16), compilation_date=date(2026, 7, 16),
        compiled_by="Unit Analyst", created_by="Unit Analyst", db_path=db_path,
    )
    return db_path, package, tmp_path


def _slot(db_path: Path, package_id: str, slot_type: str) -> dict:
    return next(
        row for row in list_slot_instances(package_id, db_path=db_path)
        if row["normalized_slot_type"] == slot_type
    )


def _document(
    db_path: Path, package: dict, root: Path, filename: str, *,
    form: str = "", method: str = "PUBLIC_DISCOVERY", content: bytes | None = None,
) -> dict:
    path = root / f"{database.generate_document_id('FILE')}-{filename}"
    path.write_bytes(content or (b"%PDF-1.7\nphase6b2\n" + filename.encode()))
    return database.create_document_record(
        {
            "document_id": database.generate_document_id("DOC-6B2"), "package_id": package["package_id"],
            "ticker": package["ticker"], "category": "SEC Filing" if form else "Research",
            "document_type": form or "Research", "title": Path(filename).stem,
            "source_name": "SEC" if form else "Licensed Research",
            "source_url": f"https://www.sec.gov/Archives/{filename}" if form else f"https://licensed.invalid/{filename}",
            "source_domain": "www.sec.gov" if form else "licensed.invalid",
            "form_type": form or None, "normalized_form_family": form or None,
            "accession_number": f"0001236275-26-{len(filename):06d}" if form else None,
            "report_period": "2025-12-31" if form == "10-K" else "2026-03-31" if form == "10-Q" else None,
            "publication_date": "2026-05-12", "document_date": "2026-05-12",
            "local_filename": filename, "original_filename": filename, "local_path": str(path),
            "mime_type": "text/html" if Path(filename).suffix.lower() in {".htm", ".html"} else "application/pdf",
            "file_size_bytes": path.stat().st_size, "sha256_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
            "collection_method": method, "collection_status": "DOWNLOADED", "is_public": method == "PUBLIC_DISCOVERY",
        },
        db_path=db_path,
    )


def _discovery_run(db_path: Path, package: dict, *, omitted_slot_id: str | None = None) -> str:
    run_id = database.generate_document_id("DISC-TEST")
    now = database.utc_now_iso()
    with database.get_connection(db_path) as connection:
        instance_id = connection.execute(
            "SELECT package_recipe_instance_id FROM package_recipe_instances WHERE package_id=?",
            (package["package_id"],),
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO package_discovery_runs(
               discovery_run_id, package_id, package_recipe_instance_id, search_profile_version,
               status, started_at, completed_at, started_by, slot_count_requested, slot_count_completed
               ) VALUES (?, ?, ?, '1.0', 'COMPLETED', ?, ?, 'test', 10, 10)""",
            (run_id, package["package_id"], instance_id, now, now),
        )
        public_types = {
            row[0] for row in connection.execute(
                "SELECT normalized_slot_type FROM slot_search_profiles WHERE enabled=1 AND status='ACTIVE'"
            ).fetchall()
        }
        slots = [
            row for row in list_slot_instances(package["package_id"], db_path=db_path)
            if row["normalized_slot_type"] in public_types and row["package_slot_instance_id"] != omitted_slot_id
        ]
        for slot in slots:
            connection.execute(
                """INSERT INTO slot_discovery_runs(
                   slot_discovery_run_id, discovery_run_id, package_id, package_slot_instance_id,
                   normalized_slot_type, source_route, status, started_at, completed_at
                   ) VALUES (?, ?, ?, ?, ?, 'BRAVE_OFFICIAL', 'COMPLETED', ?, ?)""",
                (database.generate_document_id("SDISC"), run_id, package["package_id"],
                 slot["package_slot_instance_id"], slot["normalized_slot_type"], now, now),
            )
    return run_id


def test_phase6b2_schema_is_additive_and_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "schema.db"
    database.initialize_database(db_path)
    database.initialize_database(db_path)
    with database.get_connection(db_path) as connection:
        assert connection.execute(
            "SELECT schema_value FROM schema_metadata WHERE schema_key='database_schema_version'"
        ).fetchone()[0] == "6B.2"
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"package_artifacts", "public_slot_states", "analysis_corpus_snapshots", "numeric_claims", "report_repair_audits"} <= tables


def test_public_profile_has_exactly_ten_slots(phase6b2_package: tuple[Path, dict, Path]) -> None:
    db_path, package, _ = phase6b2_package
    preview = public_discovery_preview(package["package_id"], db_path=db_path)
    assert preview["total_public_slots"] == 10
    assert preview["slots_requiring_discovery"] == 10


def test_all_public_slots_receive_terminal_state(phase6b2_package: tuple[Path, dict, Path]) -> None:
    db_path, package, _ = phase6b2_package
    run_id = _discovery_run(db_path, package)
    rows = sync_public_slot_states(package["package_id"], run_id, db_path=db_path)
    assert len(rows) == 10
    assert {row["terminal_state"] for row in rows} == {"NO_CANDIDATE_FOUND"}
    assert all(row["missing_reason"] and row["next_recommended_action"] for row in rows)


def test_skipped_public_slot_is_explicit_failure(phase6b2_package: tuple[Path, dict, Path]) -> None:
    db_path, package, _ = phase6b2_package
    omitted = _slot(db_path, package["package_id"], "investor_presentations")
    run_id = _discovery_run(db_path, package, omitted_slot_id=omitted["package_slot_instance_id"])
    rows = sync_public_slot_states(package["package_id"], run_id, db_path=db_path)
    row = next(item for item in rows if item["package_slot_instance_id"] == omitted["package_slot_instance_id"])
    assert row["terminal_state"] == "FAILED"
    assert "not executed" in row["terminal_reason"]


def test_acknowledged_unavailable_is_terminal(phase6b2_package: tuple[Path, dict, Path]) -> None:
    db_path, package, _ = phase6b2_package
    slot = _slot(db_path, package["package_id"], "latest_earnings_call_audio")
    mark_slot(slot["package_slot_instance_id"], "NOT_AVAILABLE", reason="No official replay is published.", actor="test", db_path=db_path)
    run_id = _discovery_run(db_path, package)
    rows = sync_public_slot_states(package["package_id"], run_id, db_path=db_path)
    row = next(item for item in rows if item["package_slot_instance_id"] == slot["package_slot_instance_id"])
    assert row["terminal_state"] == "ACKNOWLEDGED_UNAVAILABLE"
    assert row["missing_reason"] == "No official replay is published."


def test_partial_filing_slot_reports_missing_reason(phase6b2_package: tuple[Path, dict, Path]) -> None:
    db_path, package, root = phase6b2_package
    slot = _slot(db_path, package["package_id"], "most_recent_10_q_and_10_k")
    ten_q = _document(db_path, package, root, "QXO-10Q.htm", form="10-Q")
    assign_document(slot["package_slot_instance_id"], ten_q["document_id"], actor="test", assignment_source="PUBLIC_DISCOVERY", db_path=db_path)
    run_id = _discovery_run(db_path, package)
    rows = sync_public_slot_states(package["package_id"], run_id, db_path=db_path)
    row = next(item for item in rows if item["package_slot_instance_id"] == slot["package_slot_instance_id"])
    assert row["terminal_state"] == "PARTIALLY_FILLED"
    assert "10-Q and 10-K" in row["missing_reason"]


def test_diagnostic_matrix_reports_all_required_fields(phase6b2_package: tuple[Path, dict, Path]) -> None:
    db_path, package, _ = phase6b2_package
    run_id = _discovery_run(db_path, package)
    sync_public_slot_states(package["package_id"], run_id, db_path=db_path)
    diagnostic = public_slot_diagnostics(package["package_id"], discovery_run_id=run_id, db_path=db_path)
    required = {
        "checklist_item", "required", "minimum_documents", "current_approved_count",
        "discovery_status", "selected_route", "queries_executed", "candidates_found",
        "candidates_rejected", "candidates_awaiting_review", "documents_downloaded",
        "missing_reason", "next_recommended_action",
    }
    assert len(diagnostic["rows"]) == 10 and required <= set(diagnostic["rows"][0])
    assert diagnostic["load_ms"] < 500


def test_run_all_wrapper_uses_every_missing_slot(monkeypatch: pytest.MonkeyPatch, phase6b2_package: tuple[Path, dict, Path]) -> None:
    db_path, package, _ = phase6b2_package
    called: dict = {}

    def fake_run(package_id: str, **kwargs: object) -> dict:
        called.update({"package_id": package_id, **kwargs})
        return {"discovery_run_id": "DISC-FAKE"}

    monkeypatch.setattr("app.services.package_discovery_service.run_discovery", fake_run)
    result = run_all_public_slots(package["package_id"], actor="test", db_path=db_path)
    assert result["discovery_run_id"] == "DISC-FAKE"
    assert len(called["slot_instance_ids"]) == 10


@pytest.mark.parametrize("form", ["10-K", "10-Q", "DEF 14A", "8-K"])
def test_full_sec_filing_artifact_is_created(
    form: str, phase6b2_package: tuple[Path, dict, Path],
) -> None:
    db_path, package, root = phase6b2_package
    slot_type = {
        "10-K": "most_recent_10_q_and_10_k", "10-Q": "most_recent_10_q_and_10_k",
        "DEF 14A": "executive_compensation_information", "8-K": "latest_earnings_release",
    }[form]
    document = _document(db_path, package, root, f"QXO-{form.replace(' ', '')}.htm", form=form)
    slot = _slot(db_path, package["package_id"], slot_type)
    assign_document(slot["package_slot_instance_id"], document["document_id"], actor="test", assignment_source="PUBLIC_DISCOVERY", db_path=db_path)
    artifacts = sync_package_artifacts(package["package_id"], db_path=db_path)
    assert any(row["artifact_type"] == "FULL_FILING" and row["source_document_id"] == document["document_id"] for row in artifacts)


def test_approved_earnings_release_backfills_missing_anchor_date(
    phase6b2_package: tuple[Path, dict, Path],
) -> None:
    db_path, package, root = phase6b2_package
    _store_anchor(
        package["package_id"],
        EarningsAnchor(
            2026, "Q1", "2026-03-31", None, "2026-05-12", "SEC_10Q",
            "MEDIUM", "CONFIRMED", ("Quarterly filing established the cycle.",),
        ),
        db_path=db_path,
    )
    document = _document(db_path, package, root, "QXO-earnings-8K.htm", form="8-K")
    slot = _slot(db_path, package["package_id"], "latest_earnings_release")
    assign_document(
        slot["package_slot_instance_id"], document["document_id"], actor="test",
        assignment_source="PUBLIC_DISCOVERY", db_path=db_path,
    )
    anchor = get_earnings_anchor(package["package_id"], db_path=db_path)
    assert anchor and anchor["earnings_release_date"] == "2026-05-12"


def test_filing_section_is_separate_artifact_without_duplicate_bytes(phase6b2_package: tuple[Path, dict, Path]) -> None:
    db_path, package, root = phase6b2_package
    document = _document(db_path, package, root, "QXO-10K.htm", form="10-K")
    full_slot = _slot(db_path, package["package_id"], "most_recent_10_q_and_10_k")
    section_slot = _slot(db_path, package["package_id"], "description_of_business_and_risk")
    assign_document(full_slot["package_slot_instance_id"], document["document_id"], actor="test", assignment_source="PUBLIC_DISCOVERY", db_path=db_path)
    assign_document(section_slot["package_slot_instance_id"], document["document_id"], actor="test", assignment_source="PUBLIC_DISCOVERY", db_path=db_path)
    artifacts = [row for row in sync_package_artifacts(package["package_id"], db_path=db_path) if row["source_document_id"] == document["document_id"]]
    assert {row["artifact_type"] for row in artifacts} == {"FULL_FILING", "FILING_SECTION_REFERENCE"}
    assert len({row["display_filename"] for row in artifacts}) == 2
    assert len({row["local_path"] for row in artifacts}) == 1
    with database.get_connection(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents WHERE package_id=? AND sha256_hash=?", (package["package_id"], document["sha256_hash"])).fetchone()[0] == 1


def test_section_conversion_is_deferred_to_phase6c(phase6b2_package: tuple[Path, dict, Path]) -> None:
    db_path, package, root = phase6b2_package
    document = _document(db_path, package, root, "QXO-Proxy.htm", form="DEF 14A")
    slot = _slot(db_path, package["package_id"], "executive_compensation_information")
    assign_document(slot["package_slot_instance_id"], document["document_id"], actor="test", assignment_source="PUBLIC_DISCOVERY", db_path=db_path)
    artifacts = sync_package_artifacts(package["package_id"], db_path=db_path)
    section = next(row for row in artifacts if row["artifact_type"] == "FILING_SECTION_REFERENCE")
    assert section["conversion_status"] == "SECTION_PDF_PENDING_PHASE6C"


def test_package_contents_exposes_flat_artifact_contract(phase6b2_package: tuple[Path, dict, Path]) -> None:
    db_path, package, root = phase6b2_package
    document = _document(db_path, package, root, "QXO-Earnings.pdf")
    slot = _slot(db_path, package["package_id"], "latest_earnings_release")
    assign_document(slot["package_slot_instance_id"], document["document_id"], actor="test", db_path=db_path)
    contents = package_contents(package["package_id"], db_path=db_path)
    assert any(row["artifact_type"] == "CHECKLIST" for row in contents)
    item = next(row for row in contents if row["document_id"] == document["document_id"])
    assert {"display_filename", "artifact_type", "checklist_item", "source", "document_date", "status", "size", "analysis_eligible", "conversion_status"} <= set(item)


@pytest.mark.parametrize(("filename", "slot_type"), [
    ("QXO JPM Sell Side 7.16.26.pdf", "sell_side_reports"),
    ("QXO Moody Credit Report 7.16.26.pdf", "credit_reports"),
    ("QXO Industry Report 7.16.26.pdf", "industry_report"),
    ("QXO DES 7.16.26.pdf", "bbg_des"),
    ("QXO DRSK 7.16.26.pdf", "drsk_default_risk"),
    ("QXO Morningstar 7.16.26.pdf", "morningstar_report_and_most_recent_model"),
    ("QXO Valuation 7.16.26.xlsx", "ccm_historical_multiples_valuation"),
])
def test_manual_fixture_filename_classification(filename: str, slot_type: str) -> None:
    result = classify_upload_filename(filename)
    assert result.normalized_slot_type == slot_type and result.confidence == "HIGH"


def test_approved_manual_upload_enters_contents_and_snapshot(phase6b2_package: tuple[Path, dict, Path]) -> None:
    db_path, package, root = phase6b2_package
    document = _document(db_path, package, root, "QXO JPM 7.16.26.pdf", method="UPLOAD")
    suggestions = suggest_document_assignments(package["package_id"], [document["document_id"]], actor="test", db_path=db_path)
    assert suggestions[0]["assignment_id"]
    update_assignment(suggestions[0]["assignment_id"], "approve", actor="test", db_path=db_path)
    contents = package_contents(package["package_id"], db_path=db_path)
    assert any(row["document_id"] == document["document_id"] and row["artifact_type"] == "LICENSED_UPLOAD" for row in contents)
    snapshot = create_analysis_snapshot(package["package_id"], db_path=db_path)
    assert document["document_id"] in json.loads(snapshot["document_ids_json"])


def test_controlled_seven_file_bulk_upload_updates_contents_and_snapshot(phase6b2_package: tuple[Path, dict, Path]) -> None:
    db_path, package, _ = phase6b2_package
    filenames = [
        "QXO JPM Sell Side 7.16.26.pdf", "QXO Moody Credit Report 7.16.26.pdf",
        "QXO Industry Report 7.16.26.pdf", "QXO DES 7.16.26.pdf",
        "QXO DRSK 7.16.26.pdf", "QXO Morningstar 7.16.26.pdf",
        "QXO Valuation 7.16.26.xlsx",
    ]
    candidates = [
        UploadCandidate(name, (b"PK\x03\x04fixture" if name.endswith(".xlsx") else b"%PDF-1.7\nfixture") + name.encode())
        for name in filenames
    ]
    reviews = prepare_batch_review(package, candidates, db_path=db_path)
    summary = store_reviewed_upload_batch(
        package, candidates, reviews, authorization_confirmed=True, db_path=db_path,
    )
    assert summary["uploaded"] == 7
    documents = database.list_documents_by_package(package["package_id"], db_path=db_path)
    suggestions = suggest_document_assignments(
        package["package_id"], [row["document_id"] for row in documents], actor="test", db_path=db_path,
    )
    assert len(suggestions) == 7 and all(row.get("assignment_id") for row in suggestions)
    for suggestion in suggestions:
        update_assignment(suggestion["assignment_id"], "approve", actor="test", db_path=db_path)
    contents = package_contents(package["package_id"], db_path=db_path)
    included_ids = {row["document_id"] for row in contents if row["analysis_eligible"]}
    assert included_ids == {row["document_id"] for row in documents}
    assert all(row.get("original_filename") in filenames for row in documents)
    snapshot = create_analysis_snapshot(package["package_id"], db_path=db_path)
    assert set(json.loads(snapshot["document_ids_json"])) == included_ids


def test_unapproved_upload_is_excluded_from_snapshot(phase6b2_package: tuple[Path, dict, Path]) -> None:
    db_path, package, root = phase6b2_package
    approved = _document(db_path, package, root, "QXO-Earnings.pdf")
    pending = _document(db_path, package, root, "QXO JPM Pending.pdf", method="UPLOAD")
    slot = _slot(db_path, package["package_id"], "latest_earnings_release")
    assign_document(slot["package_slot_instance_id"], approved["document_id"], actor="test", db_path=db_path)
    snapshot = create_analysis_snapshot(package["package_id"], db_path=db_path)
    assert pending["document_id"] not in json.loads(snapshot["document_ids_json"])


def test_superseded_assignment_blocks_snapshot_reuse(phase6b2_package: tuple[Path, dict, Path]) -> None:
    db_path, package, root = phase6b2_package
    document = _document(db_path, package, root, "QXO-Earnings.pdf")
    slot = _slot(db_path, package["package_id"], "latest_earnings_release")
    assignment = assign_document(slot["package_slot_instance_id"], document["document_id"], actor="test", db_path=db_path)
    snapshot = create_analysis_snapshot(package["package_id"], db_path=db_path)
    update_assignment(assignment["assignment_id"], "remove", actor="test", db_path=db_path)
    with pytest.raises(CorpusIsolationError, match="superseded or unapproved"):
        validate_snapshot_document_scope(snapshot["snapshot_id"], db_path=db_path)


def test_other_package_document_contamination_is_blocked(phase6b2_package: tuple[Path, dict, Path]) -> None:
    db_path, package, root = phase6b2_package
    document = _document(db_path, package, root, "QXO-Earnings.pdf")
    slot = _slot(db_path, package["package_id"], "latest_earnings_release")
    assign_document(slot["package_slot_instance_id"], document["document_id"], actor="test", db_path=db_path)
    snapshot = create_analysis_snapshot(package["package_id"], db_path=db_path)
    with database.get_connection(db_path) as connection:
        connection.execute(
            "UPDATE analysis_corpus_snapshots SET document_ids_json=? WHERE snapshot_id=?",
            (json.dumps([document["document_id"], "DOC-ANOTHER-PACKAGE"]), snapshot["snapshot_id"]),
        )
    with pytest.raises(CorpusIsolationError, match="without a current approved assignment"):
        validate_snapshot_document_scope(snapshot["snapshot_id"], db_path=db_path)


def _lineage_records(db_path: Path, package: dict, document: dict, snapshot: dict, *, metric_evidence: str = "EVID-1") -> tuple[str, str]:
    now = database.utc_now_iso()
    version = database.allocate_package_version(
        {
            "parent_package_id": package["package_id"], "ticker": package["ticker"],
            "company_name": package["company_name"], "security_type": package["security_type"],
            "research_cutoff_date": package["research_cutoff_date"], "status": "LOCKED",
            "created_by": "test", "created_at": now,
        },
        db_path=db_path,
    )
    version_document_id = "VDOC-1"
    database.create_package_version_document(
        {
            "version_id": version["version_id"], "document_id": version_document_id,
            "original_document_id": document["document_id"], "category": document["category"],
            "title": document["title"], "source_name": document["source_name"],
            "source_url": document["source_url"], "publication_date": document["publication_date"],
            "original_filename": document["original_filename"], "package_filename": document["original_filename"],
            "relative_package_path": document["original_filename"], "file_size": document["file_size_bytes"],
            "sha256_hash": document["sha256_hash"], "mime_type": document["mime_type"],
            "is_public": 1, "included_status": "INCLUDED", "created_at": now,
        },
        db_path=db_path,
    )
    processing_id = "PROC-1"
    database.create_processing_run(
        {
            "processing_run_id": processing_id, "version_id": version["version_id"],
            "package_id": package["package_id"], "pipeline_version": "test",
            "parser_config_version": "test", "started_at": now, "completed_at": now,
            "created_by": "test", "status": "COMPLETED",
        },
        db_path=db_path,
    )
    analysis_id = "AN-1"
    database.create_analysis_run(
        {
            "analysis_run_id": analysis_id, "package_id": package["package_id"],
            "version_id": version["version_id"], "processing_run_id": processing_id,
            "analysis_configuration_version": "test", "scorecard_version": "test",
            "valuation_configuration_version": "test", "created_by": "test",
            "created_at": now, "updated_at": now, "status": "CALCULATING",
            "analysis_snapshot_id": snapshot["snapshot_id"],
        },
        db_path=db_path,
    )
    database.create_evidence_record(
        {
            "evidence_id": "EVID-1", "processing_run_id": processing_id,
            "version_id": version["version_id"], "version_document_id": version_document_id,
            "evidence_type": "FINANCIAL", "claim_text": "Revenue was 100 million.",
            "metric_name": "Revenue", "value": 100.0, "unit": "million", "currency": "USD",
            "period": "Q1 2026", "source_text": "Revenue was 100 million.",
            "extraction_method": "TEST", "confidence": "HIGH", "verification_status": "SUPPORTS",
            "analyst_status": "AUTO_VERIFIED", "source_locator_json": '{"section":"Revenue"}',
            "created_by": "test", "created_at": now, "updated_at": now,
        },
        db_path=db_path,
    )
    database.create_analysis_metric(
        {
            "metric_id": "MET-1", "analysis_run_id": analysis_id, "metric_code": "REVENUE",
            "display_name": "Revenue", "value": 100.0, "unit": "million", "currency": "USD",
            "period": "Q1 2026", "calculation_method": "REPORTED",
            "formula_description": "Reported value", "source_evidence_ids_json": json.dumps([metric_evidence]),
            "confidence": "HIGH", "verification_status": "VERIFIED", "created_at": now,
        },
        db_path=db_path,
    )
    return processing_id, analysis_id


def test_snapshot_finalization_accepts_current_lineage(phase6b2_package: tuple[Path, dict, Path]) -> None:
    db_path, package, root = phase6b2_package
    document = _document(db_path, package, root, "QXO-Earnings.pdf")
    slot = _slot(db_path, package["package_id"], "latest_earnings_release")
    assign_document(slot["package_slot_instance_id"], document["document_id"], actor="test", db_path=db_path)
    snapshot = create_analysis_snapshot(package["package_id"], db_path=db_path)
    processing_id, analysis_id = _lineage_records(db_path, package, document, snapshot)
    finalized = finalize_analysis_snapshot(
        snapshot["snapshot_id"], analysis_run_id=analysis_id, processing_run_id=processing_id, db_path=db_path,
    )
    assert finalized["status"] == "READY"
    assert validate_analysis_snapshot(snapshot["snapshot_id"], db_path=db_path)["status"] == "PASSED"


def test_stale_metric_lineage_blocks_before_model_use(phase6b2_package: tuple[Path, dict, Path]) -> None:
    db_path, package, root = phase6b2_package
    document = _document(db_path, package, root, "QXO-Earnings.pdf")
    slot = _slot(db_path, package["package_id"], "latest_earnings_release")
    assign_document(slot["package_slot_instance_id"], document["document_id"], actor="test", db_path=db_path)
    snapshot = create_analysis_snapshot(package["package_id"], db_path=db_path)
    processing_id, analysis_id = _lineage_records(db_path, package, document, snapshot, metric_evidence="EVID-STALE")
    with pytest.raises(CorpusIsolationError, match="lacks current snapshot evidence lineage"):
        finalize_analysis_snapshot(
            snapshot["snapshot_id"], analysis_run_id=analysis_id, processing_run_id=processing_id, db_path=db_path,
        )


@pytest.mark.parametrize(("display", "reported", "excerpt", "period", "expected"), [
    ("100", 100.0, "Revenue was 100 million.", "Q1 2026", "VERIFIED_REPORTED"),
    ("100.0", 100.0, "Revenue was 100 million.", "Q1 2026", "VERIFIED_REPORTED"),
    ("100.4", 100.0, "Revenue was 100 million.", "Q1 2026", "ROUNDING_ACCEPTED"),
    ("105", 100.0, "Revenue was 100 million.", "Q1 2026", "VALUE_MISMATCH"),
    ("100", 100.0, "Revenue was 100 million.", None, "PERIOD_AMBIGUOUS"),
])
def test_reported_numeric_validation_statuses(display: str, reported: float, excerpt: str, period: str | None, expected: str) -> None:
    status, _ = validate_reported_numeric_claim(
        display_value=display, reported_value=reported, exact_excerpt=excerpt, period=period,
        source_document_id="DOC-1", source_artifact_id="ART-1", evidence_id="EVID-1",
    )
    assert status == expected


def test_numeric_source_missing_fails() -> None:
    status, _ = validate_reported_numeric_claim(
        display_value="100", reported_value=100.0, exact_excerpt="Revenue was 100 million.",
        period="Q1 2026", source_document_id=None, source_artifact_id=None, evidence_id=None,
    )
    assert status == "SOURCE_MISSING"


def test_derived_numeric_validation_requires_formula_and_inputs() -> None:
    passed, _ = validate_derived_numeric_claim(
        display_value="25", derived_value=25.0, formula="100 / 4", input_evidence_ids=["E1", "E2"],
        available_evidence_ids=["E1", "E2"], period="Q1 2026",
    )
    failed, _ = validate_derived_numeric_claim(
        display_value="25", derived_value=25.0, formula=None, input_evidence_ids=["E1"],
        available_evidence_ids=["E1"], period="Q1 2026",
    )
    assert passed == "VERIFIED_DERIVED" and failed == "SOURCE_MISSING"


def test_workflow_reuses_only_analysis_bound_to_requested_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    unbound = {"analysis_run_id": "AN-OLD", "status": "NEEDS_ANALYST_REVIEW", "analysis_snapshot_id": None}
    wrong = {"analysis_run_id": "AN-WRONG", "status": "NEEDS_ANALYST_REVIEW", "analysis_snapshot_id": "SNAP-A"}
    matching = {"analysis_run_id": "AN-MATCH", "status": "NEEDS_ANALYST_REVIEW", "analysis_snapshot_id": "SNAP-B"}
    monkeypatch.setattr(database, "get_analysis_run", lambda *args, **kwargs: unbound)
    monkeypatch.setattr(database, "list_analysis_runs", lambda *args, **kwargs: [wrong, matching])

    selected = _existing_analysis_run(
        {"analysis_run_id": "AN-OLD"}, {"version_id": "PV-1"},
        {"processing_run_id": "PROC-1"}, analysis_snapshot_id="SNAP-B", db_path=Path("unused.db"),
    )
    assert selected == matching

    monkeypatch.setattr(database, "list_analysis_runs", lambda *args, **kwargs: [wrong])
    assert _existing_analysis_run(
        {"analysis_run_id": "AN-OLD"}, {"version_id": "PV-1"},
        {"processing_run_id": "PROC-1"}, analysis_snapshot_id="SNAP-B", db_path=Path("unused.db"),
    ) is None
    assert _existing_analysis_run(
        {"analysis_run_id": "AN-OLD"}, {"version_id": "PV-1"},
        {"processing_run_id": "PROC-1"}, db_path=Path("unused.db"),
    ) == unbound


def test_preliminary_report_header_includes_earnings_cycle() -> None:
    sections = memo_to_sections({
        "company_name": "QXO, Inc.", "ticker": "QXO", "preliminary_package_view": True,
        "recommendation": "Analyst Review Required", "confidence": "Low",
        "research_cutoff": "July 16, 2026", "earnings_cycle": "Q1 FY26",
        "investment_view": "Approved evidence is incomplete.", "supporting_facts": [],
        "risks": [], "missing_information": ["Valuation evidence is missing."],
        "conclusion": "Analyst review is required.",
    })
    assert "Earnings cycle: Q1 FY26" in sections[0]["paragraphs"][0]


def test_preliminary_report_artifact_survives_package_sync(
    phase6b2_package: tuple[Path, dict, Path],
) -> None:
    db_path, package, root = phase6b2_package
    first = root / "QXO_Preliminary_V001.pdf"
    second = root / "QXO_Preliminary_V002.pdf"
    first.write_bytes(b"%PDF-1.7 first")
    second.write_bytes(b"%PDF-1.7 second")
    register_preliminary_report_artifact(
        package["package_id"], {"report_id": "RPT-1", "pdf_path": str(first)}, db_path=db_path,
    )
    register_preliminary_report_artifact(
        package["package_id"], {"report_id": "RPT-2", "pdf_path": str(second)}, db_path=db_path,
    )
    sync_package_artifacts(package["package_id"], db_path=db_path)
    sync_package_artifacts(package["package_id"], db_path=db_path)
    reports = [
        row for row in package_contents(package["package_id"], db_path=db_path)
        if row["artifact_type"] == "PRELIMINARY_RECOMMENDATION"
    ]
    assert [row["display_filename"] for row in reports] == [second.name]


def _memo_candidate() -> MemoEvidenceCandidate:
    return MemoEvidenceCandidate(
        candidate_id="MEC-1", evidence_id="EVID-1", version_document_id="VDOC-1",
        claim_family="revenue_growth", claim_text="Revenue was 100 million.",
        supporting_quote="Revenue was 100 million.", metric_name="Revenue", numeric_value=100.0,
        unit="million", currency="USD", reporting_period="Q1 2026",
        filing_or_publication_date="2026-05-12", source_type="SEC", form_type="10-Q",
        section_heading="Financial Statements", page_number=10, source_priority=100,
        recency_score=1, materiality_score=1, completeness_score=1, decision_relevance_score=1,
        eligible_for_memo=True,
    )


def test_deterministic_unsupported_sentence_removal_preserves_coherent_memo() -> None:
    candidate = _memo_candidate()
    draft = InvestmentMemoDraft(
        investment_view="Revenue was 100 million. Unsupported valuation was 999 million.",
        supporting_facts=[
            MemoDraftItem(candidate_id="MEC-1", concise_claim="Revenue was 100 million."),
            MemoDraftItem(candidate_id="MEC-1", concise_claim="Revenue was 999 million."),
        ],
        risks=[], missing_information=["Valuation evidence is missing."],
        conclusion="Revenue was 100 million. Analyst review is required.",
    )
    repaired = _remove_unsupported_numeric_sentences(draft, [candidate], [])
    assert "999" not in repaired.investment_view
    assert len(repaired.supporting_facts) == 1
    assert "100" in repaired.conclusion


def test_unit_failure_is_localized_and_exact_fact_is_removed() -> None:
    candidate = replace(
        _memo_candidate(), supporting_quote="Revenue was USD 100 million in Q1 2026."
    )
    draft = InvestmentMemoDraft(
        investment_view="Revenue evidence is available.",
        supporting_facts=[
            MemoDraftItem(candidate_id="MEC-1", concise_claim="Revenue was 100 in Q1 2026."),
            MemoDraftItem(candidate_id="MEC-1", concise_claim="Revenue was USD 100 million in Q1 2026."),
        ],
        risks=[], missing_information=["Valuation evidence is missing."],
        conclusion="Analyst review is required.",
    )
    failure = _draft_failure(draft, [candidate], [])
    assert failure["reason"] == "Missing or unsupported unit."
    stripped = _remove_failed_content(draft, failure, [candidate], [])
    assert [row.concise_claim for row in stripped.supporting_facts] == [
        "Revenue was USD 100 million in Q1 2026."
    ]


def test_one_repair_per_memo_attempt_is_database_enforced(tmp_path: Path) -> None:
    db_path = tmp_path / "repair.db"
    database.initialize_database(db_path)
    values = ("RPA-1", "AN-1", "MEMO-1", 1, "Bad 999.", "Unsupported", "REMOVE", "[]", "REMOVED", database.utc_now_iso())
    with database.get_connection(db_path) as connection:
        connection.execute(
            """INSERT INTO report_repair_audits(
               repair_audit_id, analysis_run_id, memo_attempt_id, repair_number,
               failed_sentence, failure_reason, action, supporting_evidence_ids_json, qa_result, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", values,
        )
    with pytest.raises(sqlite3.IntegrityError):
        with database.get_connection(db_path) as connection:
            connection.execute(
                """INSERT INTO report_repair_audits(
                   repair_audit_id, analysis_run_id, memo_attempt_id, repair_number,
                   failed_sentence, failure_reason, action, supporting_evidence_ids_json, qa_result, created_at
                   ) VALUES ('RPA-2', ?, ?, ?, ?, ?, ?, ?, ?, ?)""", values[1:],
            )


def test_sec_selection_prefers_original_and_rejects_unrelated_8k() -> None:
    filings = [
        {"accession_number": "K-A", "form_type": "10-K/A", "filing_date": "2026-03-01", "report_period": "2025-12-31"},
        {"accession_number": "K", "form_type": "10-K", "filing_date": "2026-02-27", "report_period": "2025-12-31"},
        {"accession_number": "Q", "form_type": "10-Q", "filing_date": "2026-05-12", "report_period": "2026-03-31"},
        {"accession_number": "8-UNRELATED", "form_type": "8-K", "filing_date": "2026-05-13", "filing_items": "5.02"},
        {"accession_number": "8-EARNINGS", "form_type": "8-K", "filing_date": "2026-05-12", "filing_items": "2.02"},
    ]
    selected = select_curated_sec_filings(filings, research_cutoff="2026-07-16")
    assert [row["accession_number"] for row in selected["most_recent_10_q_and_10_k"]] == ["Q", "K"]
    assert selected["latest_earnings_release"][0]["accession_number"] == "8-EARNINGS"


def test_authoritative_earnings_exhibit_99_1_is_identified() -> None:
    class Response:
        content = b'<table><tr><td>EX-99.1</td><td>Earnings Release</td><td><a href="qxo-ex991.htm">qxo-ex991.htm</a></td></tr></table>'

        @staticmethod
        def raise_for_status() -> None:
            return None

    class Session:
        @staticmethod
        def get(*args: object, **kwargs: object) -> Response:
            return Response()

    matches = discover_earnings_exhibit_99_1(
        {
            "accession_number": "0001-26-000001", "form_type": "8-K", "filing_items": "2.02",
            "filing_date": "2026-05-12", "report_period": "2026-03-31",
            "filing_index_url": "https://www.sec.gov/Archives/edgar/data/1/index.htm",
        },
        session=Session(),
    )
    assert matches[0]["form_type"] == "EX-99.1"
    assert matches[0]["primary_document_url"].endswith("qxo-ex991.htm")


def test_query_ladder_remains_bounded_and_targeted(phase6b2_package: tuple[Path, dict, Path]) -> None:
    db_path, package, _ = phase6b2_package
    with database.get_connection(db_path) as connection:
        connection.execute(
            "UPDATE packages SET official_ir_url='https://investors.qxo.com', official_website_url='https://www.qxo.com' WHERE package_id=?",
            (package["package_id"],),
        )
    package = database.get_package_by_package_id(package["package_id"], db_path=db_path) or package
    plan = next(row for row in RecipePlannerAgent().plan(package["package_id"], db_path=db_path) if row.normalized_slot_type == "latest_earnings_call_transcript")
    queries = SlotQueryPlannerAgent().plan_queries(plan, package, None)
    assert len(queries) == 3
    assert all("transcript" in query.query or "prepared remarks" in query.query for query in queries)
    assert sum(query.count for query in queries) <= 3 * 20
