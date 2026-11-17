"""The LangGraph docs page and `examples/langgraph-jsonl` run on every matrix line.

`docs/site/integrations/langgraph.mdx` shows `examples/langgraph-jsonl/agent.py`
verbatim (minus its module docstring). This suite:

* fails if the page and the example drift apart,
* runs the example in a clean directory, checks it prints what the page shows,
  that each `ToolNode` call recorded the input shape the page documents, and
  that the session verifies VALID,
* runs the page's plain-LangChain `lookup.py` the same way.

It lives in the contract suite so the LangGraph matrix (0.2.x, 0.6.x, 1.x)
exercises the example on each line, not just the one in uv.lock.
"""

from __future__ import annotations

import pytest

pytest.importorskip("langgraph", reason="LangGraph not installed")

from typer.testing import CliRunner  # noqa: E402

from rootsign.sdk.cli import app  # noqa: E402
from tests.support.docs_pages import (  # noqa: E402
    REPO,
    assert_lines_match,
    code_block,
    output_after,
    run_script,
    without_module_docstring,
)

PAGE = "integrations/langgraph.mdx"
EXAMPLE = REPO / "examples" / "langgraph-jsonl" / "agent.py"
LOOKUP_SESSION_ID = "540c4a98-002f-458d-ad84-bd61044840bb"


def _verify(path):
    return CliRunner().invoke(app, ["verify", "--local", str(path)])


def test_the_page_shows_the_example_verbatim():
    on_page = code_block(PAGE, "agent.py").strip() + "\n"
    in_repo = without_module_docstring(EXAMPLE.read_text(encoding="utf-8"))

    assert on_page == in_repo, "langgraph.mdx and examples/langgraph-jsonl/agent.py drifted"


def test_the_example_runs_records_and_verifies(tmp_path):
    run = run_script(EXAMPLE.read_text(encoding="utf-8"), tmp_path, name="agent.py")
    session = run.only_session()

    # The page's output block shows `~/.rootsign/...`; the run used a temp dir.
    assert_lines_match(
        run.stdout,
        output_after(PAGE, "python agent.py"),
        {"~/.rootsign": str(run.data_dir), "<session_id>": session.stem},
    )

    actions = run.actions()
    assert [a["tool_name"] for a in actions] == ["send_invoice", "log_payment"]
    first = actions[0]["input_redacted"]
    assert first["kwargs"] == {}
    assert first["args"][0]["args"] == {"customer_id": "acme-corp", "amount": 1500.0}
    assert first["args"][0]["id"] == "call_1", "ToolNode should pass the full tool call"

    result = _verify(session)
    assert result.exit_code == 0, result.output
    assert_lines_match(result.output, output_after(PAGE, "## Verify the session"), {})


def test_lookup_py_records_a_plain_langchain_call(tmp_path):
    run = run_script(code_block(PAGE, "lookup.py"), tmp_path, name="lookup.py")
    session = run.only_session()

    assert_lines_match(
        run.stdout, output_after(PAGE, "```python lookup.py"), {LOOKUP_SESSION_ID: session.stem}
    )
    [action] = run.actions()
    assert action["tool_name"] == "lookup_order"
    assert action["input_redacted"] == {"args": [{"order_id": "A-1001"}], "kwargs": {}}
    assert _verify(session).exit_code == 0
