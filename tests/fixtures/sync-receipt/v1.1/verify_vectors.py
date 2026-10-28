#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Providex AI
"""Standalone verifier for RootSign sync-receipt vectors, schema receipt/1.1.

Python standard library only, and deliberately no `rootsign` import: the point
is that a receipt can be checked by someone who does not trust, or does not
have, the implementation that wrote it.

Three ways to run it:

    python verify_vectors.py
        Check this directory: SHA256SUMS, then every lane against its
        expected.json. Exit 0 if everything holds, 1 otherwise.

    python verify_vectors.py --receipt R.sync.json [--spool S.jsonl]
        Check any receipt/1.1 file against the schema invariants, and, given
        the spool file it describes, against the bytes it claims to describe.

    python verify_vectors.py --lane v3-duplicates-and-hard-failure --receipt R.sync.json
        Check a receipt *your* implementation produced by replaying a lane's
        spool file, against the outcomes that lane expects.

What this does NOT check: the hash chain. Recomputing ADR-001 `self_hash`
values would mean a second implementation of the canonical hash, and every
lane already pins the verdict RootSign's own verifier reached
(expected.json `chain`). Run your chain verifier over the lane's spool file
and compare. `tests/fixtures/hash_vectors.json` pins the hash itself.

Rule identifiers (R*, B*, E*) are the ones README.md describes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "receipt/1.1"
DISPOSITIONS = ("fully_accepted", "partial", "transport_failed")
RANGE_BUCKETS = ("accepted", "duplicate", "not_attempted")
ENVELOPE_TYPES = (
    "SESSION_OPEN",
    "ACTION_RECORD",
    "DECISION_RECORD",
    "APPROVAL_RECORD",
    "SESSION_CLOSE",
)
VERDICTS = ("VALID", "TAMPERED", "INCOMPLETE")
#: Attempt fields that describe the run rather than the evidence. Not part of
#: what a lane expects; every implementation will produce its own.
PROVENANCE = ("attempt_id", "started_at", "finished_at", "endpoint", "sdk_version", "operator")
HERE = Path(__file__).resolve().parent


class Report:
    """Collects findings. A rule either holds or names what broke it."""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def check(self, rule: str, ok: bool, detail: str) -> bool:
        if ok:
            self.passed += 1
        else:
            self.failures.append(f"{rule}: {detail}")
        return ok

    def merge(self, other: Report, prefix: str) -> None:
        self.passed += other.passed
        self.failures.extend(f"{prefix}{failure}" for failure in other.failures)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_int(value: Any) -> bool:
    # bool is an int subclass in Python; JSON `true` is not a position.
    return isinstance(value, int) and not isinstance(value, bool)


def _is_hex64(value: Any) -> bool:
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


# ---------------------------------------------------------------------------
# The spool file — what a receipt claims to describe
# ---------------------------------------------------------------------------


def read_envelope_lines(data: bytes) -> list[dict[str, Any]]:
    """The spool file's envelopes, in file order — record position 1 is index 0.

    Lines whose `event_type` is not one of the five envelope types (today only
    the loss ledger's RECORD_LOSS tally) are annotations: never replayed, so
    they hold no position. Blank lines hold none either.
    """
    envelopes = []
    for line in data.decode("utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("event_type") in ENVELOPE_TYPES:
            envelopes.append(record)
    return envelopes


def _sequence_of(record: dict[str, Any]) -> Any:
    """A spool ACTION line carries its sequence at the top level (flattened)."""
    if "sequence_number" in record:
        return record["sequence_number"]
    return (record.get("payload") or {}).get("sequence_number")


# ---------------------------------------------------------------------------
# R — invariants every receipt/1.1 attempt satisfies
# ---------------------------------------------------------------------------


def expand(ranges: Any, report: Report, where: str, record_count: int) -> list[int] | None:
    """Positions named by `[[start, end], ...]`. None if malformed (reported)."""
    if not isinstance(ranges, list):
        report.check("R4", False, f"{where} is not a list")
        return None
    positions: list[int] = []
    previous_end = None
    for pair in ranges:
        well_formed = (
            isinstance(pair, list)
            and len(pair) == 2
            and _is_int(pair[0])
            and _is_int(pair[1])
            and 1 <= pair[0] <= pair[1] <= record_count
        )
        if not report.check(
            "R4",
            well_formed,
            f"{where} range {pair!r} is not [start, end] within 1..{record_count}",
        ):
            return None
        # Canonical form: ascending, maximal runs. [[1,1],[2,2]] names the same
        # records as [[1,2]] but is not how a receipt writes them, and two
        # implementations must produce comparable receipts, not merely
        # equivalent ones.
        report.check(
            "R4",
            previous_end is None or pair[0] > previous_end + 1,
            f"{where} range {pair!r} overlaps, touches, or precedes the range before it — not canonical",
        )
        previous_end = pair[1]
        positions.extend(range(pair[0], pair[1] + 1))
    return positions


def check_attempt(attempt: Any, report: Report, spool: list[dict[str, Any]] | None = None) -> None:
    """R2-R10 on one attempt. `spool` enables the position checks (R10)."""
    if not report.check("R2", isinstance(attempt, dict), "attempt is not an object"):
        return
    version = attempt.get("schema_version")
    if not report.check(
        "R2",
        version == SCHEMA_VERSION,
        f"schema_version is {version!r}, this verifier checks {SCHEMA_VERSION!r}",
    ):
        return

    for key in ("attempt_id", "started_at", "finished_at", "endpoint", "sdk_version", "operator"):
        report.check("R3", isinstance(attempt.get(key), str), f"{key} missing or not a string")
    report.check(
        "R3",
        _is_hex64(attempt.get("spool_file_sha256")),
        "spool_file_sha256 is not 64 lowercase hex characters",
    )
    record_count = attempt.get("record_count")
    if not report.check(
        "R3",
        _is_int(record_count) and record_count >= 0,
        "record_count missing or not a non-negative integer",
    ):
        return
    halted = attempt.get("halted_at_sequence")
    report.check(
        "R3", halted is None or _is_int(halted), "halted_at_sequence is neither null nor an integer"
    )
    disposition = attempt.get("disposition")
    report.check(
        "R3",
        disposition in DISPOSITIONS,
        f"disposition {disposition!r} is not one of {DISPOSITIONS}",
    )
    transport_error = attempt.get("transport_error")
    report.check(
        "R3",
        transport_error is None or isinstance(transport_error, str),
        "transport_error present but not a string",
    )

    outcomes = attempt.get("outcomes")
    if not report.check("R3", isinstance(outcomes, dict), "outcomes missing or not an object"):
        return
    for key in (*RANGE_BUCKETS, "rejected"):
        report.check("R3", key in outcomes, f"outcomes.{key} missing (receipt/1.1 writes all four)")

    buckets: dict[str, list[int]] = {}
    for key in RANGE_BUCKETS:
        positions = expand(outcomes.get(key, []), report, f"outcomes.{key}", record_count)
        if positions is None:
            return
        buckets[key] = positions

    rejected = outcomes.get("rejected", [])
    if not report.check("R6", isinstance(rejected, list), "outcomes.rejected is not a list"):
        return
    rejected_positions: list[int] = []
    for entry in rejected:
        ok = (
            isinstance(entry, dict)
            and _is_int(entry.get("record"))
            and 1 <= entry["record"] <= record_count
            and isinstance(entry.get("error_code"), str)
            and isinstance(entry.get("retryable"), bool)
            and ("sequence" not in entry or _is_int(entry["sequence"]))
            and (entry.get("error_message") is None or isinstance(entry.get("error_message"), str))
        )
        if not report.check(
            "R6",
            ok,
            f"rejected entry {entry!r} lacks record/error_code/retryable or has a mistyped field",
        ):
            return
        report.check(
            "R6",
            entry["error_code"] != "DUPLICATE_EVENT",
            f"record {entry['record']}: DUPLICATE_EVENT belongs in `duplicate`, not `rejected`",
        )
        rejected_positions.append(entry["record"])
    buckets["rejected"] = rejected_positions

    # R5 — the partition. A list, not a set, so a position claimed by two
    # buckets fails here instead of being de-duplicated into looking right.
    covered = sorted(p for positions in buckets.values() for p in positions)
    expected = list(range(1, record_count + 1))
    if covered != expected:
        overlap = sorted(p for p, n in Counter(covered).items() if n > 1)
        gap = sorted(set(expected) - set(covered))
        report.check(
            "R5", False, f"buckets do not partition 1..{record_count}: overlap {overlap}, gap {gap}"
        )
    else:
        report.check("R5", True, "")

    # R7 — delivery is a prefix: nothing after the halt was solicited, so
    # every not_attempted position comes after every observed one.
    observed = [p for key in ("accepted", "duplicate", "rejected") for p in buckets[key]]
    if buckets["not_attempted"] and observed:
        report.check(
            "R7",
            min(buckets["not_attempted"]) > max(observed),
            f"not_attempted {min(buckets['not_attempted'])} precedes observed position {max(observed)}",
        )

    # R8 — halted_at_sequence names a refusal the receipt actually records.
    if halted is not None:
        report.check(
            "R8",
            any(entry.get("sequence") == halted for entry in rejected),
            f"halted_at_sequence {halted} names no rejected entry's sequence",
        )

    # R9 — disposition agrees with the outcomes.
    if disposition == "fully_accepted":
        report.check(
            "R9",
            not rejected
            and not buckets["not_attempted"]
            and halted is None
            and transport_error is None,
            "fully_accepted but something was rejected, not attempted, or halted",
        )
    elif disposition == "partial":
        report.check("R9", bool(rejected), "partial but nothing was rejected")
        report.check(
            "R9",
            transport_error is None,
            "partial carries a transport_error (that is transport_failed)",
        )
    elif disposition == "transport_failed":
        report.check(
            "R9",
            transport_error is not None,
            "transport_failed without the transport_error that caused it",
        )

    # R10 — positions are positions in *this* file, and sequences are the
    # sequences recorded there. The record-vs-sequence trap lives here.
    if spool is not None:
        report.check(
            "R10",
            record_count == len(spool),
            f"record_count {record_count} but the spool file holds {len(spool)} envelopes",
        )
        for entry in rejected:
            if entry["record"] > len(spool) or "sequence" not in entry:
                continue
            line = spool[entry["record"] - 1]
            report.check(
                "R10",
                line.get("event_type") == "ACTION_RECORD"
                and _sequence_of(line) == entry["sequence"],
                f"rejected record {entry['record']} claims sequence {entry['sequence']}, "
                f"but position {entry['record']} in the spool file is "
                f"{line.get('event_type')} with sequence {_sequence_of(line)!r}",
            )


def check_receipt(document: Any, spool_bytes: bytes | None = None) -> Report:
    """R1-R10 over a whole receipt file.

    The spool binding (R10, and the digest) applies to attempts whose
    `spool_file_sha256` matches the given bytes. The LATEST attempt must
    match: the file as it stands is the file the last attempt replayed, and a
    mismatch there means the exhibit changed after it was observed.
    """
    report = Report()
    ok = (
        isinstance(document, dict)
        and isinstance(document.get("session_id"), str)
        and isinstance(document.get("attempts"), list)
        and document["attempts"]
    )
    if not report.check("R1", bool(ok), "not {session_id: str, attempts: [non-empty]}"):
        return report

    spool = None
    digest = None
    if spool_bytes is not None:
        digest = sha256_bytes(spool_bytes)
        spool = read_envelope_lines(spool_bytes)
        latest = document["attempts"][-1]
        report.check(
            "R10",
            isinstance(latest, dict) and latest.get("spool_file_sha256") == digest,
            f"latest attempt hashed {latest.get('spool_file_sha256') if isinstance(latest, dict) else None}, "
            f"the spool file is {digest} — the exhibit changed after it was replayed",
        )

    for index, attempt in enumerate(document["attempts"], start=1):
        sub = Report()
        bound = (
            spool
            if isinstance(attempt, dict) and attempt.get("spool_file_sha256") == digest
            else None
        )
        check_attempt(attempt, sub, bound)
        report.merge(sub, f"attempt {index}: ")
    return report


# ---------------------------------------------------------------------------
# B — the evidence bundle carries the receipt without blurring the layers
# ---------------------------------------------------------------------------


def check_bundle(bundle_dir: Path, receipt: dict[str, Any] | None) -> tuple[Report, dict[str, Any]]:
    report = Report()
    manifest_path = bundle_dir / "manifest.json"
    if not report.check("B1", manifest_path.is_file(), f"no manifest.json in {bundle_dir}"):
        return report, {}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = manifest.get("files") or {}
    for name, digest in sorted(files.items()):
        path = bundle_dir / name
        report.check(
            "B1",
            path.is_file() and sha256_bytes(path.read_bytes()) == digest,
            f"{name} is missing or does not match manifest.json",
        )
    unlisted = sorted(
        {p.name for p in bundle_dir.iterdir() if p.is_file()} - set(files) - {"manifest.json"}
    )
    report.check("B1", not unlisted, f"files present but not in manifest.json: {unlisted}")

    verification = json.loads((bundle_dir / "verification.json").read_text(encoding="utf-8"))
    report.check(
        "B3",
        verification.get("verdict") in VERDICTS,
        f"verdict {verification.get('verdict')!r} not in {VERDICTS}",
    )
    report.check(
        "B3",
        manifest.get("verdict") == verification.get("verdict"),
        "manifest.json and verification.json disagree on the verdict",
    )

    bundled_path = bundle_dir / "sync-receipt.json"
    bundled = (
        json.loads(bundled_path.read_text(encoding="utf-8")) if bundled_path.is_file() else None
    )

    # B2 — the bundle carries the receipt the spool tree holds, unaltered.
    if receipt is not None:
        report.check(
            "B2",
            bundled == receipt,
            "sync-receipt.json is absent or differs from the spool tree's receipt",
        )

    # B4 — `replay` is absent (not null) exactly when there is no receipt.
    report.check(
        "B4",
        ("replay" in verification) == (bundled is not None),
        "verification.json `replay` block present without a receipt, or missing with one",
    )

    if bundled is not None and "replay" in verification:
        latest = bundled["attempts"][-1]
        outcomes = latest.get("outcomes") or {}
        summary = {
            "attempts": len(bundled["attempts"]),
            "latest_attempt_id": latest.get("attempt_id"),
            "latest_disposition": latest.get("disposition"),
            "spool_file_sha256": latest.get("spool_file_sha256"),
            "record_count": latest.get("record_count"),
            "halted_at_sequence": latest.get("halted_at_sequence"),
            "observed_and_rejected": outcomes.get("rejected") or [],
            "not_attempted": outcomes.get("not_attempted") or [],
        }
        replay = verification["replay"]
        for key, value in summary.items():
            report.check(
                "B5",
                replay.get(key) == value,
                f"replay.{key} is {replay.get(key)!r}, latest attempt says {value!r}",
            )

    # B6 — two layers, two artifacts. The receipt never carries a chain
    # verdict, and a record the chain says is MISSING is never recorded as a
    # record the store REFUSED.
    if bundled is not None:
        text = json.dumps(bundled)
        report.check(
            "B6",
            '"verdict"' not in text and not any(f'"{v}"' in text for v in VERDICTS),
            "sync-receipt.json carries chain-verdict vocabulary",
        )
        missing = {
            s
            for start, end in verification.get("missing_ranges") or []
            for s in range(start, end + 1)
        }
        refused = {
            e.get("sequence")
            for a in bundled["attempts"]
            for e in (a.get("outcomes") or {}).get("rejected") or []
        }
        report.check(
            "B6",
            not (missing & refused),
            f"missing sequence(s) {sorted(missing & refused)} rewritten as observed rejections",
        )

    return report, verification


# ---------------------------------------------------------------------------
# E — a lane's expected outcomes
# ---------------------------------------------------------------------------


def comparable(attempt: dict[str, Any]) -> dict[str, Any]:
    """An attempt minus provenance and free text — what a lane pins."""
    kept = {k: v for k, v in attempt.items() if k not in PROVENANCE and k != "transport_error"}
    outcomes = dict(kept.get("outcomes") or {})
    outcomes["rejected"] = [
        {k: v for k, v in entry.items() if k != "error_message"}
        for entry in outcomes.get("rejected") or []
    ]
    kept["outcomes"] = outcomes
    return kept


def check_against_lane(lane_dir: Path, receipt: dict[str, Any]) -> Report:
    report = Report()
    expected = json.loads((lane_dir / "expected.json").read_text(encoding="utf-8"))
    attempts = receipt.get("attempts") or []
    want = expected["attempts"]
    report.check(
        "E2", len(attempts) == len(want), f"{len(attempts)} attempt(s), lane expects {len(want)}"
    )
    for index, (got, wanted) in enumerate(zip(attempts, want), start=1):
        if isinstance(got, dict):
            report.check(
                "E2",
                comparable(got) == wanted,
                f"attempt {index} outcomes differ from expected.json:\n    got      {json.dumps(comparable(got), sort_keys=True)}\n    expected {json.dumps(wanted, sort_keys=True)}",
            )
    return report


def check_lane(lane_dir: Path) -> Report:
    report = Report()
    expected = json.loads((lane_dir / "expected.json").read_text(encoding="utf-8"))
    files = expected["files"]
    spool_bytes = (lane_dir / files["spool_file"]).read_bytes()
    receipt = json.loads((lane_dir / files["receipt"]).read_text(encoding="utf-8"))

    report.merge(check_receipt(receipt, spool_bytes), "receipt: ")
    report.merge(check_against_lane(lane_dir, receipt), "receipt: ")
    bundle_report, verification = check_bundle(lane_dir / files["bundle"], receipt)
    report.merge(bundle_report, "bundle: ")

    chain = expected["chain"]
    got = {
        "verdict": verification.get("verdict"),
        "first_invalid_sequence": verification.get("first_invalid_sequence"),
        "missing_ranges": verification.get("missing_ranges"),
    }
    report.check("E1", got == chain, f"bundle chain verdict {got} differs from expected {chain}")
    return report


def check_sums(root: Path) -> Report:
    """SHA256SUMS covers every file in the set — `sha256sum -c` compatible."""
    report = Report()
    sums = root / "SHA256SUMS"
    if not report.check("M1", sums.is_file(), "SHA256SUMS missing"):
        return report
    listed: set[str] = set()
    for line in sums.read_text(encoding="utf-8").splitlines():
        digest, _, name = line.partition("  ")
        listed.add(name)
        path = root / name
        report.check(
            "M1",
            path.is_file() and sha256_bytes(path.read_bytes()) == digest,
            f"{name} missing or altered",
        )
    present = {
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and p.name != "SHA256SUMS" and "__pycache__" not in p.parts
    }
    report.check(
        "M1",
        present == listed,
        f"not covered by SHA256SUMS: {sorted(present - listed)}; listed but absent: {sorted(listed - present)}",
    )
    return report


def lanes(root: Path) -> list[Path]:
    return sorted(p.parent for p in root.glob("*/expected.json"))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _finish(report: Report, label: str) -> int:
    for failure in report.failures:
        print(f"  FAIL  {failure}")
    status = "OK" if not report.failures else "FAILED"
    print(f"{status}  {label}: {report.passed} check(s) passed, {len(report.failures)} failed")
    return 0 if not report.failures else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--receipt", type=Path, help="a receipt/1.1 file to check")
    parser.add_argument("--spool", type=Path, help="the spool file that receipt describes")
    parser.add_argument(
        "--lane", help="lane name or directory whose expected outcomes the receipt must match"
    )
    parser.add_argument(
        "--root", type=Path, default=HERE, help="vector set to check (default: this directory)"
    )
    args = parser.parse_args(argv)

    if args.receipt is None:
        report = check_sums(args.root)
        code = _finish(report, "SHA256SUMS")
        found = lanes(args.root)
        if not found:
            print(f"FAILED  no lanes (*/expected.json) under {args.root}")
            return 1
        for lane_dir in found:
            code |= _finish(check_lane(lane_dir), lane_dir.name)
        return code

    receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
    spool_path = args.spool
    lane_dir = None
    if args.lane:
        lane_dir = Path(args.lane) if Path(args.lane).is_dir() else args.root / args.lane
        if spool_path is None:
            spool_path = (
                lane_dir
                / json.loads((lane_dir / "expected.json").read_text(encoding="utf-8"))["files"][
                    "spool_file"
                ]
            )
    report = check_receipt(receipt, spool_path.read_bytes() if spool_path else None)
    if lane_dir is not None:
        report.merge(check_against_lane(lane_dir, receipt), "")
    return _finish(report, str(args.receipt))


if __name__ == "__main__":
    sys.exit(main())
