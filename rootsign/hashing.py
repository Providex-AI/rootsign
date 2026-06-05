"""Canonical hash specification for Action records.

CRITICAL: This module is FROZEN per the Phase 0 spec (MEMORY.md, 2026-05-01).
The byte-for-byte output of `compute_action_self_hash` must remain identical
across the SDK (Phase 1), the verify CLI (Phase 1), and the dashboard (Phase 2).

Any modification requires:
  1. A new hash algorithm version identifier
  2. A migration plan for existing records
  3. Founder sign-off

The canonical fields (in alphabetical key order because json.dumps uses sort_keys):
  action_id, input_hash, output_hash, prev_action_hash, self_hash inputs,
  sequence_number, session_id, timestamp, tool_name.

Fields explicitly excluded from the hash (mutable post-creation):
  duration_ms, input_redacted, output_redacted, authorization_status.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def compute_action_self_hash(action: dict[str, Any]) -> str:
    """Return the SHA-256 hex digest of an Action's canonical representation.

    The `timestamp` field may be a `datetime` (uses `.isoformat()`) or an
    already-serialized ISO 8601 string (used by JSON-loaded golden vectors).
    `output_hash` and `prev_action_hash` are coerced to "" when None/missing
    so the canonical JSON has stable keys.
    """
    timestamp = action["timestamp"]
    canonical = {
        "action_id": str(action["action_id"]),
        "session_id": str(action["session_id"]),
        "tool_name": action["tool_name"],
        "input_hash": action["input_hash"],
        "output_hash": action.get("output_hash") or "",
        "prev_action_hash": action.get("prev_action_hash") or "",
        "timestamp": timestamp.isoformat() if hasattr(timestamp, "isoformat") else timestamp,
        "sequence_number": action["sequence_number"],
    }
    serialized = json.dumps(canonical, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
