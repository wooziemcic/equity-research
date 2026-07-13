from __future__ import annotations

import io
import mimetypes
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app import config
from app.services.workspace_service import sanitize_filename
from app.utils import database


class DocumentDownloadError(ValueError):
    """Raised when a managed document cannot be safely served."""


@dataclass(frozen=True)
class ManagedDocumentDownload:
    filename: str
    mime_type: str
    content: bytes
    source_url: str


def _managed_document_path(document: dict, package_id: str) -> Path:
    if document.get("package_id") != package_id:
        raise DocumentDownloadError("Document does not belong to the selected package.")
    raw_path = document.get("local_path")
    if not raw_path:
        raise DocumentDownloadError("The selected document has no managed file.")
    path = Path(raw_path).resolve()
    allowed_roots = [config.DOWNLOAD_DIR.resolve(), config.UPLOAD_DIR.resolve()]
    if not any(path == root or root in path.parents for root in allowed_roots):
        raise DocumentDownloadError("The selected document is outside the managed file store.")
    if not path.is_file():
        raise DocumentDownloadError("The managed document file is missing.")
    return path


def _document_filename(document: dict, path: Path) -> str:
    candidate = document.get("original_filename") or document.get("local_filename") or path.name
    return sanitize_filename(Path(str(candidate)).name)


def _document_mime(document: dict, filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".html", ".htm"}:
        return "text/html"
    if suffix == ".pdf":
        return "application/pdf"
    return document.get("mime_type") or mimetypes.guess_type(filename)[0] or "application/octet-stream"


def get_document_download(
    package_id: str,
    document_id: str,
    *,
    db_path: Path | str = config.DATABASE_PATH,
) -> ManagedDocumentDownload:
    """Read one document only after proving its package ownership."""
    document = database.get_document_by_document_id(document_id, db_path=db_path)
    if not document or document.get("package_id") != package_id:
        raise DocumentDownloadError("The selected document is not available in this package.")
    if document.get("collection_status") != config.DOCUMENT_STATUS_DOWNLOADED:
        raise DocumentDownloadError("Only successfully collected documents can be downloaded.")
    path = _managed_document_path(document, package_id)
    filename = _document_filename(document, path)
    return ManagedDocumentDownload(
        filename=filename,
        mime_type=_document_mime(document, filename),
        content=path.read_bytes(),
        source_url=str(document.get("source_url") or ""),
    )


def create_public_documents_zip(
    package_id: str,
    *,
    db_path: Path | str = config.DATABASE_PATH,
) -> tuple[bytes, str, int, int]:
    """Create an in-memory, package-scoped ZIP of public managed documents."""
    documents = [
        document
        for document in database.list_documents_by_package(package_id, db_path=db_path)
        if int(document.get("is_public") or 0)
        and document.get("collection_status") == config.DOCUMENT_STATUS_DOWNLOADED
    ]
    buffer = io.BytesIO()
    included = 0
    missing = 0
    used_names: set[str] = set()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for document in documents:
            try:
                path = _managed_document_path(document, package_id)
            except DocumentDownloadError:
                missing += 1
                continue
            name = _document_filename(document, path)
            stem, suffix = Path(name).stem, Path(name).suffix
            candidate = name
            index = 1
            while candidate.lower() in used_names:
                candidate = f"{stem}_{index}{suffix}"
                index += 1
            used_names.add(candidate.lower())
            archive.writestr(f"Public_Documents/{candidate}", path.read_bytes())
            included += 1
    filename = sanitize_filename(f"{package_id}_Public_Collected_Files.zip")
    return buffer.getvalue(), filename, included, missing
