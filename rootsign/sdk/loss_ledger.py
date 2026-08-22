"""Accounting for records that could not be persisted (ADR-013 Decision 4a).

The spool is the last durable destination. When *it* fails — disk full,
read-only filesystem, revoked permissions — a telemetry record is genuinely
lost, and ADR-002's isolation rule still says the agent must keep running. The
one failure mode this product must not have is losing a record *silently*, so a
loss leaves three marks instead:

1. **A CRITICAL log line**, once per session, at the first loss.
2. **This ledger** — how many records, which sequence range, and why — re-logged
   when the session closes and appended to the session file if writability
   comes back.
3. **A hole in the hash chain.** `ChainState` advances before the write is
   attempted, so the next record that *does* persist references a predecessor
   that was never written. That discontinuity is not a bug to be papered over;
   it is the evidence, and it is the only one of the three an attacker cannot
   delete without breaking the chain. Logs can be rotated and this ledger lives
   in memory — the chain testifies on its own.

The ledger is deliberately small: counts, a sequence range, and a reason
histogram. It is not a queue and holds no payloads — records that could not be
written are gone, and pretending otherwise by buffering them in RAM would just
move the loss to the next crash while implying the data is safe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

#: Line type of the annotation written into a session file at close. It is a
#: *file* annotation, not an ingest event — `EventType` is unchanged, and both
#: verifiers filter it out (they rebuild the chain from ACTION_RECORD lines).
LOSS_RECORD_EVENT_TYPE = "RECORD_LOSS"


@dataclass
class LossLedger:
    """What was lost for one session, and why."""

    session_id: str
    count: int = 0
    first_sequence: int | None = None
    last_sequence: int | None = None
    #: reason -> how many records were lost to it
    reasons: dict[str, int] = field(default_factory=dict)
    first_at: datetime | None = None
    last_at: datetime | None = None

    @property
    def is_empty(self) -> bool:
        return self.count == 0

    def record(self, *, sequence_number: int | None, reason: str) -> bool:
        """Account for one lost record. Returns True if this was the first.

        The caller uses the return value to log CRITICAL exactly once — a
        session that spends an hour writing to a full disk should leave one
        alarming line and a final tally, not thousands of identical lines that
        bury everything else in the operator's log.
        """
        now = datetime.now(timezone.utc)
        first = self.count == 0
        self.count += 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1
        self.last_at = now
        if first:
            self.first_at = now
        if sequence_number is not None:
            if self.first_sequence is None:
                self.first_sequence = sequence_number
            self.last_sequence = sequence_number
        return first

    @property
    def sequence_range(self) -> str:
        if self.first_sequence is None:
            return "n/a"
        if self.first_sequence == self.last_sequence:
            return str(self.first_sequence)
        return f"{self.first_sequence}-{self.last_sequence}"

    def summary(self) -> str:
        """One line an operator can act on."""
        reasons = ", ".join(f"{reason} (x{n})" for reason, n in sorted(self.reasons.items()))
        return (
            f"{self.count} record(s) lost, sequence {self.sequence_range}; "
            f"cause: {reasons or 'unknown'}"
        )

    def as_record(self) -> dict[str, Any]:
        """The annotation line appended to the session file.

        Chain fields are deliberately absent: this line documents records that
        were never hashed into anything. Its own integrity is not protected —
        which is why the chain gap, not this line, is the load-bearing
        evidence.
        """
        return {
            "event_type": LOSS_RECORD_EVENT_TYPE,
            "session_id": self.session_id,
            "lost_count": self.count,
            "first_sequence": self.first_sequence,
            "last_sequence": self.last_sequence,
            "reasons": dict(sorted(self.reasons.items())),
            "first_loss_at": self.first_at.isoformat() if self.first_at else None,
            "last_loss_at": self.last_at.isoformat() if self.last_at else None,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
