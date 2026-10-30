"""Cross-implementation sync-receipt vectors (issue #47).

`tests/fixtures/sync-receipt/v1.1/` is published for other implementations to
run. Nothing in it may say something the implementation does not do, so this
suite holds both ends:

* **The vectors are the implementation's output.** The committed spool files
  are replayed through the real `rootsign-admin sync`, verified by the real
  chain verifier, and exported by the real export path, and the results must
  equal what is committed. The vectors cannot drift from RootSign.
* **The standalone verifier has teeth.** Every rule it states is broken
  deliberately here, and the verifier must name the broken rule. A verifier
  that passes the committed set but also passes a receipt claiming
  `record: 2` for sequence 2 would only prove that the vectors agree with
  themselves.

Regenerate with `ROOTSIGN_UPDATE_VECTORS=1 python -m pytest
tests/unit/test_sync_receipt_vectors.py`, then review the diff.
"""

from __future__ import annotations

import ast
import copy
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from rootsign.sdk.chain import verify_session_local
from rootsign.sdk.export import RECEIPT_FILE, export_local
from rootsign.sdk.receipt import partition_is_total
from tests.support.receipt_vectors import (
    LANES,
    PROVENANCE_FIELDS,
    VECTORS_ROOT,
    Lane,
    _pinned,
    build_all,
    located,
    sums_text,
    sync_attempts,
    transcript,
)

VERIFIER_PATH = VECTORS_ROOT / "verify_vectors.py"


def _load_verifier() -> Any:
    spec = importlib.util.spec_from_file_location("verify_vectors", VERIFIER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


verifier = _load_verifier()


@pytest.fixture(scope="module", autouse=True)
def _maybe_regenerate() -> None:
    """`ROOTSIGN_UPDATE_VECTORS=1` rebuilds the set before anything asserts."""
    if os.environ.get("ROOTSIGN_UPDATE_VECTORS") != "1":
        return
    build_all(VECTORS_ROOT)
    (VECTORS_ROOT / "SHA256SUMS").write_text(sums_text(VECTORS_ROOT), encoding="utf-8")


def _expected(lane: Lane) -> dict[str, Any]:
    return json.loads((lane.directory / "expected.json").read_text(encoding="utf-8"))


def _committed(lane: Lane, key: str) -> Path:
    return lane.directory / _expected(lane)["files"][key]


def _receipt(lane: Lane) -> dict[str, Any]:
    return json.loads(_committed(lane, "receipt").read_text(encoding="utf-8"))


def _rules(report: Any) -> set[str]:
    """The rule IDs a report failed, e.g. {"R5", "R10"}."""
    found = set()
    for failure in report.failures:
        for token in failure.replace(":", " ").split():
            if len(token) in (2, 3) and token[0] in "RBETM" and token[1:].isdigit():
                found.add(token)
                break
    return found


LANE_IDS = [lane.slug for lane in LANES]
V1, V2, V3 = LANES


# ---------------------------------------------------------------------------
# The committed set
# ---------------------------------------------------------------------------


class TestTheCommittedSet:
    def test_the_standalone_verifier_accepts_it(self, capsys):
        assert verifier.main(["--root", str(VECTORS_ROOT)]) == 0, capsys.readouterr().out

    def test_sha256sums_is_current(self):
        """A README edit without a SHA256SUMS refresh would fail every mirror check."""
        assert (VECTORS_ROOT / "SHA256SUMS").read_text(encoding="utf-8") == sums_text(
            VECTORS_ROOT
        ), "SHA256SUMS is stale — regenerate with ROOTSIGN_UPDATE_VECTORS=1"

    @pytest.mark.parametrize("lane", LANES, ids=LANE_IDS)
    def test_every_attempt_partitions_by_rootsigns_own_check_too(self, lane: Lane):
        for attempt in _receipt(lane)["attempts"]:
            assert partition_is_total(attempt)

    def test_the_licence_split_is_stated_where_a_consumer_looks(self):
        readme = (VECTORS_ROOT / "README.md").read_text(encoding="utf-8")
        assert "CC0 1.0" in readme and "Apache-2.0" in readme
        assert (
            (VECTORS_ROOT / "LICENSE")
            .read_text(encoding="utf-8")
            .startswith("Creative Commons Legal Code\n\nCC0 1.0 Universal")
        )
        assert "SPDX-License-Identifier: Apache-2.0" in VERIFIER_PATH.read_text(encoding="utf-8")


class TestTheLanesSayWhatIssue47Asks:
    """The issue's checklist, one assertion each, against the committed files."""

    def test_v1_tampered_chain_with_the_stores_hash_chain_broken_on_the_receipt(self):
        expected, receipt = _expected(V1), _receipt(V1)
        assert expected["chain"]["verdict"] == "TAMPERED"
        rejected = receipt["attempts"][0]["outcomes"]["rejected"]
        assert [(r["error_code"], r["retryable"]) for r in rejected] == [
            ("HASH_CHAIN_BROKEN", False)
        ]

    def test_v2_incomplete_and_the_gap_is_never_an_observed_rejection(self):
        expected, receipt = _expected(V2), _receipt(V2)
        assert expected["chain"]["verdict"] == "INCOMPLETE"
        assert expected["chain"]["missing_ranges"] == [[2, 2]]
        assert all(not a["outcomes"]["rejected"] for a in receipt["attempts"])
        # The receipt rides in the bundle carrying the digest of the bytes
        # replayed — the retired spool file in `synced/`.
        bundle = _committed(V2, "bundle")
        assert (bundle / RECEIPT_FILE).is_file()
        replay = json.loads((bundle / "verification.json").read_text())["replay"]
        assert replay["spool_file_sha256"] == verifier.sha256_bytes(
            _committed(V2, "spool_file").read_bytes()
        )

    def test_v3_all_four_buckets_with_code_retryability_and_the_root_cause(self):
        attempts = _receipt(V3)["attempts"]
        for bucket in ("accepted", "duplicate", "not_attempted", "rejected"):
            assert any(a["outcomes"][bucket] for a in attempts), f"{bucket} never observed"
        for attempt in attempts:
            (refusal,) = attempt["outcomes"]["rejected"]
            assert refusal["error_code"] == "VALIDATION_ERROR" and refusal["retryable"] is False
            assert attempt["halted_at_sequence"] == 2
            # The trap, pinned: position 3 holds sequence 2.
            assert (refusal["record"], refusal["sequence"]) == (3, 2)


# ---------------------------------------------------------------------------
# The vectors are RootSign's output, not a description of it
# ---------------------------------------------------------------------------


def _comparable(attempt: dict[str, Any]) -> dict[str, Any]:
    return verifier.comparable(attempt)


@pytest.mark.parametrize("lane", LANES, ids=LANE_IDS)
class TestReplayingTheCommittedBytes:
    """Feed the published spool files back through the real code."""

    def _replay(self, lane: Lane, tmp_path: Path) -> tuple[Path, Path, Any]:
        spool_dir = tmp_path / "spool"
        sessions = spool_dir / "sessions"
        sessions.mkdir(parents=True)
        shutil.copyfile(_committed(lane, "spool_file"), sessions / f"{lane.session_id}.jsonl")
        with pytest.MonkeyPatch.context() as mp, _pinned(lane, mp, pin_receipts=False):
            run = sync_attempts(lane, spool_dir, mp)
        spool_file, receipt_file = located(spool_dir, lane.session_id)
        return spool_file, receipt_file, run

    def test_sync_writes_the_committed_receipt(self, lane: Lane, tmp_path: Path):
        spool_file, receipt_file, run = self._replay(lane, tmp_path)
        expected = _expected(lane)
        codes = run.exit_codes

        produced = json.loads(receipt_file.read_text())
        assert codes == expected["sync_exit_codes"]
        assert [_comparable(a) for a in produced["attempts"]] == expected["attempts"]
        assert [_comparable(a) for a in produced["attempts"]] == [
            _comparable(a) for a in _receipt(lane)["attempts"]
        ]
        # And sync left the pair where the vector says it did.
        assert spool_file.relative_to(tmp_path).as_posix() == expected["files"]["spool_file"]
        assert receipt_file.relative_to(tmp_path).as_posix() == expected["files"]["receipt"]

    def test_the_store_answers_what_the_committed_transcript_says(self, lane: Lane, tmp_path: Path):
        """The transcript is the store's side of the conversation. A second
        implementation builds its receipt from it, so it has to be what the
        store actually said when the committed spool bytes were replayed."""
        spool_file, _, run = self._replay(lane, tmp_path)
        committed = json.loads((lane.directory / "transcript.json").read_text())
        assert transcript(lane, spool_file, run) == committed

    def test_the_chain_verifier_reaches_the_committed_verdict(self, lane: Lane):
        result = verify_session_local(str(_committed(lane, "spool_file")))
        assert {
            "verdict": result.verdict.value,
            "first_invalid_sequence": result.first_invalid_sequence,
            "missing_ranges": [list(r) for r in result.missing_ranges],
        } == _expected(lane)["chain"]

    def test_export_produces_the_committed_bundle(self, lane: Lane, tmp_path: Path):
        spool_file, _, _ = self._replay(lane, tmp_path)
        produced = export_local(spool_file).documents()
        committed = _committed(lane, "bundle")

        for name in ("timeline.json", "redaction.json"):
            assert produced[name] == json.loads((committed / name).read_text()), name

        volatile = {"latest_attempt_id", "finished_at", "endpoint"}
        verification = json.loads((committed / "verification.json").read_text())
        got = copy.deepcopy(produced["verification.json"])
        for document in (got, verification):
            for key in volatile:
                document["replay"].pop(key)
            for entry in document["replay"]["observed_and_rejected"]:
                entry.pop("error_message", None)
        assert got == verification

        bundled = json.loads((committed / RECEIPT_FILE).read_text())
        assert [_comparable(a) for a in produced[RECEIPT_FILE]["attempts"]] == [
            _comparable(a) for a in bundled["attempts"]
        ]

    def test_the_pinned_fields_are_only_provenance(self, lane: Lane, tmp_path: Path):
        """Pinning must not be hiding a field the comparison relies on."""
        _, receipt_file, _ = self._replay(lane, tmp_path)
        produced = json.loads(receipt_file.read_text())["attempts"][0]
        committed = _receipt(lane)["attempts"][0]
        differing = {key for key in committed if committed[key] != produced.get(key)}
        assert differing <= set(PROVENANCE_FIELDS) | {"outcomes"}
        assert set(verifier.PROVENANCE) >= set(PROVENANCE_FIELDS)


def test_regeneration_reproduces_the_committed_spool_bytes(tmp_path: Path):
    """The spool writer's output is pinned too — a format change surfaces here,
    as a reviewable regeneration, not as a mysterious digest mismatch later."""
    build_all(tmp_path)
    for lane in LANES:
        expected = _expected(lane)
        rebuilt = tmp_path / lane.slug / expected["files"]["spool_file"]
        assert rebuilt.read_bytes() == _committed(lane, "spool_file").read_bytes(), lane.slug


# ---------------------------------------------------------------------------
# The verifier is standalone
# ---------------------------------------------------------------------------


class TestTheVerifierIsStandalone:
    def test_it_imports_only_the_standard_library(self):
        tree = ast.parse(VERIFIER_PATH.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)

    def test_it_runs_with_site_packages_switched_off(self):
        """`-S -I`: no site-packages, no PYTHONPATH, no cwd on the path. If it
        needed RootSign, or anything else installed, it would fail here."""
        result = subprocess.run(
            [sys.executable, "-S", "-I", str(VERIFIER_PATH)],
            capture_output=True,
            text=True,
            cwd=VECTORS_ROOT.parent,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "v3-duplicates-and-hard-failure" in result.stdout


# ---------------------------------------------------------------------------
# The verifier has teeth — every rule, broken on purpose
# ---------------------------------------------------------------------------


def _check(lane: Lane, mutate: Any, *, spool: bytes | None = None) -> set[str]:
    receipt = _receipt(lane)
    mutate(receipt)
    data = spool if spool is not None else _committed(lane, "spool_file").read_bytes()
    return _rules(verifier.check_receipt(receipt, data))


def _attempt(n: int = 0) -> Any:
    def _get(receipt: dict[str, Any]) -> dict[str, Any]:
        return receipt["attempts"][n]

    return _get


class TestTheVerifierCatches:
    def test_nothing_on_the_committed_receipts(self):
        for lane in LANES:
            assert _check(lane, lambda r: None) == set(), lane.slug

    def test_the_record_versus_sequence_trap(self):
        """The naive runner: refused record named by its sequence (2), with the
        ranges rearranged so the partition still holds. Only R10 can see it."""

        def mutate(receipt):
            outcomes = _attempt()(receipt)["outcomes"]
            outcomes["rejected"][0]["record"] = 2
            outcomes["accepted"] = [[1, 1], [3, 4]]

        assert _check(V1, mutate) == {"R10"}

    def test_an_overlap(self):
        found = _check(V3, lambda r: _attempt(1)(r)["outcomes"]["duplicate"].append([5, 5]))
        assert "R5" in found

    def test_a_gap(self):
        def mutate(receipt):
            _attempt()(receipt)["outcomes"]["not_attempted"] = []

        assert "R5" in _check(V3, mutate)

    def test_a_non_canonical_range(self):
        def mutate(receipt):
            _attempt()(receipt)["outcomes"]["accepted"] = [[1, 3], [4, 4]]

        assert _check(V2, mutate) == {"R4"}

    def test_delivery_that_is_not_a_prefix(self):
        def mutate(receipt):
            outcomes = _attempt()(receipt)["outcomes"]
            outcomes["accepted"] = [[1, 2], [6, 6]]
            outcomes["not_attempted"] = [[4, 5]]

        assert "R7" in _check(V3, mutate)

    def test_a_halt_that_names_no_rejection(self):
        found = _check(V1, lambda r: _attempt()(r).update(halted_at_sequence=1))
        assert found == {"R8"}

    def test_a_disposition_that_contradicts_the_outcomes(self):
        found = _check(V1, lambda r: _attempt()(r).update(disposition="fully_accepted"))
        assert "R9" in found

    def test_a_duplicate_filed_as_a_rejection(self):
        def mutate(receipt):
            _attempt()(receipt)["outcomes"]["rejected"][0]["error_code"] = "DUPLICATE_EVENT"

        assert "R6" in _check(V1, mutate)

    def test_a_missing_bucket(self):
        found = _check(V2, lambda r: _attempt()(r)["outcomes"].pop("not_attempted"))
        assert "R3" in found

    def test_another_schema_version(self):
        found = _check(V2, lambda r: _attempt()(r).update(schema_version="receipt/1.0"))
        assert "R2" in found

    def test_an_exhibit_changed_after_it_was_replayed(self):
        changed = _committed(V3, "spool_file").read_bytes() + b"\n"
        assert "R10" in _check(V3, lambda r: None, spool=changed)

    def test_a_record_count_that_is_not_the_files(self):
        def mutate(receipt):
            attempt = _attempt()(receipt)
            attempt["record_count"] = 5
            attempt["outcomes"]["accepted"] = [[1, 5]]

        assert _check(V2, mutate) == {"R10"}

    def test_a_lane_mismatch(self):
        """A receipt that is internally perfect but is not what the lane expects."""
        receipt = _receipt(V3)
        outcomes = receipt["attempts"][1]["outcomes"]
        outcomes["accepted"], outcomes["duplicate"] = outcomes["duplicate"], []
        report = verifier.check_against_lane(V3.directory, receipt)
        assert _rules(report) == {"E2"}
        assert _rules(verifier.check_receipt(receipt, None)) == set()


class TestTheVerifierCatchesBundleDamage:
    @pytest.fixture
    def bundle(self, tmp_path: Path) -> Path:
        target = tmp_path / "bundle"
        shutil.copytree(_committed(V2, "bundle"), target)
        return target

    def _rewrite(self, path: Path, mutate: Any) -> None:
        document = json.loads(path.read_text())
        mutate(document)
        path.write_text(json.dumps(document, indent=2) + "\n")

    def test_an_altered_file(self, bundle: Path):
        self._rewrite(bundle / "timeline.json", lambda d: d.update(note="added"))
        assert "B1" in _rules(verifier.check_bundle(bundle, _receipt(V2))[0])

    def test_a_replay_block_that_is_null_rather_than_absent(self, bundle: Path):
        (bundle / RECEIPT_FILE).unlink()
        self._rewrite(bundle / "verification.json", lambda d: d.update(replay=None))
        assert "B4" in _rules(verifier.check_bundle(bundle, None)[0])

    def test_a_missing_record_rewritten_as_a_rejection(self, bundle: Path):
        """V2's whole point: the gap at sequence 2 must never become a refusal."""

        def mutate(document):
            document["attempts"][0]["outcomes"]["rejected"] = [
                {"record": 2, "sequence": 2, "error_code": "VALIDATION_ERROR", "retryable": False}
            ]

        self._rewrite(bundle / RECEIPT_FILE, mutate)
        assert "B6" in _rules(verifier.check_bundle(bundle, None)[0])

    def test_a_receipt_that_drifted_from_the_spool_trees(self, bundle: Path):
        receipt = _receipt(V2)
        receipt["attempts"][0]["operator"] = "someone-else"
        assert "B2" in _rules(verifier.check_bundle(bundle, receipt)[0])


class TestTheVerifierCatchesTranscriptDamage:
    def _check(self, lane: Lane, mutate: Any) -> set[str]:
        document = json.loads((lane.directory / "transcript.json").read_text())
        mutate(document)
        spool = verifier.read_envelope_lines(_committed(lane, "spool_file").read_bytes())
        return _rules(verifier.check_transcript(document, spool, _receipt(lane)))

    def test_nothing_on_the_committed_transcripts(self):
        for lane in LANES:
            assert self._check(lane, lambda d: None) == set(), lane.slug

    def test_a_request_that_skips_records(self):
        def mutate(document):
            exchange = document["attempts"][0]["exchanges"][1]
            exchange["records"] = [4, 5]
            for answer, record in zip(exchange["responses"], (4, 5)):
                answer["record"] = record

        assert "T1" in self._check(V3, mutate)

    def test_an_answer_bound_to_the_wrong_spool_line(self):
        def mutate(document):
            document["attempts"][0]["exchanges"][0]["responses"][0]["event_id"] = "not-it"

        assert self._check(V1, mutate) == {"T2"}

    def test_a_receipt_the_store_answers_do_not_support(self):
        """The store said record 4 was accepted; a receipt filing it as a
        duplicate contradicts the transcript, whatever else it gets right."""

        def mutate(document):
            answer = document["attempts"][0]["exchanges"][1]["responses"][1]
            answer.update(status="rejected", error_code="DUPLICATE_EVENT", retryable=False)

        assert self._check(V3, mutate) == {"T3"}


class TestASecondImplementationCanUseTheTranscript:
    """The path issue #47 exists for, end to end: write a receipt from the spool
    file and the transcript alone — no store, no hash — and have the verifier
    accept it against the lane."""

    @pytest.mark.parametrize("lane", LANES, ids=LANE_IDS)
    def test_a_receipt_written_from_the_transcript_passes_the_lane(
        self, lane: Lane, tmp_path: Path, capsys
    ):
        spool_bytes = _committed(lane, "spool_file").read_bytes()
        spool = verifier.read_envelope_lines(spool_bytes)
        document = json.loads((lane.directory / "transcript.json").read_text())
        attempts = []
        for n, attempt in enumerate(document["attempts"], start=1):
            derived = verifier.derive_attempt(spool, attempt["exchanges"])
            attempts.append(
                {
                    "schema_version": "receipt/1.1",
                    "attempt_id": f"elsewhere-{n}",
                    "started_at": "2026-10-29T00:00:00Z",
                    "finished_at": "2026-10-29T00:00:01Z",
                    "endpoint": "https://another.example.test/ingest",
                    "sdk_version": "other-implementation 0.0.1",
                    "operator": "someone-else",
                    "spool_file_sha256": verifier.sha256_bytes(spool_bytes),
                    **derived,
                }
            )
        theirs = tmp_path / "theirs.sync.json"
        theirs.write_text(json.dumps({"session_id": lane.session_id, "attempts": attempts}))

        code = verifier.main(["--lane", str(lane.directory), "--receipt", str(theirs)])
        assert code == 0, capsys.readouterr().out


def test_sha256sums_catches_an_altered_vector(tmp_path: Path):
    copied = tmp_path / "v1.1"
    shutil.copytree(VECTORS_ROOT, copied, ignore=shutil.ignore_patterns("__pycache__"))
    target = copied / V3.slug / "expected.json"
    target.write_text(target.read_text().replace('"partial"', '"fully_accepted"', 1))
    assert _rules(verifier.check_sums(copied)) == {"M1"}
