"""Sync receipts — the durable record of a replay attempt (ADR-015).

Driven off a **real spool tree** and the real `rootsign-admin sync` command,
for the same reason `test_spool_sync.py` is: a receipt that only ever
describes fixture-built input proves the fixture agrees with itself.

The load-bearing property, asserted on every receipt this module produces, is
the partition:

    accepted ∪ duplicate ∪ rejected ∪ not_attempted == [1, record_count]

pairwise disjoint. It is what lets a reviewer confirm a receipt accounts for
every record in the file it describes without trusting whoever wrote it
(ADR-015 Decision 3a).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from rootsign.cli import app as admin_app
from rootsign.ingest.schemas import ErrorCode
from rootsign.replay import ReplayReport
from rootsign.sdk.receipt import (
    DISPOSITION_FULLY_ACCEPTED,
    DISPOSITION_PARTIAL,
    DISPOSITION_TRANSPORT_FAILED,
    RECEIPT_SCHEMA_VERSION,
    append_attempt,
    build_attempt,
    compress_ranges,
    find_receipt,
    load_receipt,
    partition_is_total,
    receipt_path,
)
from tests.support.spool_tree import Endpoint, bind_transport

runner = CliRunner()


def _sync(spool_dir: Path, *extra: str) -> Any:
    return runner.invoke(admin_app, ["sync", "--spool-dir", str(spool_dir), *extra])


def _receipt_for(session_id: str, spool_dir: Path) -> dict[str, Any]:
    found = find_receipt(session_id, spool_dir)
    assert found is not None, f"no receipt written for {session_id}"
    return load_receipt(found)


def _assert_well_formed(document: dict[str, Any]) -> None:
    """Every invariant a receipt must satisfy, applied to every attempt."""
    assert "session_id" in document
    for attempt in document["attempts"]:
        assert attempt["schema_version"] == RECEIPT_SCHEMA_VERSION
        assert partition_is_total(attempt), (
            f"attempt does not account for every record: {attempt['outcomes']} "
            f"over record_count={attempt['record_count']}"
        )
        assert len(attempt["spool_file_sha256"]) == 64


# ---------------------------------------------------------------------------
# Range compression — the primitive the partition rests on
# ---------------------------------------------------------------------------


class TestCompressRanges:
    @pytest.mark.parametrize(
        ("positions", "expected"),
        [
            ([], []),
            ([1], [[1, 1]]),
            ([1, 2, 3], [[1, 3]]),
            ([1, 2, 3, 7, 8], [[1, 3], [7, 8]]),
            ([3, 1, 2], [[1, 3]]),  # unsorted input
            ([1, 1, 2], [[1, 2]]),  # duplicates collapse
            ([2, 4, 6], [[2, 2], [4, 4], [6, 6]]),
        ],
    )
    def test_compression(self, positions: list[int], expected: list[list[int]]):
        assert compress_ranges(positions) == expected

    def test_emits_lists_not_tuples(self):
        """A receipt read back from JSON must equal the one written."""
        ranges = compress_ranges([1, 2])
        assert json.loads(json.dumps(ranges)) == ranges


class TestPartitionCheck:
    def test_a_gap_is_caught(self):
        attempt = {
            "record_count": 5,
            "outcomes": {
                "accepted": [[1, 3]],
                "duplicate": [],
                "rejected": [],
                "not_attempted": [],
            },
        }
        assert partition_is_total(attempt) is False

    def test_an_overlap_is_caught(self):
        """Two buckets claiming the same record must fail, not de-duplicate."""
        attempt = {
            "record_count": 3,
            "outcomes": {
                "accepted": [[1, 3]],
                "duplicate": [[2, 2]],
                "rejected": [],
                "not_attempted": [],
            },
        }
        assert partition_is_total(attempt) is False


# ---------------------------------------------------------------------------
# Building an attempt from a ReplayReport
# ---------------------------------------------------------------------------


def _response(status: str, **kw: Any) -> Any:
    from uuid import uuid4

    from rootsign.ingest.schemas import IngestResponse

    return IngestResponse(status=status, event_id=uuid4(), **kw)


def _envelopes(n: int) -> list[dict[str, Any]]:
    return [{"payload": {"sequence_number": i + 1}} for i in range(n)]


class TestBuildAttempt:
    def test_records_after_the_halt_are_not_attempted(self):
        """The bucket whose absence was the defect in the v1.0 draft."""
        report = ReplayReport(total=5, accepted=2, failed_index=2)
        report.responses = [
            _response("accepted"),
            _response("accepted"),
            _response("rejected", error_code=ErrorCode.HASH_CHAIN_BROKEN, retryable=False),
        ]
        attempt = build_attempt(
            envelopes=_envelopes(5),
            report=report,
            endpoint="https://e.test",
            spool_file_sha256="a" * 64,
            started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        assert attempt["outcomes"]["accepted"] == [[1, 2]]
        assert attempt["outcomes"]["not_attempted"] == [[4, 5]]
        assert attempt["disposition"] == DISPOSITION_PARTIAL
        assert partition_is_total(attempt)

    def test_rejected_entries_carry_code_retryability_and_sequence(self):
        """Issue #37 asked for retryability; the wire already had it."""
        report = ReplayReport(total=2, failed_index=0)
        report.responses = [
            _response(
                "rejected",
                error_code=ErrorCode.VALIDATION_ERROR,
                error_message="bad",
                retryable=False,
            )
        ]
        attempt = build_attempt(
            envelopes=_envelopes(2),
            report=report,
            endpoint="https://e.test",
            spool_file_sha256="a" * 64,
            started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        (entry,) = attempt["outcomes"]["rejected"]
        assert entry["record"] == 1
        assert entry["sequence"] == 1
        assert entry["error_code"] == "VALIDATION_ERROR"
        assert entry["retryable"] is False
        assert attempt["halted_at_sequence"] == 1

    def test_answers_after_the_refusal_in_the_same_batch_are_recorded(self):
        """ADR-015 Decision 2: fail-fast is drawn at the BATCH boundary.

        Spec §7 rule 3 forbids a rejection from aborting later elements, and
        §8.3 forbids rejecting a record for a `prev_action_hash` the store has
        never seen — so the elements after a refusal are processed normally,
        and normally means accepted. Those acceptances are real: the store
        now holds those records. Discarding the responses (the pre-ADR-015
        behaviour) would file them as `not_attempted` and under-report what
        was actually delivered.
        """
        report = ReplayReport(total=6, accepted=3, failed_index=1)
        report.responses = [
            _response("accepted"),
            _response("rejected", error_code=ErrorCode.VALIDATION_ERROR, retryable=False),
            _response("accepted"),
            _response("accepted"),
        ]
        attempt = build_attempt(
            envelopes=_envelopes(6),
            report=report,
            endpoint="https://e.test",
            spool_file_sha256="a" * 64,
            started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        # The two accepted AFTER the refusal are recorded as accepted.
        assert attempt["outcomes"]["accepted"] == [[1, 1], [3, 4]]
        assert [e["record"] for e in attempt["outcomes"]["rejected"]] == [2]
        assert attempt["halted_at_sequence"] == 2
        # Only what was never sent — the batch after this one.
        assert attempt["outcomes"]["not_attempted"] == [[5, 6]]
        assert partition_is_total(attempt)

    def test_transport_failure_observes_nothing(self):
        report = ReplayReport(total=4)
        attempt = build_attempt(
            envelopes=_envelopes(4),
            report=report,
            endpoint="https://e.test",
            spool_file_sha256="a" * 64,
            started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            transport_error="ConnectError: dead",
        )
        assert attempt["disposition"] == DISPOSITION_TRANSPORT_FAILED
        assert attempt["transport_error"] == "ConnectError: dead"
        assert attempt["outcomes"]["not_attempted"] == [[1, 4]]
        assert partition_is_total(attempt)


# ---------------------------------------------------------------------------
# The file: append-only, never overwritten
# ---------------------------------------------------------------------------


class TestReceiptFile:
    def test_a_second_attempt_appends_rather_than_replacing(self, spool: Path):
        for _ in range(2):
            append_attempt("s1", {"attempt_id": "x", "outcomes": {}}, spool)
        document = load_receipt(receipt_path("s1", spool))
        assert len(document["attempts"]) == 2

    def test_an_unreadable_receipt_is_moved_aside_not_overwritten(self, spool: Path):
        """Overwriting a corrupt record of evidence handling to make room for
        a clean one is the move this module exists to prevent."""
        path = receipt_path("s1", spool)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json")

        written, _ = append_attempt("s1", {"attempt_id": "x", "outcomes": {}}, spool)

        assert path.read_text() == "{ not json", "the corrupt original was destroyed"
        assert written != path
        assert "corrupt" in written.name

    def test_the_printed_hash_matches_the_bytes_on_disk(self, spool: Path):
        from rootsign.sdk.receipt import sha256_text

        path, digest = append_attempt("s1", {"attempt_id": "x", "outcomes": {}}, spool)
        assert digest == sha256_text(path.read_text())


# ---------------------------------------------------------------------------
# End to end, through the real CLI
# ---------------------------------------------------------------------------


class TestSyncWritesReceipts:
    def test_a_clean_sync_records_fully_accepted_and_follows_the_file(
        self, monkeypatch, spool: Path, spooled_session, cloud_api_key
    ):
        session_id, _ = spooled_session
        bind_transport(monkeypatch, Endpoint().transport())

        result = _sync(spool)
        assert result.exit_code == 0

        document = _receipt_for(session_id, spool)
        _assert_well_formed(document)
        (attempt,) = document["attempts"]
        assert attempt["disposition"] == DISPOSITION_FULLY_ACCEPTED
        assert attempt["halted_at_sequence"] is None
        assert attempt["outcomes"]["not_attempted"] == []

        # Followed the exhibit into synced/ (ADR-015 Decision 1).
        assert receipt_path(session_id, spool, synced=True).is_file()
        assert not receipt_path(session_id, spool).exists()

    def test_a_rejected_sync_records_partial_and_leaves_both_in_place(
        self, monkeypatch, spool: Path, spooled_session, cloud_api_key
    ):
        session_id, path = spooled_session
        bind_transport(monkeypatch, Endpoint(reject_at_sequence=2).transport())

        result = _sync(spool)
        assert result.exit_code == 1

        document = _receipt_for(session_id, spool)
        _assert_well_formed(document)
        (attempt,) = document["attempts"]
        assert attempt["disposition"] == DISPOSITION_PARTIAL
        assert attempt["halted_at_sequence"] == 2
        (rejected,) = [e for e in attempt["outcomes"]["rejected"] if e["sequence"] == 2]
        assert rejected["error_code"] == "VALIDATION_ERROR"
        assert rejected["retryable"] is False

        # The exhibit and its record both stay put.
        assert path.exists()
        assert receipt_path(session_id, spool).is_file()

    def test_the_operator_is_told_rejected_not_incomplete(
        self, monkeypatch, spool: Path, spooled_session, cloud_api_key
    ):
        """INCOMPLETE is the verdict word for 'a record is missing'. A record
        the store refused is the opposite — observed, and declined (ADR-015
        Decision 7)."""
        bind_transport(monkeypatch, Endpoint(reject_at_sequence=2).transport())

        result = _sync(spool)

        assert "REJECTED" in result.output
        assert "INCOMPLETE" not in result.output

    def test_the_receipt_path_and_hash_are_printed(
        self, monkeypatch, spool: Path, spooled_session, cloud_api_key
    ):
        """The digest is only worth anything noted out of band (ADR-014 D2)."""
        session_id, _ = spooled_session
        bind_transport(monkeypatch, Endpoint().transport())

        result = _sync(spool)

        assert "receipt" in result.output
        assert "receipt SHA-256" in result.output

    def test_re_running_after_a_fix_appends_attempt_two(
        self, monkeypatch, spool: Path, spooled_session, cloud_api_key
    ):
        """The history of handling survives rather than being overwritten."""
        session_id, _ = spooled_session

        bind_transport(monkeypatch, Endpoint(reject_at_sequence=2).transport())
        assert _sync(spool).exit_code == 1

        # Second run against a healthy endpoint: records already taken come
        # back DUPLICATE_EVENT, the rest are accepted.
        endpoint = Endpoint()
        bind_transport(monkeypatch, endpoint.transport())
        assert _sync(spool).exit_code == 0

        document = _receipt_for(session_id, spool)
        _assert_well_formed(document)
        assert len(document["attempts"]) == 2
        first, second = document["attempts"]
        assert first["disposition"] == DISPOSITION_PARTIAL
        assert second["disposition"] == DISPOSITION_FULLY_ACCEPTED
        assert second["halted_at_sequence"] is None

    def test_an_all_duplicate_replay_is_distinguishable_from_a_first_upload(
        self, monkeypatch, spool: Path, spooled_session, cloud_api_key
    ):
        """Forensically different events; the receipt is where that survives.

        Both dispositions are `fully_accepted` — the difference lives in the
        outcome buckets, which is exactly why DUPLICATE_EVENT is recorded
        distinctly rather than folded into `accepted` (ADR-015 Decision 3).
        """
        session_id, path = spooled_session
        endpoint = Endpoint()
        bind_transport(monkeypatch, endpoint.transport())
        assert _sync(spool).exit_code == 0

        first = _receipt_for(session_id, spool)["attempts"][0]
        assert first["outcomes"]["duplicate"] == []
        assert first["outcomes"]["accepted"] != []

        # Restore the exhibit and replay it into the same store.
        moved = spool / "synced" / f"{session_id}.jsonl"
        moved.replace(path)
        from rootsign.sdk.receipt import receipt_path as rp

        rp(session_id, spool, synced=True).replace(rp(session_id, spool))

        bind_transport(monkeypatch, endpoint.transport())
        assert _sync(spool).exit_code == 0

        second = _receipt_for(session_id, spool)["attempts"][1]
        assert second["outcomes"]["accepted"] == []
        assert second["outcomes"]["duplicate"] != []
        assert second["disposition"] == DISPOSITION_FULLY_ACCEPTED

    def test_a_whole_batch_answer_is_recorded_end_to_end(
        self, monkeypatch, spool: Path, spooled_session, cloud_api_key
    ):
        """ADR-015 Decision 2, through the real command.

        The session fits in one batch, so the endpoint answers all five
        elements index-aligned: the refusal at sequence 2, and — per spec §7
        rule 3 and §8.3 — normal processing for everything after it. All five
        are observations, so nothing is `not_attempted`.

        Before fail-fast moved to the batch boundary, the three answers after
        the refusal were discarded and the receipt would have claimed those
        records went unattempted while the store already held them.
        """
        session_id, _ = spooled_session
        bind_transport(monkeypatch, Endpoint(reject_at_sequence=2).transport())

        assert _sync(spool).exit_code == 1

        document = _receipt_for(session_id, spool)
        _assert_well_formed(document)
        (attempt,) = document["attempts"]

        assert [e["sequence"] for e in attempt["outcomes"]["rejected"]] == [2]
        assert attempt["halted_at_sequence"] == 2
        assert attempt["outcomes"]["not_attempted"] == []
        assert attempt["disposition"] == DISPOSITION_PARTIAL
        # Everything the store did take is on the record.
        assert attempt["outcomes"]["accepted"] != []

    def test_dry_run_observes_nothing_so_writes_nothing(
        self, monkeypatch, spool: Path, spooled_session, cloud_api_key
    ):
        session_id, _ = spooled_session

        result = _sync(spool, "--dry-run")

        assert "Dry run" in result.output
        assert find_receipt(session_id, spool) is None

    def test_a_transport_failure_is_still_recorded(
        self, monkeypatch, spool: Path, spooled_session, cloud_api_key
    ):
        """ "We tried and the network died" is a fact about evidence handling."""
        import httpx

        session_id, _ = spooled_session

        def _dead(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        bind_transport(monkeypatch, httpx.MockTransport(_dead))

        result = _sync(spool)
        assert result.exit_code == 1

        document = _receipt_for(session_id, spool)
        _assert_well_formed(document)
        (attempt,) = document["attempts"]
        assert attempt["disposition"] == DISPOSITION_TRANSPORT_FAILED
        assert "transport_error" in attempt


# ---------------------------------------------------------------------------
# The bundle (ADR-015 Decision 5)
# ---------------------------------------------------------------------------


class TestBundleIntegration:
    def test_a_session_with_no_receipt_gets_no_replay_key_at_all(self, tmp_path: Path):
        """Absent, not null — this is what keeps the addition additive under
        bundle v1.0, and what the golden fixture pins."""
        from rootsign.sdk.export import RECEIPT_FILE, export_local
        from tests.support.session_files import write_session_file

        path = write_session_file(tmp_path / "plain.jsonl", actions=2)
        bundle = export_local(path)

        assert "replay" not in bundle.verification
        assert RECEIPT_FILE not in bundle.documents()

    def test_a_synced_session_carries_the_receipt_and_the_replay_block(
        self, monkeypatch, spool: Path, spooled_session, cloud_api_key
    ):
        from rootsign.sdk.export import RECEIPT_FILE, export_local

        session_id, path = spooled_session
        bind_transport(monkeypatch, Endpoint(reject_at_sequence=2).transport())
        assert _sync(spool).exit_code == 1  # leaves the file in place to export

        bundle = export_local(path)

        assert RECEIPT_FILE in bundle.documents()
        replay = bundle.verification["replay"]
        assert replay["latest_disposition"] == DISPOSITION_PARTIAL
        assert replay["halted_at_sequence"] == 2
        assert replay["observed_and_rejected"]

    def test_the_manifest_hashes_the_receipt_like_every_other_file(
        self, monkeypatch, spool: Path, spooled_session, cloud_api_key
    ):
        from rootsign.sdk.export import RECEIPT_FILE, export_local, sha256_text

        session_id, path = spooled_session
        bind_transport(monkeypatch, Endpoint(reject_at_sequence=2).transport())
        _sync(spool)

        bundle = export_local(path)
        files = bundle.files()

        assert bundle.manifest["files"][RECEIPT_FILE] == sha256_text(files[RECEIPT_FILE])

    def test_check_covers_the_receipt(
        self, monkeypatch, tmp_path: Path, spool: Path, spooled_session, cloud_api_key
    ):
        """A tampered receipt must fail `--check` exactly like a tampered
        timeline — it is in the manifest, so it is under the seal."""
        from rootsign.sdk.export import RECEIPT_FILE, check_bundle, export_local, write_bundle

        session_id, path = spooled_session
        bind_transport(monkeypatch, Endpoint(reject_at_sequence=2).transport())
        _sync(spool)

        directory = write_bundle(export_local(path), tmp_path / "out")
        assert check_bundle(directory).intact

        receipt_file = Path(directory) / RECEIPT_FILE
        document = json.loads(receipt_file.read_text())
        document["attempts"][0]["disposition"] = DISPOSITION_FULLY_ACCEPTED
        receipt_file.write_text(json.dumps(document, indent=2) + "\n")

        result = check_bundle(directory)
        assert not result.intact
        assert RECEIPT_FILE in result.altered
