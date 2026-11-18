"""The "Redact PII" guide and the JSONL half of "Require human approval" stay true.

`guides/redaction.mdx`: runs the page's `support.py` and checks that the two
action records and `redaction.json` are exactly the JSON the page prints
(including the free-text result that is NOT redacted), that every row of the
"How matching works" table holds, and that the custom-rule examples behave as
the page says, including the warning that `customer.tax_id` matches nothing.

`guides/human-approval.mdx`: the JSONL inline prompt and the headless error.
The PostgreSQL approve/reject/timeout flow is in
tests/integration/test_docs_site_approval.py.
"""

from __future__ import annotations

import getpass
import json
import re
import sys
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from rootsign.sdk.cli import app
from tests.support.docs_pages import code_block, page_text, run_script

REDACTION = "guides/redaction.mdx"
APPROVAL = "guides/human-approval.mdx"


def _json_block(page: str, title: str):
    return json.loads(code_block(page, title, language="json"))


# --- guides/redaction.mdx -----------------------------------------------------


class TestRedactionGuide:
    @pytest.fixture
    def run(self, tmp_path):
        return run_script(code_block(REDACTION, "support.py"), tmp_path, name="support.py")

    def test_the_records_are_exactly_as_shown(self, run):
        actions = {a["tool_name"]: a for a in run.actions()}

        for tool in ("lookup_customer", "email_customer"):
            shown = _json_block(REDACTION, tool)
            assert actions[tool]["input_redacted"] == shown["input_redacted"], tool
            assert actions[tool]["output_redacted"] == shown["output_redacted"], tool

    def test_the_free_text_result_is_stored_as_the_page_warns(self, run):
        email = {a["tool_name"]: a for a in run.actions()}["email_customer"]

        assert "alice@example.com" in email["output_redacted"]["result"]
        assert "alice@example.com" not in json.dumps(email["input_redacted"])

    def test_the_session_verifies(self, run):
        assert (
            CliRunner().invoke(app, ["verify", "--local", str(run.only_session())]).exit_code == 0
        )

    def test_redaction_json_is_as_shown(self, run, tmp_path):
        result = CliRunner().invoke(
            app, ["export", "--local", str(run.only_session()), "--out", str(tmp_path / "b")]
        )
        assert result.exit_code == 0, result.output
        [bundle] = (tmp_path / "b").iterdir()
        actual = json.loads((bundle / "redaction.json").read_text())
        shown = _json_block(REDACTION, "redaction.json")

        assert actual["totals"] == shown["totals"]
        assert [
            {k: r[k] for k in ("sequence_number", "input_paths", "output_paths")}
            for r in actual["records"]
        ] == shown["records"]

    def test_the_matching_table(self):
        """Each row of "How matching works", in order."""
        from rootsign import StandardPIIConfig
        from rootsign.sdk.decorator import _redacted_call_input

        config = StandardPIIConfig()

        def email_customer(email, subject):
            pass

        def stored(*args, **kwargs):
            return json.dumps(_redacted_call_input(email_customer, args, kwargs, config))

        def returned(value):
            return json.dumps(config.redact({"result": value}))

        address = "alice@example.com"
        assert address not in stored(email=address, subject="x")
        assert address not in stored(address, "x")
        assert address not in returned({"email": address})
        assert address not in returned({"contact": {"email": address}})
        assert address in returned(f"sent 'Your invoice' to {address}")
        assert "not an address" in stored(email="not an address", subject="x")

    def test_the_extra_rules_example(self):
        namespace: dict = {}
        exec(  # noqa: S102 — the page's own snippet
            "from rootsign import FinancialPIIConfig\n"
            'redaction = FinancialPIIConfig(extra_rules={"policy_number": r"POL-\\d{6}"})\n',
            namespace,
        )
        assert namespace["redaction"].redact({"policy_number": "POL-123456"}) == {
            "policy_number": "[REDACTED]"
        }
        assert 'extra_rules={"policy_number": r"POL-\\d{6}"}' in page_text(REDACTION)

    def test_path_rules_start_at_kwargs_as_the_warning_says(self):
        from rootsign import RedactionConfig
        from rootsign.sdk.decorator import _redacted_call_input

        def update(customer):
            pass

        call = {"customer": {"tax_id": "123-45"}}
        wrong = _redacted_call_input(update, (), call, RedactionConfig({"customer.tax_id": r".+"}))
        right = _redacted_call_input(
            update, (), call, RedactionConfig({"kwargs.customer.tax_id": r".+"})
        )

        assert wrong["kwargs"]["customer"]["tax_id"] == "123-45"
        assert right["kwargs"]["customer"]["tax_id"] == "[REDACTED]"

    def test_the_prebuilt_table_lists_each_config_field(self):
        from rootsign import FinancialPIIConfig, HealthcarePIIConfig, StandardPIIConfig

        text = page_text(REDACTION)
        for config in (StandardPIIConfig(), FinancialPIIConfig(), HealthcarePIIConfig()):
            for field in config._leaf_rules:
                assert f"`{field}`" in text, (
                    f"{type(config).__name__}.{field} missing from the table"
                )


# --- guides/human-approval.mdx: JSONL --------------------------------------------


class TestApprovalGuideJsonl:
    def _transfer(self, tmp_path):
        source = code_block(APPROVAL, "transfer.py")
        assert "timeout_seconds=300" in source
        return source

    def test_headless_fails_before_the_tool_runs(self, tmp_path):
        source = self._transfer(tmp_path)
        (tmp_path / "transfer.py").write_text(source)
        import os
        import subprocess

        env = {**os.environ, "ROOTSIGN_DATA_DIR": str(tmp_path / "d"), "ROOTSIGN_BACKEND": "jsonl"}
        result = subprocess.run(
            [sys.executable, "transfer.py"],
            cwd=tmp_path,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )

        shown = re.search(
            r"rootsign\.errors\.HiTLUnsupportedBackendError: [^\n]+", page_text(APPROVAL)
        )
        assert result.returncode != 0
        assert shown.group(0) in result.stderr
        assert "transferred" not in result.stdout

    async def test_the_inline_prompt(self, tmp_path, capsys):
        """`y <note>` approves, the tool runs, and the approval names the OS user."""
        from rootsign.sdk.context import SessionContext
        from rootsign.sdk.decorator import trace
        from rootsign.sdk.jsonl_client import JsonlIngestClient
        from uuid import uuid4

        client = JsonlIngestClient(data_dir=tmp_path)
        ctx = SessionContext(agent_id=uuid4(), session_id=uuid4())

        @trace(ingest_client=client, session_context=ctx, require_approval=True)
        async def wire_transfer(account: str, amount: float) -> str:
            return f"transferred ${amount:,.2f} to {account}"

        answer = re.search(r"is a note\): (.+)", page_text(APPROVAL)).group(1)
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value=answer),
        ):
            out = await wire_transfer(account="GB29NWBK60161331926819", amount=250000.0)

        assert out == "transferred $250,000.00 to GB29NWBK60161331926819"
        assert out in page_text(APPROVAL)
        lines = [
            json.loads(x)
            for x in client._session_path(str(ctx.session_id)).read_text().splitlines()
        ]
        [approval] = [r for r in lines if r["event_type"] == "APPROVAL_RECORD"]
        [action] = [r for r in lines if r["event_type"] == "ACTION_RECORD"]
        assert approval["payload"]["approver_id"] == f"tty:{getpass.getuser()}"
        assert approval["payload"]["decision_reason"] == "Verified with the supplier"
        # The page's note: on JSONL the action stays pending after the decision.
        assert action["authorization_status"] == "pending"
