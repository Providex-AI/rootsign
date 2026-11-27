"""The README stays a short front door to the docs site, and cannot drift from it.

The README is also the PyPI project page, and it used to carry a second copy of
most of the docs. D6 trimmed it to the pitch, the quickstart and a map of the
docs, so this test holds the three pieces that remain to the site:

* the quickstart's `agent.py` and its `VALID` output are the docs quickstart's,
  character for character, so a change to one fails until the other follows;
* the documentation map links every page in `docs/site/docs.json` and nothing
  that is not a page, so a new page cannot ship without an entry;
* every link is absolute, because PyPI renders the README without the repo
  around it and a relative link there leads nowhere.
"""

from __future__ import annotations

import json
import re

from tests.support.docs_pages import REPO, SITE, code_block, output_after

README = REPO / "README.md"
DOCS = "https://docs.getprovidex.com"


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def _nav_pages() -> set[str]:
    pages: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, str):
            pages.add(node)
        elif isinstance(node, dict):
            for key in ("tabs", "groups", "pages"):
                for child in node.get(key, []):
                    walk(child)

    walk(json.loads((SITE / "docs.json").read_text(encoding="utf-8"))["navigation"])
    return pages


def test_quickstart_code_is_the_docs_quickstart() -> None:
    blocks = re.findall(r"```python\n(.*?)\n```", _readme(), re.DOTALL)
    assert blocks == [code_block("quickstart.mdx", "agent.py")]


def test_verify_output_is_the_docs_quickstart_output() -> None:
    readme = re.search(r"```text\n(.*?)\n```", _readme(), re.DOTALL)
    assert readme, "the README no longer shows the verify output"
    shown = [line.strip() for line in readme.group(1).splitlines() if line.strip()]
    assert shown == output_after(
        "quickstart.mdx", "Replace the ID with the one your script printed"
    )


def test_documentation_map_links_every_page_and_only_pages() -> None:
    linked = set()
    for path in re.findall(rf"\]\({re.escape(DOCS)}(/[^)#]*)?(?:#[^)]*)?\)", _readme()):
        page = path.strip("/") or "index"
        assert (SITE / f"{page}.mdx").exists(), f"README links {DOCS}/{page}, which is not a page"
        linked.add(page)
    assert _nav_pages() <= linked, f"README does not link {sorted(_nav_pages() - linked)}"


def test_every_link_is_absolute() -> None:
    """PyPI shows the README with no repository around it."""
    targets = re.findall(r"\]\(([^)]+)\)", _readme()) + re.findall(r'src="([^"]+)"', _readme())
    relative = [t for t in targets if not t.startswith(("https://", "http://"))]
    assert not relative, f"relative links break on PyPI: {relative}"
