"""Unit tests for compute_action_self_hash.

These tests are the primary contract for the hash spec — they validate that
the implementation matches the golden vectors in tests/fixtures/hash_vectors.json
and that the canonical-vs-excluded field split behaves exactly as MEMORY.md
documents.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

from providex.hashing import compute_action_self_hash

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "hash_vectors.json"
VECTORS = json.loads(FIXTURE_PATH.read_text())["vectors"]

# Canonical fields — mutating any of these MUST change the hash (AC-1.5).
CANONICAL_FIELDS = (
    "action_id",
    "session_id",
    "tool_name",
    "input_hash",
    "output_hash",
    "prev_action_hash",
    "timestamp",
    "sequence_number",
)

# Excluded fields — mutating these MUST NOT change the hash.
EXCLUDED_FIELDS = (
    "duration_ms",
    "input_redacted",
    "output_redacted",
    "authorization_status",
)


def _base_action() -> dict:
    return {
        "action_id": UUID("11111111-1111-4111-8111-111111111111"),
        "session_id": UUID("22222222-2222-4222-8222-222222222222"),
        "tool_name": "send_email",
        "input_hash": "a" * 64,
        "output_hash": "b" * 64,
        "prev_action_hash": "c" * 64,
        "timestamp": datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc),
        "sequence_number": 1,
    }


class TestGoldenVectors:
    @pytest.mark.parametrize("vector", VECTORS, ids=[v["description"] for v in VECTORS])
    def test_golden_vector_matches(self, vector):
        # AC-1.4 — Action self_hash matches golden vector
        actual = compute_action_self_hash(vector["input"])
        assert actual == vector["expected_self_hash"], (
            f"Hash mismatch for: {vector['description']}\n"
            f"  expected = {vector['expected_self_hash']}\n"
            f"  actual   = {actual}"
        )

    def test_first_vector_matches_agents_md_reference(self):
        """The AGENTS.md TASK 4 checkpoint hash is committed as the first vector."""
        assert VECTORS[0]["expected_self_hash"] == (
            "9feaf921f62124bc85fe448dc010494f953fb4271e45f8d26303937f4d65e18d"
        )


class TestCanonicalFieldsInvalidateHash:
    """AC-1.5 — mutating ANY canonical field must change the hash."""

    @pytest.mark.parametrize("field", CANONICAL_FIELDS)
    def test_mutation_changes_hash(self, field: str):
        base = _base_action()
        base_hash = compute_action_self_hash(base)
        mutated = dict(base)
        if field == "sequence_number":
            mutated[field] = base[field] + 1
        elif field == "timestamp":
            mutated[field] = base[field].replace(second=1)
        elif field == "action_id":
            mutated[field] = UUID("99999999-9999-4999-8999-999999999999")
        elif field == "session_id":
            mutated[field] = UUID("88888888-8888-4888-8888-888888888888")
        elif field == "tool_name":
            mutated[field] = "different_tool"
        else:
            mutated[field] = "d" * 64
        assert compute_action_self_hash(mutated) != base_hash, (
            f"Hash did not change after mutating canonical field '{field}'"
        )


class TestExcludedFieldsDoNotAffectHash:
    """Excluded fields (mutable post-creation) must NOT affect the hash."""

    @pytest.mark.parametrize("field", EXCLUDED_FIELDS)
    def test_excluded_field_does_not_change_hash(self, field: str):
        base = _base_action()
        base_hash = compute_action_self_hash(base)
        polluted = dict(base)
        # Add the excluded field with a nonsense value — the hash should ignore it.
        polluted[field] = {"foo": "bar"} if "redacted" in field else "anything"
        assert compute_action_self_hash(polluted) == base_hash, (
            f"Excluded field '{field}' affected the hash — spec violation"
        )


class TestRepresentationInvariants:
    def test_datetime_and_iso_string_produce_same_hash(self):
        """JSON-loaded vectors store timestamps as ISO strings, runtime uses datetime.
        Both must hash identically."""
        a = _base_action()
        b = dict(a)
        b["timestamp"] = a["timestamp"].isoformat()
        assert compute_action_self_hash(a) == compute_action_self_hash(b)

    def test_null_prev_action_hash_normalizes_to_empty_string(self):
        a = _base_action()
        a["prev_action_hash"] = None
        b = _base_action()
        b["prev_action_hash"] = ""
        assert compute_action_self_hash(a) == compute_action_self_hash(b)

    def test_null_output_hash_normalizes_to_empty_string(self):
        a = _base_action()
        a["output_hash"] = None
        b = _base_action()
        b["output_hash"] = ""
        assert compute_action_self_hash(a) == compute_action_self_hash(b)

    def test_uuid_object_and_string_produce_same_hash(self):
        a = _base_action()
        b = dict(a)
        b["action_id"] = str(a["action_id"])
        b["session_id"] = str(a["session_id"])
        assert compute_action_self_hash(a) == compute_action_self_hash(b)

    def test_output_is_64_hex_chars(self):
        h = compute_action_self_hash(_base_action())
        assert len(h) == 64
        int(h, 16)  # raises if non-hex
