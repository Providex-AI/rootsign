"""The "Capture decisions" guide stays true.

`guides/decision-capture.mdx`: runs the page's `refund.py` with capture on and
checks the two records against the JSON the page shows, then holds each claim
in the linking, depth, refused-values, recorded-fields and limits sections to
a real run: a second decision replaces the first, a held call takes the
decision, `full` keeps the alternatives, an invalid confidence logs exactly
the warning shown, `--redact-previews` drops what the page says, and edits to
decisions still verify as VALID.
"""

from __future__ import annotations

import json
import re
import textwrap
from unittest.mock import patch
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from rootsign.ingest.schemas import DecisionRecordPayload
from rootsign.sdk.cli import app
from tests.support.docs_pages import code_block, output_after, page_text, run_script

PAGE = "guides/decision-capture.mdx"
CAPTURE = {"ROOTSIGN_CAPTURE_DECISIONS": "true"}


def _json_block(title: str):
    return json.loads(code_block(PAGE, title, language="json"))


def _records(run) -> list[dict]:
    lines = run.only_session().read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _of(records, event_type):
    return [r for r in records if r["event_type"] == event_type]


@pytest.fixture
def refund(tmp_path):
    return run_script(code_block(PAGE, "refund.py"), tmp_path, name="refund.py", extra_env=CAPTURE)


def _variant(tmp_path, body: str, extra_env: dict[str, str] | None = None):
    """The page's script with `main()`'s session body replaced by `body`."""
    source = code_block(PAGE, "refund.py")
    start = source.index("        await ctx.record_decision(")
    end = source.index("    print(ctx.session_id)")
    source = source[:start] + textwrap.indent(textwrap.dedent(body), " " * 8) + source[end:]
    return run_script(
        source, tmp_path, name="variant.py", extra_env={**CAPTURE, **(extra_env or {})}
    )


class TestTheWalkthrough:
    def test_the_output_is_as_shown(self, refund):
        shown = output_after(PAGE, "python refund.py")
        printed = refund.stdout.strip().splitlines()
        assert printed[0] == shown[0]
        assert re.fullmatch(r"[0-9a-f-]{36}", printed[1])
        assert re.fullmatch(r"[0-9a-f-]{36}", shown[1])

    def test_the_decision_record_is_as_shown(self, refund):
        (decision,) = _of(_records(refund), "DECISION_RECORD")
        shown = _json_block("DECISION_RECORD")
        stored = {k: v for k, v in decision["payload"].items() if k != "timestamp"}
        assert stored == shown["payload"]
        assert set(shown) == {"event_type", "decision_id", "payload"}

    def test_the_action_carries_the_decision_id(self, refund):
        records = _records(refund)
        (decision,) = _of(records, "DECISION_RECORD")
        (action,) = _of(records, "ACTION_RECORD")
        shown = _json_block("ACTION_RECORD")
        assert action["decision_id"] == decision["decision_id"]
        for field in ("tool_name", "sequence_number"):
            assert action[field] == shown[field]
        assert shown["decision_id"] == _json_block("DECISION_RECORD")["decision_id"]

    def test_full_depth_keeps_the_alternatives(self, tmp_path):
        run = run_script(
            code_block(PAGE, "refund.py"),
            tmp_path,
            name="refund.py",
            extra_env={**CAPTURE, "ROOTSIGN_REASONING_DEPTH": "full"},
        )
        (decision,) = _of(_records(run), "DECISION_RECORD")
        assert decision["payload"]["alternatives_considered"] == [
            "escalate_to_human",
            "deny_refund",
        ]
        assert "Run it with `ROOTSIGN_REASONING_DEPTH=full` and the record keeps both." in (
            page_text(PAGE)
        )

    def test_the_session_verifies(self, refund):
        result = CliRunner().invoke(app, ["verify", "--local", str(refund.only_session())])
        assert result.exit_code == 0, result.output


class TestLinking:
    def test_one_decision_links_to_one_action(self, tmp_path):
        run = _variant(
            tmp_path,
            """
            await ctx.record_decision(selected_action="issue_refund")
            await issue_refund("ord-1", 1.0)
            await issue_refund("ord-2", 2.0)
            """,
        )
        first, second = _of(_records(run), "ACTION_RECORD")
        assert first["decision_id"] is not None
        assert second["decision_id"] is None

    def test_a_second_decision_replaces_the_first(self, tmp_path):
        run = _variant(
            tmp_path,
            """
            await ctx.record_decision(selected_action="deny_refund")
            await ctx.record_decision(selected_action="issue_refund")
            await issue_refund("ord-1", 1.0)
            """,
        )
        records = _records(run)
        first, second = _of(records, "DECISION_RECORD")
        (action,) = _of(records, "ACTION_RECORD")
        assert action["decision_id"] == second["decision_id"] != first["decision_id"]

    def test_the_link_is_by_order_not_by_name(self, tmp_path):
        run = _variant(
            tmp_path,
            """
            await ctx.record_decision(selected_action="something_else")
            await issue_refund("ord-1", 1.0)
            """,
        )
        records = _records(run)
        (decision,) = _of(records, "DECISION_RECORD")
        (action,) = _of(records, "ACTION_RECORD")
        assert action["decision_id"] == decision["decision_id"]

    async def test_a_held_call_takes_the_decision(self, tmp_path, monkeypatch):
        from rootsign.sdk.context import SessionContext
        from rootsign.sdk.decorator import trace
        from rootsign.sdk.jsonl_client import JsonlIngestClient

        monkeypatch.setenv("ROOTSIGN_CAPTURE_DECISIONS", "true")
        client = JsonlIngestClient(data_dir=tmp_path)
        ctx = SessionContext(agent_id=uuid4())

        @trace(ingest_client=client, session_context=ctx, require_approval=True)
        async def wire_transfer(amount: float) -> str:
            return "sent"

        decision_id = await ctx.record_decision(
            selected_action="wire_transfer", ingest_client=client
        )
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value="y"),
        ):
            await wire_transfer(amount=10.0)

        lines = client._session_path(str(ctx.session_id)).read_text().splitlines()
        (pending,) = [r for r in map(json.loads, lines) if r["event_type"] == "ACTION_RECORD"]
        assert pending["authorization_status"] == "pending"
        assert pending["decision_id"] == str(decision_id)


class TestDepth:
    def test_the_limits_in_the_depth_sentence(self, tmp_path):
        long = "x" * 12_000
        stored = {}
        for depth in ("minimal", "summary", "full"):
            (tmp_path / depth).mkdir()
            run = _variant(
                tmp_path / depth,
                f"""
                await ctx.record_decision(
                    selected_action="issue_refund",
                    reasoning_summary="{long}",
                    confidence=0.5,
                    alternatives_considered=["a"],
                )
                await issue_refund("ord-1", 1.0)
                """,
                {"ROOTSIGN_REASONING_DEPTH": depth},
            )
            (decision,) = _of(_records(run), "DECISION_RECORD")
            stored[depth] = decision["payload"]

        assert stored["minimal"]["reasoning_summary"] is None
        assert stored["minimal"]["confidence"] == 0.5
        assert stored["minimal"]["reasoning_captured"] is False
        assert len(stored["summary"]["reasoning_summary"]) == 500
        assert stored["summary"]["alternatives_considered"] == []
        assert len(stored["full"]["reasoning_summary"]) == 10_000
        assert stored["full"]["alternatives_considered"] == ["a"]
        assert {p["reasoning_depth"] for p in stored.values()} == {"minimal", "summary", "full"}
        text = page_text(PAGE)
        assert "first 500 characters" in text and "up to 10,000 characters" in text


class TestRefusedValues:
    def test_the_limits_table_matches_the_schema(self):
        fields = DecisionRecordPayload.model_fields
        meta = {
            name: {type(m).__name__: m for m in fields[name].metadata}
            for name in ("selected_action", "confidence")
        }
        assert meta["selected_action"]["MinLen"].min_length == 1
        assert meta["selected_action"]["MaxLen"].max_length == 200
        assert meta["confidence"]["Ge"].ge == 0.0
        assert meta["confidence"]["Le"].le == 1.0
        assert fields["selected_action"].is_required()
        text = page_text(PAGE)
        assert "| `selected_action` | Required, 1 to 200 characters |" in text
        assert "| `confidence` | `0.0` to `1.0`, or `None` |" in text

    def test_the_warning_is_as_shown(self, tmp_path):
        source = code_block(PAGE, "refund.py").replace("confidence=0.92", "confidence=1.5")
        source = source.replace(
            "import rootsign\n",
            "import logging\nimport sys\n\nimport rootsign\n\n"
            'logging.basicConfig(stream=sys.stdout, format="%(message)s")\n',
            1,
        )
        run = run_script(source, tmp_path, name="refund.py", extra_env=CAPTURE)

        (shown,) = output_after(PAGE, "so the agent keeps running:")
        assert shown in run.stdout.splitlines()
        assert _of(_records(run), "DECISION_RECORD") == []
        (action,) = _of(_records(run), "ACTION_RECORD")
        assert action["decision_id"] is None


def test_capture_off_records_nothing(tmp_path):
    run = _variant(
        tmp_path,
        """
        print(await ctx.record_decision(selected_action="issue_refund"))
        await issue_refund("ord-1", 1.0)
        """,
        {"ROOTSIGN_CAPTURE_DECISIONS": "false"},
    )
    assert run.stdout.splitlines()[0] == "None"
    assert _of(_records(run), "DECISION_RECORD") == []


class TestWhatGetsRecorded:
    def test_the_decision_fields_row(self, refund):
        row = re.search(r"^\| Decision record \| (.+) \|$", page_text(PAGE), re.M).group(1)
        listed = set(re.findall(r"`(\w+)`", row))
        (decision,) = _of(_records(refund), "DECISION_RECORD")
        assert listed == set(decision["payload"]) | {"decision_id"}

    def test_redact_previews_drops_what_the_page_says(self, refund, tmp_path):
        out = tmp_path / "bundle"
        result = CliRunner().invoke(
            app,
            [
                "export",
                "--local",
                str(refund.only_session()),
                "--out",
                str(out),
                "--redact-previews",
            ],
        )
        assert result.exit_code == 0, result.output
        [bundle] = out.iterdir()
        timeline = json.loads((bundle / "timeline.json").read_text())
        (decision,) = [e for e in timeline["events"] if e["type"] == "DECISION"]
        assert "reasoning_summary" not in decision
        assert "alternatives_considered" not in decision
        assert decision["selected_action"] == "issue_refund"
        assert decision["confidence"] == 0.92


class TestLimits:
    @pytest.mark.parametrize("edit", ["reasoning", "pointer"])
    def test_editing_a_decision_in_a_sealed_session_is_tampered(self, refund, tmp_path, edit):
        records = _records(refund)
        for record in records:
            if edit == "reasoning" and record["event_type"] == "DECISION_RECORD":
                record["payload"]["reasoning_summary"] = "Customer threatened a chargeback."
            if edit == "pointer" and record["event_type"] == "ACTION_RECORD":
                record["decision_id"] = str(uuid4())
        edited = tmp_path / "edited.jsonl"
        edited.write_text("".join(json.dumps(r) + "\n" for r in records))

        result = CliRunner().invoke(app, ["verify", "--local", str(edited)])
        assert result.exit_code == 1
        assert "TAMPERED" in result.output and "decision" in result.output

    def test_without_a_seal_the_same_edit_still_verifies(self, refund, tmp_path):
        """What the page says about sessions recorded before the seal."""
        records = _records(refund)
        for record in records:
            if record["event_type"] == "DECISION_RECORD":
                record["payload"]["reasoning_summary"] = "Customer threatened a chargeback."
            if record["event_type"] == "SESSION_CLOSE":
                record["payload"].pop("seal")
        edited = tmp_path / "edited.jsonl"
        edited.write_text("".join(json.dumps(r) + "\n" for r in records))

        result = CliRunner().invoke(app, ["verify", "--local", str(edited)])
        assert result.exit_code == 0
        assert "VALID" in result.output and "Seal:     none" in result.output

    def test_redaction_does_not_reach_decisions(self, tmp_path):
        run = _variant(
            tmp_path,
            """
            await ctx.record_decision(
                selected_action="issue_refund",
                reasoning_summary="Refund requested by alice@example.com",
            )
            await issue_refund("ord-1", 1.0)
            """,
        )
        source = (tmp_path / "variant.py").read_text()
        assert "redaction_config" not in source  # the decision API takes none
        (decision,) = _of(_records(run), "DECISION_RECORD")
        assert "alice@example.com" in decision["payload"]["reasoning_summary"]
