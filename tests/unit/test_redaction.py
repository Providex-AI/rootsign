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

    def test_non_string_value_at_matched_path_is_matched_not_crashed(self):
        """A non-string value must not crash the matcher — and must not slip past it.

        This test previously asserted `{"count": 42}` came back as `42`,
        under the heading "we don't crash trying to regex-match an int".
        Crash-avoidance was the real requirement; "leave the value alone"
        was only how it happened to be implemented, by gating every rule on
        `isinstance(value, str)`. That gate also let numeric PII through in
        the clear — an SSN or account number arriving as a JSON number — and
        since redaction runs before hashing (ADR-006), the raw value reached
        both the hash input and `input_redacted`.

        The matcher now tests the value's string form, so an explicitly
        configured rule fires on the value the user actually configured it
        for. Crash-avoidance still holds, and is asserted directly below.
        """
        cfg = RedactionConfig({"count": r"\d+"})
        result = cfg.redact({"count": 42})
        assert result["count"] == REDACTED_PLACEHOLDER  # str(42) matches r"\d+"

        # The rule still decides — a value whose string form does not match
        # is returned untouched rather than blanket-redacted by key alone.
        assert cfg.redact({"count": "none at all"})["count"] == "none at all"

    def test_matcher_never_raises_on_exotic_value_types(self):
        """The original intent of the test above, asserted on its own terms."""

        class Hostile:
            def __str__(self):
                raise RuntimeError("boom")

        cfg = RedactionConfig({"count": r"\d+"})
        for value in (42, 4.2, None, True, b"bytes", {"a": 1}, [1, 2], Hostile()):
            cfg.redact({"count": value})  # must not raise
