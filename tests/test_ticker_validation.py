from __future__ import annotations

from datetime import date, timedelta

from app.utils.validation import sanitize_analyst_notes, validate_cutoff_date, validate_ticker


def test_normal_uppercase_ticker_is_valid() -> None:
    result = validate_ticker("QXO")
    assert result.is_valid
    assert result.value == "QXO"


def test_lowercase_ticker_is_normalized() -> None:
    result = validate_ticker("googl")
    assert result.is_valid
    assert result.value == "GOOGL"


def test_ticker_with_dot_is_valid() -> None:
    result = validate_ticker("BRK.B")
    assert result.is_valid
    assert result.value == "BRK.B"


def test_ticker_with_hyphen_is_valid() -> None:
    result = validate_ticker("BF-B")
    assert result.is_valid
    assert result.value == "BF-B"


def test_leading_and_trailing_spaces_are_stripped() -> None:
    result = validate_ticker("  qxo  ")
    assert result.is_valid
    assert result.value == "QXO"


def test_empty_ticker_is_rejected() -> None:
    assert not validate_ticker("").is_valid


def test_invalid_characters_are_rejected() -> None:
    assert not validate_ticker("QXO!").is_valid


def test_embedded_spaces_are_rejected() -> None:
    assert not validate_ticker("BRK B").is_valid


def test_excessive_length_is_rejected() -> None:
    assert not validate_ticker("THISISALONGSENTENCE").is_valid


def test_url_is_rejected() -> None:
    assert not validate_ticker("https://example.com").is_valid


def test_future_cutoff_date_is_rejected() -> None:
    result = validate_cutoff_date(date.today() + timedelta(days=1))
    assert not result.is_valid


def test_analyst_notes_preserve_text_safely() -> None:
    result = sanitize_analyst_notes("  <script>alert('x')</script> thesis note  ")
    assert result.is_valid
    assert result.value == "<script>alert('x')</script> thesis note"
