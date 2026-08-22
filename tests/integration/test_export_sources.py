"""Export from both sources, and the parity between them (Sprint B T3.1).

A bundle is supposed to be about a *session*, not about where that session
happens to be stored. So the same scripted session driven into Postgres and
into a JSONL file must produce bundles that agree on every fact either source
can know — the verdict above all, since that is the line an auditor reads
first and the one they would have no way to reconcile if the two disagreed.

What they legitimately differ on is worth pinning too: a session file carries
an `agent_id` and nothing else about the agent, while Postgres has the
registration row. The bundle from a file says so rather than emitting empty
name/owner fields (ADR-014 Decision 4's honesty rule, applied to identity).

`seeded_agent` (committed) because the Postgres side spans more than one
session boundary (Flag 3).
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from rootsign.sdk.client import LocalIngestClient
from rootsign.sdk.export import export_local, export_session
from rootsign.sdk.hashing import compute_payload_hash
from rootsign.sdk.jsonl_client import JsonlIngestClient
from tests.conftest import make_envelope

TOOLS = ("send_email", "query_db", "charge_card")


def _scripted(agent_id, session_id):
    """SESSION_OPEN → a decision → three actions → SESSION_CLOSE."""
    envelopes = [
        make_envelope("SESSION_OPEN", agent_id, session_id, {"objective": "export parity"}),
        make_envelope(
            "DECISION_RECORD",
            agent_id,
            session_id,
            {
                "selected_action": "proceed",
                "confidence": 0.9,
                "reasoning_summary": "within policy",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        ),
    ]
    for i, tool in enumerate(TOOLS):
        redacted = {"to": "[REDACTED]", "subject": f"item {i}"}
        envelopes.append(
            make_envelope(
                "ACTION_RECORD",
                agent_id,
                session_id,
                {
                    "tool_name": tool,
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


@pytest.fixture
async def both_sources(tmp_path, clean_db, seeded_agent):
    """One session, written to Postgres and to a session file."""
    session_id = uuid4()
    envelopes = _scripted(seeded_agent.agent_id, session_id)

    jsonl = JsonlIngestClient(data_dir=tmp_path)
    postgres = LocalIngestClient(db=clean_db)
    for envelope in envelopes:
        await jsonl.handle(envelope)
        await postgres.handle(envelope)
    await clean_db.commit()

    return session_id, jsonl._session_path(str(session_id))


class TestExportSourceParity:
    async def test_both_sources_agree_on_every_fact_they_both_know(self, both_sources, clean_db):
        """The verdict, the chain shape, and the narrative.

        Not the hashes: each backend mints its own `action_id`, so the
        `self_hash` values differ for the same logical action unless identity
        is pinned. That is what `tests/integration/test_cross_backend_hash.py`
        pins; here the claim is that two bundles describe the same session.
        """
        session_id, path = both_sources

        from_file = export_local(path)
        from_db = await export_session(session_id, clean_db)

        assert from_file.verification["verdict"] == from_db.verification["verdict"] == "VALID"
        assert from_file.verification["valid"] is from_db.verification["valid"] is True
        assert from_file.verification["record_count"] == from_db.verification["record_count"] == 3

        def facts(bundle):
            return [
                (r["sequence_number"], r["tool_name"], r["chain_status"])
                for r in bundle.verification["records"]
            ]

        assert facts(from_file) == facts(from_db)
        assert facts(from_db) == [
            (1, TOOLS[0], "verified"),
            (2, TOOLS[1], "verified"),
            (3, TOOLS[2], "verified"),
        ]

    async def test_both_sources_report_the_same_redaction_posture(self, both_sources, clean_db):
        """`redaction.json` is derived from the stored payloads, so a store that
        dropped or rewrote a preview would show up as a different posture for
        the same session."""
        session_id, path = both_sources

        from_file = export_local(path).redaction
        from_db = (await export_session(session_id, clean_db)).redaction

        assert (
            from_file["totals"]
            == from_db["totals"]
            == {
                "actions_with_redactions": 3,
                "redacted_fields": 3,
            }
        )
        assert [r["input_paths"] for r in from_file["records"]] == [["to"]] * 3
        assert [r["input_paths"] for r in from_db["records"]] == [["to"]] * 3

    async def test_both_sources_tell_the_same_story_in_the_same_order(self, both_sources, clean_db):
        session_id, path = both_sources

        from_file = [e["type"] for e in export_local(path).timeline["events"]]
        from_db = [
            e["type"] for e in (await export_session(session_id, clean_db)).timeline["events"]
        ]

        assert from_file == from_db
        assert from_file == [
            "SESSION_OPEN",
            "DECISION",
            "ACTION",
            "ACTION",
            "ACTION",
            "SESSION_CLOSE",
        ]


class TestPostgresSource:
    async def test_the_manifest_carries_the_registered_agent(
        self, both_sources, clean_db, seeded_agent
    ):
        """What the database knows and a file does not.

        An auditor reading a bundle needs to know *whose* agent this was —
        owner, environment, risk tier. That is the registration row, so only
        the Postgres-sourced bundle can carry it.
        """
        session_id, path = both_sources

        from_db = (await export_session(session_id, clean_db)).manifest
        from_file = export_local(path).manifest

        assert from_db["source"] == {"backend": "postgres", "location": "database"}
        assert from_db["agent"]["agent_id"] == str(seeded_agent.agent_id)
        assert from_db["agent"]["name"] == seeded_agent.name
        assert from_db["agent"]["owner"] == seeded_agent.owner
        assert from_db["agent"]["risk_tier"] == seeded_agent.risk_tier

        # The file source knows the id and admits the rest is not there.
        assert set(from_file["agent"]) == {"agent_id"}

    async def test_the_session_block_comes_from_the_stored_row(self, both_sources, clean_db):
        session_id, _ = both_sources

        session = (await export_session(session_id, clean_db)).timeline["session"]

        assert session["session_id"] == str(session_id)
        assert session["objective"] == "export parity"
        assert session["status"] == "completed"
        assert session["start_time"] and session["end_time"]

    async def test_an_unknown_session_is_an_actionable_error(self, clean_db):
        """Better than an empty bundle: a bundle for a session that does not
        exist would look like a session with nothing in it."""
        with pytest.raises(LookupError, match="not found"):
            await export_session(uuid4(), clean_db)

    async def test_redact_previews_strips_the_database_source_too(self, both_sources, clean_db):
        session_id, _ = both_sources

        timeline = (await export_session(session_id, clean_db, redact_previews=True)).timeline

        assert timeline["previews"]["included"] is False
        actions = [e for e in timeline["events"] if e["type"] == "ACTION"]
        assert all("input_preview" not in a for a in actions)
        assert all(a["input_hash"] for a in actions)
