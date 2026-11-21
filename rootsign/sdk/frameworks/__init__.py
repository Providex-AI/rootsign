"""Framework-specific tracers (LangGraph in Sprint 2; CrewAI / AutoGen in Sprint 3).

Each submodule keeps its third-party imports lazy so the SDK can be installed
without any framework extras. See ADR-004 for the LangGraph interception
strategy.
"""

from __future__ import annotations

import inspect
from typing import Any


def _not_a_tool(entry_point: str, framework: str, needs: str, obj: Any) -> TypeError:
    """The error for an item `wrap_tools()` / `wrap_crewai_tools()` cannot wrap.

    Most often it is a plain function, which belongs under `@rootsign.trace()`.
    Without this the tracers failed reaching for an attribute the item lacks
    (`'function' object has no attribute 'invoke'`), naming neither the item
    nor the fix.
    """
    if inspect.isroutine(obj):
        got = f"function {getattr(obj, '__name__', '?')!r}"
    else:
        got = f"{type(obj).__name__} object"
    return TypeError(
        f"{entry_point} takes {framework} tools (objects with {needs}); got {got}. "
        "For a plain function, decorate it with @rootsign.trace() instead."
    )
