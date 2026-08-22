# ADR-014: `rootsign export` — the evidence bundle

- **Date**: 2026-08 (Pre-Phase 2 Sprint B — targets v0.3.0)
- **Status**: Proposed
- **Decider**: Founder
- **Related**: ADR-011 (session files are one input), ADR-001 (the
  verification proof embedded in every bundle), ADR-013 (bundles from
  spooled sessions work identically), PRD 2.4 (this is its CLI-shaped
  precursor)

## Context

Phase 2's headline feature is one-click audit report generation — but
its *value hypothesis* can be proven a phase early, without a dashboard.
Design partners today can generate verified chains; what they cannot do
is hand anything to a compliance officer. The artifact gap, not the
capture gap, is what keeps RootSign classified as a developer tool
inside partner organizations.

`rootsign export` closes it: one command, one self-contained evidence
bundle per session, consumable by someone who has never seen a terminal
render JSON. It also forces the report schema into existence *before*
the Phase 2 dashboard builds UI on top of it — schema first, chrome
second.

## Decisions

### 1. One command, both backends, three formats from one source of truth

```
rootsign export <session_id>            # postgres-backed session (see note)
rootsign export --local <path.jsonl>    # jsonl session or spool file
        [--format json|md|html] [--out DIR] [--redact-previews]
```

In v0.3.0 `export <session_id>` reads from **Postgres only**. Cloud-backed
export needs a server read API, and Sprint B builds only the *write* half of the
cloud transport, against a mock — there is nothing to read from yet (ADR-013). A
cloud-mode user exports from the spool via `--local`, or from Postgres. Say this
plainly in `--help`: a command that appears to support a backend it cannot reach
is worse than one that scopes itself.

The bundle is built as a **JSON evidence document first** (the machine
truth); Markdown and HTML are pure renderings of that JSON. There is no
format-specific data assembly — if a fact is not in the JSON, it cannot
appear in the HTML. Auditors get the JSON; humans get the HTML; the
README gets the screenshot.

### 2. Bundle contents (`evidence-<session_id>/`)

| File | Contents |
| --- | --- |
| `manifest.json` | bundle version, generator (`rootsign x.y.z`), generated_at, session_id, agent identity block, source backend, content hashes of every file in the bundle |
| `verification.json` | full `VerifyResult` + per-record listing: sequence, action_id, self_hash, prev_action_hash, verdict — the chain proof |
| `timeline.json` | ordered Session narrative: SESSION_OPEN → Decisions → Actions (tool, hashes, duration, authorization_status) → Approvals (approver, context presented, decision, note) → SESSION_CLOSE |
| `redaction.json` | the redaction posture: which rule set was active, and the field paths carrying a `[REDACTED]` sentinel per record — the "PII never stored" proof |
| `report.md` / `report.html` | human rendering of all of the above, verification verdict first |

**The bundle is self-verifying**: `manifest.json` carries a SHA-256 per
bundle file, and the manifest's own hash is printed to stdout at export
time for out-of-band noting (email, ticket, chain-of-custody log). A
recipient can prove the bundle they received is the bundle that was
generated. Evidence about evidence — the product's ethos applied to its
own output.

For that claim to hold, `rootsign export --check DIR` must **print the manifest
hash it computed**, not merely report per-file agreement. Re-hashing files
against the manifest proves only internal consistency — an attacker who rewrites
a file *and* updates the manifest passes that check trivially. The out-of-band
manifest hash noted at export time is the only real anchor, so `--check` has to
surface the value a recipient can compare against it, and the docs must say that
comparing it is the actual verification step.

**Scope note on `redaction.json` — read before implementing.** RootSign does
not currently record redaction provenance. `RedactionConfig.redact()` returns
only the redacted payload (`rootsign/sdk/redaction.py`), and neither
`ActionRecordPayload` nor the `Action` model has a field for which rule fired
or when. "By which rule" is therefore **not derivable from stored data**, and
never will be for records already captured.

For v0.3.0 the bundle reports what is honestly knowable: the active rule-set
identity (e.g. `StandardPIIConfig`) from configuration, plus the field paths in
`input_redacted`/`output_redacted` holding the `[REDACTED]` sentinel. Per-rule
attribution would require instrumenting the redactor to emit an audit trail and
carrying it in the envelope — additive, but a real change to the ADR-006
contract with its own storage cost, and retroactively impossible. If partners
ask for it, that is a follow-up ADR, not a quiet widening of this one.

Decision 4's honesty rule applies here too: a bundle must not imply provenance
it does not have.

**The verdict vocabulary is three-valued from day one.** ADR-013 Decision 4a
adds `INCOMPLETE` (records missing) alongside `VALID` and `TAMPERED` (records
altered) — a distinction that matters more to an auditor than to a programmer.
`verification.json` must encode all three from the first bundle, even before a
gap-bearing session exists to produce one.

This is not optional tidiness. Decision 6 of the sprint freezes this schema with
a golden-file test, so a verdict field that can only say valid/invalid would
force a bundle-version bump the first time a spool failure produces a gap —
exactly the break the reserved `compliance` block (Decision 5) exists to avoid.
Reserve the vocabulary, not just the block.

The shape is settled (ADR-013 Decision 4b): `VerifyResult` gains
`verdict: "VALID" | "TAMPERED" | "INCOMPLETE"` and keeps `valid: bool` (false
for both failure verdicts), so `verification.json` carries both — `verdict` as
the auditor-facing value, `valid` for anything already consuming the two-valued
shape. Both backends return the same enum under the same precedence rule, so a
bundle exported from Postgres and one exported from the same session's spool
file cannot disagree about the verdict.

**The vocabulary freezes at bundle v1.0, not the values observed at v1.0.** The
golden file must encode all three verdicts as legal even though the first
bundles will all say VALID — otherwise the first gap-bearing session forces
bundle v1.1 in week one, which is the break the reserved `compliance` block
(Decision 5) exists to avoid.

### 3. Zero new core dependencies

JSON and Markdown are stdlib. HTML is rendered from a single inline
template string (styles inlined, no CSS framework, no JS) — the
document must open identically from a file share, an email attachment,
or an air-gapped review machine. **No PDF in v0.3.0**: every PDF
library drags heavy dependencies into a four-dependency core, and
browser print-to-PDF from the HTML is pixel-adequate for the interim.
PDF generation belongs server-side in Phase 2 (PRD 2.4), where
dependency weight is free.

### 4. Previews are opt-out honest

Input/output *hashes* are always included (they are the chain).
Redacted previews of payloads appear only where Decision capture or
payload retention was enabled, and `--redact-previews` strips them
entirely for bundles leaving the building. The report never pretends to
contain what was never stored — a bundle from a hash-only session says
so explicitly, because an auditor discovering an implied-but-absent
field trusts the whole artifact less.

### 5. What export deliberately is not

- Not a compliance *mapping* (no SOC 2 / EU AI Act clause tagging) —
  that is Phase 2's LLM-assisted engine (PRD 2.3). The bundle leaves a
  reserved, empty `compliance` block in `manifest.json` so Phase 2
  slots in without a bundle-version bump.
- Not multi-session aggregation ("Q1 report") — Phase 2 dashboard
  scope.
- Not a signing/PKI system. Content hashes prove integrity, not
  authorship. If design partners ask for signatures, that is its own
  ADR (likely sigstore-shaped) — do not improvise one.

## Consequences

- Design partners get a compliance-officer-ready deliverable in
  Phase 1.5, which is the strongest possible setup for the Phase 2
  paid conversion ("you've been reading these bundles for a quarter —
  here's the dashboard that generates them continuously").
- The evidence JSON schema becomes a tested, versioned contract before
  any UI depends on it; Phase 2's report generator starts from a frozen
  schema instead of inventing one under deadline.
- The demo GIF and README gain the artifact screenshot that converts
  non-developer stakeholders — the missing asset for conference CFPs
  and design-partner case studies.

## Trade-offs accepted

- **Inline-template HTML will not win design awards.** Correct
  trade-off: audit evidence should look like a document, not a landing
  page. Phase 2 can restyle; the JSON contract is what persists.
- **A reserved-but-empty `compliance` block** invites "when?"
  questions. Better than the alternative — a bundle-version break the
  first week of Phase 2.
