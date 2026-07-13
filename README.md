# Cutler Equity Research Workbench

Cutler Research AI is an internal Streamlit research product for searching a ticker, building a locked document-grounded research package, processing that package into cited evidence, and turning verified evidence into auditable investment-analysis drafts for analyst and portfolio-manager review.

## Current Status

Implemented through Phase 7:

- Phase 1: package setup, SQLite persistence, validation, dashboard, shared UI, and navigation.
- Phase 2: SEC company resolution, SEC filing preview/download, investor-relations PDF discovery, public document metadata, hashes, duplicate prevention, and collection history.
- Phase 3: licensed-file uploads, classification suggestions, analyst category correction, upload history, audit events, ZIP inspection, document inventory editing, controlled deletion, and checklist review.
- Phase 4: readiness validation, manifest generation, inventory exports, checklist snapshots, integrity reports, immutable package snapshots, ZIP generation, explicit locking, and version comparison.
- Phase 5: closed-corpus document processing, native extraction, optional local OCR, spreadsheet-safe parsing, citation-preserving chunks, keyword retrieval, deterministic evidence extraction, citation verification, duplicate grouping, conflict detection, analyst evidence review, and exports.
- Phase 6: deterministic financial metrics, evidence-backed scorecards, bull/base/bear scenarios, OpenAI-validated evidence interpretation and narrative generation, Buy/Hold/Sell/Insufficient Evidence/Analyst Review Required recommendations, analyst review, PM approval, DOCX/PDF reports, citation audits, report versioning, and report hashes.
- Phase 7: polished three-screen workflow, SEC-backed ticker search, consolidated Research Workspace, simplified Investment Result page, resumable workflow orchestration, recent research history, Advanced Workbench navigation, and a combined research-package plus AI-report ZIP export.

Not implemented: authentication, cloud deployment, continuous monitoring, or trading integration. The system does not execute trades.

## Phase 7 Primary Workflow

The default application experience is now:

1. `Search` - `app/Home.py` is a minimal ticker-search landing page. It strips spaces, uppercases the ticker, validates symbol format, and verifies an exact match through the supported SEC company database. It does not use general web search or unsupported lookup services. After the analyst confirms the SEC company record, the app reuses the newest editable package for that ticker or creates a new Common Equity package.
2. `Research` - `app/pages/0_Research_Workspace.py` consolidates package settings, automated public collection, optional licensed uploads, package coverage, checklist acknowledgement, and the primary `Build Package and Generate Analysis` action. Filing-year selection now lives in the Automated Research card and updates the working package's `filing_history_years`.
3. `Result` - `app/pages/6_Investment_Result.py` shows the preliminary, analyst-reviewed, or PM-approved signal; separates recommendation confidence, evidence coverage, and package coverage; renders report sections with citations; and exposes governance details through an Advanced Review drawer.

Secondary navigation includes `Dashboard / History` for previous packages and `Advanced Workbench` for the detailed Phase 1-6 pages: package setup, public collection, licensed uploads, package review, evidence exploration, analyst review, PM approval, generated reports, and audit history.

## Research Workspace Details

Automated Research supports:

- Filing history options: 1, 2, 3, and 5 years.
- Research cutoff date, blocked from future dates by default.
- SEC form selection: 10-K, 10-Q, 8-K, DEF 14A, 20-F, and 6-K.
- Public company material preferences and an optional investor-relations URL.
- A planned collection preview and a real collection timeline derived from collection runs, upload runs, documents, checklist rows, and readiness validation.

Additional Research supports the existing secure upload workflow for authorized Bloomberg, Morningstar, FactSet, sell-side, credit, transcript, model, activist, industry, company-material, and internal files. Existing file validation, signature checks, ZIP inspection, duplicate detection, classification suggestions, category correction, authorization acknowledgement, and audit recording remain in force.

The primary proceed action calls the existing services in sequence:

1. Validate package readiness.
2. Build a package version, manifest, inventory, checklist snapshot, integrity report, and Phase 4 package ZIP.
3. Lock the verified package version.
4. Run document processing and evidence extraction.
5. Verify citations through the existing evidence pipeline.
6. Run deterministic investment analysis.
7. Generate a draft DOCX/PDF investment report.

Workflow state is persisted in `research_workflow_runs`, including version ID, processing run ID, analysis run ID, report ID, stage statuses, warnings, errors, and an idempotency key so Streamlit reruns do not duplicate backend runs.

## Final Combined ZIP

The Result page can generate `Download Research Package + AI Report`, a new versioned combined export recorded in `combined_exports`. It does not alter the immutable Phase 4 package snapshot or overwrite the Phase 4 ZIP.

The combined export includes only:

- Files from the selected locked package version.
- Package manifest, document inventory, checklist snapshot, and integrity report.
- Included public documents and licensed uploads.
- The selected analysis run's DOCX/PDF report.
- `12_Final_Analysis/evidence_ledger.xlsx`.
- `12_Final_Analysis/conflicts.csv`.

The export verifies locked package file hashes and report hashes where available, uses relative archive paths only, excludes databases, `.env`, logs, secrets, temporary files, and unrelated package files, writes atomically, hashes the final ZIP, and versions each export.

## Closed-Corpus Analysis Rule

Phase 6 operates only on:

- A selected package version with status `LOCKED`.
- Integrity status `VERIFIED` or `VERIFIED_WITH_WARNINGS`.
- A completed Phase 5 processing run for that exact version.
- Evidence records from that processing run.

The analysis pipeline does not use web search, live market data, external research, Bloomberg calls, external documents, OpenAI embeddings, or facts from model memory. Source files are hash-verified before analysis eligibility succeeds. Unsupported citations are not silently used.

## Evidence And Citation Foundation

Phase 5 evidence records include claim text, source text, evidence type, source document, page/sheet/row/cell/line/section locator, source-text hash, verification status, analyst status, and optional value/unit/currency/period fields.

Phase 6 uses verified or partially verified evidence that has not been rejected by an analyst. Evidence coverage is separate from Phase 3 package checklist coverage.

## Calculation Engine

Arithmetic is deterministic Python, not LLM-driven. The engine uses `Decimal` where precision matters and stores formula descriptions with source evidence IDs.

Supported calculations include:

- Revenue and revenue growth
- Margin and free-cash-flow conversion
- Cash flow
- Cash, gross debt, net debt
- Debt/EBITDA
- EPS and price-target evidence
- Reference price when present in the locked package
- Guidance midpoint helper when package-supported low/high values exist

The engine abstains with warnings when inputs are ambiguous, incompatible, cross-period, cross-currency, missing, OCR-derived with low confidence, cached-spreadsheet-dependent, or otherwise unsafe to combine.

## Valuation Boundary

Reference share price is used only when it appears in the locked package evidence. If no reliable package-contained reference price exists, the system does not fetch a live price or invent upside/downside. The recommendation can become `INSUFFICIENT_EVIDENCE` or `ANALYST_REVIEW_REQUIRED`.

## Scorecard Methodology

Scorecard profiles are versioned and stored in reviewable code configuration. Weights total 100%.

Default Common Equity pillars:

- Business Quality
- Revenue and Earnings Direction
- Profitability and Cash Flow
- Balance Sheet and Liquidity
- Valuation
- Catalysts
- Downside Risk
- Evidence Quality

Convertible and credit profiles include security-specific pillars such as bond floor, conversion premium, leverage, interest coverage, covenant risk, maturity profile, recovery/downside, and rating direction.

Missing evidence receives an explicit missing-evidence score and rationale; it is not treated as neutral. Analyst overrides preserve the system score and require a rationale.

## Recommendation Outcomes

Recommendations are generated by transparent deterministic rules:

- `BUY`
- `HOLD`
- `SELL`
- `INSUFFICIENT_EVIDENCE`
- `ANALYST_REVIEW_REQUIRED`

Rules consider effective score, evidence coverage, valuation availability, package-contained upside/downside, unresolved conflicts, unsupported citations, and confidence. The system can abstain instead of forcing Hold.

Every decision stores:

- Preliminary rating
- Effective rating
- Main rationale
- Why not Buy
- Why not Hold
- Why not Sell
- Confidence
- Evidence coverage
- Abstention reason, when applicable

## Evidence Coverage And Confidence

The app tracks separate concepts:

- Research Package Coverage: Phase 3 checklist completeness.
- Evidence Coverage: material analysis areas supported by verified evidence.
- Recommendation Confidence: verification quality, source quality, conflicts, OCR/cached input reliance, missing valuation, and review state.

Confidence levels are `HIGH`, `MEDIUM`, `LOW`, and `INSUFFICIENT`.

## Scenarios

Bull, base, and bear scenarios use package-supported assumptions only. When price targets and reference prices exist in the package, implied values and upside/downside are shown. Otherwise valuation abstains with warnings.

Scenario assumptions are labeled as package-reported, system-derived, analyst-entered, or system-abstained. Probabilities are absent until an analyst enters them, and entered probabilities must total 100%.

## Analyst Review

Analysts can:

- Accept or change the preliminary recommendation.
- Override scorecard items with rationale.
- Enter scenario probabilities.
- Add review notes.
- Mark the analysis ready for PM review.

Analyst edits do not modify original evidence or locked package files.

## PM Approval

PM approval is separate from package locking and analyst review. PM actions:

- Approve
- Reject
- Return for revision
- Add PM notes

Final reports require PM approval. Approval does not execute trades.

## DOCX/PDF Reports

Phase 6 generates both DOCX and PDF reports. DOCX uses `python-docx`; PDF uses ReportLab and does not require Microsoft Word.

Reports include:

- Package version, processing run, analysis run, research cutoff
- Closed-corpus limitation and no-trade disclaimer
- Recommendation status and PM approval status
- Investment conclusion and why-not explanations
- Financial metrics and formulas
- Scorecard
- Bull/base/bear scenarios
- Thesis, catalysts, and risks
- Evidence coverage and citation audit
- Source inventory
- Analyst and PM review sections

Reports are written under:

```text
data/reports/<version_id>/<analysis_run_id>/
```

Example names:

```text
QXO_Investment_Report_V001_DRAFT.docx
QXO_Investment_Report_V002_PM_APPROVED.pdf
```

Reports are never overwritten. DOCX/PDF SHA-256 hashes are stored in SQLite.

## Citation Audit

Before report generation, Phase 6 checks material thesis items for supported or partially supported citations. Draft reports may carry citation-audit warnings. Final report generation fails if unsupported material claims remain.

## Configuration

Key Phase 6 and Phase 7 settings:

- `ANALYSIS_PIPELINE_VERSION`
- `ANALYSIS_CONFIGURATION_VERSION`
- `SCORECARD_VERSION`
- `VALUATION_CONFIGURATION_VERSION`
- `REPORT_TEMPLATE_VERSION`
- `MIN_EVIDENCE_COVERAGE`
- `BUY_SCORE_THRESHOLD`
- `HOLD_SCORE_THRESHOLD`
- `SELL_SCORE_THRESHOLD`
- `MAX_UNRESOLVED_CONFLICTS`
- `MIN_BUY_UPSIDE`
- `MAX_SELL_DOWNSIDE`
- `OPENAI_REQUIRED`
- `OPENAI_MODEL`
- `OPENAI_REASONING_EFFORT`
- `EXTERNAL_LLM_EXTRACTION_ENABLED`
- `EXTERNAL_NARRATIVE_MODEL_ENABLED`
- `NARRATIVE_MODEL_NAME`
- `SESSION_ACTIVE_PACKAGE_ID`
- `SESSION_ACTIVE_VERSION_ID`
- `SESSION_ACTIVE_PROCESSING_RUN_ID`
- `SESSION_ACTIVE_ANALYSIS_RUN_ID`
- `SESSION_ACTIVE_REPORT_ID`

Arithmetic, ratios, formulas, hashes, database writes, package integrity, citation checks, and score thresholds remain deterministic. With `OPENAI_REQUIRED=true`, analysis and narrative generation stop safely until the configured model passes an explicit OpenAI preflight check. OpenAI requests use the Responses API at `/v1/responses`, with no web or external tools, and only selected locked-package evidence.

Package versions use a globally unique internal `version_id` such as `PV-...` and a package-scoped human-readable `display_version` such as `QXO-20260713-V001`.

## Setup On Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

For SEC public collection:

```powershell
$env:SEC_USER_AGENT = "Cutler Capital Research Workbench research-team@your-domain.com"
```

Run tests:

```powershell
pytest
python -m compileall app tests
```

Launch the app from the repository root:

```powershell
python -m streamlit run app\Home.py --server.port 8505
```

Or from any current directory:

```powershell
& "C:\path\to\cutler-equity-research-blueprint\scripts\run_app.ps1"
```

Direct page navigation is supported for the primary workflow and the Advanced Workbench pages. If you open the Research or Result page directly without an active package, the app offers a persisted package or analysis selection instead of crashing.

## Known Limitations

Phase 7 is a UX and workflow consolidation over the Phase 1-6 backend. It does not add Bloomberg, FactSet, Morningstar, browser automation, live market data, portfolio allocation, trade execution, or a new frontend framework. SEC public collection still requires a configured SEC user agent. Investor-relations discovery remains conservative and requires an analyst-supplied public IR URL. Scenario valuation abstains when package-contained valuation inputs are missing. Calculations and recommendation rules depend on the quality and structure of Phase 5 evidence extraction.
