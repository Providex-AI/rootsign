"""Generate golden hash vectors for tests/fixtures/hash_vectors.json.

This file is the canonical reference for compute_action_self_hash. It must be
committed and treated as immutable — modifying it without founder approval
breaks the hash chain contract.

Run with: .venv/bin/python scripts/generate_hash_vectors.py
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from providex.hashing import compute_action_self_hash

VECTORS_SRC: list[dict] = [
    {
        "description": "first action in session — null prev_action_hash",
        "input": {
            "action_id": UUID("550e8400-e29b-41d4-a716-446655440000"),
            "session_id": UUID("550e8400-e29b-41d4-a716-446655440001"),
            "tool_name": "send_email",
            "input_hash": "abc123def456abc123def456abc123def456abc123def456abc123def456abc1",
            "output_hash": "def456abc123def456abc123def456abc123def456abc123def456abc123def4",
            "prev_action_hash": None,
            "timestamp": datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc),
            "sequence_number": 1,
        },
    },
    {
        "description": "middle of chain — prev_action_hash points to previous self_hash",
        "input": {
            "action_id": UUID("550e8400-e29b-41d4-a716-446655440002"),
            "session_id": UUID("550e8400-e29b-41d4-a716-446655440001"),
            "tool_name": "query_database",
            "input_hash": "1111111111111111111111111111111111111111111111111111111111111111",
            "output_hash": "2222222222222222222222222222222222222222222222222222222222222222",
            "prev_action_hash": (
                "9feaf921f62124bc85fe448dc010494f953fb4271e45f8d26303937f4d65e18d"
            ),
            "timestamp": datetime(2026, 5, 1, 10, 0, 5, tzinfo=timezone.utc),
            "sequence_number": 2,
        },
    },
    {
        "description": "void action — null output_hash (e.g. side-effect tool returned nothing)",
        "input": {
            "action_id": UUID("550e8400-e29b-41d4-a716-446655440003"),
            "session_id": UUID("550e8400-e29b-41d4-a716-446655440001"),
            "tool_name": "rotate_secret",
            "input_hash": "3333333333333333333333333333333333333333333333333333333333333333",
            "output_hash": None,
            "prev_action_hash": (
                "0000000000000000000000000000000000000000000000000000000000000001"
            ),
            "timestamp": datetime(2026, 5, 1, 10, 0, 10, tzinfo=timezone.utc),
            "sequence_number": 3,
        },
    },
]


def _serialize(value):  # noqa: ANN001
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def main() -> None:
    out = {"vectors": []}
    for v in VECTORS_SRC:
        expected = compute_action_self_hash(v["input"])
        out["vectors"].append(
            {
                "description": v["description"],
                "input": {k: _serialize(val) for k, val in v["input"].items()},
                "expected_self_hash": expected,
            }
        )

    path = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "hash_vectors.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2, sort_keys=False) + "\n")
    for entry in out["vectors"]:
        print(entry["description"])
        print("  expected_self_hash:", entry["expected_self_hash"])
    print(f"\nWrote {len(out['vectors'])} vectors to {path}")


if __name__ == "__main__":
    main()
