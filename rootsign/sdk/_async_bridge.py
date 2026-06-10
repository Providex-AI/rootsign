"""Shared sync→async bridge for framework tracers — see ADR-004 § "Sync-path
correctness" and ADR-005.

Both `LangGraphTracer.traced_invoke` and `CrewAITracer.traced_run` need to
execute an async ingest coroutine from a synchronous call site. The naive
`asyncio.get_event_loop().run_until_complete(coro)` pattern raises
`RuntimeError: This event loop is already running` whenever the sync entry
point is reached from inside an async context (e.g. a LangGraph node, or an
integration test that uses `await client.handle(...)` then `tool._run(...)`).

`_run_sync` detects loop state and routes accordingly:

  * No running loop on this thread → `asyncio.run`.
  * Running loop on this thread    → run in a fresh worker thread that owns
    its own loop, then join.

The worker-thread path is best-effort. Callers in an async context who need
shared state bound to the caller's loop (e.g. an `AsyncSession`) should
prefer the async path of the tracer (`await tool.ainvoke(...)` for
LangGraph; CrewAI does not expose an async surface).
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any


def _run_sync(coro: Any) -> Any:
    """Execute *coro* and return its result, regardless of caller loop state."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    container: dict[str, Any] = {}

    def _worker() -> None:
        try:
            container["result"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 — re-raised on join
            container["error"] = exc

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join()
    if "error" in container:
        raise container["error"]
    return container["result"]
