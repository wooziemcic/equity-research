# Cutler Equity Research Workbench

Cutler Research AI is an internal Streamlit workbench for creating research workspaces, collecting public documents, uploading authorized licensed materials, reviewing coverage, and building versioned research package exports.

## Current Status

Implemented through Phase 4:

- Phase 1: package setup, SQLite persistence, validation, dashboard, shared dark UI, and multi-page navigation.
- Phase 2: SEC company resolution, SEC filing preview/download, investor-relations PDF discovery, public document metadata, hashes, duplicate prevention, and collection history.
- Phase 3: licensed-file uploads, lightweight classification suggestions, analyst category correction, upload history, audit events, ZIP inspection, document inventory editing, controlled deletion, and security-type-aware checklist review.
- Phase 4: readiness validation, package manifest generation, document inventory CSV/XLSX, checklist snapshots, integrity reports, versioned immutable package snapshots, ZIP generation, explicit locking, export downloads, and version comparison.

Not implemented:

- Document parsing for analysis, OCR, embeddings, RAG, LLM calls, evidence extraction, citations, contradiction detection, financial calculations, Buy/Sell/Hold recommendations, investment report generation, PM investment approval, authentication, or cloud deployment.
- Document analysis begins in Phase 5. Buy/Sell/Hold and final investment reports begin in Phase 6.

## Working Package Versus Versioned Package

The working package remains editable. Analysts can add documents, change licensed-file metadata, and update checklist overrides. A built package version is a snapshot copied into `data/packages/<package_id>/<version_id>/`. A locked version is immutable and becomes the future Phase 5 corpus.

Changing a working package after locking requires creating a new version. Locked versions are not edited in place.

## Readiness Validation

Before build, the app checks:

- Package and company setup exist.
- At least one document is available.
- Included files exist physically and stay inside managed data directories.
- No failed or deleted document is included.
- Documents have categories and SHA-256 hashes.
- Checklist review acknowledgement is saved.
- Missing core, stale, and needs-review items are acknowledged.
- Duplicate records are warned about rather than silently included twice.

Readiness states are `NOT_READY`, `READY_WITH_WARNINGS`, and `READY`.

## Checklist Acknowledgement

Package Review includes this acknowledgement:

```text
I reviewed the package checklist and understand that missing, stale, unavailable, or not-applicable research may affect later analysis.
```

This does not imply investment approval.

## Standard Package Structure

Each version is built under:

```text
data/packages/<package_id>/<version_id>/
├── 00_Package_Manifest/
│   ├── package_manifest.json
│   ├── document_inventory.csv
│   ├── document_inventory.xlsx
│   ├── package_checklist.json
│   ├── package_checklist.csv
│   └── integrity_report.json
├── 01_SEC_Filings/
├── 02_Company_Materials/
├── 03_Earnings_Transcripts/
├── 04_Bloomberg/
├── 05_Sell_Side_Research/
├── 06_Credit_Research/
├── 07_Industry_Research/
├── 08_Activist_and_Bear_Research/
├── 09_Financial_Models/
├── 10_Internal_Analyst_Materials/
└── 11_Other/
```

Original working files are not moved or altered. Files are copied into the version snapshot and verified by SHA-256.

## Manifest And Inventory

`package_manifest.json` includes package identity, version identity, company metadata, analyst review acknowledgement, document counts, category counts, checklist coverage, warnings, missing/stale/needs-review items, and all included document metadata.

`document_inventory.csv` and `document_inventory.xlsx` include document IDs, categories, public/licensed status, source, dates, original/stored filenames, relative package paths, file size, SHA-256, and notes. The workbook is values-only, with a frozen header and filters.

`package_checklist.json` and `package_checklist.csv` snapshot the exact checklist state at build time. Locked snapshots are not recalculated when the working checklist later changes.

## Integrity Verification And Locking

`integrity_report.json` records files checked, passed, failed, missing files, hash mismatches, size mismatches, unexpected files, verification timestamp, and status. A package cannot be locked if integrity verification fails.

Builds create ZIP files such as:

```text
QXO_Equity_Research_Package_2026-07-13_V001.zip
```

ZIPs are written outside their source directory, verified by reopening, and hashed with SHA-256. ZIPs do not include SQLite databases, `.env` files, temp files, absolute paths, or recursive ZIP inclusion.

## Versioning And Comparison

Version IDs look like:

```text
QXO-20260713-V001
QXO-20260713-V002
```

The app compares two versions and reports added documents, removed documents, same-hash renamed files, recategorized documents, hash changes, checklist status changes, research cutoff changes, public/licensed count changes, and total size changes.

## Storage Locations

Working public/licensed files:

```text
data/downloaded/<package_id>/
```

Built version snapshots and ZIPs:

```text
data/packages/<package_id>/
```

SQLite metadata:

```text
data/database/cutler_research.db
```

## Security Controls

- Path containment checks
- Filename sanitization
- Atomic writes where practical
- Staging directories for builds
- SHA-256 verification after copy
- ZIP path safety and ZIP verification
- No arbitrary file inclusion
- No symlink following during copy
- No macro execution
- No spreadsheet formula refresh
- No external model submission
- No internet use during package build
- Audit logging for build, manifest, inventory, checklist, integrity, ZIP, lock, and download events

No antivirus scanning is claimed.

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

Launch the app:

```powershell
python -m streamlit run app\Home.py --server.port 8505
```

Or use the launcher:

```powershell
.\scripts\run_app.ps1
```

## Known Limitations

Phase 4 builds auditable document packages only. It does not parse, summarize, analyze, cite, score, or recommend investments from document contents.
