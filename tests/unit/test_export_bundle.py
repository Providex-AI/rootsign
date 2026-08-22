"""The evidence bundle's assembly (Sprint B T3.1, ADR-014).

Everything here is driven off a **store-written session file** rather than
hand-built dicts: the bundle's job is to report what a store actually recorded,
so a fixture that invents the records would let the two drift and still pass.

What the assertions are really guarding is honesty. A bundle goes to someone
who cannot check it against the system that produced it, so every way it could
overstate its knowledge is a way to mislead an auditor:

* records after a chain break are `unverified`, never `verified` — the walk
  stopped there and proved nothing about them;
* previews appear only where a payload was actually retained, and a hash-only
  session says so in words;
* `redaction.json` reports sentinel paths, never which rule fired, because that
  is not recorded anywhere (ADR-014 scope note);
* `compliance` is present and empty, so Phase 2 fills it without a version bump.

The golden-file schema tripwire, `--check` tampering, and the HTML render are
T3.6 / T3.3 / T3.2 — this file is the data.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rootsign.sdk.export import (
    CONTENT_FIELDS,
    EVIDENCE_BUNDLE_VERSION,
    MANIFEST_FILE,
    REDACTION_FILE,
    TIMELINE_FILE,
    VERIFICATION_FILE,
    export_local,
    load_local_session,
    sha256_text,
)
from tests.support.session_files import (
    TOOLS,
    damage_action,
    drop_action,
    write_session_file,
)


@pytest.fixture
def session_file(tmp_path: Path) -> Path:
    return write_session_file(tmp_path)


class TestManifest:
    def test_the_manifest_describes_the_bundle_and_reserves_the_compliance_block(
        self, session_file
    ):
        """The reserved block is the cheapest thing in the bundle and the most
        valuable: Phase 2's compliance mapping lands in it without forcing
        every bundle already in an auditor's inbox to a new version."""
        manifest = export_local(session_file).manifest

        assert manifest["bundle_version"] == EVIDENCE_BUNDLE_VERSION == "1.0"
        assert manifest["generator"].startswith("rootsign ")
        assert manifest["source"] == {"backend": "jsonl", "location": str(session_file)}
        assert manifest["compliance"] == {}
        assert manifest["verdict"] == "VALID"

    def test_every_bundle_file_is_hashed_except_the_manifest_itself(self, session_file):
        """A manifest cannot hash itself, which is exactly why its own digest is
        the out-of-band anchor rather than something inside the bundle."""
        bundle = export_local(session_file)
        manifest = bundle.manifest

        assert set(manifest["files"]) == {VERIFICATION_FILE, TIMELINE_FILE, REDACTION_FILE}
        assert MANIFEST_FILE not in manifest["files"]
        for name, text in bundle.files().items():
            assert manifest["files"][name] == sha256_text(text)

    def test_an_attached_rendering_joins_the_manifest(self, session_file):
        """T3.2's hook: a rendered report is bundle content, so it is covered by
        the same integrity claim as the JSON."""
        bundle = export_local(session_file)
        bundle.attach("report.md", "# Evidence\n")

        assert bundle.manifest["files"]["report.md"] == sha256_text("# Evidence\n")

    def test_the_manifest_hash_moves_when_any_document_does(self, session_file):
        """The anchor has to be sensitive to the whole bundle, or noting it
        down proves nothing about the parts it missed."""
        bundle = export_local(session_file)
        before = bundle.manifest_hash

        bundle.attach("report.md", "# Evidence\n")

        assert bundle.manifest_hash != before

    def test_two_exports_of_an_unchanged_session_agree(self, session_file):
        """Same records in, same hashes out — otherwise a recipient comparing
        against a re-export would see phantom tampering."""
        first, second = export_local(session_file), export_local(session_file)

        assert first.manifest["files"] == second.manifest["files"]


class TestVerification:
    def test_a_clean_session_verifies_and_every_record_is_marked_verified(self, session_file):
        verification = export_local(session_file).verification

        assert verification["verdict"] == "VALID"
        assert verification["valid"] is True
        assert verification["record_count"] == 3
        assert [r["chain_status"] for r in verification["records"]] == ["verified"] * 3
        assert all(r["self_hash"] for r in verification["records"])

    def test_records_after_a_break_are_unverified_not_verified(self, tmp_path):
        """The claim the bundle must not make.

        A verifier stops at the first break, so it has said nothing about what
        follows. Marking those `verified` would be the bundle inventing proof;
        omitting them would hide records that exist. `unverified` is the only
        honest third answer.
        """
        path = write_session_file(tmp_path, actions=4, previews=False)
        damage_action(path, 2, "tool_name", "SOMETHING_ELSE")

        verification = export_local(path).verification

        assert verification["verdict"] == "TAMPERED"
        assert verification["valid"] is False
        assert verification["first_invalid_sequence"] == 2
        assert [r["chain_status"] for r in verification["records"]] == [
            "verified",
            "failed",
            "unverified",
            "unverified",
        ]

    def test_a_gap_is_reported_as_incomplete_with_its_range(self, tmp_path):
        """INCOMPLETE has to be legal in bundle v1.0 even though early bundles
        will all say VALID — the first spool failure would otherwise force
        v1.1 (ADR-014 Decision 2)."""
        path = write_session_file(tmp_path, actions=4, previews=False)
        drop_action(path, 2)

        verification = export_local(path).verification

        assert verification["verdict"] == "INCOMPLETE"
        assert verification["valid"] is False
        assert verification["missing_ranges"] == [[2, 2]]
        assert "missing" in verification["summary"].lower()

    def test_the_hash_block_names_the_frozen_spec(self, session_file):
        """An auditor should be able to re-derive the proof without reading the
        source. The canonical formula is frozen under ADR-001, so naming it is
        a durable reference rather than a version-of-the-week."""
        assert export_local(session_file).verification["hash"]["algorithm"] == "sha256"
        assert export_local(session_file).verification["hash"]["canonical_spec"] == "ADR-001"


class TestPreviewHonesty:
    def test_stored_previews_are_included(self, session_file):
        timeline = export_local(session_file).timeline

        assert timeline["previews"] == {
            "included": True,
            "note": None,
            "withheld_fields": [],
            "withheld_from_events": 0,
        }
        actions = [e for e in timeline["events"] if e["type"] == "ACTION"]
        assert actions[0]["input_preview"]["meta"]["region"] == "eu-west-1"

    def test_redact_previews_strips_them_and_says_why(self, session_file):
        timeline = export_local(session_file, redact_previews=True).timeline

        assert timeline["previews"]["included"] is False
        assert "--redact-previews" in timeline["previews"]["note"]
        actions = [e for e in timeline["events"] if e["type"] == "ACTION"]
        assert all("input_preview" not in a for a in actions)
        # The hashes stay: they are the chain, not a preview of it.
        assert all(a["input_hash"] for a in actions)

    def test_redact_previews_strips_every_content_field_not_just_the_obvious_two(self, tmp_path):
        """ADR-014 Decision 4 says "strips them entirely", and entirely has to
        mean entirely.

        A payload that survives inside an approval's `context_presented` has
        left the building exactly as thoroughly as one in `input_preview` —
        that field is built from the redacted input, so it embeds the payload
        by another name. Decision capture's free-text summaries are the same
        story. Privacy controls fail closed: content fields go unless they are
        demonstrably metadata.
        """
        path = write_session_file(tmp_path, actions=2, approval=True, decision=True)

        timeline = export_local(path, redact_previews=True).timeline

        for event in timeline["events"]:
            assert not set(event) & set(CONTENT_FIELDS), event["type"]
        # `inputs_summary` is absent because this session did not record one —
        # only fields that actually held something are named as withheld.
        assert timeline["previews"]["withheld_fields"] == [
            "context_presented",
            "input_preview",
            "output_preview",
            "reasoning_summary",
        ]
        assert timeline["previews"]["withheld_from_events"] == 4  # 2 actions + decision + approval

    def test_nothing_sensitive_survives_anywhere_in_a_redacted_bundle(self, tmp_path):
        """The claim, checked across every byte the bundle would ship.

        Per-document assertions can each pass while a payload survives in a
        document nobody thought to check — which is exactly how this kind of
        leak happens. This searches the whole serialized bundle, reports
        included.
        """
        from rootsign.sdk.report import attach_reports

        path = write_session_file(tmp_path, actions=2, approval=True, decision=True)
        bundle = attach_reports(export_local(path, redact_previews=True))

        haystack = "\n".join(bundle.files().values()) + json.dumps(bundle.manifest)

        for secret in ("eu-west-1", "ops@example.com", "amount over threshold", "input_summary"):
            assert secret not in haystack, f"{secret!r} survived --redact-previews"

        # And the evidence itself is untouched: hashes, identities, verdict.
        assert bundle.verification["verdict"] == "VALID"
        assert all(r["self_hash"] for r in bundle.verification["records"])
        assert "[REDACTED]" in json.dumps(bundle.redaction)

    def test_the_withheld_fields_are_named_rather_than_silently_absent(self, tmp_path):
        """A reader who cannot tell whether a field was stripped or never
        recorded has to assume the worse of the two, and has no idea what to
        ask for."""
        path = write_session_file(tmp_path, actions=1, approval=True)

        previews = export_local(path, redact_previews=True).timeline["previews"]

        assert previews["included"] is False
        assert "--redact-previews" in previews["note"]
        assert "context_presented" in previews["withheld_fields"]
        assert "input_preview" in previews["withheld_fields"]

    def test_a_hash_only_session_says_previews_were_never_retained(self, tmp_path):
        """ADR-014 Decision 4. An auditor who finds an implied-but-absent field
        discounts the whole artifact, so the absence is stated rather than
        left to be noticed."""
        path = write_session_file(tmp_path, previews=False)

        timeline = export_local(path).timeline

        assert timeline["previews"]["included"] is False
        assert timeline["previews"]["note"] == (
            "payload previews not retained for this session — only the "
            "input/output hashes the chain is built from were stored"
        )


class TestRedactionPosture:
    def test_sentinel_paths_are_reported_including_inside_lists(self, session_file):
        """Paths, not shapes: `cc[1]` identifies the field that was redacted,
        which is what makes the claim checkable against the payload."""
        redaction = export_local(session_file).redaction

        assert redaction["sentinel"] == "[REDACTED]"
        record = redaction["records"][0]
        assert set(record["input_paths"]) == {"to", "cc[1]", "meta.account"}
        assert record["output_paths"] == []
        assert redaction["totals"] == {"actions_with_redactions": 3, "redacted_fields": 9}

    def test_unredacted_values_are_never_listed(self, session_file):
        """The file reports where redaction *happened*. Listing what survived
        would turn the redaction proof into a directory of retained PII."""
        paths = export_local(session_file).redaction["records"][0]["input_paths"]

        assert not any("region" in p for p in paths)

    def test_the_rule_set_is_null_with_the_reason_spelled_out(self, session_file):
        """RootSign does not store which rule set was active, and it is not
        derivable. Naming a plausible one would be the bundle claiming
        provenance it does not have (ADR-014 scope note)."""
        rule_set = export_local(session_file).redaction["rule_set"]

        assert rule_set["name"] is None
        assert "not derivable from stored records" in rule_set["provenance"]

    def test_a_session_without_redactions_reports_none(self, tmp_path):
        path = write_session_file(tmp_path, previews=False)

        redaction = export_local(path).redaction

        assert redaction["records"] == []
        assert redaction["totals"]["redacted_fields"] == 0


class TestTimeline:
    def test_the_narrative_runs_in_the_order_it_happened(self, tmp_path):
        path = write_session_file(tmp_path, actions=2, approval=True, decision=True)

        events = export_local(path).timeline["events"]

        assert [e["type"] for e in events] == [
            "SESSION_OPEN",
            "DECISION",
            "ACTION",
            "ACTION",
            "APPROVAL",
            "SESSION_CLOSE",
        ]

    def test_a_rejected_approval_carries_what_the_human_was_shown(self, tmp_path):
        """The interesting narrative case: a human saw a summary and said no.
        Without `context_presented` the record proves a decision was made but
        not what it was made on."""
        path = write_session_file(tmp_path, actions=1, approval=True)

        approval = next(e for e in export_local(path).timeline["events"] if e["type"] == "APPROVAL")

        assert approval["decision"] == "rejected"
        assert approval["decision_reason"] == "amount exceeds mandate"
        assert approval["approver_id"] == "sile"
        assert approval["context_presented"]["tool_name"] == TOOLS[0]

    def test_the_session_block_counts_what_the_bundle_contains(self, tmp_path):
        path = write_session_file(tmp_path, actions=2, approval=True, decision=True)

        session = export_local(path).timeline["session"]

        assert session["objective"] == "quarterly run"
        assert session["status"] == "completed"
        assert (session["action_count"], session["decision_count"], session["approval_count"]) == (
            2,
            1,
            1,
        )

    def test_lost_records_appear_in_the_narrative(self, tmp_path):
        """A `RECORD_LOSS` tally is the only trace of records that were never
        written (ADR-013 D4a). It belongs in the timeline precisely because it
        is absent from everywhere else."""
        path = write_session_file(tmp_path, actions=2, previews=False)
        with path.open("a") as fh:
            fh.write(
                json.dumps(
                    {
                        "event_type": "RECORD_LOSS",
                        "session_id": load_local_session(path).session_id,
                        "lost_count": 3,
                        "first_sequence": 3,
                        "last_sequence": 5,
                        "reasons": {"OSError: No space left on device": 3},
                        "first_loss_at": "2026-08-21T10:05:00+00:00",
                    }
                )
                + "\n"
            )

        loss = next(e for e in export_local(path).timeline["events"] if e["type"] == "RECORD_LOSS")

        assert loss["lost_count"] == 3
        assert (loss["first_sequence"], loss["last_sequence"]) == (3, 5)


class TestLocalSource:
    def test_a_spool_file_exports_like_any_other_session(self, tmp_path):
        """A spooled session is an ordinary session file (ADR-013 D4), so an
        operator can hand over evidence for work that never reached the cloud."""
        spool = tmp_path / "spool"
        path = write_session_file(spool, actions=2, previews=False)

        bundle = export_local(path)

        assert bundle.verification["verdict"] == "VALID"
        assert bundle.manifest["source"]["backend"] == "jsonl"
        assert bundle.directory_name == f"evidence-{bundle.session_id}"

    def test_a_missing_file_is_an_actionable_error(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="Session file not found"):
            export_local(tmp_path / "nope.jsonl")

    def test_the_agent_block_admits_what_a_file_does_not_know(self, session_file):
        """A session file carries an `agent_id` and nothing else about the
        agent. The manifest says so rather than emitting empty name/owner
        fields that read as "unnamed agent"."""
        manifest = export_local(session_file).manifest

        assert set(manifest["agent"]) == {"agent_id"}
        assert manifest["agent"]["agent_id"]
