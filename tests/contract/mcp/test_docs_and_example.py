"""The MCP docs page and `examples/mcp-proxy` stay true, in the MCP contract job.

`docs/site/integrations/mcp.mdx` shows `examples/mcp-proxy/demo.py` and
`proxy.py` verbatim (minus their module docstrings). This suite fails if they
drift, then runs `demo.py` in a clean directory: a real MCP client, the
RootSign proxy and a FastMCP server talking HTTP on 127.0.0.1:8000/8001. It
checks the output the page shows, that the first record's input and output are
exactly the JSON the page prints, and that the session verifies VALID. The
page's `audit_server.py` is imported to prove it builds an ASGI app with no
database configured.
"""

from __future__ import annotations

import json
import re

import pytest

pytest.importorskip("mcp", reason="rootsign[mcp] not installed")
pytest.importorskip("uvicorn", reason="rootsign[mcp] not installed")

from typer.testing import CliRunner  # noqa: E402

from rootsign.sdk.cli import app as cli  # noqa: E402
from tests.support.docs_pages import (  # noqa: E402
    REPO,
    assert_lines_match,
    code_block,
    output_after,
    page_text,
    run_script,
    without_module_docstring,
)

PAGE = "integrations/mcp.mdx"
EXAMPLES = REPO / "examples" / "mcp-proxy"


def _json_after(marker: str) -> dict:
    """The ```json block that follows `marker` on the page, parsed."""
    text = page_text(PAGE)
    start = text.index(marker)
    match = re.search(r"```json\n(.*?)\n```", text[start:], re.DOTALL)
    assert match, f"no json block after {marker!r}"
    return json.loads(match.group(1))


@pytest.mark.parametrize("name", ["demo.py", "proxy.py"])
def test_the_page_shows_the_example_verbatim(name):
    on_page = code_block(PAGE, name).strip() + "\n"
    in_repo = without_module_docstring((EXAMPLES / name).read_text(encoding="utf-8"))

    assert on_page == in_repo, f"mcp.mdx and examples/mcp-proxy/{name} drifted"


def test_demo_runs_records_and_verifies(tmp_path):
    run = run_script((EXAMPLES / "demo.py").read_text(encoding="utf-8"), tmp_path, name="demo.py")
    session = run.only_session()

    assert_lines_match(
        run.stdout,
        output_after(PAGE, "python demo.py"),
        {"~/.rootsign": str(run.data_dir), "<session_id>": session.stem},
    )

    actions = run.actions()
    assert [a["tool_name"] for a in actions] == ["send_invoice", "log_payment"]
    first = actions[0]
    assert first["input_redacted"] == _json_after("exactly as the client sent them")
    assert first["output_redacted"] == _json_after("so the record shows exactly what")

    result = CliRunner().invoke(cli, ["verify", "--local", str(session)])
    assert result.exit_code == 0, result.output
    assert_lines_match(result.output, output_after(PAGE, "### Verify the session"), {})


def test_audit_server_builds_without_a_database():
    namespace: dict = {}
    exec(code_block(PAGE, "audit_server.py"), namespace)  # noqa: S102 — the page's own code

    assert callable(namespace["app"]), "audit_server.py must expose an ASGI app named `app`"
