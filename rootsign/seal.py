"""The session seal (ADR-016): what makes a session's length and bounds verifiable.

The action chain (ADR-001) proves every action it holds; it cannot prove what
it does not hold. The seal, written on `SESSION_CLOSE`, states how long the
chain is, which record ends it, what the session was opened for, how it ended,
and (as a digest) the side records that sit outside the chain: decisions,
approvals, and each action's final `authorization_status` and `decision_id`.

**One module, never re-implemented.** This file is the seal's specification,
under the same rule as `rootsign.hashing`: every writer (the JSONL writer, the
PostgreSQL handler, the cloud transport) and every verifier imports it, and the
golden vectors in `tests/fixtures/session-seal/v1/` pin its output. A second
implementation (the TypeScript SDK, the hosted backend) reproduces the vectors;
it does not port this code by reading it.

**Canonical form.** Every digest is the SHA-256 of `json.dumps(value,
sort_keys=True, ensure_ascii=True)`, the form ADR-001 hashes an action with.
Values are JSON-native; ids are strings.

**What is deliberately not sealed.** Fields the stores record differently, so
that a client's seal and a store's would disagree without any tampering: the
session's open and close times (PostgreSQL stamps server time, JSONL keeps the
SDK's), side-record timestamps, and `SESSION_CLOSE.error_message` (PostgreSQL
does not store it).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any
from uuid import UUID

#: Bumped only with a new ADR. A verifier treats any other version as unsealed.
SEAL_VERSION = 1

#: The fields each side record contributes to `side_digest`, in no particular
#: order (the canonical form sorts keys). Absent fields hash as null.
DECISION_FIELDS = (
    "decision_id",
    "selected_action",
    "confidence",
    "reasoning_depth",
    "reasoning_summary",
    "alternatives_considered",
    "inputs_summary",
)
APPROVAL_FIELDS = (
    "approval_id",
    "action_id",
    "decision",
    "decision_reason",
    "approver_id",
    "approver_type",
    "context_presented",
    "response_latency_ms",
    "parent_approval_id",
)
ACTION_FIELDS = ("action_id", "sequence_number", "authorization_status", "decision_id")

#: The keys of a seal, in the order a writer emits them.
SEAL_FIELDS = ("version", "final_sequence", "tail_hash", "open_hash", "side_digest", "seal_hash")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _plain(value: Any) -> Any:
    """Ids as strings, everything else as stored. A UUID and its string hash alike."""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _project(record: Mapping[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {name: _plain(record.get(name)) for name in fields}


def open_hash(
    *,
    session_id: str | UUID,
    agent_id: str | UUID | None,
    objective: str | None,
    user_id: str | None,
) -> str:
    """Digest of what the session was opened as."""
    return _digest(
        {
            "session_id": _plain(session_id),
            "agent_id": _plain(agent_id),
            "objective": objective,
            "user_id": user_id,
        }
    )


def side_digest(
    *,
    decisions: Iterable[Mapping[str, Any]],
    approvals: Iterable[Mapping[str, Any]],
    actions: Iterable[Mapping[str, Any]],
) -> str:
    """Digest of the side records, independent of the order they are passed in.

    Decisions sort by `decision_id`, approvals by `approval_id`, actions by
    `sequence_number`, so a writer that holds them in write order and a
    verifier that reads them in query order agree.
    """
    return _digest(
        {
            "decisions": sorted(
                (_project(d, DECISION_FIELDS) for d in decisions),
                key=lambda e: str(e["decision_id"]),
            ),
            "approvals": sorted(
                (_project(a, APPROVAL_FIELDS) for a in approvals),
                key=lambda e: str(e["approval_id"]),
            ),
            "actions": sorted(
                (_project(a, ACTION_FIELDS) for a in actions),
                key=lambda e: e["sequence_number"],
            ),
        }
    )


def _seal_hash(seal: Mapping[str, Any], *, session_id: str | UUID, status: str | None) -> str:
    body = {name: seal.get(name) for name in SEAL_FIELDS if name != "seal_hash"}
    return _digest({**body, "session_id": _plain(session_id), "status": status})


@dataclass(frozen=True)
class SessionFacts:
    """Everything a seal covers, as one store holds it."""

    session_id: str | UUID
    agent_id: str | UUID | None
    objective: str | None
    user_id: str | None
    status: str | None
    decisions: tuple[Mapping[str, Any], ...] = ()
    approvals: tuple[Mapping[str, Any], ...] = ()
    #: Every action, each with at least ACTION_FIELDS and `self_hash`.
    actions: tuple[Mapping[str, Any], ...] = ()

    @property
    def chain_length(self) -> int:
        return len(self.actions)

    @property
    def chain_tail(self) -> str | None:
        if not self.actions:
            return None
        return max(self.actions, key=lambda a: a["sequence_number"]).get("self_hash")


def compute_seal(facts: SessionFacts, *, final_sequence: int | None = None) -> dict[str, Any]:
    """The seal a writer puts on `SESSION_CLOSE`.

    `final_sequence` is the SDK's own count of actions (`SESSION_CLOSE`'s
    `metadata.total_actions`) when the writer has it: the independent
    statement of how long the chain should be. Without it, the writer's count.
    """
    seal: dict[str, Any] = {
        "version": SEAL_VERSION,
        "final_sequence": facts.chain_length if final_sequence is None else int(final_sequence),
        "tail_hash": facts.chain_tail,
        "open_hash": open_hash(
            session_id=facts.session_id,
            agent_id=facts.agent_id,
            objective=facts.objective,
            user_id=facts.user_id,
        ),
        "side_digest": side_digest(
            decisions=facts.decisions, approvals=facts.approvals, actions=facts.actions
        ),
    }
    seal["seal_hash"] = _seal_hash(seal, session_id=facts.session_id, status=facts.status)
    return seal


class SealOutcome(str, Enum):
    """What checking a seal against the records found."""

    SEALED = "sealed"
    UNSEALED = "unsealed"
    UNKNOWN_VERSION = "unknown_version"
    MISSING_AT_END = "missing_at_end"
    RECORDS_AFTER_SEAL = "records_after_seal"
    TAIL_MISMATCH = "tail_mismatch"
    OPEN_EDITED = "open_edited"
    SIDE_EDITED = "side_edited"
    SEAL_EDITED = "seal_edited"


#: Outcomes that mean a record is missing rather than altered.
INCOMPLETE_OUTCOMES = frozenset({SealOutcome.MISSING_AT_END})
#: Outcomes that mean something was altered.
TAMPERED_OUTCOMES = frozenset(
    {
        SealOutcome.RECORDS_AFTER_SEAL,
        SealOutcome.TAIL_MISMATCH,
        SealOutcome.OPEN_EDITED,
        SealOutcome.SIDE_EDITED,
        SealOutcome.SEAL_EDITED,
    }
)


@dataclass(frozen=True)
class SealCheck:
    outcome: SealOutcome
    detail: str
    #: For MISSING_AT_END: the inclusive range of sequences missing at the end.
    missing: tuple[int, int] | None = None

    @property
    def sealed(self) -> bool:
        return self.outcome is SealOutcome.SEALED


def check_seal(seal: Mapping[str, Any] | None, facts: SessionFacts) -> SealCheck:
    """Recompute a seal from the records it covers and say what differs.

    `facts` must be the whole session as one store holds it. The caller decides
    what the outcome means for the verdict (ADR-016 Decision 3); this function
    only compares, in an order that never lets a missing record hide an edit
    it could still see: the seal itself, then the open fields, then the
    length and tail, then the side records (which need the whole chain).
    """
    if not seal:
        return SealCheck(SealOutcome.UNSEALED, "not sealed: its end cannot be verified")
    if seal.get("version") != SEAL_VERSION:
        return SealCheck(
            SealOutcome.UNKNOWN_VERSION,
            f"seal version {seal.get('version')!r} is not one this RootSign checks",
        )

    if seal.get("seal_hash") != _seal_hash(seal, session_id=facts.session_id, status=facts.status):
        return SealCheck(
            SealOutcome.SEAL_EDITED,
            "seal does not recompute: the seal or the close status was edited",
        )

    # The open fields do not depend on the actions, so an edit to them is
    # reported even when records are also missing: TAMPERED wins.
    if seal.get("open_hash") != open_hash(
        session_id=facts.session_id,
        agent_id=facts.agent_id,
        objective=facts.objective,
        user_id=facts.user_id,
    ):
        return SealCheck(SealOutcome.OPEN_EDITED, "the session open record was edited")

    final = seal.get("final_sequence")
    length = facts.chain_length
    if not isinstance(final, int):
        return SealCheck(SealOutcome.SEAL_EDITED, "seal has no final_sequence")
    if length < final:
        return SealCheck(
            SealOutcome.MISSING_AT_END,
            f"{final - length} record(s) missing at the end (sealed at {final}, {length} "
            "present); decisions and approvals could not be checked",
            missing=(length + 1, final),
        )
    if length > final:
        return SealCheck(
            SealOutcome.RECORDS_AFTER_SEAL,
            f"{length - final} record(s) after the seal (sealed at {final}, {length} present)",
        )
    if seal.get("tail_hash") != facts.chain_tail:
        return SealCheck(SealOutcome.TAIL_MISMATCH, "the last record is not the one the seal names")

    if seal.get("side_digest") != side_digest(
        decisions=facts.decisions, approvals=facts.approvals, actions=facts.actions
    ):
        return SealCheck(
            SealOutcome.SIDE_EDITED,
            "a decision, an approval, or an action's authorization_status or decision_id was edited",
        )
    return SealCheck(SealOutcome.SEALED, f"sealed at {final} record(s)")


#: The fields an action record contributes to a seal: its side-entry fields and
#: the `self_hash` the tail is read from.
ACTION_SEAL_FIELDS = (*ACTION_FIELDS, "self_hash")


def jsonl_session_facts(
    records: Iterable[Mapping[str, Any]], *, status: str | None
) -> SessionFacts:
    """Facts from session-file records, the shape the JSONL writer appends.

    The JSONL writer, the cloud transport (which keeps the same shape for what
    it sends) and the offline verifier all read a session through this one
    function, so a seal one of them writes is a seal another can check.

    `SESSION_OPEN`, `DECISION_RECORD` and `APPROVAL_RECORD` lines are envelopes
    with a nested `payload` (the decision or approval id sits beside it); an
    `ACTION_RECORD` line is flat. A line with no `event_type` is an action, as
    in store exports. `status` is the close status, passed separately because a
    writer seals before its close line exists.
    """
    session_id: Any = None
    agent_id: Any = None
    objective: str | None = None
    user_id: str | None = None
    decisions: list[dict[str, Any]] = []
    approvals: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    opened = False
    for record in records:
        event_type = record.get("event_type", "ACTION_RECORD")
        session_id = session_id or record.get("session_id")
        payload = record.get("payload") or {}
        if event_type == "SESSION_OPEN" and not opened:
            opened = True
            agent_id = record.get("agent_id")
            objective = payload.get("objective")
            user_id = payload.get("user_id")
        elif event_type == "ACTION_RECORD":
            actions.append({name: record.get(name) for name in ACTION_SEAL_FIELDS})
        elif event_type == "DECISION_RECORD":
            decisions.append({**payload, "decision_id": record.get("decision_id")})
        elif event_type == "APPROVAL_RECORD":
            approvals.append({**payload, "approval_id": record.get("approval_id")})
    return SessionFacts(
        session_id=session_id,
        agent_id=agent_id,
        objective=objective,
        user_id=user_id,
        status=status,
        decisions=tuple(decisions),
        approvals=tuple(approvals),
        actions=tuple(actions),
    )


def total_actions(close_payload: Mapping[str, Any]) -> int | None:
    """The SDK's own count from a `SESSION_CLOSE` payload, when it sent one."""
    metadata = close_payload.get("metadata") or {}
    count = metadata.get("total_actions") if isinstance(metadata, Mapping) else None
    return count if isinstance(count, int) and not isinstance(count, bool) else None
