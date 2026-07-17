from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import dotenv_values, load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PROJECT_DOTENV = dotenv_values(PROJECT_ROOT / ".env")
load_dotenv(PROJECT_ROOT / ".env", override=False)


APP_NAME = "Cutler Research AI"
APP_SUBTITLE = "Equity Research Workbench"
PAGE_TITLE = APP_NAME
PAGE_ICON = "📊"

DATA_DIR = PROJECT_ROOT / "data"
DATABASE_DIR = DATA_DIR / "database"
_database_path_override = os.getenv("CUTLER_DATABASE_PATH", "").strip()
DATABASE_PATH = Path(_database_path_override).expanduser() if _database_path_override else DATABASE_DIR / "cutler_research.db"
if not DATABASE_PATH.is_absolute():
    DATABASE_PATH = PROJECT_ROOT / DATABASE_PATH
MIGRATION_BACKUP_DIR = DATABASE_DIR / "migration_backups"
UPLOAD_DIR = DATA_DIR / "uploads"
DOWNLOAD_DIR = DATA_DIR / "downloaded"
PROCESSED_DIR = DATA_DIR / "processed"
PACKAGE_DIR = DATA_DIR / "packages"
REPORT_DIR = DATA_DIR / "reports"
CACHE_DIR = DATA_DIR / "cache"
STYLE_PATH = Path(__file__).resolve().parent / "styles" / "main.css"


def _setting(name: str, default: str = "") -> str:
    """Read import-time settings without issuing a Streamlit command."""
    return os.getenv(name, default)


def _secret_setting(name: str, default: str = "") -> str:
    """Read project dotenv, then Streamlit secrets, then process environment."""
    dotenv_value = _PROJECT_DOTENV.get(name)
    if dotenv_value not in (None, ""):
        return str(dotenv_value)
    try:
        from streamlit.runtime import exists

        if exists():
            import streamlit as st

            streamlit_value = st.secrets.get(name, "")
            if streamlit_value not in (None, ""):
                return str(streamlit_value)
    except Exception:
        pass
    return os.getenv(name, default)

ENVIRONMENT = _setting("CUTLER_ENV", "development")
_database_environment = _setting("DATABASE_ENVIRONMENT", "").strip().upper()
if _database_environment not in {"DEVELOPMENT", "TEST", "STREAMLIT_CLOUD", "UNKNOWN"}:
    _database_environment = "STREAMLIT_CLOUD" if _setting("STREAMLIT_SHARING_MODE") else "DEVELOPMENT"
DATABASE_ENVIRONMENT = _database_environment

_recipe_workbook_value = _setting("CUTLER_RECIPE_WORKBOOK_PATH", "").strip()
RECIPE_WORKBOOK_PATH = (
    Path(_recipe_workbook_value).expanduser()
    if _recipe_workbook_value
    else PROJECT_ROOT / "reference" / "Equity Research Package.xlsx"
)
if not RECIPE_WORKBOOK_PATH.is_absolute():
    RECIPE_WORKBOOK_PATH = PROJECT_ROOT / RECIPE_WORKBOOK_PATH

SEC_USER_AGENT = _setting("SEC_USER_AGENT", "")
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
MAX_PDF_PAGES = int(os.getenv("MAX_PDF_PAGES", "500"))
FINAL_PACKAGE_MAX_FILENAME_LENGTH = int(os.getenv("FINAL_PACKAGE_MAX_FILENAME_LENGTH", "180"))
SEC_READER_RENDERER_VERSION = os.getenv("SEC_READER_RENDERER_VERSION", "6C.2")
SECTION_EXTRACTION_VERSION = os.getenv("SECTION_EXTRACTION_VERSION", "6C.1")
COMPANY_FACTS_VERSION = os.getenv("COMPANY_FACTS_VERSION", "6C.1")
LICENSED_AUDIT_BYTES_ENABLED = os.getenv("LICENSED_AUDIT_BYTES_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
MAX_SPREADSHEET_SHEETS = int(os.getenv("MAX_SPREADSHEET_SHEETS", "50"))
MAX_SPREADSHEET_CELLS = int(os.getenv("MAX_SPREADSHEET_CELLS", "250000"))
MAX_EXTRACTED_CHARACTERS = int(os.getenv("MAX_EXTRACTED_CHARACTERS", str(5 * 1024 * 1024)))
MAX_OCR_PAGES = int(os.getenv("MAX_OCR_PAGES", "25"))
OCR_ENABLED = os.getenv("OCR_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
OCR_CONFIDENCE_THRESHOLD = float(os.getenv("OCR_CONFIDENCE_THRESHOLD", "0.80"))
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "1800"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "200"))
RETRIEVAL_RESULT_COUNT = int(os.getenv("RETRIEVAL_RESULT_COUNT", "20"))
RETRIEVAL_MODE = os.getenv("RETRIEVAL_MODE", "keyword").strip().lower()
LOCAL_EMBEDDING_MODEL = os.getenv("LOCAL_EMBEDDING_MODEL", "keyword-only")
EXTERNAL_LLM_EXTRACTION_ENABLED = os.getenv("EXTERNAL_LLM_EXTRACTION_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
OPENAI_REQUIRED = os.getenv("OPENAI_REQUIRED", "true").strip().lower() in {"1", "true", "yes", "on"}
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
OPENAI_API_MODE = os.getenv("OPENAI_API_MODE", "auto").strip().lower()
if OPENAI_API_MODE not in {"auto", "responses", "chat_completions"}:
    OPENAI_API_MODE = "auto"
OPENAI_USE_REASONING = os.getenv("OPENAI_USE_REASONING", "false").strip().lower() in {"1", "true", "yes", "on"}
OPENAI_REASONING_EFFORT = os.getenv("OPENAI_REASONING_EFFORT", "low")
OPENAI_PREFLIGHT_CACHE_SECONDS = int(os.getenv("OPENAI_PREFLIGHT_CACHE_SECONDS", "60"))
OPENAI_EXTRACTION_BATCH_SIZE = max(1, int(os.getenv("OPENAI_EXTRACTION_BATCH_SIZE", "15")))
OPENAI_MAX_EXTRACTION_CHUNKS = max(1, int(os.getenv("OPENAI_MAX_EXTRACTION_CHUNKS", "150")))
OPENAI_MAX_CHUNK_CHARACTERS = max(500, int(os.getenv("OPENAI_MAX_CHUNK_CHARACTERS", "5000")))
OPENAI_EXTRACTION_MAX_OUTPUT_TOKENS = max(1000, int(os.getenv("OPENAI_EXTRACTION_MAX_OUTPUT_TOKENS", "8000")))
MAX_DETECTED_CLAIM_CONFLICTS = max(1, int(os.getenv("MAX_DETECTED_CLAIM_CONFLICTS", "500")))
OPENAI_MAX_NARRATIVE_EVIDENCE = max(1, int(os.getenv("OPENAI_MAX_NARRATIVE_EVIDENCE", "250")))
OPENAI_MAX_NARRATIVE_CONFLICTS = max(1, int(os.getenv("OPENAI_MAX_NARRATIVE_CONFLICTS", "100")))
OPENAI_MODEL_PRICING = {
    "gpt-4.1-mini": {"input": 0.40, "cached_input": 0.10, "output": 1.60},
}
AI_REVIEW_STATUS_NOT_REQUIRED = "NOT_REQUIRED"
AI_REVIEW_STATUS_RUNNING = "RUNNING"
AI_REVIEW_STATUS_COMPLETED = "COMPLETED"
PROCESSING_PIPELINE_VERSION = os.getenv("PROCESSING_PIPELINE_VERSION", "5.0")
PARSER_CONFIG_VERSION = os.getenv("PARSER_CONFIG_VERSION", "5.0")
PROCESSING_MAX_WORKERS = max(1, min(4, int(os.getenv("PROCESSING_MAX_WORKERS", "2"))))
PROCESSING_CONCURRENCY_ENABLED = os.getenv("PROCESSING_CONCURRENCY_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
PROCESSING_EXTRACTION_CONFIG_VERSION = os.getenv("PROCESSING_EXTRACTION_CONFIG_VERSION", "1.0")
ANALYSIS_PIPELINE_VERSION = os.getenv("ANALYSIS_PIPELINE_VERSION", "6.0")
ANALYSIS_CONFIGURATION_VERSION = os.getenv("ANALYSIS_CONFIGURATION_VERSION", "6.0")
SCORECARD_VERSION = os.getenv("SCORECARD_VERSION", "6.0")
VALUATION_CONFIGURATION_VERSION = os.getenv("VALUATION_CONFIGURATION_VERSION", "6.0")
_configured_report_template_version = os.getenv("REPORT_TEMPLATE_VERSION", "8.0").strip()
try:
    REPORT_TEMPLATE_VERSION = (
        _configured_report_template_version
        if float(_configured_report_template_version) >= 8.0
        else "8.0"
    )
except ValueError:
    REPORT_TEMPLATE_VERSION = "8.0"
MEMO_SYNTHESIS_REQUIRED = os.getenv("MEMO_SYNTHESIS_REQUIRED", "true").strip().lower() in {"1", "true", "yes", "on"}
MEMO_PROMPT_VERSION = os.getenv("MEMO_PROMPT_VERSION", "1.0")
MEMO_SCHEMA_VERSION = os.getenv("MEMO_SCHEMA_VERSION", "1.0")
MEMO_MAX_OUTPUT_TOKENS = max(1000, int(os.getenv("MEMO_MAX_OUTPUT_TOKENS", "4000")))
MIN_EVIDENCE_COVERAGE = float(os.getenv("MIN_EVIDENCE_COVERAGE", "0.55"))
BUY_SCORE_THRESHOLD = float(os.getenv("BUY_SCORE_THRESHOLD", "7.25"))
HOLD_SCORE_THRESHOLD = float(os.getenv("HOLD_SCORE_THRESHOLD", "4.75"))
SELL_SCORE_THRESHOLD = float(os.getenv("SELL_SCORE_THRESHOLD", "3.50"))
MAX_UNRESOLVED_CONFLICTS = int(os.getenv("MAX_UNRESOLVED_CONFLICTS", "3"))
MIN_BUY_UPSIDE = float(os.getenv("MIN_BUY_UPSIDE", "0.10"))
MAX_SELL_DOWNSIDE = float(os.getenv("MAX_SELL_DOWNSIDE", "-0.10"))
EXTERNAL_NARRATIVE_MODEL_ENABLED = os.getenv("EXTERNAL_NARRATIVE_MODEL_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
NARRATIVE_MODEL_NAME = os.getenv("NARRATIVE_MODEL_NAME", OPENAI_MODEL)

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
DOCUMENT_STATUS_RESOLVED = "RESOLVED"
DOCUMENT_STATUS_SUPERSEDED = "SUPERSEDED"
DOCUMENT_STATUS_OUTSIDE_SELECTED_WINDOW = "OUTSIDE_SELECTED_WINDOW"

COLLECTION_ITEM_DOWNLOADED_NOW = "DOWNLOADED_NOW"
COLLECTION_ITEM_ALREADY_COLLECTED = "ALREADY_COLLECTED"
COLLECTION_ITEM_DUPLICATE = "DUPLICATE"
COLLECTION_ITEM_FAILED = "FAILED"
COLLECTION_ITEM_NOT_FOUND = "NOT_FOUND"

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
CHECKLIST_STATUS_NOT_FILED_IN_PERIOD = "NOT_FILED_IN_PERIOD"
CHECKLIST_STATUS_OPTIONAL_NOT_DISCOVERED = "OPTIONAL_NOT_DISCOVERED"
CHECKLIST_STATUS_AWAITING_SELECTION = "AWAITING_SELECTION"

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

PROCESSING_STATUS_PENDING = "PENDING"
PROCESSING_STATUS_RUNNING = "RUNNING"
PROCESSING_STATUS_COMPLETED = "COMPLETED"
PROCESSING_STATUS_COMPLETED_WITH_WARNINGS = "COMPLETED_WITH_WARNINGS"
PROCESSING_STATUS_FAILED = "FAILED"
PROCESSING_STATUS_CANCELLED = "CANCELLED"
PROCESSING_STATUS_STALE = "STALE"
PROCESSING_STATUS_INTERRUPTED = "INTERRUPTED"
PROCESSING_STATUS_PARTIAL = "PARTIAL"

DOCUMENT_PROCESSING_SUCCESS = "SUCCESS"
DOCUMENT_PROCESSING_PARTIAL = "PARTIAL"
DOCUMENT_PROCESSING_FAILED = "FAILED"
DOCUMENT_PROCESSING_SKIPPED = "SKIPPED"

VERIFICATION_PENDING = "PENDING"
VERIFICATION_SUPPORTS = "SUPPORTS"
VERIFICATION_PARTIALLY_SUPPORTS = "PARTIALLY_SUPPORTS"
VERIFICATION_DOES_NOT_SUPPORT = "DOES_NOT_SUPPORT"
VERIFICATION_SOURCE_MISSING = "SOURCE_MISSING"
VERIFICATION_AMBIGUOUS = "AMBIGUOUS"
VERIFICATION_HASH_MISMATCH = "SOURCE_TEXT_HASH_MISMATCH"

ANALYST_STATUS_UNREVIEWED = "UNREVIEWED"
ANALYST_STATUS_ACCEPTED = "ACCEPTED"
ANALYST_STATUS_REJECTED = "REJECTED"
ANALYST_STATUS_NEEDS_REVIEW = "NEEDS_REVIEW"

ANALYSIS_STATUS_DRAFT = "DRAFT"
ANALYSIS_STATUS_CALCULATING = "CALCULATING"
ANALYSIS_STATUS_GENERATED = "GENERATED"
ANALYSIS_STATUS_NEEDS_ANALYST_REVIEW = "NEEDS_ANALYST_REVIEW"
ANALYSIS_STATUS_ANALYST_REVIEWED = "ANALYST_REVIEWED"
ANALYSIS_STATUS_NEEDS_PM_APPROVAL = "NEEDS_PM_APPROVAL"
ANALYSIS_STATUS_PM_APPROVED = "PM_APPROVED"
ANALYSIS_STATUS_PM_REJECTED = "PM_REJECTED"
ANALYSIS_STATUS_SUPERSEDED = "SUPERSEDED"
ANALYSIS_STATUS_COMPLETED_WITH_WARNINGS = "COMPLETED_WITH_WARNINGS"
ANALYSIS_STATUS_FAILED = "FAILED"

RECOMMENDATION_BUY = "BUY"
RECOMMENDATION_HOLD = "HOLD"
RECOMMENDATION_SELL = "SELL"
RECOMMENDATION_INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
RECOMMENDATION_ANALYST_REVIEW_REQUIRED = "ANALYST_REVIEW_REQUIRED"

CONFIDENCE_HIGH = "HIGH"
CONFIDENCE_MEDIUM = "MEDIUM"
CONFIDENCE_LOW = "LOW"
CONFIDENCE_INSUFFICIENT = "INSUFFICIENT"

REPORT_STATUS_DRAFT = "DRAFT"
REPORT_STATUS_FINAL = "FINAL"
REPORT_STATUS_FAILED = "FAILED"

SCORECARD_PROFILES = {
    "Common Equity": {
        "BUSINESS_QUALITY": ("Business Quality", 0.15),
        "REVENUE_EARNINGS_DIRECTION": ("Revenue and Earnings Direction", 0.15),
        "PROFITABILITY_CASH_FLOW": ("Profitability and Cash Flow", 0.15),
        "BALANCE_SHEET_LIQUIDITY": ("Balance Sheet and Liquidity", 0.12),
        "VALUATION": ("Valuation", 0.15),
        "CATALYSTS": ("Catalysts", 0.10),
        "DOWNSIDE_RISK": ("Downside Risk", 0.10),
        "EVIDENCE_QUALITY": ("Evidence Quality", 0.08),
    },
    "Convertible Security": {
        "BOND_FLOOR": ("Bond Floor", 0.14),
        "CONVERSION_PREMIUM": ("Conversion Premium", 0.12),
        "EQUITY_SENSITIVITY": ("Equity Sensitivity", 0.12),
        "COUPON_MATURITY": ("Coupon and Maturity", 0.12),
        "CREDIT_QUALITY": ("Credit Quality", 0.16),
        "CALL_PROVISIONS": ("Call Provisions", 0.10),
        "DOWNSIDE_PROTECTION": ("Downside Protection", 0.14),
        "EVIDENCE_QUALITY": ("Evidence Quality", 0.10),
    },
    "Credit / Debt": {
        "LIQUIDITY": ("Liquidity", 0.17),
        "LEVERAGE": ("Leverage", 0.17),
        "INTEREST_COVERAGE": ("Interest Coverage", 0.14),
        "COVENANT_RISK": ("Covenant Risk", 0.12),
        "MATURITY_PROFILE": ("Maturity Profile", 0.12),
        "RECOVERY_DOWNSIDE": ("Recovery / Downside", 0.14),
        "RATING_DIRECTION": ("Rating Direction", 0.08),
        "EVIDENCE_QUALITY": ("Evidence Quality", 0.06),
    },
    "Other": {
        "BUSINESS_QUALITY": ("Business Quality", 0.25),
        "FINANCIAL_DIRECTION": ("Financial Direction", 0.20),
        "BALANCE_SHEET_LIQUIDITY": ("Balance Sheet and Liquidity", 0.15),
        "VALUATION": ("Valuation", 0.15),
        "RISKS": ("Risks", 0.15),
        "EVIDENCE_QUALITY": ("Evidence Quality", 0.10),
    },
}

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

SEC_SUPPORTED_FORMS = ("10-K", "10-Q", "8-K", "S-3", "S-4", "DEF 14A", "144")
FORM_144_AUTO_SELECT_ENABLED = os.getenv("FORM_144_AUTO_SELECT_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _optional_float(name: str) -> float | None:
    value = os.getenv(name, "").strip()
    return float(value) if value else None


FORM_144_MIN_SHARES = _optional_float("FORM_144_MIN_SHARES")
FORM_144_MIN_MARKET_VALUE = _optional_float("FORM_144_MIN_MARKET_VALUE")
SEC_8K_COLLECTION_MODE = os.getenv("SEC_8K_COLLECTION_MODE", "ALL_8K").strip().upper()
if SEC_8K_COLLECTION_MODE not in {"ALL_8K", "MATERIAL_8K_ONLY", "ANALYST_SELECTION"}:
    SEC_8K_COLLECTION_MODE = "ALL_8K"
SEC_8K_APPROVED_ITEMS = tuple(
    item.strip().upper()
    for item in os.getenv("SEC_8K_APPROVED_ITEMS", "").split(",")
    if item.strip()
)
SEARCH_PROVIDER = os.getenv("SEARCH_PROVIDER", "none").strip().lower()
SEARCH_API_KEY = os.getenv("SEARCH_API_KEY", "")
SEARCH_MAX_RESULTS = max(1, int(os.getenv("SEARCH_MAX_RESULTS", "10")))
BRAVE_SEARCH_API_KEY = str(_PROJECT_DOTENV.get("BRAVE_SEARCH_API_KEY") or os.getenv("BRAVE_SEARCH_API_KEY", ""))
BRAVE_SEARCH_ENDPOINT = str(_PROJECT_DOTENV.get("BRAVE_SEARCH_ENDPOINT") or os.getenv(
    "BRAVE_SEARCH_ENDPOINT", "https://api.search.brave.com/res/v1/web/search"
)).strip()
BRAVE_SEARCH_COUNTRY = os.getenv("BRAVE_SEARCH_COUNTRY", "US").strip().upper()
BRAVE_SEARCH_LANGUAGE = os.getenv("BRAVE_SEARCH_LANGUAGE", "en").strip().lower()
BRAVE_SEARCH_UI_LANGUAGE = os.getenv("BRAVE_SEARCH_UI_LANGUAGE", "en-US").strip()
BRAVE_SEARCH_SAFESEARCH = os.getenv("BRAVE_SEARCH_SAFESEARCH", "moderate").strip().lower()
BRAVE_SEARCH_EXTRA_SNIPPETS = os.getenv("BRAVE_SEARCH_EXTRA_SNIPPETS", "true").strip().lower() in {"1", "true", "yes", "on"}
BRAVE_MAX_RESULTS_PER_QUERY = max(1, min(20, int(os.getenv("BRAVE_MAX_RESULTS_PER_QUERY", os.getenv("BRAVE_SEARCH_MAX_RESULTS", "10")))))
BRAVE_SEARCH_MAX_RESULTS = BRAVE_MAX_RESULTS_PER_QUERY
BRAVE_MAX_QUERIES_PER_PACKAGE = max(1, int(os.getenv("BRAVE_MAX_QUERIES_PER_PACKAGE", "40")))
BRAVE_MAX_QUERIES_PER_SLOT = max(1, int(os.getenv("BRAVE_MAX_QUERIES_PER_SLOT", "3")))
BRAVE_MAX_PAGES_PER_QUERY = max(1, min(2, int(os.getenv("BRAVE_MAX_PAGES_PER_QUERY", "2"))))
BRAVE_QUERY_CACHE_HOURS = max(1, int(os.getenv("BRAVE_QUERY_CACHE_HOURS", "24")))
BRAVE_REQUEST_TIMEOUT_SECONDS = max(1.0, float(os.getenv("BRAVE_REQUEST_TIMEOUT_SECONDS", "20")))
BRAVE_REQUEST_MAX_RETRIES = max(0, int(os.getenv("BRAVE_REQUEST_MAX_RETRIES", "2")))
BRAVE_REQUEST_BACKOFF_SECONDS = max(0.0, float(os.getenv("BRAVE_REQUEST_BACKOFF_SECONDS", "1.0")))
BRAVE_COST_PER_1000_REQUESTS = _optional_float("BRAVE_COST_PER_1000_REQUESTS")
OPENAI_DISCOVERY_CLASSIFICATION_ENABLED = os.getenv("OPENAI_DISCOVERY_CLASSIFICATION_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
OPENAI_DISCOVERY_MAX_CANDIDATES_PER_SLOT = max(1, int(os.getenv("OPENAI_DISCOVERY_MAX_CANDIDATES_PER_SLOT", "5")))
OPENAI_DISCOVERY_MAX_CALLS_PER_PACKAGE = max(1, int(os.getenv("OPENAI_DISCOVERY_MAX_CALLS_PER_PACKAGE", "5")))
IR_MAX_REDIRECTS = max(0, int(os.getenv("IR_MAX_REDIRECTS", "5")))
IR_REQUEST_DELAY_SECONDS = float(os.getenv("IR_REQUEST_DELAY_SECONDS", "0.2"))
OPENAI_EVIDENCE_PROMPT_VERSION = os.getenv("OPENAI_EVIDENCE_PROMPT_VERSION", "1.0")
OPENAI_EVIDENCE_SCHEMA_VERSION = os.getenv("OPENAI_EVIDENCE_SCHEMA_VERSION", "1.0")
REPORT_MODE = os.getenv("REPORT_MODE", "COMPACT_INVESTMENT_MEMO").strip().upper()
DURABLE_STORAGE_APPROVED = os.getenv("DURABLE_STORAGE_APPROVED", "false").strip().lower() in {"1", "true", "yes", "on"}
SEC_TICKER_MAPPING_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SEC_SUBMISSIONS_URL_TEMPLATE = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_ARCHIVES_BASE_URL = "https://www.sec.gov/Archives/edgar/data"
SEC_COMPANY_PAGE_TEMPLATE = "https://www.sec.gov/edgar/browse/?CIK={cik}"

SESSION_ACTIVE_PACKAGE_ID = "active_package_id"
SESSION_ACTIVE_TICKER = "active_ticker"
SESSION_CURRENT_WORKFLOW_STEP = "current_workflow_step"
SESSION_ACTIVE_VERSION_ID = "active_version_id"
SESSION_ACTIVE_PROCESSING_RUN_ID = "active_processing_run_id"
SESSION_ACTIVE_ANALYSIS_RUN_ID = "active_analysis_run_id"
SESSION_ACTIVE_REPORT_ID = "active_report_id"
SESSION_PRIMARY_SCREEN = "primary_screen"
SESSION_COLLECTION_STATE = "collection_state"
SESSION_WORKFLOW_STATE = "workflow_state"

WORKFLOW_STATUS_RUNNING = "RUNNING"
WORKFLOW_STATUS_COMPLETED = "COMPLETED"
WORKFLOW_STATUS_COMPLETED_WITH_WARNINGS = "COMPLETED_WITH_WARNINGS"
WORKFLOW_STATUS_FAILED = "FAILED"
WORKFLOW_STATUS_BLOCKED = "BLOCKED"

COMBINED_EXPORT_STATUS_CREATED = "CREATED"
COMBINED_EXPORT_STATUS_FAILED = "FAILED"

EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
SEC_USER_AGENT_PLACEHOLDERS = ("example.com", "your-domain", "placeholder")

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
    raw_value = SEC_USER_AGENT.strip()
    value = raw_value.lower()
    if not value:
        return False
    if any(placeholder in value for placeholder in SEC_USER_AGENT_PLACEHOLDERS):
        return False
    if not EMAIL_PATTERN.search(raw_value):
        return False
    application_name = EMAIL_PATTERN.sub("", raw_value).strip(" \t\r\n()[]{}<>;:-_,")
    return bool(application_name)


def brave_search_api_key() -> str:
    """Resolve the optional Brave key lazily after Streamlit page configuration."""
    return (_secret_setting("BRAVE_SEARCH_API_KEY", "") or _secret_setting("SEARCH_API_KEY", "")).strip()
