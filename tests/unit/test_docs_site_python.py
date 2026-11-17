"""`docs/site/integrations/python.mdx` runs as pasted and records what it says.

The page's `tools.py` is run in a clean directory, then checked three ways
against the page: what it prints, the "What gets recorded" table (tool names,
recorded inputs, the empty output of the call that raised), and the VALID
verdict. Needs no framework extra, so it runs in the unit job.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from rootsign.sdk.cli import app
from tests.support.docs_pages import (
    assert_lines_match,
    code_block,
    output_after,
    page_text,
    run_script,
)

PAGE = "integrations/python.mdx"
PAGE_SESSION_ID = "92cfe80f-68ce-48b4-a22e-296da8179dca"


def test_tools_py_prints_what_the_page_shows(tmp_path):
    run = run_script(code_block(PAGE, "tools.py"), tmp_path, name="tools.py")

    assert_lines_match(
        run.stdout,
        output_after(PAGE, "python tools.py"),
        {PAGE_SESSION_ID: run.only_session().stem},
    )


def test_the_recorded_actions_match_the_table(tmp_path):
    run = run_script(code_block(PAGE, "tools.py"), tmp_path, name="tools.py")
    actions = run.actions()
    page = page_text(PAGE)

    assert [a["tool_name"] for a in actions] == ["lookup_customer", "refund"]
    for action in actions:
        for column in ("input_redacted", "output_redacted"):
            shown = json.dumps(action[column])
            assert f"`{shown}`" in page, f"page's table does not show {column} = {shown}"
    assert actions[1]["output_redacted"] is None, "the call that raised must record no output"


def test_the_session_verifies(tmp_path):
    run = run_script(code_block(PAGE, "tools.py"), tmp_path, name="tools.py")

    result = CliRunner().invoke(app, ["verify", "--local", str(run.only_session())])

    assert result.exit_code == 0, result.output
    assert_lines_match(result.output, output_after(PAGE, "## Verify the session"), {})
