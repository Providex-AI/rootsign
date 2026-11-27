"""The Resources tab stays true to the files it summarises.

`resources/framework-support.mdx`: the tested lines are the CI matrix, the
Python versions are CI's, the extras exist, and the repo's
`docs/framework-support.md` table agrees. `resources/changelog.mdx`: one entry
per release, newest first, the newest is the version in `pyproject.toml`, and
each links to its tag. `resources/design-decisions.mdx`: every ADR in
`docs/adr`, linked by its real file name. `resources/security.mdx`: the
reporting terms, scope and merge rules match `SECURITY.md`, `CONTRIBUTING.md`
and `pyproject.toml`.
"""

from __future__ import annotations

import json
import re
import tomllib

import pytest

import rootsign
from tests.support.docs_pages import REPO, page_text

SUPPORT = "resources/framework-support.mdx"
CHANGELOG = "resources/changelog.mdx"
ADRS = "resources/design-decisions.mdx"
SECURITY = "resources/security.mdx"
ROADMAP = "resources/roadmap.mdx"

CI = (REPO / ".github" / "workflows" / "ci.yml").read_text()
PYPROJECT = tomllib.loads((REPO / "pyproject.toml").read_text())
VERSION = PYPROJECT["project"]["version"]


def _ci_lines(job_marker: str) -> list[str]:
    """The `line:` values of the matrix in the job whose name contains `job_marker`."""
    start = CI.index(job_marker)
    block = CI[start : start + 3000]
    include = block[block.index("include:") :]
    lines = []
    for raw in include.splitlines()[1:]:
        found = re.search(r"\{ line: '([^']+)'", raw)
        if found:
            lines.append(found.group(1))
        elif lines and raw.strip() and not raw.strip().startswith("#"):
            break
    return lines


def _row(page: str, first_cell: str) -> list[str]:
    for line in page_text(page).splitlines():
        if line.startswith(f"| {first_cell} |"):
            return [c.strip() for c in line.strip().strip("|").split(" | ")]
    raise AssertionError(f"{page}: no row {first_cell!r}")


class TestFrameworkSupport:
    @pytest.mark.parametrize(
        ("row", "job", "name"),
        [
            ("LangGraph and LangChain tools", "LangGraph contract (langgraph", "LangGraph"),
            ("CrewAI", "CrewAI contract (crewai", "CrewAI"),
        ],
    )
    def test_tested_lines_are_the_ci_matrix(self, row, job, name):
        lines = _ci_lines(job)
        assert len(lines) == 3, lines
        tested = _row(SUPPORT, row)[2]
        assert tested == f"{name} " + ", ".join(lines[:-1]) + f", {lines[-1]}"

    def test_the_repo_support_table_agrees(self):
        repo_doc = (REPO / "docs" / "framework-support.md").read_text()
        for job, name in (
            ("LangGraph contract (langgraph", "LangGraph"),
            ("CrewAI contract (crewai", "CrewAI"),
        ):
            lines = ", ".join(_ci_lines(job))
            assert re.search(
                rf"^\| {name}\s*\| Supported \| {re.escape(lines)} \(CI matrix\)", repo_doc, re.M
            ), name

    def test_mcp_floor_is_tested_and_stated(self):
        """The extra's floor, the CI row that tests it and the page say the same release."""
        (spec,) = [
            d for d in PYPROJECT["project"]["optional-dependencies"]["mcp"] if d.startswith("mcp")
        ]
        floor = re.fullmatch(r"mcp>=(\d+)\.(\d+),<2\.0", spec)
        assert floor, spec
        line = f"{floor.group(1)}.{floor.group(2)}"
        assert f"line: '{line}.x', spec: '~={line}.0'" in CI
        assert "line: 'newest 1.x', spec: ''" in CI
        assert _row(SUPPORT, "MCP")[2] == f"`mcp` {line}.x and the newest 1.x release on each run"
        assert "pip install -e '.[dev,mcp]' \"mcp${{ matrix.spec }}\"" in CI

    def test_the_required_mcp_check_keeps_its_name(self):
        """Branch protection requires "MCP proxy contract tests"; the newest row must keep it."""
        assert "name: MCP proxy contract tests${{ matrix.suffix }}" in CI
        assert "line: 'newest 1.x', spec: '', prefix: '1.', suffix: ''" in CI

    def test_install_extras_exist(self):
        extras = PYPROJECT["project"]["optional-dependencies"]
        for extra in re.findall(r"pip install 'rootsign\[(\w+)\]'", page_text(SUPPORT)):
            assert extra in extras, extra
        assert "autogen" not in extras

    def test_python_versions_are_cis(self):
        (matrix,) = re.findall(r"python-version: \[([^\]]+)\]", CI)
        versions = [v.strip(" '") for v in matrix.split(",")]
        assert versions == ["3.11", "3.12"]
        assert PYPROJECT["project"]["requires-python"].startswith(">=3.11")
        assert "CI tests 3.11 and 3.12, so those are the supported versions" in page_text(SUPPORT)

    def test_the_stable_calls_exist(self):
        for name in ("init", "session", "wrap_tools", "trace", "verify_session"):
            assert callable(getattr(rootsign, name)), name
        assert "| `rootsign verify` and `verify_session()` |" in page_text(SUPPORT)

    def test_the_issue_template_exists(self):
        assert "template=framework_integration.yml" in page_text(SUPPORT)
        assert (REPO / ".github" / "ISSUE_TEMPLATE" / "framework_integration.yml").exists()


class TestChangelog:
    def _labels(self) -> list[str]:
        return re.findall(r'<Update label="([^"]+)"', page_text(CHANGELOG))

    def test_one_entry_per_release_newest_first(self):
        labels = self._labels()
        assert labels[0] == "Unreleased"
        released = labels[1:]
        files = {p.stem for p in (REPO / "docs" / "releases").glob("v*.md")}
        # v0.2.1 has a GitHub release and no file in docs/releases.
        assert set(released) == files | {"v0.2.1"}

        def key(label: str) -> tuple[int, ...]:
            return tuple(int(part) for part in label[1:].split("."))

        assert released == sorted(released, key=key, reverse=True)
        assert released[0] == f"v{VERSION}"

    def test_each_release_links_to_its_tag(self):
        text = page_text(CHANGELOG)
        for label in self._labels()[1:]:
            entry = text[text.index(f'<Update label="{label}"') :]
            entry = entry[: entry.index("</Update>")]
            assert f"releases/tag/{label})" in entry, label

    def test_unreleased_links_are_pull_requests(self):
        text = page_text(CHANGELOG)
        entry = text[: text.index("</Update>")]
        links = re.findall(
            r"\[#(\d+)\]\(https://github\.com/Providex-AI/rootsign/pull/(\d+)\)", entry
        )
        assert links and all(a == b for a, b in links)


class TestDesignDecisions:
    def test_every_adr_is_listed_by_its_file(self):
        files = sorted(p.name for p in (REPO / "docs" / "adr").glob("ADR-*.md"))
        rows = re.findall(
            r"^\| \[(ADR-\d{3})\]\(https://github\.com/Providex-AI/rootsign/blob/main/docs/adr/([^)]+)\) \|",
            page_text(ADRS),
            re.M,
        )
        assert [name for _, name in rows] == files
        assert all(name.startswith(number) for number, name in rows)
        assert len(files) == 15 and "fifteen" in page_text(ADRS)

    def test_all_are_accepted(self):
        for path in (REPO / "docs" / "adr").glob("ADR-*.md"):
            status = re.search(r"\*\*Status\*\*:?\s*(\w+)", path.read_text())
            assert status is None or status.group(1) == "Accepted", path.name


class TestSecurityAndContributing:
    SECURITY_MD = (REPO / "SECURITY.md").read_text()
    CONTRIBUTING_MD = (REPO / "CONTRIBUTING.md").read_text()

    def _bullets(self, text: str, start: str, end: str) -> list[str]:
        section = text[text.index(start) : text.index(end, text.index(start))]
        return [line for line in section.splitlines() if line.startswith("- ")]

    def test_reporting_terms_match_security_md(self):
        page = page_text(SECURITY)
        assert "https://github.com/Providex-AI/rootsign/security/advisories/new" in self.SECURITY_MD
        assert "https://github.com/Providex-AI/rootsign/security/advisories/new" in page
        assert "**info@getprovidex.com**" in self.SECURITY_MD and "`info@getprovidex.com`" in page
        assert "within 48 hours" in self.SECURITY_MD and "within 48 hours" in page
        assert "within 14 days" in self.SECURITY_MD and "within 14 days" in page

    def test_supported_version_is_the_current_minor(self):
        minor = ".".join(VERSION.split(".")[:2])
        assert f"| {minor}.x   | Yes       |" in self.SECURITY_MD
        assert f"the latest release, {minor}.x" in page_text(SECURITY)

    def test_scope_lists_match(self):
        page = page_text(SECURITY)
        for heading in ("**In scope:**", "**Out of scope:**"):
            nxt = "**Out of scope:**" if heading == "**In scope:**" else "##"
            ours = self._bullets(page, heading, nxt)
            theirs = self._bullets(self.SECURITY_MD, heading, nxt)
            assert len(ours) == len(theirs), heading

    def test_merge_rules_match_contributing(self):
        page = page_text(SECURITY)
        ours = self._bullets(page, "### What is not merged", "##")
        theirs = self._bullets(self.CONTRIBUTING_MD, "## What we will NOT merge", "##")
        assert len(ours) == len(theirs)
        assert PYPROJECT["tool"]["coverage"]["report"]["fail_under"] == 85
        assert "at least 90% test coverage, and overall coverage stays at 85%" in page
        assert "New code must have test coverage >= 90%" in self.CONTRIBUTING_MD
        for prefix in ("feat/", "fix/", "test/", "docs/"):
            assert prefix in self.CONTRIBUTING_MD and f"`{prefix}`" in page

    def test_contributing_md_has_no_internal_or_stale_lines(self):
        for stale in ("Sprint", "AGENTS.md", "`pytest tests/", "coming alongside"):
            assert stale not in self.CONTRIBUTING_MD, stale

    def test_the_discord_link_is_the_sites(self):
        docs = json.loads((REPO / "docs" / "site" / "docs.json").read_text())
        (discord,) = [
            link["href"] for link in docs["navbar"]["links"] if link.get("type") == "discord"
        ]
        assert f"[Discord]({discord})" in page_text(SECURITY)
        assert f"[Discord]({discord})" in self.CONTRIBUTING_MD


def test_the_roadmap_names_the_current_release():
    assert f"RootSign v{VERSION} records tool calls" in page_text(ROADMAP)
