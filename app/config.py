from __future__ import annotations

import os
from pathlib import Path


APP_NAME = "Cutler Research AI"
APP_SUBTITLE = "Equity Research Workbench"
PAGE_TITLE = APP_NAME
PAGE_ICON = "📊"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
DATABASE_DIR = DATA_DIR / "database"
DATABASE_PATH = DATABASE_DIR / "cutler_research.db"
UPLOAD_DIR = DATA_DIR / "uploads"
DOWNLOAD_DIR = DATA_DIR / "downloaded"
PROCESSED_DIR = DATA_DIR / "processed"
PACKAGE_DIR = DATA_DIR / "packages"
REPORT_DIR = DATA_DIR / "reports"
CACHE_DIR = DATA_DIR / "cache"
STYLE_PATH = Path(__file__).resolve().parent / "styles" / "main.css"

ENVIRONMENT = os.getenv("CUTLER_ENV", "development")

SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "")
SEC_REQUEST_DELAY_SECONDS = float(os.getenv("SEC_REQUEST_DELAY_SECONDS", "0.2"))
SEC_CACHE_HOURS = int(os.getenv("SEC_CACHE_HOURS", "24"))
HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", "20"))
HTTP_MAX_RETRIES = int(os.getenv("HTTP_MAX_RETRIES", "3"))
MAX_DOWNLOAD_BYTES = int(os.getenv("MAX_DOWNLOAD_BYTES", str(25 * 1024 * 1024)))
IR_MAX_PAGES = int(os.getenv("IR_MAX_PAGES", "8"))
IR_MAX_DEPTH = int(os.getenv("IR_MAX_DEPTH", "1"))
MAX_UPLOAD_FILE_MB = int(os.getenv("MAX_UPLOAD_FILE_MB", "250"))
MAX_UPLOAD_BATCH_MB = int(os.getenv("MAX_UPLOAD_BATCH_MB", "1000"))
MAX_ZIP_ENTRIES = int(os.getenv("MAX_ZIP_ENTRIES", "500"))
MAX_ZIP_UNCOMPRESSED_MB = int(os.getenv("MAX_ZIP_UNCOMPRESSED_MB", "2000"))

SUPPORTED_SECURITY_TYPES = (
    "Common Equity",
    "Convertible Security",
    "Credit / Debt",
    "Other",
)

FILING_HISTORY_OPTIONS = {
    "1 year": 1,
    "2 years": 2,
    "3 years": 3,
    "5 years": 5,
}

STATUS_DRAFT = "DRAFT"
STATUS_SETUP = "SETUP"
STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_COMPLETE = "COMPLETE"
STATUS_WARNING = "WARNING"
STATUS_AWAITING_REVIEW = "AWAITING_REVIEW"
STATUS_UPCOMING = "UPCOMING"
STATUS_PUBLIC_COLLECTION = "PUBLIC_COLLECTION"
STATUS_PUBLIC_COLLECTION_PARTIAL = "PUBLIC_COLLECTION_PARTIAL"
STATUS_PUBLIC_COLLECTION_COMPLETE = "PUBLIC_COLLECTION_COMPLETE"
STATUS_LICENSED_UPLOADS = "LICENSED_UPLOADS"
STATUS_PACKAGE_REVIEW = "PACKAGE_REVIEW"
STATUS_PACKAGE_REVIEW_INCOMPLETE = "PACKAGE_REVIEW_INCOMPLETE"
STATUS_PACKAGE_READY_FOR_BUILD = "PACKAGE_READY_FOR_BUILD"
STATUS_PACKAGE_LOCKED = "PACKAGE_LOCKED"

PACKAGE_STATUSES = (
    STATUS_DRAFT,
    STATUS_SETUP,
    STATUS_IN_PROGRESS,
    STATUS_COMPLETE,
    STATUS_WARNING,
    STATUS_AWAITING_REVIEW,
    STATUS_PUBLIC_COLLECTION,
    STATUS_PUBLIC_COLLECTION_PARTIAL,
    STATUS_PUBLIC_COLLECTION_COMPLETE,
    STATUS_LICENSED_UPLOADS,
    STATUS_PACKAGE_REVIEW,
    STATUS_PACKAGE_REVIEW_INCOMPLETE,
    STATUS_PACKAGE_READY_FOR_BUILD,
    STATUS_PACKAGE_LOCKED,
)

DOCUMENT_STATUS_DISCOVERED = "DISCOVERED"
DOCUMENT_STATUS_DOWNLOADED = "DOWNLOADED"
DOCUMENT_STATUS_DUPLICATE = "DUPLICATE"
DOCUMENT_STATUS_FAILED = "FAILED"
DOCUMENT_STATUS_SKIPPED = "SKIPPED"

COLLECTION_STATUS_RUNNING = "RUNNING"
COLLECTION_STATUS_COMPLETE = "COMPLETE"
COLLECTION_STATUS_PARTIAL = "PARTIAL"
COLLECTION_STATUS_FAILED = "FAILED"

UPLOAD_STATUS_STARTED = "STARTED"
UPLOAD_STATUS_COMPLETED = "COMPLETED"
UPLOAD_STATUS_COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
UPLOAD_STATUS_FAILED = "FAILED"

CHECKLIST_STATUS_AVAILABLE = "AVAILABLE"
CHECKLIST_STATUS_MISSING = "MISSING"
CHECKLIST_STATUS_NOT_AVAILABLE = "NOT_AVAILABLE"
CHECKLIST_STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"
CHECKLIST_STATUS_NEEDS_REVIEW = "NEEDS_REVIEW"
CHECKLIST_STATUS_STALE = "STALE"

READINESS_NOT_READY = "NOT_READY"
READINESS_READY_WITH_WARNINGS = "READY_WITH_WARNINGS"
READINESS_READY = "READY"

VERSION_STATUS_BUILDING = "BUILDING"
VERSION_STATUS_BUILD_FAILED = "BUILD_FAILED"
VERSION_STATUS_BUILT = "BUILT"
VERSION_STATUS_LOCKED = "LOCKED"
VERSION_STATUS_SUPERSEDED = "SUPERSEDED"
VERSION_STATUS_ARCHIVED = "ARCHIVED"

INTEGRITY_VERIFIED = "VERIFIED"
INTEGRITY_VERIFIED_WITH_WARNINGS = "VERIFIED_WITH_WARNINGS"
INTEGRITY_FAILED = "FAILED"

SUPPORTED_UPLOAD_EXTENSIONS = (
    ".pdf",
    ".xlsx",
    ".xlsm",
    ".csv",
    ".docx",
    ".txt",
    ".zip",
    ".png",
    ".jpg",
    ".jpeg",
)

LICENSED_SOURCE_TYPES = {
    "bloomberg": "Bloomberg",
    "sell_side": "Sell-Side Research",
    "credit_research": "Credit Research",
    "morningstar": "Morningstar",
    "factset": "FactSet",
    "transcripts": "Transcripts",
    "industry_research": "Industry Research",
    "activist_bear_research": "Activist / Bear Research",
    "financial_models": "Financial Models",
    "company_materials": "Company Materials",
    "other": "Other",
}

SEC_SUPPORTED_FORMS = ("10-K", "10-Q", "8-K", "DEF 14A", "20-F", "6-K")
SEC_TICKER_MAPPING_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SEC_SUBMISSIONS_URL_TEMPLATE = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_ARCHIVES_BASE_URL = "https://www.sec.gov/Archives/edgar/data"
SEC_COMPANY_PAGE_TEMPLATE = "https://www.sec.gov/edgar/browse/?CIK={cik}"

SESSION_ACTIVE_PACKAGE_ID = "active_package_id"
SESSION_ACTIVE_TICKER = "active_ticker"
SESSION_CURRENT_WORKFLOW_STEP = "current_workflow_step"

REQUIRED_DIRECTORIES = (
    DATA_DIR,
    DATABASE_DIR,
    UPLOAD_DIR,
    DOWNLOAD_DIR,
    PROCESSED_DIR,
    PACKAGE_DIR,
    REPORT_DIR,
    CACHE_DIR,
)


def ensure_directories() -> None:
    """Create application data directories required for the workbench."""
    for directory in REQUIRED_DIRECTORIES:
        directory.mkdir(parents=True, exist_ok=True)


def sec_user_agent_is_configured() -> bool:
    """Return whether SEC requests have a usable user-agent value."""
    value = SEC_USER_AGENT.strip().lower()
    if not value:
        return False
    return "example.com" not in value and "research@example.com" not in value
