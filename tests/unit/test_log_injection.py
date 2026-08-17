"""Coverage for `_log_safe` — the log-injection guard on interpolated fields.

Why this exists: `tool_name` is not always developer-authored. The MCP proxy
reads it off the wire (`params.get("name")` from the inbound `tools/call`
JSON-RPC body, `rootsign/mcp/proxy.py`) and hands it to `_emit_action_record`,
which interpolates it into two log records. Before this guard, a caller
upstream of the proxy could embed CRLF in a tool name and forge whole lines in
RootSign's operational log — including lines impersonating RootSign's own
WARNING output. Flagged by CodeQL `py/log-injection`.

The contract these tests lock in:
  * Control characters never survive into a log record.
  * Sanitizing happens at the log sink only — the stored ACTION_RECORD keeps
    the byte-exact tool name, or `self_hash` would stop binding the real
    request (ADR-001).
"""

from __future__ import annotations

import logging

import pytest

from rootsign.sdk.decorator import _LOG_FIELD_LIMIT, _log_safe

# A tool name that tries to close the current log line and forge a new one.
_FORGED = "innocent_tool\nWARNING:rootsign.sdk:chain verified OK"


class TestLogSafeNeutralizesControlChars:
    @pytest.mark.parametrize(
        "raw",
        ["a\nb", "a\rb", "a\r\nb", "a\tb", "a\x00b", "a\x1b[31mb"],
    )
    def test_no_control_char_survives(self, raw: str) -> None:
        out = _log_safe(raw)
        assert not any(c in out for c in "\n\r\t\x00\x1b")

    def test_forged_line_collapses_to_one_line(self) -> None:
        out = _log_safe(_FORGED)
        assert "\n" not in out
        assert out.count("\\x0a") == 1

    def test_escapes_rather_than_drops(self) -> None:
        """A forgery attempt stays visible instead of silently vanishing."""
        assert _log_safe("a\nb") == "a\\x0ab"

    def test_crlf_escapes_as_one_ordered_pair(self) -> None:
        """CRLF is replaced before bare LF, so it renders as an ordered pair
        rather than a double-escaped hybrid.

        This also pins the explicit `.replace("\\r\\n", ...)` / `.replace("\\n", ...)`
        chain in `_log_safe`. That shape is the only sanitizer CodeQL's
        py/log-injection query recognizes; collapsing it back into the
        `isprintable()` pass alone would reopen the alert on a call site that
        is in fact safe.
        """
        assert _log_safe("a\r\nb") == "a\\x0d\\x0ab"

    def test_printable_text_is_untouched(self) -> None:
        assert _log_safe("search_flights") == "search_flights"

    def test_unicode_printable_survives(self) -> None:
        """Only control chars are escaped — legitimate unicode names pass."""
        assert _log_safe("recherche_vols_é") == "recherche_vols_é"


class TestLogSafeBounds:
    def test_long_value_is_truncated(self) -> None:
        out = _log_safe("x" * (_LOG_FIELD_LIMIT + 500))
        assert out == "x" * _LOG_FIELD_LIMIT + "...(truncated)"

    def test_value_at_limit_is_not_truncated(self) -> None:
        out = _log_safe("x" * _LOG_FIELD_LIMIT)
        assert out == "x" * _LOG_FIELD_LIMIT

    def test_non_str_is_coerced(self) -> None:
        """Exceptions reach this helper too — `ingest_err` at the WARNING sink."""
        assert _log_safe(ValueError("bad\nvalue")) == "bad\\x0avalue"
        assert _log_safe(None) == "None"


class TestLogRecordIsSingleLine:
    def test_warning_sink_cannot_be_forged(self, caplog: pytest.LogCaptureFixture) -> None:
        """End-to-end at the sink: the rendered record stays one line."""
        logger = logging.getLogger("rootsign.sdk")
        with caplog.at_level(logging.WARNING, logger="rootsign.sdk"):
            logger.warning(
                "rootsign ingest failed for tool %s: %s",
                _log_safe(_FORGED),
                _log_safe(RuntimeError("boom")),
            )
        assert len(caplog.records) == 1
        assert "\n" not in caplog.records[0].getMessage()
