"""Unit tests for the shared client-side sealer (T2.3, ADR-013 Decision 1).

`rootsign.chain_state` is the one place the client-side backends assign chain
identity, so these cover the three properties everything downstream leans on:
the seal is the frozen canonical formula, an existing seal is adopted rather
than re-minted, and a half-seal is an error instead of a silent completion.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from rootsign.chain_state import (
    ChainRegistry,
    ChainState,
    is_sealed,
    new_record_id,
)
from rootsign.hashing import compute_action_self_hash

SESSION = str(uuid4())


def _payload(tool: str = "send_email", **overrides: Any) -> dict[str, Any]:
    payload = {
        "tool_name": tool,
        "input_hash": "a" * 64,
        "output_hash": "b" * 64,
        "timestamp": "2026-08-20T12:00:00+00:00",
    }
    payload.update(overrides)
    return payload


def test_seal_uses_the_frozen_canonical_formula() -> None:
    state = ChainState()
    payload = _payload()

    sealed = state.seal(SESSION, payload)

    assert sealed.self_hash == compute_action_self_hash(
        {
            "action_id": sealed.action_id,
            "session_id": SESSION,
            "tool_name": payload["tool_name"],
            "input_hash": payload["input_hash"],
            "output_hash": payload["output_hash"],
            "prev_action_hash": None,
            "timestamp": payload["timestamp"],
            "sequence_number": 1,
        }
    )


def test_sequences_are_dense_and_one_based_and_the_chain_links() -> None:
    state = ChainState()

    first = state.seal(SESSION, _payload("one"))
    second = state.seal(SESSION, _payload("two"))
    third = state.seal(SESSION, _payload("three"))

    assert [s.sequence_number for s in (first, second, third)] == [1, 2, 3]
    assert first.prev_action_hash is None
    assert second.prev_action_hash == first.self_hash
    assert third.prev_action_hash == second.self_hash


def test_registry_keeps_one_chain_per_session() -> None:
    registry = ChainRegistry()
    other = str(uuid4())

    a1 = registry.seal(SESSION, _payload())
    b1 = registry.seal(other, _payload())
    a2 = registry.seal(SESSION, _payload())

    assert a1.sequence_number == 1
    assert b1.sequence_number == 1  # a separate session starts its own chain
    assert a2.sequence_number == 2
    assert a2.prev_action_hash == a1.self_hash


def test_an_existing_seal_is_adopted_not_reminted() -> None:
    """The retry and spool paths both depend on this (see the module docstring)."""
    origin = ChainState()
    sealed = origin.seal(SESSION, _payload())
    payload = {**_payload(), **sealed.as_payload_fields()}

    adopted = ChainState().seal(SESSION, payload)

    assert adopted.adopted is True
    assert adopted.action_id == sealed.action_id
    assert adopted.sequence_number == sealed.sequence_number
    assert adopted.self_hash == sealed.self_hash


def test_adoption_advances_the_chain_so_later_records_link_onto_it() -> None:
    origin = ChainState()
    first = origin.seal(SESSION, _payload("one"))

    downstream = ChainState()
    downstream.seal(SESSION, {**_payload("one"), **first.as_payload_fields()})
    second = downstream.seal(SESSION, _payload("two"))

    assert second.sequence_number == 2
    assert second.prev_action_hash == first.self_hash


def test_a_partially_sealed_payload_is_an_error() -> None:
    """Half client-assigned, half store-assigned identity verifies as nothing."""
    payload = _payload(action_id=str(uuid4()), sequence_number=1)

    with pytest.raises(ValueError, match="partially sealed"):
        ChainState().seal(SESSION, payload)


def test_a_seal_without_its_identity_is_an_error() -> None:
    payload = _payload(self_hash="c" * 64)

    with pytest.raises(ValueError, match="must be complete"):
        ChainState().seal(SESSION, payload)


def test_is_sealed_reads_self_hash_not_prev() -> None:
    # prev_action_hash is legitimately None on record #1, so it can never be
    # the sealed/unsealed discriminator.
    assert is_sealed(_payload()) is False
    assert is_sealed(_payload(prev_action_hash=None)) is False
    assert is_sealed(_payload(self_hash="c" * 64)) is True


def test_record_ids_come_from_one_patchable_point() -> None:
    """The property the parity harness depends on (T2.3)."""
    import rootsign.chain_state as chain_state

    pinned = UUID(int=42)
    original = chain_state.uuid4
    chain_state.uuid4 = lambda: pinned
    try:
        assert new_record_id() == pinned
        assert ChainState().seal(SESSION, _payload()).action_id == pinned
    finally:
        chain_state.uuid4 = original
