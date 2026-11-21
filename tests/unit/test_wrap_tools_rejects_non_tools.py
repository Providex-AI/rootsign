"""`wrap_tools()` and `wrap_crewai_tools()` say what they take.

Given something that is not a tool of their framework (most often a plain
function, which belongs under `@rootsign.trace()`), `wrap_tools` failed with
`AttributeError: 'function' object has no attribute 'invoke'`, and
`wrap_crewai_tools` failed or wrapped nothing useful, neither naming the item
or the fix. Both now raise TypeError naming the item and pointing at
`@rootsign.trace()`.

The checks are duck-typed, on the attributes each tracer uses, so they need
neither framework installed.
"""

from __future__ import annotations

import pytest

import rootsign


def plain_lookup(q: str) -> str:
    return q


class NotATool:
    name = "not-a-tool"


@pytest.mark.parametrize(
    ("wrap", "framework"),
    [
        (rootsign.wrap_tools, "LangChain"),
        (rootsign.wrap_crewai_tools, "CrewAI"),
    ],
)
class TestRejectsNonTools:
    def test_a_plain_function_points_at_trace(self, wrap, framework):
        with pytest.raises(TypeError) as raised:
            wrap([plain_lookup])

        message = str(raised.value)
        assert f"takes {framework} tools" in message
        assert "got function 'plain_lookup'" in message
        assert "@rootsign.trace()" in message

    def test_another_object_is_named_by_its_type(self, wrap, framework):
        with pytest.raises(TypeError, match="got NotATool object"):
            wrap([NotATool()])

    def test_nothing_is_wrapped_when_one_item_is_wrong(self, wrap, framework):
        with pytest.raises(TypeError):
            wrap([plain_lookup, "a string"])
        assert not getattr(plain_lookup, "_rootsign_instrumented", False)
