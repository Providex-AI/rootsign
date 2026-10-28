# Sync-receipt conformance vectors, schema `receipt/1.1`

Fixture vectors for RootSign's sync receipt (ADR-015). An independent
implementation can use them to check its own behaviour against RootSign's
without installing RootSign. They are tracked in issue #47. The three scenarios
come from issue #37.

This directory in the RootSign repository is the authoritative copy. A mirror
or a release asset can be checked against it with `sha256sum -c SHA256SUMS` (on
macOS, `shasum -a 256 -c SHA256SUMS`), before you run any code from it.

## Licence

| What | Licence |
| --- | --- |
| Every file in this directory **except** `verify_vectors.py`: spool files, receipts, bundles, `expected.json`, this README, `SHA256SUMS` | **CC0 1.0** ([`LICENSE`](LICENSE)). No rights reserved, so you can copy them into your own test suite. |
| `verify_vectors.py` | **Apache-2.0**, like the rest of the repository (SPDX header in the file) |

## Layout

```
v1.1/
  README.md  LICENSE  SHA256SUMS
  verify_vectors.py            standalone verifier, Python stdlib only
  <lane>/
    expected.json              what the lane pins (see below)
    spool/...                  the spool tree as `rootsign-admin sync` left it
    bundle/evidence-<id>/      the evidence bundle `rootsign export` produced
```

Each lane stands alone. `expected.json` names its own files under `files`, so
nothing depends on the directory names. The spool tree is left the way sync
left it. A partial sync leaves the file in `spool/sessions/` and the receipt in
`spool/receipts/`. A fully accepted one retires both to `spool/synced/`. That
move is part of what the vectors show.

## The lanes

| Lane | Recorded | What happened | Chain verdict | Receipt |
| --- | --- | --- | --- | --- |
| `v1-contiguous-tamper` | open, seq 1–2, close | `tool_name` of seq 2 edited before sync | `TAMPERED` at 2 | `partial`: record 3 / seq 2 `HASH_CHAIN_BROKEN`, records 1–2 and 4 accepted |
| `v2-sequence-gap` | open, seq 1–3, close | line for seq 2 deleted before sync | `INCOMPLETE`, missing `[2,2]` | `fully_accepted`: all 4 records. **No rejection for the missing record.** |
| `v3-duplicates-and-hard-failure` | open, seq 1–4, close | store refuses seq 2 (`VALIDATION_ERROR`, not retryable); synced twice, batch size 2 | `VALID` | attempt 1: accepted, rejected, not_attempted. Attempt 2: duplicate, rejected, not_attempted |

What each lane demonstrates:

- **V1.** A broken chain still produces a receipt. `sync` does not pre-verify
  (ADR-015 Decision 6). The store's `HASH_CHAIN_BROKEN` is the observation the
  receipt exists to record. The chain verdict and the transport outcome are two
  separate artifacts: `verification.json` holds one and the receipt holds the
  other.
- **V2.** A store must not refuse a record because its `prev_action_hash` names
  something it has never seen (ingest spec §8.3: "discontinuity is data"). So
  the record after the gap is *accepted*. The gap appears only as the chain
  verdict. It is never rewritten as an observed rejection.
- **V3.** All four outcome buckets, with the replay done twice. The refusal is
  the store's decision about a valid chain, so the transport layer and the
  chain layer disagree, and they are allowed to.

## The receipt model, briefly

A receipt is `{session_id, attempts: [...]}`. Attempts are appended and never
rewritten. `schema_version` is stamped **per attempt**, because one file can
legitimately hold attempts written under different schema versions.

**Outcomes index record position, not sequence number.** Position *n* is the
*n*-th envelope in the spool file, counting from 1. Only `ACTION_RECORD`
envelopes have a `sequence_number`. `SESSION_OPEN` and `SESSION_CLOSE` do not,
but they still take up positions. So in V1 and V3 the refused record is
`record: 3` and `sequence: 2`. A runner that treats the two as interchangeable
passes on a session whose first envelope happens to be an action and fails
everywhere else. Rule R10 catches it.

- `accepted`, `duplicate`, `not_attempted`: inclusive `[start, end]` position
  ranges, ascending, written as maximal runs (`[[1,2],[4,4]]`, never
  `[[1,1],[2,2],[4,4]]`).
- `rejected`: one object per refused record: `record` (the position),
  `error_code` (the ingest-spec registry value, verbatim), `retryable`, an
  optional `error_message` (free text), and `sequence` when the envelope has
  one.
- `halted_at_sequence` is an **attempt-level** field, not part of `rejected[]`.
  It names the sequence whose refusal stopped further batches, or is `null`.
- `disposition`: `fully_accepted`, `partial` (the store deterministically
  refused a record), or `transport_failed` (the transport gave up; carries
  `transport_error`).
- Fail-fast applies at the **batch** boundary. Answers already returned for
  the rest of a batch are recorded (V1: record 4 accepted after record 3 was
  refused). After that, nothing more is sent, so delivery is always a prefix
  of the file and `not_attempted` is always a suffix.
- `PREDECESSOR_REJECTED` does not exist. Ingest spec §7 rule 3 forbids a
  rejection from aborting later elements, so there is no cascade for such a
  code to name. Do not expect one.

## Canonicalization

**Hashing needs none. Every digest is computed over raw bytes, as stored.**

- `spool_file_sha256` is SHA-256 of the spool file's bytes **as replayed**. It
  is computed before the first batch is sent, not over a parsed or normalized
  reconstruction. The latest attempt's digest must equal the file as it stands
  now. If it does not, the exhibit changed after it was observed.
- `manifest.json` `files` hashes the bundle files as written. `SHA256SUMS`
  hashes this directory's files.

**JSON comparison is semantic.** Parse the JSON, then compare values. Key order
and whitespace do not matter. Integers stay integers, and booleans are not
integers.

**These fields are not compared**, because they describe the run rather than
the evidence and every implementation produces its own: `attempt_id`,
`started_at`, `finished_at`, `endpoint`, `sdk_version`, `operator`,
`rejected[].error_message`, and `transport_error`. In this set they are pinned
(`vector-generator`, 2026-10-21) so that regenerating the files produces no
diff.

## Using the vectors

```sh
python verify_vectors.py
```

This checks the committed set: `SHA256SUMS`, then every lane against its own
`expected.json`.

To check **your** implementation against a lane:

1. **Chain layer.** Run your chain verifier over the lane's spool file and
   compare the result with `expected.json` → `chain` (`verdict`,
   `first_invalid_sequence`, `missing_ranges`).
2. **Receipt layer.** Replay the spool file against a store that behaves as
   described in `scenario.store`, using the batch size and number of attempts
   given in `scenario.sync`. Then run:
   ```sh
   python verify_vectors.py --lane v3-duplicates-and-hard-failure --receipt your.sync.json
   ```
   This checks every R rule against the lane's spool bytes, and E2 against the
   lane's expected outcomes.
3. **Any receipt, anywhere:**
   ```sh
   python verify_vectors.py --receipt some.sync.json --spool some.jsonl
   ```

The verifier exits 0 when every rule holds and 1 otherwise. It prints the rule
ID of each failure.

## Rules

| ID | Rule |
| --- | --- |
| R1 | The file is `{session_id: string, attempts: [non-empty]}` |
| R2 | Each attempt is an object with `schema_version` `"receipt/1.1"` |
| R3 | All fields are present and correctly typed: `spool_file_sha256` is 64 lowercase hex characters, and `disposition` is one of the three values |
| R4 | Ranges are `[start, end]` with `1 ≤ start ≤ end ≤ record_count`, ascending, maximal runs |
| R5 | **Partition:** `accepted ∪ duplicate ∪ rejected ∪ not_attempted` covers `[1, record_count]` exactly once, with no gap and no overlap |
| R6 | Each `rejected` entry has `record`, `error_code`, `retryable`, and optionally `sequence`. `DUPLICATE_EVENT` belongs in `duplicate`, never in `rejected` |
| R7 | Delivery is a prefix: every `not_attempted` position comes after every observed position |
| R8 | A non-null `halted_at_sequence` names the `sequence` of a recorded rejection |
| R9 | `disposition` agrees with the outcomes: `fully_accepted` has no rejection, no unattempted record, and no halt; `partial` has a rejection and no `transport_error`; `transport_failed` has a `transport_error` |
| R10 | Bound to the spool file: the latest attempt's `spool_file_sha256` matches the file's bytes, `record_count` equals the number of envelopes in it, and each rejected `record` is an `ACTION_RECORD` whose `sequence_number` equals the entry's `sequence` |
| B1 | Every bundle file matches `manifest.json`, and no file is present without being listed |
| B2 | The bundle's `sync-receipt.json` is the spool tree's receipt, unaltered |
| B3 | `verification.json` has a verdict from `VALID` / `TAMPERED` / `INCOMPLETE`, and the manifest gives the same verdict |
| B4 | The `replay` block is **absent** (not `null`) exactly when there is no receipt |
| B5 | The `replay` block summarizes the latest attempt |
| B6 | Layer separation: the receipt contains no chain-verdict vocabulary, and no missing sequence appears as an observed rejection |
| E1 | The bundle's chain verdict equals `expected.json` → `chain` |
| E2 | Each receipt attempt, without the fields that are not compared, equals `expected.json` → `attempts` |
| M1 | `SHA256SUMS` covers every file in the set, and every file matches it |

### What the verifier does not check

**The hash chain.** Recomputing ADR-001 `self_hash` would mean a second
implementation of the canonical hash. RootSign's rule is that there is exactly
one implementation of that hash. Each lane instead pins the verdict that
RootSign's own verifier reached, so your verifier can be compared against it.
The hash itself is pinned by
[`tests/fixtures/hash_vectors.json`](../../hash_vectors.json).

**Signatures.** Receipts are content-hashed, not signed. Receipt signing and
key custody are deferred to the signing ADR (ADR-014 Decision 5).

## How these were produced

All files come from RootSign's production code paths (`tests/support/receipt_vectors.py`):

1. An `HttpIngestClient` records each session while its endpoint is down. The
   spool file is the real failover output.
2. The damage is a text edit to that file.
3. The real `rootsign-admin sync` replays the file. V1 and V2 use a mock store
   that verifies each record's seal. V3 uses one that refuses sequence 2.
4. The real `rootsign export` produces the bundle.

The RootSign suite (`tests/unit/test_sync_receipt_vectors.py`) replays these
committed spool files through the same code on every CI run and compares the
results. If anything differs, the implementation and the vectors disagree.
Regenerate with:

```sh
ROOTSIGN_UPDATE_VECTORS=1 python -m pytest tests/unit/test_sync_receipt_vectors.py
```

Review the diff before committing it. A changed receipt is a schema decision,
not a routine refresh.
