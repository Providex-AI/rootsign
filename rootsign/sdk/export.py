"""The evidence bundle — `rootsign export` (ADR-014, Sprint B T3.1).

A session's records are already tamper-evident; what nobody outside
engineering can do with them is *read* them. This module turns one session
into a self-contained bundle a compliance officer can open: the machine truth
as JSON, and (T3.2) Markdown and HTML rendered from that JSON and nothing else.

**One assembly, two sources.** A session lives either in Postgres or in a JSONL
file (an ordinary local session, or a spool file waiting to be synced — the
same reader handles both, which is the payoff of ADR-013 reusing the ADR-011
writer). Both are normalized into `SessionEvidence` first, so the documents are
built once. If a fact is not in the JSON it cannot appear in the HTML, and if
the two sources disagreed about a session the difference would show up here
rather than in a renderer.

**The honesty rules are the interesting part**, because a bundle that overstates
what it knows is worse than no bundle:

* `verification.json` reports what the verifier proved, per record. The walk
  stops at the first break, so records after it are `unverified` — not
  `verified`, and not silently omitted.
* `redaction.json` reports the `[REDACTED]` sentinel paths it can actually see
  in the stored payloads. Which *rule* fired is not recorded anywhere (ADR-014
  scope note), so the bundle says so instead of guessing.
* Previews appear only where a payload was actually retained. A session that
  stored hashes only says that explicitly (Decision 4).
* `compliance` is reserved and empty. Phase 2 fills it without a version bump.

The bundle-format version is `EVIDENCE_BUNDLE_VERSION`, frozen by a golden-file
test (T3.6). The **verdict vocabulary** freezes with it: all three verdicts are
legal from 1.0 even though early bundles only say VALID, or the first
gap-bearing session forces 1.1 in week one.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from rootsign._version import SDK_VERSION
from rootsign.sdk.redaction import REDACTED_PLACEHOLDER

#: Bundle format version. Bump only for a breaking schema change — additive
#: fields do not. The reserved `compliance` block and the three-valued verdict
#: vocabulary exist so the obvious near-term additions need no bump at all.
EVIDENCE_BUNDLE_VERSION = "1.0"

#: Filenames inside `evidence-<session_id>/`. Ordered as a reader meets them.
VERIFICATION_FILE = "verification.json"
TIMELINE_FILE = "timeline.json"
REDACTION_FILE = "redaction.json"
MANIFEST_FILE = "manifest.json"

#: Why `rule_set.name` is null rather than a guess (ADR-014 scope note).
_REDACTION_PROVENANCE = (
    "RootSign does not record which redaction rule set was active when a "
    "session ran, and it is not derivable from stored records. The paths below "
    "are the fields that carry the redaction sentinel in the stored payloads — "
    "evidence that redaction happened, not evidence of which rule caused it."
)

_NO_PREVIEWS_STORED = (
    "payload previews not retained for this session — only the input/output "
    "hashes the chain is built from were stored"
)
_PREVIEWS_STRIPPED = (
    "stored content withheld from this bundle (--redact-previews); hashes, "
    "identities and timings are unaffected"
)

#: Every timeline field that carries *stored content* rather than metadata.
#: `--redact-previews` removes all of them, not just the two obvious ones
#: (ADR-014 Decision 4: the flag is for bundles leaving the building, and a
#: payload that survives inside an approval's `context_presented` has left the
#: building just as thoroughly as one in `input_preview`). Privacy controls
#: fail closed: a field is on this list unless it is demonstrably metadata.
CONTENT_FIELDS = (
    "input_preview",
    "output_preview",
    # What the human was shown at a HiTL checkpoint — built from the redacted
    # input, so it embeds the payload by another name.
    "context_presented",
    # ADR-008 decision capture: free text summarizing the inputs the model saw
    # and the reasoning it produced. Narrative loss is the price of the flag,
    # and `withheld_fields` tells the reader exactly what to ask for.
    "inputs_summary",
    "reasoning_summary",
)


# ---------------------------------------------------------------------------
# Normalized input
# ---------------------------------------------------------------------------


@dataclass
class SessionEvidence:
    """One session, flattened out of whichever store it came from.

    Plain dicts rather than ORM rows or envelopes: the builders below must not
    be able to tell which source they are serving, or the two would drift into
    producing subtly different bundles for the same session.
    """

    session_id: str
    source_backend: str
    source_location: str
    verify: Any
    session: dict[str, Any] = field(default_factory=dict)
    agent: dict[str, Any] | None = None
    actions: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    #: `RECORD_LOSS` tallies (ADR-013 D4a). Present only in file sources, and
    #: only when a spool write failed — the loudest thing in a bundle when it is.
    losses: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# The bundle
# ---------------------------------------------------------------------------


@dataclass
class EvidenceBundle:
    """The documents that make up one bundle, plus their content hashes.

    `manifest` is computed rather than stored, because it hashes the others: a
    stored manifest is a manifest that can fall out of date with the files it
    describes, which is precisely the failure it exists to detect.
    """

    session_id: str
    verification: dict[str, Any]
    timeline: dict[str, Any]
    redaction: dict[str, Any]
    source_backend: str
    source_location: str
    agent: dict[str, Any] | None = None
    generated_at: str = ""
    #: Rendered files (T3.2) attached after assembly: name -> text.
    rendered: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.generated_at:
            self.generated_at = datetime.now(timezone.utc).isoformat()

    @property
    def directory_name(self) -> str:
        return f"evidence-{self.session_id}"

    def attach(self, filename: str, text: str) -> None:
        """Add a rendered file so the manifest covers it (T3.2's hook)."""
        self.rendered[filename] = text

    def documents(self) -> dict[str, dict[str, Any]]:
        """The JSON documents, in reading order. Manifest is not among them —
        it is derived from them."""
        return {
            VERIFICATION_FILE: self.verification,
            TIMELINE_FILE: self.timeline,
            REDACTION_FILE: self.redaction,
        }

    def files(self) -> dict[str, str]:
        """Every bundle file except the manifest: name -> serialized text."""
        files = {name: _dumps(doc) for name, doc in self.documents().items()}
        files.update(self.rendered)
        return files

    @property
    def manifest(self) -> dict[str, Any]:
        return {
            "bundle_version": EVIDENCE_BUNDLE_VERSION,
            "generator": f"rootsign {SDK_VERSION}",
            "generated_at": self.generated_at,
            "session_id": self.session_id,
            "agent": self.agent or {"detail": "agent identity not recorded in this source"},
            "source": {"backend": self.source_backend, "location": self.source_location},
            "verdict": self.verification["verdict"],
            # SHA-256 of every other file. The manifest cannot hash itself; its
            # own digest is the out-of-band anchor printed at export time
            # (T3.3), which is what a recipient actually compares against.
            "files": {name: sha256_text(text) for name, text in sorted(self.files().items())},
            # Reserved for Phase 2's compliance mapping (ADR-014 Decision 5).
            # Present and empty on purpose: filling it later must not be a
            # bundle-version break.
            "compliance": {},
        }

    @property
    def manifest_hash(self) -> str:
        """The anchor. Printed at export, compared by hand on receipt."""
        return sha256_text(_dumps(self.manifest))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    """Hash raw bytes.

    `--check` reads files someone else sent, and "someone else" includes an
    attacker who dropped a JPEG into the directory. Hashing bytes rather than
    decoded text means a file that is not UTF-8 gets checked like anything
    else instead of raising out of the middle of the check. For the files this
    package writes the two are identical: they are written as UTF-8.
    """
    return hashlib.sha256(data).hexdigest()


def _dumps(document: dict[str, Any]) -> str:
    """Serialize a bundle document. Deterministic — hashes depend on it.

    Key order is the builders' insertion order, which reads in a sensible order
    for a human and is stable across runs. `default=str` catches UUIDs and
    datetimes that slipped through normalization rather than raising mid-export.
    """
    return json.dumps(document, indent=2, ensure_ascii=False, default=str) + "\n"


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def build_bundle(evidence: SessionEvidence, *, redact_previews: bool = False) -> EvidenceBundle:
    """Turn normalized evidence into the bundle documents."""
    previews_stored = any(
        action.get("input_redacted") is not None or action.get("output_redacted") is not None
        for action in evidence.actions
    )
    include_previews = previews_stored and not redact_previews

    return EvidenceBundle(
        session_id=evidence.session_id,
        verification=_build_verification(evidence),
        timeline=_build_timeline(
            evidence, include_previews=include_previews, previews_stored=previews_stored
        ),
        redaction=_build_redaction(evidence),
        source_backend=evidence.source_backend,
        source_location=evidence.source_location,
        agent=evidence.agent,
    )


def _build_verification(evidence: SessionEvidence) -> dict[str, Any]:
    """The chain proof, per record.

    Record status is deliberately three-valued. A verifier walks until it
    finds a break and then stops, so it has said nothing about the records
    after it — calling those `verified` would be the bundle claiming proof
    nobody produced.
    """
    result = evidence.verify
    verdict = getattr(result.verdict, "value", result.verdict)
    first_invalid = result.first_invalid_sequence

    records = []
    for action in evidence.actions:
        sequence = action.get("sequence_number")
        records.append(
            {
                "sequence_number": sequence,
                "action_id": action.get("action_id"),
                "timestamp": action.get("timestamp"),
                "tool_name": action.get("tool_name"),
                "prev_action_hash": action.get("prev_action_hash"),
                "self_hash": action.get("self_hash"),
                "chain_status": _record_status(verdict, sequence, first_invalid),
            }
        )

    return {
        "verdict": verdict,
        # Kept alongside `verdict` for anything consuming the older two-valued
        # shape; false for both failure verdicts (ADR-013 Decision 4b).
        "valid": bool(result.valid),
        "summary": result.summary,
        "record_count": result.record_count,
        "first_invalid_sequence": first_invalid,
        "missing_ranges": [list(r) for r in (result.missing_ranges or [])],
        "error": result.error,
        "hash": {
            "algorithm": "sha256",
            "canonical_spec": "ADR-001",
            "note": (
                "Each record's self_hash is recomputed from its canonical fields "
                "and checked against the stored value; each record's "
                "prev_action_hash must equal its predecessor's self_hash."
            ),
        },
        "records": records,
    }


def _record_status(verdict: str, sequence: Any, first_invalid: int | None) -> str:
    if verdict == "VALID" or first_invalid is None or not isinstance(sequence, int):
        return "verified"
    if sequence < first_invalid:
        return "verified"
    if sequence == first_invalid:
        # For INCOMPLETE, `first_invalid_sequence` points at the start of the
        # gap — a record that is not here at all — so a present record at that
        # sequence can only be the break itself.
        return "failed"
    return "unverified"


def _build_timeline(
    evidence: SessionEvidence, *, include_previews: bool, previews_stored: bool
) -> dict[str, Any]:
    """The session narrative, in the order it happened."""
    events: list[dict[str, Any]] = []

    session = evidence.session
    if session:
        events.append(
            {
                "type": "SESSION_OPEN",
                "timestamp": session.get("start_time"),
                "objective": session.get("objective"),
                "user_id": session.get("user_id"),
            }
        )

    for decision in evidence.decisions:
        events.append(
            {
                "type": "DECISION",
                "timestamp": decision.get("timestamp"),
                "decision_id": decision.get("decision_id"),
                "selected_action": decision.get("selected_action"),
                "confidence": decision.get("confidence"),
                "alternatives_considered": decision.get("alternatives_considered"),
                "reasoning_summary": decision.get("reasoning_summary"),
                "inputs_summary": decision.get("inputs_summary"),
            }
        )

    for action in evidence.actions:
        event = {
            "type": "ACTION",
            "timestamp": action.get("timestamp"),
            "sequence_number": action.get("sequence_number"),
            "action_id": action.get("action_id"),
            "tool_name": action.get("tool_name"),
            "authorization_status": action.get("authorization_status"),
            "duration_ms": action.get("duration_ms"),
            "decision_id": action.get("decision_id"),
            "input_hash": action.get("input_hash"),
            "output_hash": action.get("output_hash"),
        }
        # Added unconditionally; the sweep above removes them (and every other
        # content field) when the flag is set, so there is one place that
        # decides what "stored content" means.
        event["input_preview"] = action.get("input_redacted")
        event["output_preview"] = action.get("output_redacted")
        events.append(event)

    for approval in evidence.approvals:
        events.append(
            {
                "type": "APPROVAL",
                "timestamp": approval.get("timestamp"),
                "approval_id": approval.get("approval_id"),
                "action_id": approval.get("action_id"),
                "approver_id": approval.get("approver_id"),
                "approver_type": approval.get("approver_type"),
                "decision": approval.get("decision"),
                "decision_reason": approval.get("decision_reason"),
                "response_latency_ms": approval.get("response_latency_ms"),
                "context_presented": approval.get("context_presented"),
            }
        )

    for loss in evidence.losses:
        # Records that were never written at all (ADR-013 D4a). They belong in
        # the narrative precisely because they are absent from everywhere else.
        events.append(
            {
                "type": "RECORD_LOSS",
                "timestamp": loss.get("first_loss_at") or loss.get("recorded_at"),
                "lost_count": loss.get("lost_count"),
                "first_sequence": loss.get("first_sequence"),
                "last_sequence": loss.get("last_sequence"),
                "reasons": loss.get("reasons"),
            }
        )

    if session and session.get("end_time"):
        events.append(
            {
                "type": "SESSION_CLOSE",
                "timestamp": session.get("end_time"),
                "status": session.get("status"),
            }
        )

    events.sort(key=_narrative_order)

    withheld_fields: set[str] = set()
    withheld_events = 0
    if not include_previews:
        for event in events:
            present = [key for key in CONTENT_FIELDS if key in event]
            # A null field held nothing, so nothing was withheld by dropping it.
            # Counting it would tell a reader to go ask for content that never
            # existed — the mirror image of the mistake this block prevents.
            carried_content = [key for key in present if event[key] is not None]
            if carried_content:
                withheld_events += 1
                withheld_fields.update(carried_content)
            for key in present:
                del event[key]

    return {
        "session": {
            "session_id": evidence.session_id,
            "agent_id": session.get("agent_id"),
            "objective": session.get("objective"),
            "user_id": session.get("user_id"),
            "status": session.get("status"),
            "start_time": session.get("start_time"),
            "end_time": session.get("end_time"),
            "action_count": len(evidence.actions),
            "decision_count": len(evidence.decisions),
            "approval_count": len(evidence.approvals),
        },
        "previews": {
            "included": include_previews,
            "note": None
            if include_previews
            else (_NO_PREVIEWS_STORED if not previews_stored else _PREVIEWS_STRIPPED),
            # Named, not merely absent. A reader who cannot tell whether a field
            # was withheld or never existed cannot know what to ask for — and
            # would have to assume the worse of the two.
            "withheld_fields": sorted(withheld_fields),
            "withheld_from_events": withheld_events,
        },
        "events": events,
    }


def _build_redaction(evidence: SessionEvidence) -> dict[str, Any]:
    """The redaction posture: what is provably redacted, and what is not known."""
    records = []
    redacted_fields = 0
    for action in evidence.actions:
        input_paths = _redacted_paths(action.get("input_redacted"))
        output_paths = _redacted_paths(action.get("output_redacted"))
        if not input_paths and not output_paths:
            continue
        redacted_fields += len(input_paths) + len(output_paths)
        records.append(
            {
                "sequence_number": action.get("sequence_number"),
                "action_id": action.get("action_id"),
                "input_paths": input_paths,
                "output_paths": output_paths,
            }
        )

    return {
        "sentinel": REDACTED_PLACEHOLDER,
        "rule_set": {"name": None, "provenance": _REDACTION_PROVENANCE},
        "totals": {
            "actions_with_redactions": len(records),
            "redacted_fields": redacted_fields,
        },
        "records": records,
    }


def _redacted_paths(payload: Any, prefix: str = "") -> list[str]:
    """Dotted paths of every value equal to the redaction sentinel.

    Lists are indexed (`items[2].ssn`) so a path identifies one field rather
    than a shape. Non-container leaves that are not the sentinel are ignored —
    the bundle reports where redaction *happened*, never what survived it.
    """
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            found.extend(_redacted_paths(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            found.extend(_redacted_paths(value, f"{prefix}[{index}]"))
    elif payload == REDACTED_PLACEHOLDER and prefix:
        found.append(prefix)
    return found


#: Session boundaries are pinned rather than sorted. An action's `timestamp`
#: comes from the tool call while SESSION_OPEN/CLOSE carry the envelope's
#: `emitted_at`, so clock skew or a delayed close can order a record after the
#: close that contained it. A session opens before its contents and closes
#: after them — that is structure, not a measurement.
_BOUNDARY_RANK = {"SESSION_OPEN": 0, "SESSION_CLOSE": 2}


def _narrative_order(event: dict[str, Any]) -> tuple[int, str]:
    """Sort key: boundary rank first, then timestamp.

    An event with no usable timestamp sorts last within its rank rather than
    crashing the export — a bundle that fails to generate tells an auditor
    nothing, and a slightly mis-ordered narrative still carries every fact.
    """
    timestamp = event.get("timestamp")
    return (
        _BOUNDARY_RANK.get(event.get("type", ""), 1),
        "9999" if timestamp is None else str(timestamp),
    )


# ---------------------------------------------------------------------------
# Source: a JSONL session file (ordinary or spooled)
# ---------------------------------------------------------------------------


def load_local_session(jsonl_path: str | os.PathLike[str]) -> SessionEvidence:
    """Read a session file into normalized evidence. No database required.

    Works on a spool file for the same reason `verify --local` does: a spooled
    session is an ordinary session file (ADR-013 Decision 4).
    """
    from rootsign.sdk.chain import verify_session_local

    path = Path(jsonl_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Session file not found: {path}")

    session: dict[str, Any] = {}
    actions: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    approvals: list[dict[str, Any]] = []
    losses: list[dict[str, Any]] = []
    session_id: str | None = None

    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            # Corruption is the verifier's story to tell, and it will: a
            # malformed line makes `verify_session_local` return TAMPERED, which
            # lands in the bundle. Skipping here keeps the timeline building.
            continue

        session_id = session_id or record.get("session_id")
        event_type = record.get("event_type", "ACTION_RECORD")
        payload = record.get("payload") or {}

        if event_type == "ACTION_RECORD":
            actions.append(record)
        elif event_type == "SESSION_OPEN":
            session.update(
                {
                    "agent_id": record.get("agent_id"),
                    "objective": payload.get("objective"),
                    "user_id": payload.get("user_id"),
                    "start_time": record.get("emitted_at"),
                    "status": "running",
                }
            )
        elif event_type == "SESSION_CLOSE":
            session.update(
                {
                    "end_time": record.get("emitted_at"),
                    "status": payload.get("status"),
                }
            )
        elif event_type == "DECISION_RECORD":
            decisions.append({**payload, "decision_id": record.get("decision_id")})
        elif event_type == "APPROVAL_RECORD":
            approvals.append({**payload, "approval_id": record.get("approval_id")})
        else:
            # RECORD_LOSS, and anything a later annotation adds.
            losses.append(record)

    actions.sort(key=lambda a: a.get("sequence_number") or 0)

    return SessionEvidence(
        session_id=str(session_id or path.stem),
        source_backend="jsonl",
        source_location=str(path),
        verify=verify_session_local(str(path)),
        session=session,
        agent={"agent_id": session.get("agent_id")} if session.get("agent_id") else None,
        actions=actions,
        decisions=decisions,
        approvals=approvals,
        losses=losses,
    )


def export_local(
    jsonl_path: str | os.PathLike[str], *, redact_previews: bool = False
) -> EvidenceBundle:
    """Build a bundle from a session file. The bare-install path."""
    return build_bundle(load_local_session(jsonl_path), redact_previews=redact_previews)


# ---------------------------------------------------------------------------
# Source: Postgres
# ---------------------------------------------------------------------------


async def load_postgres_session(session_id: UUID, db: Any) -> SessionEvidence:
    """Read a stored session into normalized evidence.

    Postgres only in v0.3.0 — cloud-backed export needs a server read API that
    Sprint B does not build (ADR-014 Decision 1). The DB stack is imported here
    rather than at module scope so `export --local` keeps working on a bare
    install (ADR-011's packaging discipline).
    """
    from rootsign.errors import postgres_extra_required

    with postgres_extra_required():
        from sqlalchemy import select

        from rootsign.crud import action as action_crud
        from rootsign.crud import agent as agent_crud
        from rootsign.crud import decision as decision_crud
        from rootsign.crud import session as session_crud
        from rootsign.models.approval import Approval

    from rootsign.sdk.chain import verify_session

    row = await session_crud.get(db, id=session_id)
    if row is None:
        raise LookupError(f"Session {session_id} not found in the configured database")

    agent_row = await agent_crud.get(db, id=row.agent_id)
    actions = await action_crud.get_session_chain(db, session_id=session_id)
    decisions = await decision_crud.get_by_session(db, session_id=session_id)
    approval_rows = (
        (
            await db.execute(
                select(Approval)
                .where(Approval.session_id == session_id)
                .order_by(Approval.timestamp.asc())
            )
        )
        .scalars()
        .all()
    )

    return SessionEvidence(
        session_id=str(session_id),
        source_backend="postgres",
        source_location="database",
        verify=await verify_session(session_id, db),
        session={
            "agent_id": str(row.agent_id),
            "objective": row.objective,
            "user_id": row.user_id,
            "status": row.status,
            "start_time": _iso(row.start_time),
            "end_time": _iso(row.end_time),
        },
        agent=_agent_block(agent_row),
        actions=[_action_dict(a) for a in actions],
        decisions=[_decision_dict(d) for d in decisions],
        approvals=[_approval_dict(a) for a in approval_rows],
    )


async def export_session(
    session_id: UUID, db: Any, *, redact_previews: bool = False
) -> EvidenceBundle:
    """Build a bundle from a Postgres-stored session."""
    evidence = await load_postgres_session(session_id, db)
    return build_bundle(evidence, redact_previews=redact_previews)


def _agent_block(agent: Any) -> dict[str, Any] | None:
    if agent is None:
        return None
    return {
        "agent_id": str(agent.agent_id),
        "name": agent.name,
        "owner": agent.owner,
        "environment": agent.environment,
        "risk_tier": agent.risk_tier,
        "framework": agent.framework,
        "model_version": agent.model_version,
        "regulatory_categories": list(agent.regulatory_categories or []),
    }


def _action_dict(action: Any) -> dict[str, Any]:
    return {
        "action_id": str(action.action_id),
        "sequence_number": action.sequence_number,
        "timestamp": _iso(action.timestamp),
        "tool_name": action.tool_name,
        "input_hash": action.input_hash,
        "output_hash": action.output_hash,
        "prev_action_hash": action.prev_action_hash,
        "self_hash": action.self_hash,
        "input_redacted": action.input_redacted,
        "output_redacted": action.output_redacted,
        "authorization_status": action.authorization_status,
        "duration_ms": action.duration_ms,
        "decision_id": str(action.decision_id) if action.decision_id else None,
    }


def _decision_dict(decision: Any) -> dict[str, Any]:
    return {
        "decision_id": str(decision.decision_id),
        "timestamp": _iso(decision.timestamp),
        "selected_action": decision.selected_action,
        "confidence": decision.confidence,
        "alternatives_considered": list(decision.alternatives_considered or []),
        "reasoning_summary": decision.reasoning_summary,
        "inputs_summary": decision.inputs_summary,
    }


def _approval_dict(approval: Any) -> dict[str, Any]:
    return {
        "approval_id": str(approval.approval_id),
        "action_id": str(approval.action_id),
        "timestamp": _iso(approval.timestamp),
        "approver_id": approval.approver_id,
        "approver_type": approval.approver_type,
        "decision": approval.decision,
        "decision_reason": approval.decision_reason,
        "response_latency_ms": approval.response_latency_ms,
        "context_presented": approval.context_presented,
    }


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


# ---------------------------------------------------------------------------
# Writing a bundle, and checking one that arrived (ADR-014 Decision 2, T3.3)
# ---------------------------------------------------------------------------


class BundleExists(Exception):
    """The target directory already holds a bundle.

    Overwriting is refused rather than defaulted: evidence directories are the
    kind of thing that gets re-exported into by accident, and a half-replaced
    bundle whose manifest describes the previous run is worse than either
    version alone.
    """


def write_bundle(
    bundle: EvidenceBundle, out_dir: str | os.PathLike[str], *, overwrite: bool = False
) -> Path:
    """Write `evidence-<session_id>/` under `out_dir`. Returns the bundle directory.

    `manifest.json` is written last and serialized exactly as `manifest_hash`
    computes it, so the digest printed at export is the digest of the bytes on
    disk — otherwise the anchor an auditor writes down would be a value nothing
    can reproduce.
    """
    directory = Path(out_dir).expanduser() / bundle.directory_name
    if directory.exists() and any(directory.iterdir()) and not overwrite:
        raise BundleExists(
            f"{directory} already exists and is not empty. Export to a different "
            "--out directory, or remove it first."
        )
    directory.mkdir(parents=True, exist_ok=True)

    for name, text in bundle.files().items():
        (directory / name).write_text(text, encoding="utf-8")
    (directory / MANIFEST_FILE).write_text(_dumps(bundle.manifest), encoding="utf-8")
    return directory


@dataclass
class BundleCheck:
    """What `rootsign export --check` found.

    `manifest_hash` is not a detail — it is the point. Re-hashing the files
    against the manifest proves the bundle is *internally consistent*, which an
    attacker who edits a file and updates the manifest satisfies trivially. The
    only real anchor is the manifest's own digest, compared against the value
    noted out of band when the bundle was exported. So this carries it whether
    or not anything else went wrong, and the CLI prints it either way.
    """

    directory: Path
    manifest_hash: str | None = None
    verified: list[str] = field(default_factory=list)
    altered: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def intact(self) -> bool:
        return not (self.altered or self.missing or self.unexpected or self.error)

    @property
    def summary(self) -> str:
        if self.error:
            return f"UNREADABLE — {self.error}"
        if self.intact:
            return f"INTACT — {len(self.verified)} file(s) match manifest.json"
        problems = []
        if self.altered:
            problems.append(f"{len(self.altered)} altered ({', '.join(self.altered)})")
        if self.missing:
            problems.append(f"{len(self.missing)} missing ({', '.join(self.missing)})")
        if self.unexpected:
            problems.append(f"{len(self.unexpected)} unexpected ({', '.join(self.unexpected)})")
        return "ALTERED — " + "; ".join(problems)


def check_bundle(directory: str | os.PathLike[str]) -> BundleCheck:
    """Re-hash a received bundle against its manifest. Never raises.

    Three ways a bundle can be wrong, and all three are reported: a listed file
    whose content changed, a listed file that is gone, and a file that is
    present but not listed. The third matters because the first two are what a
    naive check looks for — an attacker who *adds* a document to a bundle
    passes a check that only re-hashes what the manifest names.
    """
    root = Path(directory).expanduser()
    result = BundleCheck(directory=root)

    manifest_path = root / MANIFEST_FILE
    if not manifest_path.is_file():
        result.error = f"no {MANIFEST_FILE} in {root}"
        return result

    # Hash first, parse second: the digest is a fact about the bytes that
    # arrived, and it is the value worth comparing even when nothing in them
    # parses.
    raw = manifest_path.read_bytes()
    result.manifest_hash = sha256_bytes(raw)
    try:
        manifest = json.loads(raw.decode("utf-8"))
        expected = dict(manifest["files"])
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        result.error = f"{MANIFEST_FILE} is not a readable bundle manifest ({exc})"
        return result

    for name, digest in sorted(expected.items()):
        path = root / name
        if not path.is_file():
            result.missing.append(name)
        elif sha256_bytes(path.read_bytes()) == digest:
            result.verified.append(name)
        else:
            result.altered.append(name)

    present = {p.name for p in root.iterdir() if p.is_file()}
    result.unexpected = sorted(present - set(expected) - {MANIFEST_FILE})
    return result
