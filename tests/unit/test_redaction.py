"""Unit tests for RedactionConfig."""

from __future__ import annotations

from rootsign.sdk.redaction import REDACTED_PLACEHOLDER, RedactionConfig


class TestSimpleFieldRedaction:
    def test_email_value_matching_pattern_redacted(self):
        cfg = RedactionConfig({"email": r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"})
        result = cfg.redact({"email": "user@example.com", "name": "Alice"})
        assert result["email"] == REDACTED_PLACEHOLDER
        assert result["name"] == "Alice"

    def test_email_value_not_matching_pattern_passes_through(self):
        cfg = RedactionConfig({"email": r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"})
        result = cfg.redact({"email": "not-an-email"})
        assert result["email"] == "not-an-email"

    def test_unconfigured_field_passes_through(self):
        cfg = RedactionConfig({"ssn": r"\d{3}-\d{2}-\d{4}"})
        result = cfg.redact({"name": "Alice", "age": 30})
        assert result == {"name": "Alice", "age": 30}


class TestNestedRedaction:
    def test_dot_notation_redacts_nested_field(self):
        cfg = RedactionConfig({"user.email": r"[^@]+@[^@]+"})
        result = cfg.redact({"user": {"email": "x@y.com", "name": "Alice"}})
        assert result["user"]["email"] == REDACTED_PLACEHOLDER
        assert result["user"]["name"] == "Alice"

    def test_top_level_email_not_redacted_when_only_nested_configured(self):
        cfg = RedactionConfig({"user.email": r"[^@]+@[^@]+"})
        result = cfg.redact({"email": "leaked@x.com", "user": {"email": "x@y.com"}})
        assert result["email"] == "leaked@x.com"  # not configured
        assert result["user"]["email"] == REDACTED_PLACEHOLDER


class TestEdgeCases:
    def test_empty_config_passthrough(self):
        cfg = RedactionConfig({})
        payload = {"key": "value"}
        assert cfg.redact(payload) is payload  # same object, no copy needed

    def test_none_payload_passthrough(self):
        cfg = RedactionConfig({"email": r".+"})
        assert cfg.redact(None) is None

    def test_non_dict_payload_passthrough(self):
        cfg = RedactionConfig({"email": r".+"})
        assert cfg.redact("just a string") == "just a string"
        assert cfg.redact(42) == 42

    def test_does_not_mutate_input(self):
        original = {"email": "user@example.com", "nested": {"phone": "123"}}
        snapshot = {"email": "user@example.com", "nested": {"phone": "123"}}
        cfg = RedactionConfig({"email": r".+"})
        _ = cfg.redact(original)
        assert original == snapshot
        assert original["nested"] == snapshot["nested"]

    def test_non_string_value_at_matched_path_not_coerced(self):
        """If the field exists but the value isn't a string, leave it
        alone. Tests that we don't crash trying to regex-match an int."""
        cfg = RedactionConfig({"count": r"\d+"})
        result = cfg.redact({"count": 42})
        assert result["count"] == 42  # not stringified, not redacted
