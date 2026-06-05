"""SDK-side payload hashing for Action.input_hash / Action.output_hash.

Distinct from `rootsign.hashing.compute_action_self_hash` (the store-side
canonical hash spec frozen in ADR-001). This function fingerprints the
*payload* of a single tool call — args/kwargs going in, return value coming
out — after redaction has been applied. The store then hashes these into the
canonical chain.

The two hash functions serve different purposes and must not be conflated:

    compute_payload_hash       — input/output digest, SDK side, per-call
    compute_action_self_hash   — chain link, store side, references the above
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def compute_payload_hash(payload: Any) -> str:
    """SHA-256 of a tool call payload, serialised deterministically.

    Rules:
      - None payload → hash of the empty string. This is the legitimate
        case for void tools (e.g. send_email returning no body) and must be
        distinguishable from "hash not computed" (which would be a bug).
      - dict / list / primitive payloads → json.dumps with sort_keys=True
        and ensure_ascii=True. Matches the canonical spec's serialisation
        rules so payloads built on either side hash identically.
      - Non-JSON-serialisable values (e.g. datetime, UUID) → coerced via
        str() through json.dumps(default=str). The decorator never feeds
        raw ORM objects in; this is a defensive backstop.

    Returns a 64-character lowercase hex digest.
    """
    if payload is None:
        serialized = ""
    else:
        serialized = json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
