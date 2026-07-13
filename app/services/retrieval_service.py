from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app import config
from app.utils import database


TOKEN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%-]*")


@dataclass(frozen=True)
class RetrievalResult:
    chunk_id: str
    version_document_id: str
    chunk_text: str
    score: float
    source_locator: dict[str, Any]
    page_number: int | None = None
    sheet_name: str | None = None
    row_range: str | None = None
    section_heading: str | None = None


def tokenize(text: str) -> list[str]:
    return [token.lower() for token in TOKEN_PATTERN.findall(text or "")]


def search_chunks(
    *,
    version_id: str,
    processing_run_id: str,
    query: str,
    document_category: str | None = None,
    public_only: bool | None = None,
    version_document_id: str | None = None,
    limit: int | None = None,
    db_path: Path | str = config.DATABASE_PATH,
) -> list[RetrievalResult]:
    """Keyword-search chunks from exactly one locked version and processing run."""
    terms = tokenize(query)
    if not terms:
        return []
    chunks = database.list_document_chunks(
        processing_run_id,
        version_id=version_id,
        version_document_id=version_document_id,
        db_path=db_path,
    )
    version_docs = {
        doc["document_id"]: doc
        for doc in database.list_package_version_documents(version_id, db_path=db_path)
    }
    results: list[RetrievalResult] = []
    seen_hashes: set[str] = set()
    for chunk in chunks:
        version_doc = version_docs.get(chunk["version_document_id"], {})
        if document_category and version_doc.get("category") != document_category:
            continue
        if public_only is True and not int(version_doc.get("is_public", 0)):
            continue
        if public_only is False and int(version_doc.get("is_public", 0)):
            continue
        if chunk["chunk_hash"] in seen_hashes:
            continue
        tokens = tokenize(chunk["chunk_text"])
        if not tokens:
            continue
        token_counts = {token: tokens.count(token) for token in set(tokens)}
        matches = sum(token_counts.get(term, 0) for term in terms)
        phrase_bonus = 2.0 if query.lower() in chunk["chunk_text"].lower() else 0.0
        coverage = len({term for term in terms if term in token_counts}) / len(set(terms))
        score = matches + phrase_bonus + math.log(len(tokens) + 1, 10) * coverage
        if score <= 0:
            continue
        seen_hashes.add(chunk["chunk_hash"])
        results.append(
            RetrievalResult(
                chunk_id=chunk["chunk_id"],
                version_document_id=chunk["version_document_id"],
                chunk_text=chunk["chunk_text"],
                score=round(score, 4),
                source_locator=json.loads(chunk["source_locator_json"]),
                page_number=chunk.get("page_number"),
                sheet_name=chunk.get("sheet_name"),
                row_range=chunk.get("row_range"),
                section_heading=chunk.get("section_heading"),
            )
        )
    results.sort(key=lambda item: item.score, reverse=True)
    return results[: limit or config.RETRIEVAL_RESULT_COUNT]
