from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from app import config
from app.utils import database


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass
class StageTimer:
    stage_name: str
    workflow_run_id: str | None = None
    package_id: str | None = None
    version_id: str | None = None
    processing_run_id: str | None = None
    analysis_run_id: str | None = None
    db_path: Path | str = config.DATABASE_PATH
    started_at: str = field(default_factory=_now)
    _started: float = field(default_factory=perf_counter)

    def finish(self, *, reused: bool = False, details: dict[str, Any] | None = None, **counts: int) -> dict[str, Any]:
        completed_at = _now()
        record = {
            "performance_id": f"PERF-{secrets.token_hex(8).upper()}",
            "workflow_run_id": self.workflow_run_id,
            "package_id": self.package_id,
            "version_id": self.version_id,
            "processing_run_id": self.processing_run_id,
            "analysis_run_id": self.analysis_run_id,
            "stage_name": self.stage_name,
            "started_at": self.started_at,
            "completed_at": completed_at,
            "duration_seconds": round(perf_counter() - self._started, 6),
            "files_examined": int(counts.get("files_examined", 0)),
            "files_reused": int(counts.get("files_reused", 0)),
            "files_processed": int(counts.get("files_processed", 0)),
            "chunks_examined": int(counts.get("chunks_examined", 0)),
            "openai_batches": int(counts.get("openai_batches", 0)),
            "openai_input_size": int(counts.get("openai_input_size", 0)),
            "evidence_created": int(counts.get("evidence_created", 0)),
            "metrics_created": int(counts.get("metrics_created", 0)),
            "conflicts_examined": int(counts.get("conflicts_examined", 0)),
            "reports_generated": int(counts.get("reports_generated", 0)),
            "reused": int(bool(reused)),
            "details_json": json.dumps(details or {}, sort_keys=True),
        }
        return database.create_workflow_stage_performance(record, db_path=self.db_path)


def performance_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"slowest_stage": None, "total_duration_seconds": 0.0, "stages": []}
    slowest = max(rows, key=lambda row: float(row.get("duration_seconds") or 0))
    return {
        "slowest_stage": slowest.get("stage_name"),
        "slowest_duration_seconds": float(slowest.get("duration_seconds") or 0),
        "total_duration_seconds": sum(float(row.get("duration_seconds") or 0) for row in rows),
        "stages": rows,
    }
