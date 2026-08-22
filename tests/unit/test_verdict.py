"""The shared verification vocabulary (ADR-013 Decision 4b, T2.4b).

`rootsign.verdict` is deliberately small and deliberately shared: both
verifiers import the same gap detection, the same "is this break explained by
a gap" test, and the same precedence rule. Testing it here — apart from either
verifier — is what makes the parity claim in T2.4c a statement about two
callers of one rule rather than two rules that happen to agree today.
"""

from __future__ import annotations

import pytest

from rootsign.verdict import (
    EXIT_CODES,
    FailureKind,
    Verdict,
    decide,
    describe_missing,
    exit_code,
    explains_break,
    missing_count,
    missing_ranges,
)


class TestMissingRanges:
    def test_a_dense_chain_has_no_gaps(self):
        assert missing_ranges([1, 2, 3, 4]) == []

    def test_out_of_order_input_is_still_measured_correctly(self):
        assert missing_ranges([4, 1, 3, 2]) == []

    def test_a_single_missing_record(self):
        assert missing_ranges([1, 2, 4]) == [(3, 3)]

    def test_a_run_of_missing_records_is_one_range(self):
        assert missing_ranges([1, 5]) == [(2, 4)]

    def test_several_runs(self):
        assert missing_ranges([1, 4, 5, 9]) == [(2, 3), (6, 8)]

    def test_a_chain_that_does_not_start_at_one_is_missing_its_head(self):
        """Chains are 1-based by construction, so a missing #1 is detectable."""
        assert missing_ranges([2, 3]) == [(1, 1)]

    def test_no_records_means_no_gaps(self):
        assert missing_ranges([]) == []

    def test_duplicates_are_not_gaps(self):
        # A duplicate is a separate (worse) failure the caller handles.
        assert missing_ranges([1, 2, 2, 3]) == []

    def test_a_truncated_tail_is_undetectable_from_the_records_alone(self):
        """Documented limit: nothing in a chain says how long it should be.

        Records 4 and 5 being deleted from the end leaves [1,2,3], which is a
        perfectly dense chain. SESSION_CLOSE's `total_actions` is the
        cross-check for that case — which is why the store warns when the two
        disagree rather than shrugging.
        """
        assert missing_ranges([1, 2, 3]) == []


class TestExplainsBreak:
    def test_the_record_after_a_gap_is_explained(self):
        assert explains_break(4, [(2, 3)]) is True

    def test_a_record_elsewhere_is_not(self):
        assert explains_break(5, [(2, 3)]) is False
        assert explains_break(2, [(2, 3)]) is False

    def test_the_first_record_of_a_headless_chain_is_explained(self):
        assert explains_break(2, [(1, 1)]) is True

    def test_nothing_is_explained_without_gaps(self):
        assert explains_break(3, []) is False


class TestPrecedence:
    def test_clean_chain_is_valid(self):
        assert decide(missing=[], failure_kind=None) is Verdict.VALID

    def test_gaps_alone_are_incomplete(self):
        assert decide(missing=[(2, 3)], failure_kind=None) is Verdict.INCOMPLETE

    def test_a_failure_alone_is_tampered(self):
        assert decide(missing=[], failure_kind=FailureKind.SELF_HASH_MISMATCH) is Verdict.TAMPERED

    @pytest.mark.parametrize("kind", list(FailureKind))
    def test_tampered_wins_over_incomplete_for_every_failure_kind(self, kind: FailureKind):
        """Worst verdict wins — the whole point of the rule.

        A tampered session that also has gaps must never be reported as "just
        incomplete", or an attacker could downgrade a theft to a shrug by
        deleting one extra record.
        """
        assert decide(missing=[(2, 3)], failure_kind=kind) is Verdict.TAMPERED


class TestExitCodes:
    def test_the_three_codes(self):
        assert exit_code(Verdict.VALID) == 0
        assert exit_code(Verdict.TAMPERED) == 1
        assert exit_code(Verdict.INCOMPLETE) == 2

    def test_every_verdict_has_a_code(self):
        assert set(EXIT_CODES) == set(Verdict)

    def test_existing_consumers_are_unaffected(self):
        """The addition is backward compatible by arithmetic, not by promise.

        Anything testing `!= 0` still sees failure for both failure verdicts;
        anything testing `== 1` still means exactly TAMPERED.
        """
        assert exit_code(Verdict.VALID) == 0
        assert all(exit_code(v) != 0 for v in (Verdict.TAMPERED, Verdict.INCOMPLETE))


class TestRendering:
    def test_describe_missing_reads_naturally(self):
        assert describe_missing([(3, 3)]) == "3"
        assert describe_missing([(3, 5), (9, 9)]) == "3-5, 9"

    def test_missing_count_sums_the_ranges(self):
        assert missing_count([(3, 5), (9, 9)]) == 4
        assert missing_count([]) == 0

    def test_verdicts_serialize_as_their_own_names(self):
        # The bundle schema (ADR-014) and the MCP tool result both carry these
        # as plain strings; a str-Enum keeps that free.
        assert Verdict.INCOMPLETE.value == "INCOMPLETE"
        assert f"{Verdict.TAMPERED.value}" == "TAMPERED"
