"""Batch replay — push a list of already-recorded envelopes at a store, in order.

Two operator commands need this and neither owns it: `rootsign-admin sync`
uploads a spooled session to the cloud (ADR-013 Decision 4), and
`rootsign-admin replay-pending` will re-drive records the store never resolved.
The transports differ; the walk does not, so it lives here — at the package
root next to `verdict` and `chain_state`, importable without either optional
extra.

**Order is not a nicety.** These envelopes are a hash chain: record N+1 names
record N's `self_hash` as its parent. Uploading them out of order, or
continuing past a rejection, produces a chain on the far side that references
a record the store never took — which verifies as INCOMPLETE at best. So the
walk is strictly sequential and **stops at the first hard failure**, leaving
the source intact for a later attempt.

**`DUPLICATE_EVENT` is success.** Idempotency is by `event_id` server-side
(spec §7.5), so re-running after a partial upload re-sends records the store
already has. It answers `DUPLICATE_EVENT`, and treating that as failure would
make the second run of a resumed sync permanently unable to finish.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from rootsign.ingest.schemas import ErrorCode, IngestResponse

logger = logging.getLogger("rootsign.replay")

#: Envelopes per request. The spec allows 1..N; this keeps a large spooled
#: session off one enormous body without making the request count silly.
DEFAULT_BATCH_SIZE = 100


@dataclass
class ReplayReport:
    """What a replay achieved, and where it stopped if it did.

    `failed_index` is an index into the *envelope list* the caller passed, not
    into the batch that happened to contain it — the caller thinks in records,
    not in transport chunking.
    """

    total: int
    accepted: int = 0
    duplicates: int = 0
    failed_index: int | None = None
    error_code: ErrorCode | None = None
    error_message: str | None = None
    #: Responses for everything actually sent, index-aligned with the input up
    #: to `failed_index`. Kept so a caller can report per-record detail.
    responses: list[IngestResponse] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """True when every envelope was taken (or already present)."""
        return self.failed_index is None

    @property
    def delivered(self) -> int:
        """Records the store now holds because of this run, plus duplicates."""
        return self.accepted + self.duplicates


def _is_success(response: IngestResponse) -> bool:
    # A rejection the store issues because it already has the record is not a
    # failure of this replay — it is the outcome the replay wanted.
    return response.status == "accepted" or response.error_code is ErrorCode.DUPLICATE_EVENT


async def _send(client: Any, batch: list[dict[str, Any]]) -> list[IngestResponse]:
    """One request if the client batches, else one call per envelope.

    `handle_batch` stays duck-typed rather than joining the `IngestClient` ABC
    (ADR-013): the local and JSONL clients have no batch endpoint to offer and
    would only grow a loop pretending to be one. Probed here so replay works
    against any client, at whatever granularity that client actually supports.
    """
    batch_method = getattr(client, "handle_batch", None)
    if callable(batch_method):
        return list(await batch_method(batch))
    return [await client.handle(envelope) for envelope in batch]


async def replay_envelopes(
    client: Any,
    envelopes: Sequence[dict[str, Any]],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    on_progress: Callable[[int, IngestResponse], Awaitable[None] | None] | None = None,
) -> ReplayReport:
    """Send `envelopes` in order; stop at the first response that is not success.

    Never raises for a rejected record — the report carries the outcome, in
    keeping with ADR-002's rule that ingest failures are data, not exceptions.
    Transport-level exceptions are the client's business and pass through.
    """
    report = ReplayReport(total=len(envelopes))
    if not envelopes:
        return report

    size = max(1, int(batch_size))
    for start in range(0, len(envelopes), size):
        batch = list(envelopes[start : start + size])
        responses = await _send(client, batch)

        for offset, response in enumerate(responses):
            index = start + offset
            report.responses.append(response)
            if on_progress is not None:
                result = on_progress(index, response)
                if result is not None:
                    await result
            if response.status == "accepted":
                report.accepted += 1
                continue
            if response.error_code is ErrorCode.DUPLICATE_EVENT:
                report.duplicates += 1
                continue
            report.failed_index = index
            report.error_code = response.error_code
            report.error_message = response.error_message
            return report

        if len(responses) < len(batch):
            # A store that answers short is out of contract (spec §7.1 requires
            # index alignment). Treat the first unanswered envelope as failed
            # rather than assuming the silence meant yes.
            index = start + len(responses)
            report.failed_index = index
            report.error_code = ErrorCode.INTERNAL_ERROR
            report.error_message = (
                f"store returned {len(responses)} response(s) for a {len(batch)}-envelope batch"
            )
            return report

    return report
