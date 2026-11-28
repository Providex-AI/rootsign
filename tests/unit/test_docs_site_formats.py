"""The Formats reference pages stay true to the code.

`reference/evidence-bundle.mdx` describes every file and field of an evidence
bundle; `reference/ingest-spec-v1.mdx` is the wire specification itself, moved
onto the site. These tests hold both to what the code produces and accepts:

- the bundle page's field lists match the frozen golden bundle
  (tests/fixtures/evidence_bundle_v1.json) and a synced bundle's receipt;
  its JSON examples are real documents, and its claims about hashing,
  serialization and `--redact-previews` are checked against a fresh export;
- the spec's envelope, payload, response and error-code tables match
  `rootsign/ingest/schemas.py`, as do the versions it states.
"""

from __future__ import annotations

import hashlib
import json
import re
import textwrap
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from rootsign import errors
from rootsign.ingest import schemas
from rootsign.seal import SEAL_FIELDS
from rootsign.ingest.handler import SUPPORTED_SCHEMA_MAJOR
from rootsign.sdk import export, receipt
from rootsign.sdk.cli import app
from tests.support.docs_pages import REPO, code_block, page_text, run_script

BUNDLE = "reference/evidence-bundle.mdx"
SPEC = "reference/ingest-spec-v1.mdx"
GOLDEN = json.loads((REPO / "tests/fixtures/evidence_bundle_v1.json").read_text())
SYNCED = next((REPO / "tests/fixtures/sync-receipt/v1.1/v2-sequence-gap/bundle").glob("evidence-*"))

runner = CliRunner()


def _section(page: str, heading: str) -> str:
    text = page_text(page)
    start = text.index(f"\n{heading}\n")
    level = heading.split(" ")[0]
    end = re.search(rf"\n#{{1,{len(level)}}} ", text[start + 1 :])
    return text[start : start + 1 + end.start()] if end else text[start:]


def _field_names(text: str) -> list[str]:
    return re.findall(r'<ResponseField name="(\w+)"', text)


def _table_column(text: str, after: str) -> list[str]:
    """First-column code names of the first Markdown table after `after`.

    Handles tables nested (indented) inside a component and both separator
    styles, `| --- |` and `|---|`.
    """
    rows = []
    started = False
    for line in text[text.index(after) :].splitlines():
        stripped = line.strip()
        if re.fullmatch(r"\|[\s:|-]*-[\s:|-]*", stripped):
            started = True
            continue
        if started:
            if not stripped.startswith("|"):
                break
            rows.append(stripped)
    names = []
    for row in rows:
        names += re.findall(r"`(\w+)`", row.split("|")[1])
    return names


def _json_blocks(text: str) -> list[object]:
    return [json.loads(b) for b in re.findall(r"```json\n(.*?)\n```", text, re.S)]


# ===========================================================================
# Evidence bundle
# ===========================================================================


def test_the_manifest_fields_and_example_are_the_golden_bundle_s():
    section = _section(BUNDLE, "## manifest.json")
    assert _field_names(section) == list(GOLDEN["manifest.json"])
    assert _json_blocks(section) == [GOLDEN["manifest.json"]]


def test_the_manifest_agent_block_for_each_source():
    section = _section(BUNDLE, "## manifest.json")
    agent = section.split('name="agent"')[1].split("</ResponseField>")[0]
    row = SimpleNamespace(
        agent_id="a",
        name="n",
        owner="o",
        environment="e",
        risk_tier="r",
        framework="f",
        model_version="m",
        regulatory_categories=[],
    )
    postgres = list(export._agent_block(row))
    assert postgres[0] == "agent_id"
    for field in postgres[1:]:
        assert f"`{field}`" in agent, field
    # From a JSONL file the block holds only agent_id (the golden bundle is one).
    assert list(GOLDEN["manifest.json"]["agent"]) == ["agent_id"]
    assert 'source_location="database"' in (REPO / "rootsign/sdk/export.py").read_text()
    assert "or `database` for PostgreSQL" in section


def test_verification_fields():
    section = _section(BUNDLE, "## verification.json")
    real = [*GOLDEN["verification.json"], "replay"]
    assert _field_names(section) == real

    records = _table_column(section, 'name="records"')
    assert records == list(GOLDEN["verification.json"]["records"][0])

    replay = json.loads((SYNCED / "verification.json").read_text())["replay"]
    listed = re.findall(r"`(\w+)`", section.split('name="replay"')[1].split("</ResponseField>")[0])
    assert [n for n in listed if n in replay] == list(replay)


def test_chain_status_values():
    statuses = {
        export._record_status("VALID", 1, None),
        export._record_status("TAMPERED", 2, 2),
        export._record_status("TAMPERED", 3, 2),
    }
    assert statuses == {"verified", "failed", "unverified"}
    row = _section(BUNDLE, "## verification.json").split("| `chain_status` |")[1].split("\n")[0]
    assert set(re.findall(r"`(\w+)`", row)) == statuses


def test_timeline_fields():
    section = _section(BUNDLE, "## timeline.json")
    timeline = GOLDEN["timeline.json"]
    assert _field_names(section) == list(timeline)

    session = section.split('name="session"')[1].split("</ResponseField>")[0]
    assert [n for n in re.findall(r"`(\w+)`", session) if n in timeline["session"]] == list(
        timeline["session"]
    )
    assert _table_column(section, 'name="previews"') == list(timeline["previews"])


def test_each_event_type_lists_its_fields():
    section = _section(BUNDLE, "## timeline.json")
    rows = dict(re.findall(r"^\| `([A-Z_]+)` \| (.+?) \|$", section, re.M))

    observed: dict[str, list[str]] = {}
    for event in GOLDEN["timeline.json"]["events"]:
        observed.setdefault(event["type"], [k for k in event if k not in ("type", "timestamp")])
    source = (REPO / "rootsign/sdk/export.py").read_text()
    loss = source[source.index('"type": "RECORD_LOSS"') :].split("}")[0]
    observed["RECORD_LOSS"] = [
        k for k in re.findall(r'"(\w+)":', loss) if k not in ("type", "timestamp")
    ]

    assert set(rows) == set(observed) == set(re.findall(r'"type": "([A-Z_]+)"', source))
    for kind, fields in observed.items():
        assert re.findall(r"`(\w+)`", rows[kind]) == fields, kind


def test_the_action_example_is_the_golden_rejected_action():
    (example,) = _json_blocks(_section(BUNDLE, "## timeline.json"))
    actions = [e for e in GOLDEN["timeline.json"]["events"] if e["type"] == "ACTION"]
    assert example in actions
    assert example["authorization_status"] == "human_rejected"


def test_redact_previews_removes_the_listed_fields():
    section = _section(BUNDLE, "## timeline.json")
    sentence = section[section.index("With `--redact-previews`") :].split("\n")[0]
    assert set(re.findall(r"`(\w+)`", sentence)) - {"--redact-previews"} == set(
        export.CONTENT_FIELDS
    )
    assert len(export.CONTENT_FIELDS) == 6 and "six fields" in sentence


def test_redaction_fields_and_example(tmp_path):
    section = _section(BUNDLE, "## redaction.json")
    assert _field_names(section) == list(GOLDEN["redaction.json"])
    (example,) = _json_blocks(section)

    source = textwrap.dedent(
        """
        import asyncio
        import rootsign
        from rootsign import StandardPIIConfig

        rootsign.init(agent="support-agent")


        @rootsign.trace(redaction_config=StandardPIIConfig())
        async def email_customer(email: str, subject: str, phone: str = "") -> dict:
            return {"status": "queued"}


        async def main() -> None:
            async with rootsign.session(objective="notify a customer") as ctx:
                await email_customer("alice@example.com", subject="Your refund", phone="+44 20 7946 0958")
            print(ctx.session_id)


        asyncio.run(main())
        """
    )
    run = run_script(source, tmp_path)
    out = tmp_path / "bundles"
    result = runner.invoke(
        app, ["export", "--local", str(run.only_session()), "--out", str(out), "-f", "json"]
    )
    assert result.exit_code == 0, result.output
    produced = json.loads(next(out.glob("evidence-*/redaction.json")).read_text())

    example["records"][0]["action_id"] = produced["records"][0]["action_id"]
    assert produced == example


def test_the_sync_receipt_table():
    section = _section(BUNDLE, "## sync-receipt.json")
    document = json.loads((SYNCED / "sync-receipt.json").read_text())
    attempt = document["attempts"][0]
    assert list(document) == ["session_id", "attempts"]
    assert "`session_id`, and `attempts`" in section

    listed = _table_column(section, "| Field | Meaning |")
    assert listed == list(attempt)
    assert attempt["schema_version"] == receipt.RECEIPT_SCHEMA_VERSION
    assert f"`{receipt.RECEIPT_SCHEMA_VERSION}`" in section

    outcomes = section.split("| `outcomes` |")[1].split("\n")[0]
    assert re.findall(r"`(\w+)`", outcomes) == list(attempt["outcomes"])
    disposition = section.split("| `disposition` |")[1].split("\n")[0]
    assert set(re.findall(r"`(\w+)`", disposition)) == {
        receipt.DISPOSITION_FULLY_ACCEPTED,
        receipt.DISPOSITION_PARTIAL,
        receipt.DISPOSITION_TRANSPORT_FAILED,
    }


def test_the_bundle_version():
    assert export.EVIDENCE_BUNDLE_VERSION == "1.0"
    assert "`bundle_version` is `1.0`" in _section(BUNDLE, "## Versioning")


def test_hashes_serialization_and_the_printed_anchor(tmp_path):
    """What the page says about bytes, hashes and the printed SHA-256."""
    run = run_script(code_block("quickstart.mdx", "agent.py"), tmp_path, name="agent.py")
    out = tmp_path / "bundles"
    result = runner.invoke(app, ["export", "--local", str(run.only_session()), "--out", str(out)])
    assert result.exit_code == 0, result.output
    bundle = next(out.glob("evidence-*"))

    manifest_bytes = (bundle / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    assert list(manifest["files"]) == sorted(manifest["files"])
    assert set(manifest["files"]) == {p.name for p in bundle.iterdir()} - {"manifest.json"}
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((bundle / name).read_bytes()).hexdigest() == digest
    printed = re.search(r"manifest.json SHA-256:\s+(\w+)", result.output).group(1)
    assert printed == hashlib.sha256(manifest_bytes).hexdigest()

    for path in bundle.glob("*.json"):
        text = path.read_bytes().decode("utf-8")
        assert text.endswith("}\n")
        assert text.startswith('{\n  "')
    assert "indented with two spaces, and end with a newline" in page_text(BUNDLE)


# ===========================================================================
# Ingest specification
# ===========================================================================

SPEC_TABLES = [
    ("## 2. The envelope", schemas.IngestEnvelope),
    ("### 3.1 `SESSION_OPEN`", schemas.SessionOpenPayload),
    ("### 3.2 `ACTION_RECORD`", schemas.ActionRecordPayload),
    ("### 3.3 `DECISION_RECORD`", schemas.DecisionRecordPayload),
    ("### 3.4 `APPROVAL_RECORD`", schemas.ApprovalRecordPayload),
    ("### 3.5 `SESSION_CLOSE`", schemas.SessionClosePayload),
    ("## 4. Responses", schemas.IngestResponse),
]

#: ACTION_RECORD's cloud-only sealing fields, specified in §8.2 rather than §3.2.
SEALED = ["action_id", "sequence_number", "prev_action_hash", "self_hash"]


@pytest.mark.parametrize(
    ("heading", "model"), SPEC_TABLES, ids=[m.__name__ for _, m in SPEC_TABLES]
)
def test_each_spec_table_matches_its_schema(heading, model):
    documented = re.findall(r"^\| `([a-z_]+)`", _section(SPEC, heading), re.M)
    real = list(model.model_fields)
    if model is schemas.ActionRecordPayload:
        real = [f for f in real if f not in SEALED]
        sealing = _section(SPEC, "### 8.2 What the client sends")
        for field in SEALED:
            assert f"`{field}`" in sealing, field
    assert documented == real


def test_the_seal_table_lists_the_seal_fields():
    """§8.5 lists the keys `compute_seal` writes, in order, and the rules it follows."""
    section = _section(SPEC, "### 8.5 The session seal")
    fields = section.split("**What each party does:**")[0]
    documented = re.findall(r"^\| `([a-z_]+)` \|", fields, re.M)
    assert documented == list(SEAL_FIELDS)
    assert "seal" not in schemas.SessionClosePayload.model_fields
    assert "`tests/fixtures/session-seal/v1/`" in section
    assert (REPO / "tests/fixtures/session-seal/v1/vectors.json").is_file()


def test_extra_fields_policy():
    allow = {m.__name__ for _, m in SPEC_TABLES if m.model_config.get("extra") == "allow"}
    assert allow == {"SessionOpenPayload", "SessionClosePayload"}
    assert "### 3.6 Why two payloads allow extras and three forbid them" in page_text(SPEC)
    assert 'extra="forbid"' in _section(SPEC, "## 2. The envelope")


def test_event_types():
    stated = re.findall(r"^### 3\.\d `([A-Z_]+)`", page_text(SPEC), re.M)
    assert stated == [e.value for e in schemas.EventType]


def test_error_code_tables():
    source = (REPO / "rootsign/ingest/schemas.py").read_text()
    enum = source[source.index("class ErrorCode") :].split("\n\n\n")[0]
    split = enum.index("# Retryable")
    non_retryable = re.findall(r'= "(\w+)"', enum[:split])
    retryable = re.findall(r'= "(\w+)"', enum[split:])
    assert set(non_retryable) | set(retryable) == {c.value for c in schemas.ErrorCode}

    registry = _section(SPEC, "## 5. Error-code registry")
    assert _table_column(registry, "### Non-retryable") == non_retryable
    assert _table_column(registry, "### Retryable") == retryable

    for cls in errors.IngestError.__subclasses__():
        expected = retryable if cls.retryable else non_retryable
        assert cls.error_code in expected, cls.__name__


def test_versions_stated():
    assert schemas.SCHEMA_VERSION == "1.1"
    assert 'Emitted as `"1.1"` by the current SDK' in _section(SPEC, "## 2. The envelope")
    assert f'`SUPPORTED_SCHEMA_MAJOR` is\n   `"{SUPPORTED_SCHEMA_MAJOR}"`' in page_text(SPEC)


def test_the_old_path_points_at_the_new_one():
    pointer = (REPO / "docs/ingest-spec-v1.md").read_text()
    assert "site/reference/ingest-spec-v1.mdx" in pointer
    assert (REPO / "docs/site" / Path(SPEC)).is_file()


@pytest.mark.parametrize(
    ("row_status", "statuses"),
    [
        ("400", [400]),
        ("401", [401, 403]),
        ("404", [404]),
        ("413", [413]),
        ("429", [429]),
        ("5xx", [500, 502, 503, 504]),
    ],
)
async def test_the_http_status_table_is_what_the_client_does(row_status, statuses, tmp_path):
    """Every row of the spec's HTTP mapping table, against the real client."""
    import httpx

    from rootsign.ingest.schemas import ErrorCode
    from rootsign.sdk.client import HttpIngestClient

    section = _section(SPEC, "### HTTP status mapping (cloud binding)")
    (row,) = [r for r in section.splitlines() if r.startswith(f"| `{row_status}`")]
    maps_to = row.split(" | ")[-1]
    retryable = "non-retryable" not in maps_to
    named = {c for c in re.findall(r"`([A-Z_]+)`", maps_to) if c in ErrorCode.__members__}

    for status in statuses:
        client = HttpIngestClient(
            "https://ingest.example.test/v1",
            "key",
            max_retries=1,
            transport=httpx.MockTransport(lambda request, s=status: httpx.Response(s)),
            spool_dir=str(tmp_path / str(status)),
        )
        envelope = {
            "schema_version": "1.0",
            "sdk_version": "0.3.1",
            "event_type": "SESSION_OPEN",
            "event_id": str(uuid4()),
            "emitted_at": "2026-11-22T12:00:00+00:00",
            "agent_id": str(uuid4()),
            "session_id": str(uuid4()),
            "payload": {},
        }
        response = await client.handle(envelope)
        code = client.spool_reason if client.is_spooling else response.error_code
        assert code.value in named, (status, code, row)
        assert client.is_spooling is retryable, (status, row)
