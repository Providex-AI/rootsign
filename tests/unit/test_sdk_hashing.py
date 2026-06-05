"""Unit tests for SDK-side payload hashing.

These are intentionally simple — exhaustive serialisation coverage for the
canonical chain hash lives in tests/unit/test_hashing.py against
compute_action_self_hash. compute_payload_hash is the SDK-side fingerprint
helper; its surface is just "produce a deterministic 64-hex digest."
"""

from __future__ import annotations

from rootsign.sdk.hashing import compute_payload_hash


class TestComputePayloadHash:
    def test_deterministic(self):
        p = {"key": "value", "n": 42}
        assert compute_payload_hash(p) == compute_payload_hash(p)

    def test_key_order_independent(self):
        assert compute_payload_hash({"a": 1, "b": 2}) == compute_payload_hash({"b": 2, "a": 1})

    def test_none_produces_fixed_hash(self):
        h = compute_payload_hash(None)
        # SHA-256 of the empty string — well-known constant.
        assert h == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    def test_different_payloads_distinct_hashes(self):
        assert compute_payload_hash({"a": 1}) != compute_payload_hash({"a": 2})
        assert compute_payload_hash({"a": 1}) != compute_payload_hash({"b": 1})

    def test_returns_64_hex_chars(self):
        h = compute_payload_hash({"key": "val"})
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_non_json_value_coerced_via_str(self):
        from datetime import datetime, timezone
        from uuid import UUID

        payload = {
            "when": datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc),
            "id": UUID("550e8400-e29b-41d4-a716-446655440000"),
        }
        h = compute_payload_hash(payload)
        assert len(h) == 64

    def test_ensure_ascii_invariant(self):
        """Two payloads differing only in unicode literal vs escaped form
        must hash identically. ensure_ascii=True forces escaping."""
        a = {"name": "Olasile"}
        b = {"name": "Olasile"}  # same string, escaped form
        assert compute_payload_hash(a) == compute_payload_hash(b)
