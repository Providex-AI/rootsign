"""How it works quotes a benchmark; the benchmark it names exists and holds that budget.

The figures on the page are measurements and cannot be pinned, but the command
a reader copies and the 5 ms budget the page promises can: a renamed test file,
a dropped `benchmark` marker or a changed threshold fails here.
"""

from __future__ import annotations

import re
import tomllib

from tests.support.docs_pages import REPO, page_text

PAGE = "how-it-works.mdx"


def _section() -> str:
    text = page_text(PAGE)
    start = text.index("## Overhead per call")
    return text[start : text.index("\n## ", start + 1)]


def test_the_benchmark_command_names_a_real_test_and_marker() -> None:
    command = re.search(r"```bash\n(.*?)\n```", _section(), re.DOTALL).group(1)
    path = re.search(r"(tests/\S+\.py)", command).group(1)
    assert (REPO / path).exists(), f"the page runs {path}, which does not exist"
    assert "-m benchmark" in command
    markers = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"][
        "markers"
    ]
    assert any(m.startswith("benchmark") for m in markers)
    assert "@pytest.mark.benchmark" in (REPO / path).read_text()


def test_the_budget_on_the_page_is_the_one_the_test_asserts() -> None:
    page_budget = re.search(r"p99 goes above (\d+) ms", _section()).group(1)
    source = (REPO / "tests/performance/test_langgraph_benchmarks.py").read_text()
    assert f"assert p99 < {page_budget}.0" in source
