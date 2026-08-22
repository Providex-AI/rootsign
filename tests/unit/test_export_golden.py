"""The bundle schema, frozen (Sprint B T3.6, ADR-014 Decisions 2 and 5).

`tests/fixtures/evidence_bundle_v1.json` is a complete bundle generated from a
pinned session. Any change to a key name, a nesting level, or a field's absence
fails this file — which is the point. Phase 2's dashboard and any partner
tooling read this schema, so it stops being ours to adjust quietly once a
bundle has left the building.

**A failure here is not a bug, it is a decision.** Two ways to resolve one:
additive changes (a new optional field, a value inside the reserved
`compliance` block) are fine — regenerate the fixture with
`ROOTSIGN_UPDATE_GOLDEN=1 python -m pytest tests/unit/test_export_golden.py`
and commit the diff. Anything that renames or removes a field is a bundle
version bump, and `EVIDENCE_BUNDLE_VERSION` moves with it.

The session behind the fixture is deliberately the *interesting* one rather
than the simplest: a decision that escalated, an auto-authorized action, a
second action a human **rejected**, and the approval that rejected it. A
golden file built from a two-action happy path would freeze a schema that had
never met the narrative it exists to carry.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from rootsign.sdk.export import (
    EVIDENCE_BUNDLE_VERSION,
    MANIFEST_FILE,
    REDACTION_FILE,
    TIMELINE_FILE,
    VERIFICATION_FILE,
    export_local,
    sha256_text,
)
from rootsign.sdk.hashing import compute_payload_hash
from rootsign.sdk.jsonl_client import JsonlIngestClient
from rootsign.sdk.report import bundle_documents, render_html, render_markdown
from rootsign.verdict import Verdict
from tests.support.session_files import damage_action, drop_action, write_session_file

GOLDEN_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "evidence_bundle_v1.json"

AGENT_ID = "11111111-1111-4111-8111-111111111111"
SESSION_ID = "22222222-2222-4222-8222-222222222222"
BASE_TIME = "2026-08-21T09:00:00+00:00"

#: Fields that cannot be pinned because they describe *this* export rather than
#: the session: when it ran, which version ran it, and where the source lived.
VOLATILE = {
    "generated_at": "<generated_at>",
    "generator": "<generator>",
    "location": "<location>",
}


def _uuid(n: int) -> str:
    return f"{n:08d}-0000-4000-8000-000000000000"


def _envelope(index: int, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """A fully pinned envelope — no clock, no random ids."""
    return {
        "schema_version": "1.1",
        "sdk_version": "0.3.0",
        "event_type": event_type,
        "event_id": _uuid(100 + index),
        "emitted_at": BASE_TIME,
        "agent_id": AGENT_ID,
        "session_id": SESSION_ID,
        "payload": payload,
    }


def _write_pinned_session(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write the fixture session: escalation, an action, a rejected action.

    `rootsign.chain_state.uuid4` is the single point where any record id is
    minted (T2.3), so pinning it there is enough to make the whole chain —
    action ids, and therefore every `self_hash` — reproducible.
    """
    import asyncio
    import itertools

    counter = itertools.count(1)
    monkeypatch.setattr("rootsign.chain_state.uuid4", lambda: UUID(_uuid(next(counter))))

    email_input = {"to": "[REDACTED]", "subject": "Q3 invoice", "cc": ["ops@example.com"]}
    transfer_input = {"account": "[REDACTED]", "amount": 250000, "currency": "GBP"}

    envelopes = [
        _envelope(
            0, "SESSION_OPEN", {"objective": "close the Q3 ledger", "user_id": "finance-bot"}
        ),
        _envelope(
            1,
            "DECISION_RECORD",
            {
                "selected_action": "escalate_to_human",
                "confidence": 0.41,
                "alternatives_considered": ["auto_approve", "reject"],
                "reasoning_summary": "transfer exceeds the unattended threshold",
                "timestamp": BASE_TIME,
            },
        ),
        _envelope(
            2,
            "ACTION_RECORD",
            {
                "tool_name": "send_email",
                "input_hash": compute_payload_hash(email_input),
                "output_hash": compute_payload_hash({"status": "sent"}),
                "input_redacted": email_input,
                "output_redacted": {"status": "sent"},
                "timestamp": BASE_TIME,
                "authorization_status": "auto_authorized",
                "duration_ms": 412,
            },
        ),
        _envelope(
            3,
            "ACTION_RECORD",
            {
                "tool_name": "wire_transfer",
                "input_hash": compute_payload_hash(transfer_input),
                "output_hash": None,
                "input_redacted": transfer_input,
                "timestamp": BASE_TIME,
                # The gate said no, so the tool never ran and there is no output.
                "authorization_status": "human_rejected",
            },
        ),
        _envelope(
            4,
            "APPROVAL_RECORD",
            {
                # The third minted id. The counter runs over every record that
                # gets one, in write order: decision (1), send_email (2),
                # wire_transfer (3) — not just the actions.
                "action_id": _uuid(3),
                "approver_id": "sile",
                "approver_type": "human",
                "context_presented": {"tool_name": "wire_transfer", "amount": "[REDACTED]"},
                "decision": "rejected",
                "decision_reason": "over the mandate for an unattended run",
                "response_latency_ms": 91_000,
                "timestamp": BASE_TIME,
            },
        ),
        _envelope(5, "SESSION_CLOSE", {"status": "completed"}),
    ]

    async def _run() -> Path:
        client = JsonlIngestClient(data_dir=data_dir)
        for envelope in envelopes:
            await client.handle(envelope)
        return client._session_path(SESSION_ID)

    return asyncio.run(_run())


def _normalize(value: Any) -> Any:
    """Replace the fields that describe this export rather than the session."""
    if isinstance(value, dict):
        return {
            key: VOLATILE[key] if key in VOLATILE else _normalize(inner)
            for key, inner in value.items()
        }
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return value


@pytest.fixture
def documents(tmp_path, monkeypatch) -> dict[str, Any]:
    path = _write_pinned_session(tmp_path, monkeypatch)
    return {
        name: _normalize(document)
        for name, document in bundle_documents(export_local(path)).items()
    }


@pytest.fixture
def golden() -> dict[str, Any]:
    if not GOLDEN_PATH.is_file():
        pytest.fail(
            f"{GOLDEN_PATH} is missing. Regenerate it with "
            "ROOTSIGN_UPDATE_GOLDEN=1 python -m pytest tests/unit/test_export_golden.py"
        )
    return json.loads(GOLDEN_PATH.read_text())


@pytest.fixture(autouse=True)
def _maybe_regenerate(request, tmp_path, monkeypatch):
    """`ROOTSIGN_UPDATE_GOLDEN=1` rewrites the fixture before the assertions.

    Deliberate schema changes should be one command and a reviewable diff —
    if updating the fixture were tedious, the pressure would be to loosen the
    comparison instead, and a tripwire nobody trusts is a tripwire nobody keeps.
    """
    if os.environ.get("ROOTSIGN_UPDATE_GOLDEN") != "1":
        return
    path = _write_pinned_session(tmp_path / "regen", monkeypatch)
    documents = {
        name: _normalize(doc) for name, doc in bundle_documents(export_local(path)).items()
    }
    GOLDEN_PATH.write_text(json.dumps(documents, indent=2, ensure_ascii=False) + "\n")


class TestSchemaIsFrozen:
    @pytest.mark.parametrize(
        "name", [VERIFICATION_FILE, TIMELINE_FILE, REDACTION_FILE, MANIFEST_FILE]
    )
    def test_the_document_matches_the_golden_file(self, documents, golden, name: str):
        """One assertion per document, so a failure names which one drifted."""
        assert documents[name] == golden[name], (
            f"{name} no longer matches tests/fixtures/evidence_bundle_v1.json. "
            "If the change is additive, regenerate with ROOTSIGN_UPDATE_GOLDEN=1; "
            "if it renames or removes a field, bump EVIDENCE_BUNDLE_VERSION."
        )

    def test_the_pinned_session_is_reproducible(self, tmp_path, monkeypatch):
        """The fixture is only a tripwire if the same session always produces
        the same bytes — otherwise the diff would be noise and the test would
        be disabled within a week."""
        first = _write_pinned_session(tmp_path / "a", monkeypatch)
        second = _write_pinned_session(tmp_path / "b", monkeypatch)

        assert first.read_text() == second.read_text()

    def test_the_documents_hash_to_the_digests_the_manifest_pins(self, documents, golden):
        """Content freeze, not just shape: the manifest in the golden file
        carries a SHA-256 per document, so a changed *value* fails too."""
        from rootsign.sdk.export import _dumps

        for name in (VERIFICATION_FILE, TIMELINE_FILE, REDACTION_FILE):
            assert sha256_text(_dumps(documents[name])) == golden[MANIFEST_FILE]["files"][name]

    def test_the_version_the_golden_file_pins_is_the_one_the_code_emits(self, golden):
        assert golden[MANIFEST_FILE]["bundle_version"] == EVIDENCE_BUNDLE_VERSION == "1.0"

    def test_the_compliance_block_is_reserved_and_empty(self, golden):
        """Phase 2 fills this without a version bump — that is the whole reason
        it ships empty in 1.0 (ADR-014 Decision 5)."""
        assert golden[MANIFEST_FILE]["compliance"] == {}


class TestVerdictVocabularyIsFrozenToo:
    """ADR-014: the vocabulary freezes at v1.0, not the values seen at v1.0.

    Every early bundle will say VALID. If the schema only ever had to describe
    that, the first spooled session with a gap would force bundle v1.1 in week
    one — exactly the break the reserved block exists to avoid. So the other
    two verdicts are exercised against the same schema here.
    """

    @pytest.mark.parametrize(
        ("damage", "verdict"),
        [
            (None, Verdict.VALID),
            ("tamper", Verdict.TAMPERED),
            ("drop", Verdict.INCOMPLETE),
        ],
        ids=["valid", "tampered", "incomplete"],
    )
    def test_each_verdict_produces_the_same_document_shape(
        self, tmp_path, golden, damage, verdict: Verdict
    ):
        path = write_session_file(tmp_path, actions=4, previews=False)
        if damage == "tamper":
            damage_action(path, 2, "tool_name", "SOMETHING_ELSE")
        elif damage == "drop":
            drop_action(path, 2)

        verification = export_local(path).verification

        assert verification["verdict"] == verdict.value
        assert set(verification) == set(golden[VERIFICATION_FILE])
        assert set(verification["records"][0]) == set(golden[VERIFICATION_FILE]["records"][0])
        assert set(verification["hash"]) == set(golden[VERIFICATION_FILE]["hash"])

    def test_every_verdict_in_the_enum_is_legal_in_a_bundle(self):
        """A bundle reader that only understood two of them would reject a
        legitimate INCOMPLETE bundle as malformed."""
        assert {v.value for v in Verdict} == {"VALID", "TAMPERED", "INCOMPLETE"}


class TestTheNarrativeCase:
    """The session the fixture is built from — an approval that said no."""

    def test_the_rejected_action_and_its_approval_are_both_recorded(self, documents):
        events = documents[TIMELINE_FILE]["events"]
        actions = [e for e in events if e["type"] == "ACTION"]
        approval = next(e for e in events if e["type"] == "APPROVAL")

        rejected = next(a for a in actions if a["authorization_status"] == "human_rejected")
        assert rejected["tool_name"] == "wire_transfer"
        # The gate refused, so the tool never produced anything to hash.
        assert rejected["output_hash"] is None
        assert approval["decision"] == "rejected"
        assert approval["action_id"] == rejected["action_id"], "the approval names another action"
        assert approval["decision_reason"] == "over the mandate for an unattended run"

    def test_a_refused_action_is_still_in_the_chain(self, documents):
        """It has to be. "The agent tried to move £250k and was stopped" is a
        record an auditor needs, and a chain that omitted refused attempts
        would be a chain that only remembers what succeeded.
        """
        verification = documents[VERIFICATION_FILE]

        assert verification["verdict"] == "VALID"
        assert [r["sequence_number"] for r in verification["records"]] == [1, 2]
        assert verification["records"][1]["tool_name"] == "wire_transfer"
        assert verification["records"][1]["chain_status"] == "verified"

    def test_the_report_tells_that_story(self, documents):
        markdown, page = render_markdown(documents), render_html(documents)

        for rendered in (markdown, page):
            assert "wire_transfer" in rendered
            assert "human_rejected" in rendered
            assert "over the mandate for an unattended run" in rendered
            assert "escalate_to_human" in rendered

    def test_the_html_renders_the_verdict_and_parses(self, documents):
        """The smoke test T3.6 asks for, on the narrative bundle: the document
        opens, the tags balance, and the first thing in it is the verdict."""
        from html.parser import HTMLParser

        page = render_html(documents)

        class _Balance(HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=True)
                self.stack: list[str] = []
                self.unbalanced: list[str] = []

            def handle_starttag(self, tag, attrs):
                if tag not in {"meta", "br", "hr", "img", "input", "link", "source"}:
                    self.stack.append(tag)

            def handle_endtag(self, tag):
                if self.stack and self.stack[-1] == tag:
                    self.stack.pop()
                else:
                    self.unbalanced.append(tag)

        parser = _Balance()
        parser.feed(page)

        assert parser.unbalanced == [] and parser.stack == []
        body = page[page.index("<body>") :]
        assert body.index("VALID") < body.index("Session")
