"""Verdict parity across both verifiers (ADR-013 Decision 4b, Sprint B T2.4c).

T2.3 proved the backends feed SHA-256 identical bytes. This file applies the
same principle one level up: the same damage, done to the same session in both
stores, must produce the same **conclusion**. Byte-identity at the input is
worth little if the two verifiers then disagree about what those bytes mean —
an auditor who gets VALID from `rootsign verify <id>` and TAMPERED from
`rootsign verify --local` on the same session has no verifier at all.

There are two implementations of the walk on purpose (`sdk.chain` is DB-free
core; `crud.action.verify_chain` lives behind the `postgres` extra, and neither
may import the other), so drift is a live risk. What they *do* share is
`rootsign.verdict` — gap detection, the explained-break rule, and the
precedence function. These tests pin that the shared vocabulary is actually
reached from both sides, including the case the precedence rule exists for:

* a gap alone reads INCOMPLETE, not TAMPERED, even though the record after a
  gap has a `prev_action_hash` no surviving record can match;
* an alteration alone reads TAMPERED;
* **both** reads TAMPERED — worst verdict wins — with the gaps still reported;
* a record altered *after* a gap is still caught, i.e. the verifiers re-anchor
  rather than stopping at the first explained break. A dropped record must not
  become a way to hide an edited one.

Plus the three CLI exit codes (0/1/2) at **both** verify sites, since that is
the surface CI actually consumes.

`seeded_agent` (committed) throughout: the CLI path runs through
`asyncio.to_thread` against its own engine and cannot see SAVEPOINT data
(Flag 3).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from typer.testing import CliRunner

from rootsign.config import settings
from rootsign.crud import action as action_crud
from rootsign.models.action import Action
from rootsign.sdk.chain import verify_session_local
from rootsign.sdk.cli import app
from rootsign.sdk.client import LocalIngestClient
from rootsign.sdk.hashing import compute_payload_hash
from rootsign.sdk.jsonl_client import JsonlIngestClient
from rootsign.verdict import EXIT_CODES, Verdict
from tests.conftest import make_envelope

runner = CliRunner()

#: How many actions the scripted session writes. Six leaves room for a gap in
#: the middle and an alteration well after it, which is the case that catches a
#: verifier that stops walking at the first explained break.
ACTION_COUNT = 6

TAMPERED_TOOL = "ATTACKER_RENAMED_THIS"


def _scripted_envelopes(agent_id: UUID, session_id: UUID) -> list[dict]:
    """SESSION_OPEN -> ACTION_COUNT actions -> SESSION_CLOSE.

    `tool_name` is canonical (inside `self_hash`), so rewriting it is the
    smallest edit that both verifiers must catch.
    """
    envelopes = [make_envelope("SESSION_OPEN", agent_id, session_id, {"objective": "verdict"})]
    for i in range(ACTION_COUNT):
        redacted = {"tool": f"tool_{i}", "i": i}
        envelopes.append(
            make_envelope(
                "ACTION_RECORD",
                agent_id,
                session_id,
                {
                    "tool_name": f"tool_{i}",
                    "input_hash": compute_payload_hash(redacted),
                    "output_hash": "b" * 64,
                    "input_redacted": redacted,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "authorization_status": "auto_authorized",
                },
            )
        )
    envelopes.append(make_envelope("SESSION_CLOSE", agent_id, session_id, {"status": "completed"}))
    return envelopes


# ---------------------------------------------------------------------------
# Damage, applied identically to each store
# ---------------------------------------------------------------------------


def _damage_jsonl(path: Path, *, drop: set[int], tamper: set[int]) -> None:
    """Delete and/or rewrite records in a session file, by sequence number."""
    kept: list[str] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("event_type", "ACTION_RECORD") != "ACTION_RECORD":
            kept.append(line)
            continue
        sequence = record["sequence_number"]
        if sequence in drop:
            continue
        if sequence in tamper:
            record["tool_name"] = TAMPERED_TOOL
            line = json.dumps(record)
        kept.append(line)
    path.write_text("\n".join(kept) + "\n")


async def _damage_postgres(db, session_id: UUID, *, drop: set[int], tamper: set[int]) -> None:
    """The same edits against the hypertable.

    Every lookup is the two-column `(action_id, timestamp)` form — `actions` is
    partitioned by `timestamp`, so a single-column `action_id` predicate scans
    every chunk (CLAUDE.md test invariant 5).
    """
    rows = (
        (
            await db.execute(
                select(Action).where(
                    Action.session_id == session_id,
                    Action.sequence_number.in_(sorted(drop | tamper)),
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        pk = (Action.action_id == row.action_id, Action.timestamp == row.timestamp)
        if row.sequence_number in drop:
            await db.execute(delete(Action).where(*pk))
        else:
            await db.execute(update(Action).where(*pk).values(tool_name=TAMPERED_TOOL))
    await db.commit()
    # No expunge/expire needed, and adding one would imply a hazard that does
    # not exist here: these are ORM-enabled DML statements executed through the
    # session, so SQLAlchemy synchronizes the identity map (synchronize_session
    # defaults to "auto") and the verifier's next SELECT sees the damage even
    # with `expire_on_commit=False`. A raw `text()` UPDATE would NOT synchronize
    # — that is the case worth a comment, on the day someone writes one.


async def _drive_both_backends(*, agent_id: UUID, session_id: UUID, db, jsonl_dir: Path) -> Path:
    """Write one scripted session into both stores. Returns the JSONL path."""
    envelopes = _scripted_envelopes(agent_id, session_id)

    jsonl = JsonlIngestClient(data_dir=jsonl_dir)
    postgres = LocalIngestClient(db=db)
    for envelope in envelopes:
        await jsonl.handle(envelope)
        await postgres.handle(envelope)
    await db.commit()
    return jsonl._session_path(str(session_id))


# (name, dropped sequences, tampered sequences, verdict, first_invalid, missing)
PARITY_CASES = [
    pytest.param(set(), set(), Verdict.VALID, None, [], id="clean"),
    pytest.param({3}, set(), Verdict.INCOMPLETE, 3, [(3, 3)], id="one_gap"),
    pytest.param({1}, set(), Verdict.INCOMPLETE, 1, [(1, 1)], id="missing_head"),
    pytest.param({2, 3}, set(), Verdict.INCOMPLETE, 2, [(2, 3)], id="run_of_gaps"),
    pytest.param({2, 5}, set(), Verdict.INCOMPLETE, 2, [(2, 2), (5, 5)], id="two_gaps"),
    pytest.param(set(), {4}, Verdict.TAMPERED, 4, [], id="tampered"),
    # Precedence: both conditions present, TAMPERED wins, gaps still reported.
    pytest.param({3}, {5}, Verdict.TAMPERED, 5, [(3, 3)], id="gap_then_tamper"),
    # The masking case: the altered record is the one whose prev-hash break the
    # gap already explains. Caught by its own self_hash, not by the link.
    pytest.param({3}, {4}, Verdict.TAMPERED, 4, [(3, 3)], id="tamper_right_after_gap"),
]


class TestVerdictParity:
    @pytest.mark.parametrize(
        ("drop", "tamper", "verdict", "first_invalid", "missing"), PARITY_CASES
    )
    async def test_both_verifiers_reach_the_same_verdict(
        self,
        tmp_path,
        clean_db,
        seeded_agent,
        drop: set[int],
        tamper: set[int],
        verdict: Verdict,
        first_invalid: int | None,
        missing: list[tuple[int, int]],
    ):
        session_id = uuid4()
        path = await _drive_both_backends(
            agent_id=seeded_agent.agent_id, session_id=session_id, db=clean_db, jsonl_dir=tmp_path
        )

        _damage_jsonl(path, drop=drop, tamper=tamper)
        await _damage_postgres(clean_db, session_id, drop=drop, tamper=tamper)

        local = verify_session_local(str(path))
        remote = await action_crud.verify_chain(clean_db, session_id=session_id)

        # The expected verdict, so a shared bug that moves both verifiers in
        # the same direction still fails rather than agreeing with itself.
        assert local.verdict is verdict, local.summary
        assert remote["verdict"] == verdict.value, remote

        # ...and parity, which is what this file is for.
        assert Verdict(remote["verdict"]) is local.verdict
        assert remote["valid"] is local.valid is (verdict is Verdict.VALID)
        assert local.first_invalid_sequence == first_invalid, local.summary
        assert remote["first_invalid_sequence"] == first_invalid, remote
        assert [tuple(r) for r in local.missing_ranges] == missing
        assert [tuple(r) for r in remote["missing_ranges"]] == missing
        assert local.record_count == remote["record_count"] == ACTION_COUNT - len(drop)

    async def test_the_incomplete_detail_string_is_identical_on_both_paths(
        self, tmp_path, clean_db, seeded_agent
    ):
        """A gap is described the same way whichever verifier reports it.

        The TAMPERED strings differ by design — the Postgres one carries the
        stored-vs-recomputed digests, which the offline path cannot show
        without dumping the file. The INCOMPLETE one has no such excuse: both
        render it from `describe_missing`, and an operator comparing a spooled
        file against the store should see the same sentence twice.
        """
        session_id = uuid4()
        path = await _drive_both_backends(
            agent_id=seeded_agent.agent_id, session_id=session_id, db=clean_db, jsonl_dir=tmp_path
        )
        _damage_jsonl(path, drop={2, 3}, tamper=set())
        await _damage_postgres(clean_db, session_id, drop={2, 3}, tamper=set())

        local = verify_session_local(str(path))
        remote = await action_crud.verify_chain(clean_db, session_id=session_id)

        assert local.error == remote["error"]
        assert local.error == "2 record(s) missing at sequence 2-3"

    async def test_a_gap_does_not_hide_later_tampering_on_either_path(
        self, tmp_path, clean_db, seeded_agent
    ):
        """Re-anchoring, stated as its own claim.

        The parametrized case pins the verdict; this pins *why* it matters. If
        either verifier returned at the first gap-explained break, sequence 6
        would never be reached and a dropped record would be a free pass to
        rewrite everything after it.
        """
        session_id = uuid4()
        path = await _drive_both_backends(
            agent_id=seeded_agent.agent_id, session_id=session_id, db=clean_db, jsonl_dir=tmp_path
        )
        _damage_jsonl(path, drop={2}, tamper={6})
        await _damage_postgres(clean_db, session_id, drop={2}, tamper={6})

        local = verify_session_local(str(path))
        remote = await action_crud.verify_chain(clean_db, session_id=session_id)

        assert local.verdict is Verdict.TAMPERED, local.summary
        assert remote["verdict"] == Verdict.TAMPERED.value, remote
        assert local.first_invalid_sequence == remote["first_invalid_sequence"] == 6
        # The gap is still the operator's problem even though it lost the
        # verdict to the alteration.
        assert [tuple(r) for r in local.missing_ranges] == [(2, 2)]
        assert [tuple(r) for r in remote["missing_ranges"]] == [(2, 2)]
        assert "also missing sequence 2" in local.summary


# ---------------------------------------------------------------------------
# CLI exit codes — the surface CI consumes
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_cli_session(monkeypatch):
    """Bind the CLI's session factory to TEST_DATABASE_URL.

    Same seam as tests/integration/test_verify_cli.py: the CLI runs in a worker
    thread against its own engine, so it must be pointed at `rootsign_test`
    rather than the development database.
    """
    engine = create_async_engine(settings.TEST_DATABASE_URL, poolclass=NullPool, future=True)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr("rootsign.sdk.cli.AsyncSessionLocal", factory)
    yield factory


EXIT_CODE_CASES = [
    pytest.param(set(), set(), Verdict.VALID, id="valid"),
    pytest.param(set(), {4}, Verdict.TAMPERED, id="tampered"),
    pytest.param({3}, set(), Verdict.INCOMPLETE, id="incomplete"),
]


class TestVerifyExitCodeParity:
    """0 / 1 / 2 from *both* verify sites, for the same damage.

    `rootsign verify <id>` and `rootsign verify --local <path>` are separate
    code paths that reach `exit_code()` separately; a caller scripting one and
    then the other must not have to special-case which it asked.
    """

    @pytest.mark.parametrize(("drop", "tamper", "verdict"), EXIT_CODE_CASES)
    async def test_both_verify_sites_exit_with_the_verdict_code(
        self,
        tmp_path,
        clean_db,
        seeded_agent,
        patched_cli_session,
        drop: set[int],
        tamper: set[int],
        verdict: Verdict,
    ):
        session_id = uuid4()
        path = await _drive_both_backends(
            agent_id=seeded_agent.agent_id, session_id=session_id, db=clean_db, jsonl_dir=tmp_path
        )
        _damage_jsonl(path, drop=drop, tamper=tamper)
        await _damage_postgres(clean_db, session_id, drop=drop, tamper=tamper)

        expected = EXIT_CODES[verdict]

        # Both invocations go through `asyncio.to_thread`: the DB path calls
        # `asyncio.run` internally, which refuses to start inside the test's
        # running loop (CLAUDE.md test invariant 2).
        remote = await asyncio.to_thread(runner.invoke, app, ["verify", str(session_id)])
        local = await asyncio.to_thread(runner.invoke, app, ["verify", "--local", str(path)])

        assert remote.exit_code == expected, remote.output
        assert local.exit_code == expected, local.output
        assert verdict.value in remote.output
        assert verdict.value in local.output
