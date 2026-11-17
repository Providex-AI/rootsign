"""The CrewAI docs page and `examples/crewai-jsonl` run on the CrewAI matrix.

`docs/site/integrations/crewai.mdx` shows `examples/crewai-jsonl/crew.py`
verbatim (minus its module docstring). This suite fails if the two drift, then
runs the example in a clean directory: a real `Crew.kickoff_async()` with a
scripted `BaseLLM`. It checks the output the page shows, that both tool calls
were recorded with keyword arguments as the page documents, and that the
session verifies VALID.

The example subclasses `crewai.BaseLLM`, which CrewAI exports from 0.120, so
the run is skipped on older matrix lines (0.95.x). The interception itself is
covered there by test_tool_interception.py.
"""

from __future__ import annotations

import pytest

crewai = pytest.importorskip("crewai", reason="CrewAI not installed")

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

PAGE = "integrations/crewai.mdx"
EXAMPLE = REPO / "examples" / "crewai-jsonl" / "crew.py"


def test_the_page_shows_the_example_verbatim():
    on_page = code_block(PAGE, "crew.py").strip() + "\n"
    in_repo = without_module_docstring(EXAMPLE.read_text(encoding="utf-8"))

    assert on_page == in_repo, "crewai.mdx and examples/crewai-jsonl/crew.py drifted"


@pytest.mark.skipif(not hasattr(crewai, "BaseLLM"), reason="crewai.BaseLLM needs CrewAI >= 0.120")
def test_the_example_runs_records_and_verifies(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    run = run_script(
        EXAMPLE.read_text(encoding="utf-8"),
        tmp_path,
        name="crew.py",
        # A throwaway HOME keeps CrewAI's first-run state off the machine;
        # CREWAI_TESTING skips its interactive "view your traces?" prompt.
        extra_env={"HOME": str(home), "CREWAI_TESTING": "true"},
    )
    session = run.only_session()

    assert_lines_match(
        run.stdout,
        output_after(PAGE, "python crew.py"),
        {"~/.rootsign": str(run.data_dir), "<session_id>": session.stem},
    )

    actions = run.actions()
    assert [a["tool_name"] for a in actions] == ["send_invoice", "log_payment"]
    for action in actions:
        assert action["input_redacted"] == {
            "args": [],
            "kwargs": {"customer_id": "acme-corp", "amount": 1500.0},
        }
    assert actions[0]["output_redacted"] == {"result": "invoice sent to acme-corp for $1,500.00"}

    result = CliRunner().invoke(app, ["verify", "--local", str(session)])
    assert result.exit_code == 0, result.output
    assert_lines_match(result.output, output_after(PAGE, "## Verify the session"), {})
