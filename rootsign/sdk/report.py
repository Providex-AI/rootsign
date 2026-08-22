"""Human renderings of an evidence bundle (ADR-014 Decision 1, Sprint B T3.2).

Markdown and HTML are **pure functions of the bundle's JSON**. The renderers
take the parsed documents and nothing else — not `SessionEvidence`, not the
store, not the bundle object — so a fact that is missing from the JSON cannot
appear in the report, and the JSON stays the single machine truth the Phase 2
dashboard will build on.

Three properties are load-bearing:

* **The verdict is the first visible element.** Whoever opens this needs to
  know whether the chain holds before they read a word of narrative; burying
  it under an identity block would be a design that flatters the product.
* **No JavaScript, no external assets, no CSS framework.** Styles are inlined
  in one `<style>` block so the document opens identically from a file share,
  an email attachment, or an air-gapped review machine (Decision 3).
* **The report carries no bundle hashes.** `manifest.json` hashes the report,
  so a report quoting its own digest — or the manifest's — could never be
  written. It points the reader at the real check instead: compare
  `manifest.json`'s hash against the value noted out of band at export.

Everything user-supplied (tool names, objectives, approval reasons, payload
previews) is escaped before it reaches the HTML. A bundle is generated from
data an agent handled, which is exactly the data an attacker would choose.
"""

from __future__ import annotations

import html
import json
from typing import Any

from rootsign.sdk.export import (
    EvidenceBundle,
    MANIFEST_FILE,
    REDACTION_FILE,
    TIMELINE_FILE,
    VERIFICATION_FILE,
)

MARKDOWN_FILE = "report.md"
HTML_FILE = "report.html"

#: The machine-truth documents, in reading order. The reports quote these
#: content hashes; see `_document_hashes` for why they can quote no others.
_JSON_DOCUMENT_FILES = (VERIFICATION_FILE, TIMELINE_FILE, REDACTION_FILE)

#: What each verdict means, in an auditor's terms rather than a programmer's.
_VERDICT_MEANING = {
    "VALID": "Every record verifies. The chain is intact and complete.",
    "TAMPERED": "A record was altered after it was written. Treat this log as compromised.",
    "INCOMPLETE": (
        "Records are missing. The records present verify cleanly — the gap is proven "
        "by the chain itself, not inferred."
    ),
}

_HOW_TO_VERIFY = (
    "This bundle is self-describing: `manifest.json` lists a SHA-256 for every other "
    "file. Re-hashing those files proves the bundle is internally consistent, which is "
    "not the same as proving it is the bundle that was generated — anyone who edits a "
    "file can also edit the manifest. The real check is comparing the hash of "
    "`manifest.json` itself against the value recorded out of band when the bundle was "
    "exported. `rootsign export --check <dir>` does both and prints that hash."
)


def bundle_documents(bundle: EvidenceBundle) -> dict[str, Any]:
    """The JSON a renderer is allowed to see, keyed by filename.

    Going through this function rather than reading the bundle's attributes is
    what makes "rendered from the JSON only" structurally true instead of a
    convention: the renderers accept this dict, and the same dict can be
    round-tripped through `json.dumps`/`loads` in a test to prove it.
    """
    return {
        MANIFEST_FILE: bundle.manifest,
        VERIFICATION_FILE: bundle.verification,
        TIMELINE_FILE: bundle.timeline,
        REDACTION_FILE: bundle.redaction,
    }


def attach_reports(bundle: EvidenceBundle) -> EvidenceBundle:
    """Render both reports and attach them so the manifest covers them."""
    documents = bundle_documents(bundle)
    bundle.attach(MARKDOWN_FILE, render_markdown(documents))
    bundle.attach(HTML_FILE, render_html(documents))
    return bundle


# ---------------------------------------------------------------------------
# Shared shaping — both renderers read the same facts, in the same order
# ---------------------------------------------------------------------------


def _verdict_line(verification: dict[str, Any]) -> tuple[str, str, str]:
    """(verdict, one-line meaning, detail) — the top of both documents."""
    verdict = str(verification.get("verdict", "UNKNOWN"))
    meaning = _VERDICT_MEANING.get(verdict, "Verification produced an unrecognized verdict.")
    detail = verification.get("summary") or ""
    return verdict, meaning, detail


def _identity_rows(manifest: dict[str, Any], timeline: dict[str, Any]) -> list[tuple[str, str]]:
    session = timeline.get("session", {})
    agent = manifest.get("agent") or {}
    source = manifest.get("source") or {}
    rows = [
        ("Session", str(manifest.get("session_id", ""))),
        ("Objective", _or_dash(session.get("objective"))),
        ("Status", _or_dash(session.get("status"))),
        ("Started", _or_dash(session.get("start_time"))),
        ("Ended", _or_dash(session.get("end_time"))),
        ("Agent", _agent_label(agent)),
        ("Source", f"{source.get('backend', '?')} ({source.get('location', '?')})"),
        ("Generated", f"{manifest.get('generated_at', '')} by {manifest.get('generator', '')}"),
        ("Bundle format", str(manifest.get("bundle_version", ""))),
    ]
    return rows


def _agent_label(agent: dict[str, Any]) -> str:
    """Name the agent as fully as the source allowed — and no more.

    A file-sourced bundle knows only the id (ADR-014 Decision 4 applied to
    identity), so it says that rather than rendering an empty name.
    """
    if agent.get("name"):
        owner = f", owned by {agent['owner']}" if agent.get("owner") else ""
        tier = f", risk tier {agent['risk_tier']}" if agent.get("risk_tier") else ""
        return f"{agent['name']} ({agent.get('agent_id', '')}){owner}{tier}"
    if agent.get("agent_id"):
        return f"{agent['agent_id']} — identity details not recorded in this source"
    return "not recorded in this source"


def _event_summary(event: dict[str, Any]) -> str:
    """One line describing an event, whatever kind it is."""
    kind = event.get("type")
    if kind == "SESSION_OPEN":
        return _or_dash(event.get("objective"))
    if kind == "SESSION_CLOSE":
        return f"status: {_or_dash(event.get('status'))}"
    if kind == "DECISION":
        parts = [f"selected {event.get('selected_action', '?')}"]
        if event.get("confidence") is not None:
            parts.append(f"confidence {event['confidence']}")
        if event.get("reasoning_summary"):
            parts.append(str(event["reasoning_summary"]))
        return "; ".join(parts)
    if kind == "ACTION":
        parts = [f"{event.get('tool_name', '?')} (#{event.get('sequence_number', '?')})"]
        parts.append(str(event.get("authorization_status", "")))
        if event.get("duration_ms") is not None:
            parts.append(f"{event['duration_ms']}ms")
        return " — ".join(p for p in parts if p)
    if kind == "APPROVAL":
        who = event.get("approver_id", "?")
        parts = [f"{event.get('decision', '?')} by {who} ({event.get('approver_type', '?')})"]
        if event.get("decision_reason"):
            parts.append(str(event["decision_reason"]))
        return " — ".join(parts)
    if kind == "RECORD_LOSS":
        span = _range_label(event.get("first_sequence"), event.get("last_sequence"))
        return f"{event.get('lost_count', '?')} record(s) never written{span}"
    return ""


#: Fields shown in the header of an event block rather than repeated in its
#: field list — the two the reader scans by.
_EVENT_HEADER_FIELDS = ("type", "timestamp")


def _event_fields(event: dict[str, Any]) -> list[tuple[str, str]]:
    """Every field an event carries, as (json key, rendered value) pairs.

    Deliberately *not* a per-type whitelist. An evidence document that showed
    a curated subset would be the renderer deciding what an auditor is allowed
    to see, and it would quietly drop any field a later schema version adds.
    Keys are shown verbatim so a reader can cross-reference `timeline.json`.
    """
    return [
        (key, _field_text(value)) for key, value in event.items() if key not in _EVENT_HEADER_FIELDS
    ]


def _field_text(value: Any) -> str:
    """Render one field value. Structures become compact JSON, not `dict(...)`."""
    if value is None:
        return "—"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _document_hashes(manifest: dict[str, Any]) -> list[tuple[str, str]]:
    """SHA-256 of the bundle's JSON documents, from the manifest.

    Only the JSON documents. A report cannot list its own digest — the
    manifest hashes the report, so quoting it would change the value quoted —
    and for the same reason the two reports cannot list each other's. The
    manifest's own hash is the out-of-band anchor and is not in the bundle at
    all. Filtering here rather than reading whatever `files` happens to hold
    also keeps the render stable no matter when it runs relative to `attach()`.
    """
    files = manifest.get("files") or {}
    return [(name, files[name]) for name in _JSON_DOCUMENT_FILES if name in files]


def _withheld_note(previews: dict[str, Any]) -> str:
    """Name what was withheld, so absence cannot be mistaken for non-existence.

    A reader who cannot tell whether `context_presented` was stripped or never
    recorded has to assume the worse of the two — and would have no idea what
    to ask for. Naming the fields turns a hole into a request.
    """
    fields = previews.get("withheld_fields") or []
    if not fields:
        return ""
    events = previews.get("withheld_from_events", 0)
    return (
        f"Withheld from {events} event(s): {', '.join(fields)}. "
        "Ask the exporter for an unredacted bundle if you need them."
    )


def _range_label(first: Any, last: Any) -> str:
    if first is None:
        return ""
    return f" (sequence {first})" if first == last else f" (sequence {first}-{last})"


def _missing_label(verification: dict[str, Any]) -> str:
    ranges = verification.get("missing_ranges") or []
    if not ranges:
        return ""
    return ", ".join(
        str(start) if start == end else f"{start}-{end}"
        for start, end in (tuple(r) for r in ranges)
    )


def _or_dash(value: Any) -> str:
    return "—" if value in (None, "") else str(value)


def _preview_text(value: Any) -> str:
    if value is None:
        return "—"
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def render_markdown(documents: dict[str, Any]) -> str:
    """Render `report.md` from the bundle's JSON documents."""
    manifest = documents[MANIFEST_FILE]
    verification = documents[VERIFICATION_FILE]
    timeline = documents[TIMELINE_FILE]
    redaction = documents[REDACTION_FILE]

    verdict, meaning, detail = _verdict_line(verification)
    out: list[str] = []

    # Verdict first — before the title, because the title is not the answer.
    out.append(f"# {verdict} — RootSign evidence bundle")
    out.append("")
    out.append(f"**{meaning}**")
    out.append("")
    if detail:
        out.append(f"> {detail}")
        out.append("")
    missing = _missing_label(verification)
    if missing:
        out.append(f"Missing sequence numbers: **{missing}**")
        out.append("")

    out.append("## Session")
    out.append("")
    for label, value in _identity_rows(manifest, timeline):
        out.append(f"- **{label}:** {value}")
    out.append("")

    out.append("## Chain verification")
    out.append("")
    out.append(
        f"{verification.get('record_count', 0)} action record(s) checked against the frozen "
        f"canonical hash ({verification.get('hash', {}).get('canonical_spec', 'ADR-001')}, "
        f"{verification.get('hash', {}).get('algorithm', 'sha256')})."
    )
    out.append("")
    out.append("| # | Tool | Status | self_hash | prev_action_hash |")
    out.append("| ---: | --- | --- | --- | --- |")
    for record in verification.get("records", []):
        out.append(
            "| {seq} | {tool} | {status} | `{self_hash}` | `{prev}` |".format(
                seq=record.get("sequence_number", "?"),
                tool=record.get("tool_name", "?"),
                status=record.get("chain_status", "?"),
                self_hash=record.get("self_hash", ""),
                prev=record.get("prev_action_hash") or "—",
            )
        )
    out.append("")

    out.append("## What happened")
    out.append("")
    previews = timeline.get("previews", {})
    if not previews.get("included") and previews.get("note"):
        out.append(f"_{previews['note']}._")
        out.append("")
        withheld = _withheld_note(previews)
        if withheld:
            out.append(f"_{withheld}_")
            out.append("")
    out.append(
        "Every field recorded for each event, as stored. Field names match "
        "`timeline.json` so the two can be read side by side."
    )
    out.append("")
    for position, event in enumerate(timeline.get("events", []), start=1):
        headline = _event_summary(event)
        out.append(
            f"### {position}. {event.get('type', '?')}{f' — {headline}' if headline else ''}"
        )
        out.append("")
        out.append(f"_{_or_dash(event.get('timestamp'))}_")
        out.append("")
        fields = _event_fields(event)
        if not fields:
            out.append("_no further fields recorded_")
            out.append("")
            continue
        out.append("| Field | Value |")
        out.append("| --- | --- |")
        for key, value in fields:
            out.append(f"| `{key}` | {value.replace('|', chr(92) + '|')} |")
        out.append("")

    out.append("## Redaction")
    out.append("")
    totals = redaction.get("totals", {})
    out.append(
        f"{totals.get('redacted_fields', 0)} field(s) across "
        f"{totals.get('actions_with_redactions', 0)} action(s) were replaced with "
        f"`{redaction.get('sentinel', '[REDACTED]')}` before storage."
    )
    out.append("")
    for record in redaction.get("records", []):
        paths = ", ".join(record.get("input_paths", []) + record.get("output_paths", []))
        out.append(f"- #{record.get('sequence_number')}: {paths}")
    if redaction.get("records"):
        out.append("")
    out.append(f"_{redaction.get('rule_set', {}).get('provenance', '')}_")
    out.append("")

    out.append("## Bundle contents")
    out.append("")
    out.append(
        "SHA-256 of each machine-readable document in this bundle, copied from `manifest.json`."
    )
    out.append("")
    out.append("| File | SHA-256 |")
    out.append("| --- | --- |")
    for name, digest in _document_hashes(manifest):
        out.append(f"| `{name}` | `{digest}` |")
    out.append("")
    out.append(
        "_This report and `manifest.json` are hashed too, but a document cannot "
        "quote its own digest — those values are in `manifest.json`._"
    )
    out.append("")

    out.append("## Verifying this bundle")
    out.append("")
    out.append(_HOW_TO_VERIFY)
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_STYLE = """
:root { color-scheme: light; }
body { margin: 0; padding: 2rem 1.5rem 4rem; background: #f6f7f9; color: #14181f;
       font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, sans-serif; }
main { max-width: 60rem; margin: 0 auto; }
.verdict { border-radius: 10px; padding: 1.25rem 1.5rem; margin: 0 0 1.5rem;
           border: 1px solid; }
.verdict h1 { margin: 0 0 .35rem; font-size: 1.6rem; letter-spacing: .01em; }
.verdict p { margin: .25rem 0 0; }
.verdict .detail { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .85rem; }
.valid { background: #e9f7ee; border-color: #2f8f4e; color: #14532d; }
.tampered { background: #fdecec; border-color: #c0392b; color: #7f1d1d; }
.incomplete { background: #fff7e6; border-color: #b7791f; color: #7a4b06; }
h2 { margin: 2rem 0 .75rem; font-size: 1.1rem; text-transform: uppercase;
     letter-spacing: .06em; color: #4a5568; }
section { background: #fff; border: 1px solid #e2e6ec; border-radius: 8px; padding: 1rem 1.25rem; }
table { width: 100%; border-collapse: collapse; font-size: .9rem; }
th, td { text-align: left; padding: .45rem .6rem; border-bottom: 1px solid #eceff3;
         vertical-align: top; }
th { font-weight: 600; color: #4a5568; }
td.hash, code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .8rem;
                word-break: break-all; }
dl { display: grid; grid-template-columns: minmax(8rem, 12rem) 1fr; gap: .4rem 1rem; margin: 0; }
dt { font-weight: 600; color: #4a5568; }
dd { margin: 0; }
.status-verified { color: #276749; }
.status-failed { color: #9b2c2c; font-weight: 600; }
.status-unverified { color: #975a16; }
.note { color: #4a5568; font-style: italic; margin: 0 0 .75rem; }
.event { border: 1px solid #e2e6ec; border-radius: 6px; padding: .75rem 1rem 1rem;
         margin: 0 0 .85rem; background: #fcfcfd; }
.event h3 { margin: 0 0 .5rem; font-size: .95rem; display: flex; flex-wrap: wrap;
            align-items: baseline; gap: .6rem; }
.event .kind { font-weight: 700; letter-spacing: .04em; color: #1a202c; }
.event .pos { color: #a0aec0; font-weight: 400; }
.event time { color: #4a5568; font-weight: 400; font-size: .85rem;
              font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.event .headline { margin: 0 0 .6rem; color: #2d3748; }
table.fields { table-layout: fixed; }
table.fields th { width: 12rem; font-weight: 500; }
table.fields td.value { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
                        font-size: .8rem; word-break: break-word; white-space: pre-wrap; }
table.fields tr:last-child th, table.fields tr:last-child td { border-bottom: none; }
@media (max-width: 40rem) {
  table.fields, table.fields tbody, table.fields tr, table.fields th, table.fields td
    { display: block; width: auto; }
  table.fields th { padding-bottom: 0; border-bottom: none; }
}
footer { margin-top: 2rem; color: #4a5568; font-size: .85rem; }
@media print { body { background: #fff; } section { border: none; padding: 0; } }
"""


def render_html(documents: dict[str, Any]) -> str:
    """Render `report.html` from the bundle's JSON documents.

    One inline stylesheet, no scripts, no external references — the document
    has to render the same offline as it does on a corporate file share.
    """
    manifest = documents[MANIFEST_FILE]
    verification = documents[VERIFICATION_FILE]
    timeline = documents[TIMELINE_FILE]
    redaction = documents[REDACTION_FILE]

    verdict, meaning, detail = _verdict_line(verification)
    out: list[str] = []
    e = _escape

    out.append("<!DOCTYPE html>")
    out.append('<html lang="en"><head><meta charset="utf-8">')
    out.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    out.append(
        f"<title>{e(verdict)} — RootSign evidence {e(manifest.get('session_id', ''))}</title>"
    )
    out.append(f"<style>{_STYLE}</style>")
    out.append("</head><body><main>")

    # The verdict is the first thing in the document body, by design.
    out.append(f'<div class="verdict {verdict.lower()}">')
    out.append(f"<h1>{e(verdict)}</h1>")
    out.append(f"<p>{e(meaning)}</p>")
    if detail:
        out.append(f'<p class="detail">{e(detail)}</p>')
    missing = _missing_label(verification)
    if missing:
        out.append(f"<p>Missing sequence numbers: <strong>{e(missing)}</strong></p>")
    out.append("</div>")

    out.append("<h2>Session</h2><section><dl>")
    for label, value in _identity_rows(manifest, timeline):
        out.append(f"<dt>{e(label)}</dt><dd>{e(value)}</dd>")
    out.append("</dl></section>")

    hash_block = verification.get("hash", {})
    out.append("<h2>Chain verification</h2><section>")
    out.append(
        f'<p class="note">{verification.get("record_count", 0)} action record(s) checked '
        f"against the frozen canonical hash "
        f"({e(hash_block.get('canonical_spec', 'ADR-001'))}, "
        f"{e(hash_block.get('algorithm', 'sha256'))}).</p>"
    )
    out.append("<table><thead><tr><th>#</th><th>Tool</th><th>Status</th>")
    out.append("<th>self_hash</th><th>prev_action_hash</th></tr></thead><tbody>")
    for record in verification.get("records", []):
        status = str(record.get("chain_status", "?"))
        out.append(
            "<tr><td>{seq}</td><td>{tool}</td>"
            '<td class="status-{cls}">{status}</td>'
            '<td class="hash">{self_hash}</td><td class="hash">{prev}</td></tr>'.format(
                seq=e(record.get("sequence_number", "?")),
                tool=e(record.get("tool_name", "?")),
                cls=e(status),
                status=e(status),
                self_hash=e(record.get("self_hash", "")),
                prev=e(record.get("prev_action_hash") or "—"),
            )
        )
    out.append("</tbody></table></section>")

    previews = timeline.get("previews", {})
    out.append("<h2>What happened</h2><section>")
    if not previews.get("included") and previews.get("note"):
        out.append(f'<p class="note">{e(previews["note"])}</p>')
        withheld = _withheld_note(previews)
        if withheld:
            out.append(f'<p class="note">{e(withheld)}</p>')
    out.append(
        '<p class="note">Every field recorded for each event, as stored. Field names '
        "match <code>timeline.json</code> so the two can be read side by side.</p>"
    )
    for position, event in enumerate(timeline.get("events", []), start=1):
        headline = _event_summary(event)
        out.append('<article class="event">')
        out.append(
            '<h3><span class="kind">{kind}</span>'
            '<span class="pos">#{pos}</span>'
            "<time>{ts}</time></h3>".format(
                kind=e(event.get("type", "?")),
                pos=position,
                ts=e(_or_dash(event.get("timestamp"))),
            )
        )
        if headline:
            out.append(f'<p class="headline">{e(headline)}</p>')
        fields = _event_fields(event)
        if not fields:
            out.append('<p class="note">no further fields recorded</p>')
        else:
            out.append('<table class="fields"><tbody>')
            for key, value in fields:
                out.append(
                    f'<tr><th scope="row"><code>{e(key)}</code></th>'
                    f'<td class="value">{e(value)}</td></tr>'
                )
            out.append("</tbody></table>")
        out.append("</article>")
    out.append("</section>")

    totals = redaction.get("totals", {})
    out.append("<h2>Redaction</h2><section>")
    out.append(
        f"<p>{totals.get('redacted_fields', 0)} field(s) across "
        f"{totals.get('actions_with_redactions', 0)} action(s) were replaced with "
        f"<code>{e(redaction.get('sentinel', '[REDACTED]'))}</code> before storage.</p>"
    )
    if redaction.get("records"):
        out.append("<table><thead><tr><th>#</th><th>Redacted field paths</th></tr></thead><tbody>")
        for record in redaction["records"]:
            paths = ", ".join(record.get("input_paths", []) + record.get("output_paths", []))
            out.append(
                f"<tr><td>{e(record.get('sequence_number', '?'))}</td>"
                f'<td class="hash">{e(paths)}</td></tr>'
            )
        out.append("</tbody></table>")
    out.append(f'<p class="note">{e(redaction.get("rule_set", {}).get("provenance", ""))}</p>')
    out.append("</section>")

    out.append("<h2>Bundle contents</h2><section>")
    out.append(
        '<p class="note">SHA-256 of each machine-readable document in this bundle, '
        "copied from <code>manifest.json</code>.</p>"
    )
    out.append("<table><thead><tr><th>File</th><th>SHA-256</th></tr></thead><tbody>")
    for name, digest in _document_hashes(manifest):
        out.append(f'<tr><td><code>{e(name)}</code></td><td class="hash">{e(digest)}</td></tr>')
    out.append("</tbody></table>")
    out.append(
        '<p class="note">This report and <code>manifest.json</code> are hashed too, '
        "but a document cannot quote its own digest — those values are in "
        "<code>manifest.json</code>.</p>"
    )
    out.append("</section>")

    out.append(f"<footer><h2>Verifying this bundle</h2><p>{e(_HOW_TO_VERIFY)}</p></footer>")
    out.append("</main></body></html>")
    return "\n".join(out) + "\n"


def _escape(value: Any) -> str:
    """Escape anything on its way into the HTML.

    Everything rendered here originated as data an agent handled — tool names,
    objectives, approval notes, payload previews. A bundle that executed markup
    from its own evidence would be a report that an attacker gets to write.
    """
    return html.escape(str(value), quote=True)
