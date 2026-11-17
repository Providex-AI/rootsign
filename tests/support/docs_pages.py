"""Helpers for tests that hold the docs site to "every sample runs as pasted".

The pages under `docs/site/` show code blocks and the output they print. These
helpers lift a titled code block out of a page, run it the way a reader would
(a clean directory, the default JSONL backend), and hand back what it printed
and the session file it wrote, so a test can compare both against the page.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
SITE = REPO / "docs" / "site"


def page_text(page: str) -> str:
    return (SITE / page).read_text(encoding="utf-8")


def code_block(page: str, title: str, *, language: str = "python") -> str:
    """The fenced block opened with ```<language> <title>, dedented out of any component."""
    pattern = rf"```{re.escape(language)} {re.escape(title)}\n(.*?)\n\s*```"
    match = re.search(pattern, page_text(page), re.DOTALL)
    assert match, f"{page} has no ```{language} {title} block"
    return textwrap.dedent(match.group(1))


def output_after(page: str, marker: str) -> list[str]:
    """Non-blank lines of the first ```text block after `marker` on the page."""
    text = page_text(page)
    start = text.index(marker)
    match = re.search(r"```text\n(.*?)\n\s*```", text[start:], re.DOTALL)
    assert match, f"{page}: no expected-output block after {marker!r}"
    return [line.strip() for line in textwrap.dedent(match.group(1)).splitlines() if line.strip()]


def without_module_docstring(source: str) -> str:
    """`source` minus a leading module docstring, for comparing a page to a repo file."""
    tree = ast.parse(source)
    first = tree.body[0] if tree.body else None
    if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant):
        lines = source.splitlines(keepends=True)
        source = "".join(lines[first.end_lineno :])
    return source.strip() + "\n"


@dataclass
class Run:
    stdout: str
    data_dir: Path

    def session_files(self) -> list[Path]:
        return sorted((self.data_dir / "sessions").glob("*.jsonl"))

    def only_session(self) -> Path:
        files = self.session_files()
        assert len(files) == 1, f"expected one session file, found {files}"
        return files[0]

    def actions(self) -> list[dict[str, Any]]:
        lines = self.only_session().read_text(encoding="utf-8").splitlines()
        records = [json.loads(line) for line in lines if line.strip()]
        return [r for r in records if r["event_type"] == "ACTION_RECORD"]


def run_script(
    source: str,
    workdir: Path,
    *,
    name: str = "script.py",
    extra_env: dict[str, str] | None = None,
) -> Run:
    """Run `source` in `workdir` on the JSONL backend; fail with stderr on error."""
    (workdir / name).write_text(source, encoding="utf-8")
    data_dir = workdir / "data"
    env = {
        **os.environ,
        "ROOTSIGN_DATA_DIR": str(data_dir),
        # A developer's .env or shell may point at Postgres; the pages do not.
        "ROOTSIGN_BACKEND": "jsonl",
        **(extra_env or {}),
    }
    # cwd=workdir keeps the repo's .env out of pydantic-settings' reach.
    result = subprocess.run(
        [sys.executable, name], cwd=workdir, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    return Run(stdout=result.stdout, data_dir=data_dir)


def assert_lines_match(printed: str, expected: list[str], replacements: dict[str, str]) -> None:
    """Every expected line appears in `printed`, after swapping page-captured IDs and paths."""
    seen = {line.strip() for line in printed.splitlines()}
    for line in expected:
        for old, new in replacements.items():
            line = line.replace(old, new)
        assert line in seen, f"page shows {line!r}; the run printed:\n{printed}"
