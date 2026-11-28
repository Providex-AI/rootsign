"""The session seal (ADR-016): its golden vectors, and what checking one reports.

`rootsign/seal.py` is the seal's specification. The vectors in
`tests/fixtures/session-seal/v1/` freeze its output, so a change that moves a
digest is caught here rather than by a verifier in the field calling an honest
session TAMPERED. The rest pins `check_seal`: each edit it must detect, the
order that keeps a missing record from hiding an edit, and the fields it
deliberately ignores because the stores record them differently.
"""

from __future__ import annotations

import dataclasses
import json
import random
from pathlib import Path
from uuid import UUID

import pytest

from rootsign.seal import (
    INCOMPLETE_OUTCOMES,
    SEAL_VERSION,
    TAMPERED_OUTCOMES,
    SealOutcome,
    SessionFacts,
    check_seal,
    compute_seal,
)

VECTORS_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "session-seal" / "v1" / "vectors.json"
)
VECTORS = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))


def _facts(raw: dict, **overrides) -> SessionFacts:
    facts = SessionFacts(
        session_id=raw["session_id"],
        agent_id=raw["agent_id"],
        objective=raw["objective"],
        user_id=raw["user_id"],
        status=raw["status"],
        decisions=tuple(raw.get("decisions", ())),
        approvals=tuple(raw.get("approvals", ())),
        actions=tuple(raw.get("actions", ())),
    )
    return dataclasses.replace(facts, **overrides)


def _vector(description_start: str) -> dict:
    (vector,) = [v for v in VECTORS["vectors"] if v["description"].startswith(description_start)]
    return vector


class TestVectors:
    def test_the_fixture_pins_this_version(self):
        assert VECTORS["seal_version"] == SEAL_VERSION == 1

    @pytest.mark.parametrize("vector", VECTORS["vectors"], ids=lambda v: v["description"])
    def test_each_vector_reproduces(self, vector):
        seal = compute_seal(_facts(vector["facts"]), final_sequence=vector.get("final_sequence"))
        assert seal == vector["expected"]

    @pytest.mark.parametrize("vector", VECTORS["vectors"], ids=lambda v: v["description"])
    def test_each_vector_checks_as_sealed_against_its_own_facts(self, vector):
        facts = _facts(vector["facts"])
        check = check_seal(vector["expected"], facts)
        if vector.get("final_sequence", facts.chain_length) > facts.chain_length:
            assert check.outcome is SealOutcome.MISSING_AT_END
        else:
            assert check.sealed, check.detail


def _two_actions() -> dict:
    return _vector("two actions")["facts"]


def _held_call() -> dict:
    return _vector("a held call")["facts"]


def _sealed(raw: dict) -> tuple[dict, SessionFacts]:
    facts = _facts(raw)
    return compute_seal(facts), facts


class TestWhatACheckReports:
    def test_an_intact_session_is_sealed(self):
        seal, facts = _sealed(_two_actions())
        check = check_seal(seal, facts)
        assert check.sealed and check.detail == "sealed at 2 record(s)"

    @pytest.mark.parametrize("seal", [None, {}])
    def test_no_seal_is_unsealed(self, seal):
        check = check_seal(seal, _facts(_two_actions()))
        assert check.outcome is SealOutcome.UNSEALED
        assert check.detail == "not sealed: its end cannot be verified"

    def test_an_unknown_version_is_not_trusted(self):
        seal, facts = _sealed(_two_actions())
        check = check_seal({**seal, "version": 2}, facts)
        assert check.outcome is SealOutcome.UNKNOWN_VERSION

    def test_records_cut_from_the_end(self):
        seal, facts = _sealed(_two_actions())
        cut = dataclasses.replace(facts, actions=facts.actions[:1])
        check = check_seal(seal, cut)
        assert check.outcome is SealOutcome.MISSING_AT_END
        assert check.missing == (2, 2)
        assert check.detail.startswith("1 record(s) missing at the end (sealed at 2, 1 present)")

    def test_every_action_cut(self):
        seal, facts = _sealed(_two_actions())
        check = check_seal(seal, dataclasses.replace(facts, actions=()))
        assert check.outcome is SealOutcome.MISSING_AT_END and check.missing == (1, 2)

    def test_a_shorter_count_in_the_seal_is_caught_by_its_own_hash(self):
        """Cutting the tail and lowering final_sequence to match: the seal no longer recomputes."""
        seal, facts = _sealed(_two_actions())
        cut = dataclasses.replace(facts, actions=facts.actions[:1])
        check = check_seal({**seal, "final_sequence": 1}, cut)
        assert check.outcome is SealOutcome.SEAL_EDITED

    def test_records_after_the_seal(self):
        seal, facts = _sealed(_two_actions())
        extra = {**facts.actions[1], "sequence_number": 3, "self_hash": "f" * 64}
        check = check_seal(seal, dataclasses.replace(facts, actions=(*facts.actions, extra)))
        assert check.outcome is SealOutcome.RECORDS_AFTER_SEAL

    def test_a_replaced_last_record(self):
        seal, facts = _sealed(_two_actions())
        swapped = {**facts.actions[1], "self_hash": "f" * 64}
        check = check_seal(seal, dataclasses.replace(facts, actions=(facts.actions[0], swapped)))
        assert check.outcome is SealOutcome.TAIL_MISMATCH

    @pytest.mark.parametrize(
        "field, value", [("objective", "invoice EVIL"), ("user_id", "someone"), ("agent_id", None)]
    )
    def test_an_edited_open_record(self, field, value):
        seal, facts = _sealed(_two_actions())
        check = check_seal(seal, dataclasses.replace(facts, **{field: value}))
        assert check.outcome is SealOutcome.OPEN_EDITED

    def test_an_edited_close_status(self):
        seal, facts = _sealed(_two_actions())
        check = check_seal(seal, dataclasses.replace(facts, status="failed"))
        assert check.outcome is SealOutcome.SEAL_EDITED

    def test_an_edited_approval(self):
        seal, facts = _sealed(_held_call())
        approved = ({**facts.approvals[0], "decision": "approved"},)
        check = check_seal(seal, dataclasses.replace(facts, approvals=approved))
        assert check.outcome is SealOutcome.SIDE_EDITED

    def test_a_deleted_approval(self):
        seal, facts = _sealed(_held_call())
        check = check_seal(seal, dataclasses.replace(facts, approvals=()))
        assert check.outcome is SealOutcome.SIDE_EDITED

    def test_an_edited_authorization_status(self):
        seal, facts = _sealed(_held_call())
        actions = ({**facts.actions[0], "authorization_status": "human_approved"}, facts.actions[1])
        check = check_seal(seal, dataclasses.replace(facts, actions=actions))
        assert check.outcome is SealOutcome.SIDE_EDITED

    def test_an_edited_decision_and_a_repointed_action(self):
        seal, facts = _sealed(_two_actions())
        edited = ({**facts.decisions[0], "reasoning_summary": "Approved by the CFO."},)
        assert check_seal(seal, dataclasses.replace(facts, decisions=edited)).outcome is (
            SealOutcome.SIDE_EDITED
        )
        repointed = (
            {**facts.actions[0], "decision_id": None},
            {**facts.actions[1], "decision_id": facts.decisions[0]["decision_id"]},
        )
        assert check_seal(seal, dataclasses.replace(facts, actions=repointed)).outcome is (
            SealOutcome.SIDE_EDITED
        )


class TestPrecedence:
    def test_an_open_edit_is_reported_even_when_records_are_missing(self):
        """TAMPERED wins: a missing record must never hide an edit the verifier can still see."""
        seal, facts = _sealed(_two_actions())
        both = dataclasses.replace(facts, actions=facts.actions[:1], objective="invoice EVIL")
        assert check_seal(seal, both).outcome is SealOutcome.OPEN_EDITED

    def test_a_missing_record_says_the_side_records_were_not_checked(self):
        seal, facts = _sealed(_held_call())
        check = check_seal(seal, dataclasses.replace(facts, actions=()))
        assert check.outcome is SealOutcome.MISSING_AT_END
        assert "decisions and approvals could not be checked" in check.detail

    def test_outcomes_partition_into_the_verdicts(self):
        neutral = {SealOutcome.SEALED, SealOutcome.UNSEALED, SealOutcome.UNKNOWN_VERSION}
        assert INCOMPLETE_OUTCOMES | TAMPERED_OUTCOMES | neutral == set(SealOutcome)
        assert not INCOMPLETE_OUTCOMES & TAMPERED_OUTCOMES


class TestWhatDoesNotMoveTheSeal:
    def test_input_order_does_not_matter(self):
        raw = _vector("non-ASCII")["facts"]
        facts = _facts(raw)
        shuffled = list(facts.decisions)
        random.Random(7).shuffle(shuffled)
        reversed_ = dataclasses.replace(facts, decisions=tuple(reversed(shuffled)))
        assert compute_seal(reversed_) == compute_seal(facts)

    def test_a_uuid_and_its_string_seal_alike(self):
        raw = _two_actions()
        as_uuids = _facts(
            raw,
            session_id=UUID(raw["session_id"]),
            agent_id=UUID(raw["agent_id"]),
            actions=tuple({**a, "action_id": UUID(a["action_id"])} for a in raw["actions"]),
        )
        assert compute_seal(as_uuids) == compute_seal(_facts(raw))

    def test_fields_the_stores_record_differently_are_outside_it(self):
        """Times and error_message: sealing them would make honest stores disagree."""
        seal, facts = _sealed(_held_call())
        retimed = dataclasses.replace(
            facts,
            approvals=tuple({**a, "timestamp": "2026-11-27T10:00:00Z"} for a in facts.approvals),
            actions=tuple({**a, "timestamp": "2026-11-27T10:00:00Z"} for a in facts.actions),
        )
        assert check_seal(seal, retimed).sealed

    def test_without_an_sdk_count_the_writer_counts(self):
        facts = _facts(_two_actions())
        assert compute_seal(facts)["final_sequence"] == 2
        assert compute_seal(facts, final_sequence=5)["final_sequence"] == 5


def test_a_seal_whose_count_is_not_a_number_is_not_trusted():
    """A writer bug, not an attack: the seal recomputes, but its count means nothing."""
    from rootsign.seal import _seal_hash

    facts = _facts(_two_actions())
    seal = {**compute_seal(facts), "final_sequence": "2"}
    seal["seal_hash"] = _seal_hash(seal, session_id=facts.session_id, status=facts.status)
    check = check_seal(seal, facts)
    assert check.outcome is SealOutcome.SEAL_EDITED and check.detail == "seal has no final_sequence"
