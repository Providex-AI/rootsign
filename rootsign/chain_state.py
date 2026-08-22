"""Client-side hash-chain construction — and the one place record ids are minted.

Two things live here, together on purpose.

**`ChainState` / `ChainRegistry`** hold the in-memory tail of a session's hash
chain so a client-side backend can assign `sequence_number` /
`prev_action_hash` and compute `self_hash` without a store. Postgres does this
under a row lock (`crud.action.create_with_hash`); the JSONL backend
(ADR-011 Decision 3) and the cloud transport (ADR-013 Decision 1) have no lock
and no store, so they do it here — through **one** implementation rather than
one per backend. The frozen formula itself is still `rootsign.hashing`; this
module only decides *what* gets fed into it.

**`new_record_id`** is the single minting point for record identity —
`action_id`, `decision_id`, `approval_id`, on every backend. That is a
testability property with teeth: `action_id` is inside the canonical hash, so
the cross-backend parity harness can only compare two chains byte-for-byte if
it can hold identity constant, and it can only do *that* if there is exactly
one place to pin. Before this module there were two (`sdk.jsonl_client` and
`crud.action`) and the harness patched both; a third backend would have meant
a third patch and a harness that silently proved less each time it grew. Pin
`rootsign.chain_state.uuid4` and every backend follows.

**Adoption.** `seal()` will *adopt* a seal a payload already carries instead of
minting a fresh one. Two paths need that and both are correctness, not
convenience: the cloud transport re-sends the same envelope after a retryable
rejection (re-sealing would burn a second sequence number and fork the chain),
and a spooled envelope written by the cloud client must reach the JSONL file
with the identity it was sealed under, or the offline verifier sees a chain
that never existed. A seal is minted once, at the point the action happened.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from rootsign.hashing import compute_action_self_hash

#: The chain fields a client-side sealer assigns. A payload carrying them is
#: already sealed; one carrying only some of them is a bug, not a partial seal.
SEALED_FIELDS = ("action_id", "sequence_number", "prev_action_hash", "self_hash")


def new_record_id() -> UUID:
    """Mint an id for a new record. The only `uuid4()` on any record path.

    Deterministic-identity tests patch `rootsign.chain_state.uuid4` — one
    target, every backend. See the module docstring for why that matters.
    """
    return uuid4()


@dataclass(frozen=True)
class SealedAction:
    """The four chain fields for one action, minted or adopted."""

    action_id: UUID
    sequence_number: int
    prev_action_hash: str | None
    self_hash: str
    #: True when the payload arrived already sealed and this state adopted it.
    adopted: bool = False

    def as_payload_fields(self) -> dict[str, Any]:
        """The seal in the shape an ACTION_RECORD payload carries it (spec §8.2)."""
        return {
            "action_id": str(self.action_id),
            "sequence_number": self.sequence_number,
            "prev_action_hash": self.prev_action_hash,
            "self_hash": self.self_hash,
        }


def is_sealed(payload: dict[str, Any]) -> bool:
    """True when a payload already carries a client-computed chain seal."""
    return payload.get("self_hash") is not None


class ChainState:
    """In-memory tail of one session's hash chain."""

    __slots__ = ("tail", "count")

    def __init__(self, tail: str | None = None, count: int = 0) -> None:
        self.tail = tail
        self.count = count

    def seal(self, session_id: str | UUID, payload: dict[str, Any]) -> SealedAction:
        """Return the chain fields for this action, advancing the chain.

        Mints `action_id`, `sequence_number = count + 1`, and
        `prev_action_hash = tail`, then computes `self_hash` with the frozen
        canonical formula. When `payload` already carries a seal, that seal is
        adopted verbatim and the state advances to match it — see the module
        docstring for the two paths that depend on this.

        Raises `ValueError` on a partially-sealed payload: silently completing
        one would mean a record whose identity is half client-assigned and half
        store-assigned, which no verifier could make sense of.
        """
        if is_sealed(payload):
            return self._adopt(payload)
        present = [f for f in SEALED_FIELDS if payload.get(f) is not None]
        if present:
            raise ValueError(
                f"partially sealed ACTION_RECORD payload: carries {sorted(present)} "
                f"but no self_hash. A seal is all four fields or none."
            )

        action_id = new_record_id()
        sequence_number = self.count + 1
        prev_action_hash = self.tail
        self_hash = compute_action_self_hash(
            {
                "action_id": action_id,
                "session_id": session_id,
                "tool_name": payload["tool_name"],
                "input_hash": payload["input_hash"],
                "output_hash": payload.get("output_hash"),
                "prev_action_hash": prev_action_hash,
                "timestamp": payload["timestamp"],
                "sequence_number": sequence_number,
            }
        )
        self.tail = self_hash
        self.count = sequence_number
        return SealedAction(
            action_id=action_id,
            sequence_number=sequence_number,
            prev_action_hash=prev_action_hash,
            self_hash=self_hash,
        )

    def _adopt(self, payload: dict[str, Any]) -> SealedAction:
        missing = [f for f in ("action_id", "sequence_number") if payload.get(f) is None]
        if missing:
            raise ValueError(
                f"ACTION_RECORD payload carries self_hash but not {sorted(missing)} — "
                f"an adopted seal must be complete."
            )
        sequence_number = int(payload["sequence_number"])
        self_hash = str(payload["self_hash"])
        # Advance to the adopted record so any *later* record this state seals
        # links onto it rather than onto a stale tail.
        self.tail = self_hash
        self.count = max(self.count, sequence_number)
        return SealedAction(
            action_id=UUID(str(payload["action_id"])),
            sequence_number=sequence_number,
            prev_action_hash=payload.get("prev_action_hash"),
            self_hash=self_hash,
            adopted=True,
        )


class ChainRegistry:
    """Per-session `ChainState`, keyed by session id.

    One registry per client. The cloud transport and its offline spool share
    an instance so a record keeps one identity whichever way it reaches
    durable storage (ADR-013 Decision 4).
    """

    def __init__(self) -> None:
        self._chains: dict[str, ChainState] = {}

    def state_for(self, session_id: str | UUID) -> ChainState:
        return self._chains.setdefault(str(session_id), ChainState())

    def seal(self, session_id: str | UUID, payload: dict[str, Any]) -> SealedAction:
        return self.state_for(session_id).seal(session_id, payload)
