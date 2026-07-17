from __future__ import annotations

import hashlib
import json
import secrets
from pathlib import Path
from time import perf_counter
from typing import Any

from app import config
from app.services.package_artifact_service import sync_package_artifacts
from app.services.package_recipe_service import get_package_recipe_instance, list_assignments
from app.utils import database


class CorpusIsolationError(RuntimeError):
    """Raised before model use when package lineage is not isolated."""


def _loads(value: Any) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
        return [str(item) for item in parsed] if isinstance(parsed, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def _hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def get_analysis_snapshot(snapshot_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> dict[str, Any] | None:
    database.initialize_database(db_path)
    with database.get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM analysis_corpus_snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
    return dict(row) if row else None


def latest_analysis_snapshot(package_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> dict[str, Any] | None:
    database.initialize_database(db_path)
    with database.get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM analysis_corpus_snapshots WHERE package_id=? ORDER BY created_at DESC LIMIT 1",
            (package_id,),
        ).fetchone()
    return dict(row) if row else None


def create_analysis_snapshot(
    package_id: str,
    *,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    """Freeze current approved assignments and logical artifacts before processing."""
    started = perf_counter()
    database.initialize_database(db_path)
    instance = get_package_recipe_instance(package_id, db_path=db_path)
    if not instance:
        raise CorpusIsolationError("A recipe-backed package is required for an analysis snapshot.")
    artifacts = [
        row for row in sync_package_artifacts(package_id, db_path=db_path)
        if row.get("analysis_eligible") and row.get("source_document_id")
    ]
    assignments = [
        row for row in list_assignments(package_id, db_path=db_path)
        if row["assignment_status"] == "APPROVED"
        and row.get("selected_for_package")
        and row.get("collection_status") == config.DOCUMENT_STATUS_DOWNLOADED
        and row.get("sha256_hash")
    ]
    assignment_ids = sorted({row["assignment_id"] for row in assignments})
    document_ids = sorted({row["document_id"] for row in assignments})
    artifact_ids = sorted({row["artifact_id"] for row in artifacts if row["source_document_id"] in document_ids})
    if not document_ids or not artifact_ids:
        raise CorpusIsolationError("No approved analysis-eligible package artifacts are available.")
    payload = {
        "package_id": package_id,
        "recipe_instance_id": instance["package_recipe_instance_id"],
        "assignment_ids": assignment_ids,
        "artifact_ids": artifact_ids,
        "document_ids": document_ids,
        "evidence_ids": [], "metric_ids": [], "conflict_ids": [],
    }
    snapshot_id = f"SNAP-{secrets.token_hex(8).upper()}"
    now = database.utc_now_iso()
    validation = {"status": "DOCUMENT_SCOPE_VALID", "violations": [], "creation_ms": round((perf_counter() - started) * 1000, 1)}
    with database.get_connection(db_path) as connection:
        connection.execute(
            """INSERT INTO analysis_corpus_snapshots(
               snapshot_id, package_id, recipe_instance_id, status,
               assignment_ids_json, artifact_ids_json, document_ids_json,
               evidence_ids_json, metric_ids_json, conflict_ids_json,
               snapshot_hash, validation_result_json, created_at
               ) VALUES (?, ?, ?, 'DOCUMENT_SCOPE_FROZEN', ?, ?, ?, '[]', '[]', '[]', ?, ?, ?)""",
            (snapshot_id, package_id, instance["package_recipe_instance_id"], json.dumps(assignment_ids),
             json.dumps(artifact_ids), json.dumps(document_ids), _hash(payload),
             json.dumps(validation, sort_keys=True), now),
        )
    return get_analysis_snapshot(snapshot_id, db_path=db_path) or {}


def validate_snapshot_document_scope(
    snapshot_id: str,
    *,
    version_id: str | None = None,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    snapshot = get_analysis_snapshot(snapshot_id, db_path=db_path)
    if not snapshot:
        raise CorpusIsolationError("Analysis snapshot does not exist.")
    package_id = snapshot["package_id"]
    expected_assignments = set(_loads(snapshot["assignment_ids_json"]))
    expected_artifacts = set(_loads(snapshot["artifact_ids_json"]))
    expected_documents = set(_loads(snapshot["document_ids_json"]))
    violations: list[str] = []
    with database.get_connection(db_path) as connection:
        active_assignments = {
            row["assignment_id"] for row in connection.execute(
                """SELECT assignment_id FROM slot_document_assignments
                   WHERE package_id=? AND assignment_status='APPROVED' AND selected_for_package=1""",
                (package_id,),
            ).fetchall()
        }
        active_artifacts = {
            row["artifact_id"] for row in connection.execute(
                """SELECT artifact_id FROM package_artifacts
                   WHERE package_id=? AND artifact_status='CURRENT' AND analysis_eligible=1""",
                (package_id,),
            ).fetchall()
        }
        assigned_documents = {
            row["document_id"] for row in connection.execute(
                """SELECT DISTINCT document_id FROM slot_document_assignments
                   WHERE package_id=? AND assignment_status='APPROVED' AND selected_for_package=1""",
                (package_id,),
            ).fetchall()
        }
        rejected_documents = {
            row["downloaded_document_id"] for row in connection.execute(
                """SELECT downloaded_document_id FROM discovered_candidates
                   WHERE package_id=? AND downloaded_document_id IS NOT NULL
                     AND candidate_status IN ('REJECTED','NON_INVESTOR_MATERIAL','MIME_MISMATCH','COMPANY_MISMATCH','FAILED')""",
                (package_id,),
            ).fetchall()
        }
        if version_id:
            version_documents = {
                row["original_document_id"] for row in connection.execute(
                    "SELECT original_document_id FROM package_version_documents WHERE version_id=?",
                    (version_id,),
                ).fetchall()
            }
        else:
            version_documents = expected_documents
    if expected_assignments - active_assignments:
        violations.append("Snapshot contains a superseded or unapproved assignment.")
    if expected_artifacts - active_artifacts:
        violations.append("Snapshot contains a superseded or analysis-ineligible artifact.")
    if expected_documents - assigned_documents:
        violations.append("Snapshot contains a document without a current approved assignment.")
    if expected_documents & rejected_documents:
        violations.append("A rejected discovery candidate contributes a snapshot document.")
    if version_id and version_documents != expected_documents:
        violations.append("Package-version documents do not exactly match the frozen snapshot documents.")
    result = {"status": "PASSED" if not violations else "FAILED", "violations": violations}
    if violations:
        raise CorpusIsolationError("Analysis corpus validation failed: " + " ".join(violations))
    return result


def finalize_analysis_snapshot(
    snapshot_id: str,
    *,
    analysis_run_id: str,
    processing_run_id: str,
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    """Freeze current evidence, metrics, and conflicts once before narrative generation."""
    snapshot = get_analysis_snapshot(snapshot_id, db_path=db_path)
    if not snapshot:
        raise CorpusIsolationError("Analysis snapshot does not exist.")
    if snapshot["status"] == "READY":
        if snapshot.get("analysis_run_id") != analysis_run_id or snapshot.get("processing_run_id") != processing_run_id:
            raise CorpusIsolationError("A finalized analysis snapshot cannot be reused by another run.")
        validate_analysis_snapshot(snapshot_id, db_path=db_path)
        return snapshot
    run = database.get_analysis_run(analysis_run_id, db_path=db_path)
    if not run or run["package_id"] != snapshot["package_id"] or run["processing_run_id"] != processing_run_id:
        raise CorpusIsolationError("Analysis run does not belong to the frozen package snapshot.")
    validate_snapshot_document_scope(snapshot_id, version_id=run["version_id"], db_path=db_path)
    document_ids = set(_loads(snapshot["document_ids_json"]))
    with database.get_connection(db_path) as connection:
        evidence_rows = connection.execute(
            """SELECT e.evidence_id FROM evidence_records e
               JOIN package_version_documents pvd
                 ON pvd.version_id=e.version_id AND pvd.document_id=e.version_document_id
               WHERE e.processing_run_id=? AND e.version_id=?""",
            (processing_run_id, run["version_id"]),
        ).fetchall()
        scoped_evidence_rows = connection.execute(
            """SELECT e.evidence_id FROM evidence_records e
               JOIN package_version_documents pvd
                 ON pvd.version_id=e.version_id AND pvd.document_id=e.version_document_id
               WHERE e.processing_run_id=? AND e.version_id=? AND pvd.original_document_id IN ({})""".format(
                    ",".join("?" for _ in document_ids)
                ),
            (processing_run_id, run["version_id"], *sorted(document_ids)),
        ).fetchall()
        metric_rows = connection.execute(
            "SELECT metric_id, source_evidence_ids_json FROM analysis_metrics WHERE analysis_run_id=?",
            (analysis_run_id,),
        ).fetchall()
        conflict_rows = connection.execute(
            "SELECT conflict_id, evidence_id_a, evidence_id_b FROM claim_conflicts WHERE processing_run_id=?",
            (processing_run_id,),
        ).fetchall()
    all_evidence = {row["evidence_id"] for row in evidence_rows}
    evidence_ids = {row["evidence_id"] for row in scoped_evidence_rows}
    if all_evidence != evidence_ids:
        raise CorpusIsolationError("Evidence from an unassigned document entered the current processing run.")
    metric_ids: list[str] = []
    for row in metric_rows:
        lineage = set(_loads(row["source_evidence_ids_json"]))
        if not lineage or not lineage <= evidence_ids:
            raise CorpusIsolationError(f"Metric {row['metric_id']} lacks current snapshot evidence lineage.")
        metric_ids.append(row["metric_id"])
    conflict_ids: list[str] = []
    for row in conflict_rows:
        if row["evidence_id_a"] not in evidence_ids or row["evidence_id_b"] not in evidence_ids:
            raise CorpusIsolationError(f"Conflict {row['conflict_id']} crosses the current snapshot boundary.")
        conflict_ids.append(row["conflict_id"])
    payload = {
        "package_id": snapshot["package_id"], "recipe_instance_id": snapshot["recipe_instance_id"],
        "assignment_ids": sorted(_loads(snapshot["assignment_ids_json"])),
        "artifact_ids": sorted(_loads(snapshot["artifact_ids_json"])),
        "document_ids": sorted(document_ids), "evidence_ids": sorted(evidence_ids),
        "metric_ids": sorted(metric_ids), "conflict_ids": sorted(conflict_ids),
        "processing_run_id": processing_run_id, "analysis_run_id": analysis_run_id,
    }
    now = database.utc_now_iso()
    validation = {"status": "PASSED", "violations": []}
    with database.get_connection(db_path) as connection:
        connection.execute(
            """UPDATE analysis_corpus_snapshots SET status='READY', evidence_ids_json=?, metric_ids_json=?,
               conflict_ids_json=?, processing_run_id=?, analysis_run_id=?, snapshot_hash=?,
               validation_result_json=?, finalized_at=? WHERE snapshot_id=? AND status='DOCUMENT_SCOPE_FROZEN'""",
            (json.dumps(payload["evidence_ids"]), json.dumps(payload["metric_ids"]),
             json.dumps(payload["conflict_ids"]), processing_run_id, analysis_run_id, _hash(payload),
             json.dumps(validation), now, snapshot_id),
        )
    return get_analysis_snapshot(snapshot_id, db_path=db_path) or {}


def validate_analysis_snapshot(snapshot_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> dict[str, Any]:
    snapshot = get_analysis_snapshot(snapshot_id, db_path=db_path)
    if not snapshot or snapshot["status"] != "READY":
        raise CorpusIsolationError("The analysis snapshot is not finalized.")
    run = database.get_analysis_run(snapshot["analysis_run_id"], db_path=db_path)
    if not run:
        raise CorpusIsolationError("The snapshot analysis run no longer exists.")
    validate_snapshot_document_scope(snapshot_id, version_id=run["version_id"], db_path=db_path)
    evidence_ids = set(_loads(snapshot["evidence_ids_json"]))
    metric_ids = set(_loads(snapshot["metric_ids_json"]))
    conflict_ids = set(_loads(snapshot["conflict_ids_json"]))
    current_evidence = {
        row["evidence_id"] for row in database.list_evidence_records(
            run["processing_run_id"], version_id=run["version_id"], db_path=db_path
        )
    }
    current_metrics = {row["metric_id"] for row in database.list_analysis_metrics(run["analysis_run_id"], db_path=db_path)}
    current_conflicts = {row["conflict_id"] for row in database.list_claim_conflicts(run["processing_run_id"], db_path=db_path)}
    violations = []
    if evidence_ids != current_evidence:
        violations.append("Evidence changed after the snapshot was finalized.")
    if metric_ids != current_metrics:
        violations.append("Metrics changed after the snapshot was finalized.")
    if conflict_ids != current_conflicts:
        violations.append("Conflicts changed after the snapshot was finalized.")
    if violations:
        raise CorpusIsolationError("Analysis corpus validation failed: " + " ".join(violations))
    return {"status": "PASSED", "violations": []}
