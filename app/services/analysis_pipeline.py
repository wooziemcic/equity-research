from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app import config
from app.services.analysis.financial_metrics import (
    calculate_metrics,
    diagnose_metric_skips,
    metric_input_summary,
    normalized_value,
    usable_evidence,
)
from app.services.analysis.scenario_analysis import create_scenarios
from app.services.processing_pipeline import validate_processing_eligibility
from app.services.recommendation_engine import (
    evidence_coverage,
    generate_recommendation,
    generate_scorecard,
    generate_thesis_items,
    package_coverage,
    recommendation_confidence,
)
from app.services.openai_service import preflight_openai, run_closed_corpus_ai_review
from app.utils import database


@dataclass(frozen=True)
class AnalysisEligibility:
    is_eligible: bool
    version: dict[str, Any] | None
    processing_run: dict[str, Any] | None
    errors: list[str]
    warnings: list[str]
    limitations: list[str]


class AnalysisPipelineError(RuntimeError):
    """Raised for technical analysis failures after an analysis run is created."""

    def __init__(self, safe_message: str, *, analysis_run_id: str | None, diagnostics: dict[str, Any]) -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message
        self.analysis_run_id = analysis_run_id
        self.diagnostics = diagnostics


def _analysis_run_id() -> str:
    return f"RUN-ANALYSIS-{secrets.token_hex(8).upper()}"


def _event_id() -> str:
    return f"PVE-{secrets.token_hex(8).upper()}"


def _record_event(version: dict[str, Any], event_type: str, details: dict[str, Any], *, db_path: Path | str) -> None:
    database.create_package_version_event(
        event_id=_event_id(),
        parent_package_id=version["parent_package_id"],
        version_id=version["version_id"],
        event_type=event_type,
        event_details_json=json.dumps(details, sort_keys=True),
        db_path=db_path,
    )


def validate_analysis_eligibility(
    version_id: str,
    processing_run_id: str | None = None,
    *,
    db_path: Path | str = config.DATABASE_PATH,
    record_event: bool = True,
) -> AnalysisEligibility:
    errors: list[str] = []
    warnings: list[str] = []
    limitations: list[str] = []
    processing_check = validate_processing_eligibility(version_id, db_path=db_path, record_event=False)
    version = processing_check.version
    errors.extend(processing_check.errors)
    warnings.extend(processing_check.warnings)
    run = None
    if version:
        completed_statuses = {config.PROCESSING_STATUS_COMPLETED, config.PROCESSING_STATUS_COMPLETED_WITH_WARNINGS}
        if processing_run_id:
            run = database.get_processing_run(processing_run_id, db_path=db_path)
            if not run:
                errors.append("Selected processing run does not exist.")
            elif run.get("version_id") != version_id:
                errors.append("Selected processing run belongs to another version.")
            elif run.get("status") not in completed_statuses:
                errors.append("Selected processing run is not completed.")
        else:
            runs = [
                item
                for item in database.list_processing_runs(version_id, db_path=db_path)
                if item.get("status") in completed_statuses
            ]
            run = runs[0] if runs else None
            if not run:
                errors.append("A completed Phase 5 processing run is required.")
        if run:
            evidence = database.list_evidence_records(run["processing_run_id"], version_id=version_id, db_path=db_path)
            if not evidence:
                limitations.append("Evidence generation completed, but no evidence records were extracted from the locked corpus.")
            usable = usable_evidence(evidence)
            verified = [
                item
                for item in evidence
                if item.get("verification_status") in {config.VERIFICATION_SUPPORTS, config.VERIFICATION_PARTIALLY_SUPPORTS}
            ]
            accepted = [item for item in evidence if item.get("analyst_status") == config.ANALYST_STATUS_ACCEPTED]
            if verified and not accepted:
                warnings.append("Evidence was extracted, but no records have been accepted for analysis. Automatically verified evidence will be used only for preliminary draft analysis.")
            if not usable:
                limitations.append("No usable verified evidence exists for deterministic analysis.")
            unsupported = [
                item
                for item in evidence
                if item.get("verification_status") in {config.VERIFICATION_DOES_NOT_SUPPORT, config.VERIFICATION_SOURCE_MISSING, config.VERIFICATION_HASH_MISMATCH}
            ]
            if unsupported:
                warnings.append(f"{len(unsupported)} unsupported evidence records will not be used silently.")
            if not any((item.get("metric_name") or "").lower() in {"reference_price", "share_price", "stock_price"} for item in evidence):
                limitations.append("No package-contained reference price was found.")
            conflicts = database.list_claim_conflicts(run["processing_run_id"], db_path=db_path)
            if conflicts:
                limitations.append(f"{len(conflicts)} unresolved claim conflicts are present.")
    eligible = not errors
    if not eligible and record_event and version:
        _record_event(
            version,
            "ANALYSIS_ELIGIBILITY_FAILED",
            {"errors": errors, "warnings": warnings, "limitations": limitations, "processing_run_id": processing_run_id},
            db_path=db_path,
        )
    return AnalysisEligibility(eligible, version, run, errors, warnings, limitations)


def safe_error_message(exc: Exception) -> str:
    message = str(exc) or exc.__class__.__name__
    message = re.sub(r"sk-[A-Za-z0-9_-]+", "[redacted]", message)
    message = re.sub(r"(?i)(api[_-]?key|password|secret|token)=\S+", r"\1=[redacted]", message)
    return message[:500]


def metric_stage_diagnostics(
    evidence: list[dict[str, Any]],
    *,
    analysis_run_id: str | None,
    processing_run_id: str | None,
    metrics: list[dict[str, Any]] | None = None,
    exception: Exception | None = None,
) -> dict[str, Any]:
    usable = usable_evidence(evidence)
    verified = [
        record
        for record in evidence
        if record.get("verification_status") in {config.VERIFICATION_SUPPORTS, config.VERIFICATION_PARTIALLY_SUPPORTS}
    ]
    accepted = [record for record in evidence if record.get("analyst_status") == config.ANALYST_STATUS_ACCEPTED]
    diagnostic: dict[str, Any] = {
        "exception_type": exception.__class__.__name__ if exception else None,
        "safe_error_message": safe_error_message(exception) if exception else None,
        "analysis_run_id": analysis_run_id,
        "processing_run_id": processing_run_id,
        "evidence_records": len(evidence),
        "verified_records": len(verified),
        "accepted_records": len(accepted),
        "numeric_value_records": len([record for record in evidence if normalized_value(record) is not None]),
        "usable_evidence_records": len(usable),
        "metric_inputs_discovered": metric_input_summary(evidence),
        "metrics_successfully_calculated": sorted(
            {
                metric.get("metric_code")
                for metric in metrics or []
                if metric.get("metric_code") and metric.get("value") is not None
            }
        ),
        "metrics_skipped": diagnose_metric_skips(usable, metrics),
    }
    return diagnostic


def load_analysis_diagnostics(analysis_run: dict[str, Any] | None) -> dict[str, Any]:
    if not analysis_run:
        return {}
    raw = analysis_run.get("error_message")
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {"safe_error_message": str(raw)}
    return payload if isinstance(payload, dict) else {}


def _analysis_payload(
    *,
    diagnostics: dict[str, Any],
    warnings: list[str],
    limitations: list[str],
) -> str | None:
    payload = {
        "metric_diagnostics": diagnostics,
        "warnings": sorted(set(warnings)),
        "limitations": sorted(set(limitations)),
    }
    if not payload["warnings"] and not payload["limitations"] and not diagnostics.get("exception_type"):
        skipped = diagnostics.get("metrics_skipped")
        calculated = diagnostics.get("metrics_successfully_calculated")
        if not skipped or calculated:
            return None
    return json.dumps(payload, sort_keys=True)


def _analysis_limitations(
    base_limitations: list[str],
    diagnostics: dict[str, Any],
    decision: dict[str, Any],
) -> list[str]:
    limitations = list(base_limitations)
    for skipped in diagnostics.get("metrics_skipped", []):
        reason = skipped.get("reason") if isinstance(skipped, dict) else None
        if reason:
            limitations.append(str(reason))
    abstention_reason = decision.get("abstention_reason")
    if abstention_reason:
        limitations.append(str(abstention_reason))
    return sorted(set(limitations))


def create_analysis_run(
    version_id: str,
    processing_run_id: str,
    *,
    time_horizon: str = "12 months",
    created_by: str = "analyst",
    db_path: Path | str = config.DATABASE_PATH,
) -> dict[str, Any]:
    eligibility = validate_analysis_eligibility(version_id, processing_run_id, db_path=db_path)
    if not eligibility.is_eligible or not eligibility.version or not eligibility.processing_run:
        raise ValueError("Analysis blocked: " + "; ".join(eligibility.errors))
    version = eligibility.version
    existing_runs = database.list_analysis_runs(
        version_id,
        processing_run_id=processing_run_id,
        db_path=db_path,
    )
    for existing in existing_runs:
        if existing.get("status") != config.ANALYSIS_STATUS_FAILED:
            return existing
    if config.OPENAI_REQUIRED:
        preflight = preflight_openai()
        if not preflight.connected:
            raise AnalysisPipelineError(
                preflight.message or "OpenAI preflight failed.",
                analysis_run_id=None,
                diagnostics={"provider_code": preflight.code or "OPENAI_REQUEST_FAILED"},
            )
    run_id = _analysis_run_id()
    now = database.utc_now_iso()
    analysis_run = database.create_analysis_run(
        {
            "analysis_run_id": run_id,
            "package_id": version["parent_package_id"],
            "version_id": version_id,
            "processing_run_id": processing_run_id,
            "analysis_configuration_version": config.ANALYSIS_CONFIGURATION_VERSION,
            "scorecard_version": config.SCORECARD_VERSION,
            "valuation_configuration_version": config.VALUATION_CONFIGURATION_VERSION,
            "created_by": created_by,
            "created_at": now,
            "updated_at": now,
            "status": config.ANALYSIS_STATUS_CALCULATING,
            "preliminary_recommendation": None,
            "analyst_adjusted_recommendation": None,
            "pm_approved_recommendation": None,
            "confidence": None,
            "evidence_coverage": None,
            "package_coverage": package_coverage(version),
            "research_cutoff": version.get("research_cutoff_date"),
            "reference_price": None,
            "reference_price_currency": None,
            "reference_price_date": None,
            "reference_price_evidence_id": None,
            "time_horizon": time_horizon,
            "analyst_notes": "",
            "pm_notes": "",
            "error_message": None,
            "ai_review_status": config.AI_REVIEW_STATUS_RUNNING if config.OPENAI_REQUIRED or config.EXTERNAL_LLM_EXTRACTION_ENABLED or config.EXTERNAL_NARRATIVE_MODEL_ENABLED else config.AI_REVIEW_STATUS_NOT_REQUIRED,
            "ai_model": config.OPENAI_MODEL if config.OPENAI_REQUIRED or config.EXTERNAL_LLM_EXTRACTION_ENABLED or config.EXTERNAL_NARRATIVE_MODEL_ENABLED else None,
        },
        db_path=db_path,
    )
    _record_event(version, "ANALYSIS_STARTED", {"analysis_run_id": run_id, "processing_run_id": processing_run_id}, db_path=db_path)
    evidence: list[dict[str, Any]] = []
    try:
        evidence = database.list_evidence_records(processing_run_id, version_id=version_id, db_path=db_path)
        conflicts = database.list_claim_conflicts(processing_run_id, db_path=db_path)
        calculate_metrics(evidence, analysis_run_id=run_id, db_path=db_path)
        metrics = database.list_analysis_metrics(run_id, db_path=db_path)
        diagnostics = metric_stage_diagnostics(
            evidence,
            analysis_run_id=run_id,
            processing_run_id=processing_run_id,
            metrics=metrics,
        )
        for metric in metrics:
            _record_event(version, "METRIC_CALCULATED", {"analysis_run_id": run_id, "metric_code": metric["metric_code"]}, db_path=db_path)
        scorecard = generate_scorecard(
            run_id,
            security_type=version.get("security_type") or "Other",
            evidence=evidence,
            metrics=metrics,
            conflicts=conflicts,
            db_path=db_path,
        )
        _record_event(version, "SCORECARD_GENERATED", {"analysis_run_id": run_id, "items": len(scorecard)}, db_path=db_path)
        scenarios = create_scenarios(run_id, db_path=db_path)
        ai_review = None
        if config.OPENAI_REQUIRED or config.EXTERNAL_LLM_EXTRACTION_ENABLED or config.EXTERNAL_NARRATIVE_MODEL_ENABLED:
            ai_review = run_closed_corpus_ai_review(
                version=version,
                processing_run_id=processing_run_id,
                evidence=evidence,
                metrics=metrics,
                conflicts=conflicts,
                db_path=str(db_path),
            )
            evidence_by_id = {item["evidence_id"]: item for item in evidence}
            for extracted in ai_review.extracted_claims:
                if extracted.abstain or not extracted.evidence_ids:
                    continue
                target_id = extracted.evidence_ids[0]
                if target_id in evidence_by_id:
                    database.update_evidence_record(
                        target_id,
                        {"claim_text": extracted.claim_text, "extraction_method": "OPENAI_STRUCTURED"},
                        db_path=db_path,
                    )
            _record_event(
                version,
                "AI_REVIEW_COMPLETED",
                {
                    "analysis_run_id": run_id,
                    "model": config.OPENAI_MODEL,
                    "extracted_claims": len(ai_review.extracted_claims),
                    "thesis_items": len(ai_review.thesis_items),
                    "conflict_explanations": len(ai_review.conflict_explanations),
                },
                db_path=db_path,
            )
        thesis_items = generate_thesis_items(
            run_id,
            evidence=evidence,
            ai_review=ai_review,
            db_path=db_path,
        )
        narrative = ai_review.recommendation.model_dump() if ai_review else None
        if narrative is not None:
            narrative["conflict_explanations"] = [item.claim_text for item in ai_review.conflict_explanations if not item.abstain]
        decision = generate_recommendation(
            run_id,
            evidence=evidence,
            metrics=metrics,
            scorecard_items=scorecard,
            conflicts=conflicts,
            narrative=narrative,
            db_path=db_path,
        )
        reference = _reference_price(metrics)
        coverage = evidence_coverage(evidence)
        confidence = decision["confidence"]
        limitations = _analysis_limitations(
            eligibility.limitations,
            diagnostics,
            decision,
        )
        diagnostics_payload = _analysis_payload(
            diagnostics=diagnostics,
            warnings=eligibility.warnings,
            limitations=limitations,
        )
        updated = database.update_analysis_run(
            run_id,
            {
                "status": config.ANALYSIS_STATUS_NEEDS_ANALYST_REVIEW,
                "preliminary_recommendation": decision["preliminary_rating"],
                "confidence": confidence,
                "evidence_coverage": coverage,
                "reference_price": reference.get("value"),
                "reference_price_currency": reference.get("currency"),
                "reference_price_date": reference.get("period"),
                "reference_price_evidence_id": reference.get("evidence_id"),
                "error_message": diagnostics_payload,
                "ai_review_status": config.AI_REVIEW_STATUS_COMPLETED if ai_review is not None else config.AI_REVIEW_STATUS_NOT_REQUIRED,
            },
            db_path=db_path,
        )
        _record_event(
            version,
            "RECOMMENDATION_GENERATED",
            {
                "analysis_run_id": run_id,
                "preliminary": decision["preliminary_rating"],
                "confidence": confidence,
                "scenarios": len(scenarios),
                "thesis_items": len(thesis_items),
            },
            db_path=db_path,
        )
        return updated or analysis_run
    except Exception as exc:
        metrics = database.list_analysis_metrics(run_id, db_path=db_path)
        diagnostics = metric_stage_diagnostics(
            evidence,
            analysis_run_id=run_id,
            processing_run_id=processing_run_id,
            metrics=metrics,
            exception=exc,
        )
        diagnostics_payload = _analysis_payload(
            diagnostics=diagnostics,
            warnings=eligibility.warnings,
            limitations=eligibility.limitations,
        )
        safe_message = safe_error_message(exc)
        database.update_analysis_run(
            run_id,
            {
                "status": config.ANALYSIS_STATUS_FAILED,
                "error_message": diagnostics_payload or safe_message,
                "ai_review_status": getattr(exc, "code", config.AI_REVIEW_STATUS_RUNNING),
            },
            db_path=db_path,
        )
        _record_event(
            version,
            "METRIC_CALCULATION_FAILED",
            {"analysis_run_id": run_id, "diagnostics": diagnostics},
            db_path=db_path,
        )
        _record_event(version, "ANALYSIS_FAILED", {"analysis_run_id": run_id, "error": safe_message}, db_path=db_path)
        raise AnalysisPipelineError(safe_message, analysis_run_id=run_id, diagnostics=diagnostics) from exc


def _reference_price(metrics: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = [metric for metric in metrics if metric.get("metric_code") == "REFERENCE_PRICE" and metric.get("value") is not None]
    if not candidates:
        return {}
    metric = candidates[-1]
    evidence_ids = json.loads(metric.get("source_evidence_ids_json") or "[]")
    return {
        "value": metric.get("value"),
        "currency": metric.get("currency"),
        "period": metric.get("period"),
        "evidence_id": evidence_ids[0] if evidence_ids else None,
    }


def analysis_summary(analysis_run_id: str, *, db_path: Path | str = config.DATABASE_PATH) -> dict[str, Any]:
    run = database.get_analysis_run(analysis_run_id, db_path=db_path)
    if not run:
        raise ValueError("Analysis run does not exist.")
    return {
        "run": run,
        "metrics": database.list_analysis_metrics(analysis_run_id, db_path=db_path),
        "scorecard": database.list_scorecard_items(analysis_run_id, db_path=db_path),
        "scenarios": database.list_analysis_scenarios(analysis_run_id, db_path=db_path),
        "thesis": database.list_thesis_items(analysis_run_id, db_path=db_path),
        "decision": database.get_recommendation_decision(analysis_run_id, db_path=db_path),
        "reports": database.list_generated_reports(analysis_run_id, db_path=db_path),
    }
