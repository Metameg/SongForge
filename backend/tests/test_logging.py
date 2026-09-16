"""Structured JSON logs carry a correlation ID (acceptance criterion #4, spec #77)."""

from __future__ import annotations

import io
import json

from songforge import correlation
from songforge.logging_setup import configure_logging, get_logger


def _emit_and_capture(**bind: str) -> dict[str, object]:
    stream = io.StringIO()
    configure_logging(level="INFO", stream=stream)
    log = get_logger("test")
    log.info("song_advanced", **bind)
    return json.loads(stream.getvalue().strip().splitlines()[-1])


def test_logs_are_json() -> None:
    record = _emit_and_capture(song_id="abc")
    assert record["event"] == "song_advanced"
    assert record["song_id"] == "abc"
    assert record["level"] == "info"
    assert "timestamp" in record


def test_log_carries_bound_correlation_id() -> None:
    token = correlation.set_correlation_id("corr-123")
    try:
        record = _emit_and_capture()
    finally:
        correlation.reset_correlation_id(token)
    assert record["correlation_id"] == "corr-123"


def test_log_has_no_correlation_id_when_unset() -> None:
    correlation.clear_correlation_id()
    record = _emit_and_capture()
    assert record.get("correlation_id") is None


def test_new_correlation_id_is_unique() -> None:
    a = correlation.new_correlation_id()
    b = correlation.new_correlation_id()
    assert a != b
    assert correlation.get_correlation_id() == b


def test_get_correlation_id_defaults_none() -> None:
    correlation.clear_correlation_id()
    assert correlation.get_correlation_id() is None
