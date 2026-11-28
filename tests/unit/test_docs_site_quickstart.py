"""The docs-site quickstart runs as pasted and prints what the page says it prints.

`docs/site/quickstart.mdx` is the first page a new developer reads, and its
promise is "every code sample runs as pasted, and every command shows its
expected output". Prose cannot keep that promise across releases; this test
does. It lifts the `agent.py` block out of the page, runs it in a clean
directory, then walks the same three verdicts the page walks the reader
through:

* **VALID** on the untouched session,
* **TAMPERED** after the amount on the first tool call is edited,
* **INCOMPLETE** after that line is deleted.

For each one it checks the exit code and that the output lines the page shows
are what the CLI prints today, with only the session ID substituted. If a
release changes the verifier's wording, this fails and the page gets updated
in the same PR.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rootsign.sdk.cli import app

PAGE = Path(__file__).resolve().parents[2] / "docs" / "site" / "quickstart.mdx"
# The session ID the page's sample output was captured with.
PAGE_SESSION_ID = "88f15492-3be7-4f7e-af16-3f247b1134a9"

runner = CliRunner()


def _page() -> str:
    return PAGE.read_text(encoding="utf-8")


def _agent_source() -> str:
    """The ```python agent.py block, dedented out of its <Step>."""
    match = re.search(r"```python agent\.py\n(.*?)\n\s*```", _page(), re.DOTALL)
    assert match, "quickstart.mdx no longer has a ```python agent.py block"
    return textwrap.dedent(match.group(1))


def _page_output_after(command: str) -> list[str]:
    """The ```text block that follows the first ```bash block containing `command`."""
    page = _page()
    start = page.index(command)
    match = re.search(r"```text\n(.*?)\n\s*```", page[start:], re.DOTALL)
    assert match, f"no expected-output block after {command!r}"
    return [line.strip() for line in textwrap.dedent(match.group(1)).splitlines() if line.strip()]


@pytest.fixture
def session_file(tmp_path: Path) -> Path:
    """Run the page's agent.py in a clean directory; return the session file."""
    (tmp_path / "agent.py").write_text(_agent_source(), encoding="utf-8")
    env = {
        **os.environ,
        "ROOTSIGN_DATA_DIR": str(tmp_path / "data"),
        # A developer's .env or shell may point at Postgres; the page does not.
        "ROOTSIGN_BACKEND": "jsonl",
    }
    # cwd=tmp_path keeps the repo's .env out of pydantic-settings' reach.
    result = subprocess.run(
        [sys.executable, "agent.py"], cwd=tmp_path, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    session_id = result.stdout.strip()
    path = tmp_path / "data" / "sessions" / f"{session_id}.jsonl"
    assert path.exists(), f"agent.py printed {session_id!r} but wrote no session file"
    return path


def _verify(path: Path):
    return runner.invoke(app, ["verify", "--local", str(path)])


def _assert_output_matches_page(output: str, expected: list[str], session_id: str) -> None:
    printed = {line.strip() for line in output.splitlines()}
    for line in expected:
        line = line.replace(PAGE_SESSION_ID, session_id)
        assert line in printed, f"page shows {line!r}; CLI printed:\n{output}"


class TestQuickstartPage:
    def test_the_untouched_session_is_valid(self, session_file):
        result = _verify(session_file)

        assert result.exit_code == 0, result.output
        _assert_output_matches_page(
            result.output, _page_output_after("rootsign verify --local ~/"), session_file.stem
        )

    def test_editing_the_amount_is_tampered(self, session_file, tmp_path):
        lines = session_file.read_text(encoding="utf-8").splitlines(keepends=True)
        assert "1500.0" in lines[1], "line 2 no longer carries the amount the page edits"
        lines[1] = lines[1].replace("1500.0", "15000.0")
        tampered = tmp_path / "tampered.jsonl"
        tampered.write_text("".join(lines), encoding="utf-8")

        result = _verify(tampered)

        assert result.exit_code == 1, result.output
        _assert_output_matches_page(
            result.output,
            _page_output_after("rootsign verify --local tampered.jsonl"),
            session_file.stem,
        )

    def test_deleting_the_first_tool_call_is_incomplete(self, session_file, tmp_path):
        lines = session_file.read_text(encoding="utf-8").splitlines(keepends=True)
        del lines[1]
        gap = tmp_path / "gap.jsonl"
        gap.write_text("".join(lines), encoding="utf-8")

        result = _verify(gap)

        assert result.exit_code == 2, result.output
        _assert_output_matches_page(
            result.output,
            _page_output_after("rootsign verify --local gap.jsonl"),
            session_file.stem,
        )


def test_deleting_the_last_record_is_incomplete_too(session_file, tmp_path):
    """The note under the steps: the seal catches what the chain alone cannot."""
    lines = session_file.read_text().splitlines(keepends=True)
    assert '"SESSION_CLOSE"' in lines[-1] and '"ACTION_RECORD"' in lines[-2]
    del lines[-2]  # the last tool call, just before the close
    cut = tmp_path / "cut.jsonl"
    cut.write_text("".join(lines))

    result = _verify(cut)
    assert result.exit_code == 2 and "missing at the end" in result.output
    assert "Delete the **last** line before the session close instead" in _page()
