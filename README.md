# Cutler Equity Research Workbench

Cutler Research AI is an internal Streamlit workbench for creating research workspaces, collecting public documents, uploading authorized licensed materials, building locked research package versions, and processing those locked corpora into searchable cited evidence.

## Current Status

Implemented through Phase 5:

- Phase 1: package setup, SQLite persistence, validation, dashboard, shared UI, and multi-page navigation.
- Phase 2: SEC company resolution, SEC filing preview/download, investor-relations PDF discovery, public document metadata, hashes, duplicate prevention, and collection history.
- Phase 3: licensed-file uploads, classification suggestions, analyst category correction, upload history, audit events, ZIP inspection, document inventory editing, controlled deletion, and checklist review.
- Phase 4: readiness validation, manifest generation, inventory exports, checklist snapshots, integrity reports, immutable package snapshots, ZIP generation, explicit locking, and version comparison.
- Phase 5: closed-corpus document processing, native extraction, optional local OCR, spreadsheet-safe parsing, citation-preserving chunks, keyword retrieval, deterministic evidence extraction, citation verification, duplicate grouping, conflict detection, analyst review, and evidence/conflict exports.

Not implemented:

- Buy/Sell/Hold or Insufficient Evidence recommendations, valuation conclusions, financial-model calculations, final investment memos, PM approval, trading integration, continuous monitoring, authentication, cloud deployment, RAG over open internet, or external LLM/API extraction.
- Buy/Sell/Hold and final investment reports begin in Phase 6.

## Closed-Corpus Guarantee

Phase 5 only processes documents included in a selected locked package version. It does not refresh SEC, investor-relations, market-data, Bloomberg, sell-side, web, or model-memory facts during processing or retrieval.

Every evidence record is tied to:

- A locked package version document.
- A source locator such as page, sheet, row, line, cell range, or section.
- Supporting source text and source-text hash.
- A deterministic citation-verification status.

Unsupported evidence is not marked verified.

## Locked Package Versions

The working package remains editable. A built package version is a snapshot copied into:

```text
data/packages/<package_id>/<version_id>/
```

Only versions with status `LOCKED` and integrity `VERIFIED` or `VERIFIED_WITH_WARNINGS` can be processed. Phase 5 verifies file existence, size, and SHA-256 before reading. If a locked file is missing or mutated, processing is blocked and an audit event is recorded. Create a new package version instead of modifying a locked version.

## Processing Runs

Each Phase 5 run is version-level and reproducible. A new configuration creates a new processing run rather than mutating old results.

Processing runs store:

- Pipeline and parser configuration versions.
- OCR, retrieval, and local embedding configuration metadata.
- Started/completed timestamps and status.
- Document, page, sheet, table, chunk, evidence, warning, and error counts.
- Created-by placeholder and audit events.

Statuses include `PENDING`, `RUNNING`, `COMPLETED`, `COMPLETED_WITH_WARNINGS`, `FAILED`, `CANCELLED`, and `STALE`.

## Processed Storage

Generated extraction artifacts are separate from immutable package snapshots:

```text
data/processed/<version_id>/<processing_run_id>/
  documents/<version_document_id>/
    document_metadata.json
    full_text.txt
    pages/
    sheets/
    tables/
    warnings.json
  chunks/
  evidence/
  indexes/
  run_summary.json
```

`data/processed/` is ignored by Git. Original locked files are not modified.

## Supported Formats

Phase 5 supports:

- PDF: PyMuPDF native text first, pypdf fallback, page counts, page text, image-only classification, mixed-page warnings, and OCR-needed state.
- DOCX: headings, paragraphs, tables, section order, and paragraph/table citations. Page numbers are not invented.
- TXT: encoding fallback and line-range citations.
- CSV: headers, row text, delimiter detection, row citations, and encoding capture.
- XLSX/XLSM: sheet names, hidden state, used ranges, cell references, literal values, formula text, formula-without-cached-value warnings, cached-value warnings, external-link metadata, and macro-safe handling.
- PNG/JPG/JPEG: metadata and optional local OCR.
- ZIP: stored as archive-only. Contents are not processed automatically in Phase 5.

## OCR Behavior

OCR is disabled by default and used only as a fallback for image-only or nearly image-only content when enabled by configuration. OCR is local only, limited by `MAX_OCR_PAGES`, and records confidence when available. If OCR dependencies or the local OCR engine are unavailable, documents are marked as requiring OCR rather than sent to any external service.

Low-confidence OCR-derived numeric evidence is left for analyst review.

## Spreadsheet Safety

Spreadsheets are read with `openpyxl`.

The pipeline never:

- Executes macros.
- Refreshes formulas.
- Uses Excel COM automation.
- Calls Bloomberg, FactSet, Morningstar, or other add-ins.
- Treats formula text as a calculated value.
- Follows external links.

Cell values are labeled as `LITERAL_VALUE`, `CACHED_FORMULA_VALUE`, `FORMULA_WITHOUT_CACHED_VALUE`, `EXTERNAL_LINK_VALUE`, or `UNKNOWN` where practical. Cached formula values are warned as potentially stale.

## Chunking And Retrieval

Chunks are deterministic and citation-preserving:

- PDFs are chunked by page.
- DOCX files keep headings and table context.
- TXT/CSV files keep line and row locators.
- Spreadsheets are chunked by sheet and row/cell range.

Retrieval is keyword-only by default and restricted to one selected locked version and one selected processing run. It deduplicates exact repeated chunks and returns source locators with scores. Hybrid/vector metadata can be recorded, but no external embeddings are used by default.

## Evidence And Citations

The evidence ledger stores structured rows for company facts, revenue, growth, margin, EPS, cash flow, debt, liquidity, guidance, analyst estimates, analyst ratings, price targets, credit ratings, covenants, capital allocation, risks, legal/regulatory facts, valuation multiples, convertible terms, and other facts.

Each evidence record includes claim text, evidence type, subject, metric, optional value/unit/currency/period, source text, citation locator JSON, extraction method, confidence, verification status, analyst status, and notes.

Example citation forms:

```text
[QXO Q1 2026 Earnings Release, p. 4]
[QXO Bloomberg ANR, Estimates sheet, cells F24:F30]
[QXO Earnings Transcript, lines 412-427]
[QXO 2025 10-K, Item 7, p. 63]
```

## Citation Verification

Every deterministic extracted evidence record is verified against its cited chunk. The verifier checks that the cited source exists, the stored source text hash still matches, the source text appears in the cited region, and numeric values/periods/metric terms are supported where possible.

Support statuses include:

- `SUPPORTS`
- `PARTIALLY_SUPPORTS`
- `DOES_NOT_SUPPORT`
- `SOURCE_MISSING`
- `AMBIGUOUS`
- `SOURCE_TEXT_HASH_MISMATCH`

## Duplicates, Conflicts, And Review

Phase 5 detects exact file duplicates, exact chunk duplicates, and near-identical text groups. Duplicate groups are shown without deleting source documents.

Conflict detection surfaces value differences, unit mismatches, forecast disagreements, and GAAP/adjusted mismatches for matching subject/metric/period groupings. The app does not automatically choose a winner.

Analysts can accept, reject, flag as needs review, annotate evidence, and add analyst-created evidence tied to an existing source chunk. Analyst-created evidence starts as pending verification.

## Configuration

Key Phase 5 settings are centralized in `app/config.py` and shown in `.env.example`:

- `MAX_PDF_PAGES`
- `MAX_SPREADSHEET_SHEETS`
- `MAX_SPREADSHEET_CELLS`
- `MAX_EXTRACTED_CHARACTERS`
- `OCR_ENABLED`
- `MAX_OCR_PAGES`
- `OCR_CONFIDENCE_THRESHOLD`
- `CHUNK_SIZE`
- `CHUNK_OVERLAP`
- `RETRIEVAL_RESULT_COUNT`
- `RETRIEVAL_MODE`
- `LOCAL_EMBEDDING_MODEL`
- `EXTERNAL_LLM_EXTRACTION_ENABLED`
- `PROCESSING_PIPELINE_VERSION`
- `PARSER_CONFIG_VERSION`

Default operation is deterministic, keyword-only, local, and works without an API key.

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

## Known Limitations

Phase 5 is evidence infrastructure, not investment judgment. OCR depends on locally installed OCR tooling. PDF table extraction is cautious and does not perform financial calculations. Retrieval is keyword-only by default. No external model, embedding, web, or market-data call is used by the Phase 5 pipeline.
