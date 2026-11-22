"""The "Batch writes" guide stays true.

`guides/batching.mdx`: the settings and constructor tables match the code;
the records the page says are never held reach the session file straight away
while an ordinary call waits for a write; held records are written on the
interval, at the size cap and before the session closes, in call order; a
crash loses the held calls and the session still verifies VALID; a lost record
cannot raise into a call that already returned; the failed-write line is the
one shown; the cloud transport sends a write as one request; and the page's
`batched.py` runs and verifies as shown.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from typer.testing import CliRunner

import rootsign
from rootsign.sdk.buffered_client import BufferedIngestClient
from rootsign.sdk.client import HttpIngestClient, IngestClient
from rootsign.sdk.config import SDKSettings
from rootsign.sdk.context import SessionContext
from rootsign.sdk.decorator import trace
from rootsign.sdk.jsonl_client import JsonlIngestClient
from rootsign.sdk.cli import app
from tests.support.docs_pages import code_block, output_after, page_text, run_script

PAGE = "guides/batching.mdx"


def _table(marker: str) -> dict[str, list[str]]:
    """The first markdown table after `marker`: first cell (backticks off) -> other cells."""
    lines = page_text(PAGE)[page_text(PAGE).index(marker) :].splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("|"))
    table: dict[str, list[str]] = {}
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip().strip("|").split(" | ")]
        table[cells[0].strip("`")] = cells[1:]
    return table


def _types(client: JsonlIngestClient, session_id) -> list[str]:
    path = client._session_path(str(session_id))
    if not path.exists():
        return []
    return [json.loads(line)["event_type"] for line in path.read_text().splitlines()]


class TestTables:
    def test_the_settings_table_matches_the_settings(self):
        table = _table("Two settings tune it:")
        fields = SDKSettings.model_fields
        assert table == {
            "ROOTSIGN_BUFFER_INTERVAL": [
                f"`{fields['BUFFER_INTERVAL'].default}`",
                "Seconds between background writes",
            ],
            "ROOTSIGN_BUFFER_MAX_SIZE": [
                f"`{fields['BUFFER_MAX_SIZE'].default}`",
                "Records held before a write is forced",
            ],
        }
        assert fields["BUFFERED"].default is False

    def test_the_constructor_table_matches_the_signature(self):
        table = _table("## Batch with your own client")
        params = dict(inspect.signature(BufferedIngestClient.__init__).parameters)
        params.pop("self")
        assert list(table) == list(params)
        for name, param in params.items():
            shown = table[name][0]
            if param.default is inspect.Parameter.empty:
                assert shown == "required", name
            else:
                assert shown == f"`{param.default}`", name


class TestWhatIsNeverHeld:
    async def test_only_an_ordinary_call_waits(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROOTSIGN_CAPTURE_DECISIONS", "true")
        inner = JsonlIngestClient(data_dir=tmp_path)
        client = BufferedIngestClient(inner, flush_interval_seconds=999)
        agent_id = uuid4()

        async with rootsign.session(agent_id=agent_id, client=client) as ctx:
            assert _types(inner, ctx.session_id) == ["SESSION_OPEN"]  # written straight away

            @trace(ingest_client=client, session_context=ctx)
            async def lookup(q: str) -> str:
                return q

            await lookup("a")
            assert _types(inner, ctx.session_id) == ["SESSION_OPEN"]  # held

            await ctx.record_decision(selected_action="lookup", ingest_client=client)
            # The held call is written first, then the decision, in order.
            assert _types(inner, ctx.session_id) == [
                "SESSION_OPEN",
                "ACTION_RECORD",
                "DECISION_RECORD",
            ]
        await client.close()
        assert _types(inner, ctx.session_id)[-1] == "SESSION_CLOSE"

    async def test_a_held_call_is_written_straight_away(self, tmp_path):
        from unittest.mock import patch

        inner = JsonlIngestClient(data_dir=tmp_path)
        client = BufferedIngestClient(inner, flush_interval_seconds=999)
        ctx = SessionContext(agent_id=uuid4())
        seen: list[list[str]] = []

        @trace(ingest_client=client, session_context=ctx, require_approval=True)
        async def wire_transfer(amount: float) -> str:
            return "sent"

        def answer(prompt: str = "") -> str:
            seen.append(_types(inner, ctx.session_id))  # what is on disk while asked
            return "y"

        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", answer):
            await wire_transfer(amount=1.0)
        await client.close()

        assert seen == [["ACTION_RECORD"]]
        assert _types(inner, ctx.session_id) == ["ACTION_RECORD", "APPROVAL_RECORD"]
        assert "A call held for approval, and the approval record" in page_text(PAGE)


class TestWhenHeldRecordsAreWritten:
    async def test_on_the_interval(self, tmp_path):
        inner = JsonlIngestClient(data_dir=tmp_path)
        ctx = SessionContext(agent_id=uuid4())
        async with BufferedIngestClient(inner, flush_interval_seconds=0.05) as client:

            @trace(ingest_client=client, session_context=ctx)
            async def step(n: int) -> int:
                return n

            await step(1)
            assert _types(inner, ctx.session_id) == []
            await asyncio.sleep(0.2)
            assert _types(inner, ctx.session_id) == ["ACTION_RECORD"]

    async def test_at_the_size_cap_and_the_filling_call_waits(self, tmp_path):
        class Slow(IngestClient):
            def __init__(self) -> None:
                self.stored: list[dict] = []

            async def handle(self, envelope):
                await asyncio.sleep(0.02)
                self.stored.append(envelope)

        inner = Slow()
        client = BufferedIngestClient(inner, flush_interval_seconds=999, max_buffer_size=3)
        ctx = SessionContext(agent_id=uuid4())

        @trace(ingest_client=client, session_context=ctx)
        async def step(n: int) -> int:
            return n

        await step(1)
        await step(2)
        assert inner.stored == []
        started = time.perf_counter()
        await step(3)
        assert time.perf_counter() - started >= 0.06  # waited for all three writes
        assert len(inner.stored) == 3
        await client.close()

    def test_before_the_session_closes(self, tmp_path):
        source = textwrap.dedent(
            """
            import asyncio
            import rootsign

            rootsign.init(agent="batch-check")

            @rootsign.trace()
            async def step(n: int) -> int:
                return n

            async def main():
                async with rootsign.session() as ctx:
                    for n in range(5):
                        await step(n)

            asyncio.run(main())
            """
        )
        run = run_script(
            source,
            tmp_path,
            extra_env={"ROOTSIGN_BUFFERED": "true", "ROOTSIGN_BUFFER_INTERVAL": "999"},
        )
        lines = run.only_session().read_text().splitlines()
        types = [json.loads(line)["event_type"] for line in lines]
        assert types == ["SESSION_OPEN", *["ACTION_RECORD"] * 5, "SESSION_CLOSE"]
        assert [a["sequence_number"] for a in run.actions()] == [1, 2, 3, 4, 5]


class TestLimits:
    def test_a_crash_loses_the_held_calls_and_still_verifies(self, tmp_path):
        source = textwrap.dedent(
            """
            import asyncio, os
            import rootsign

            rootsign.init(agent="batch-check")

            @rootsign.trace()
            async def step(n: int) -> int:
                return n

            async def main():
                async with rootsign.session() as ctx:
                    print(ctx.session_id, flush=True)
                    for n in range(3):
                        await step(n)
                    os._exit(0)

            asyncio.run(main())
            """
        )
        run = run_script(
            source,
            tmp_path,
            extra_env={"ROOTSIGN_BUFFERED": "true", "ROOTSIGN_BUFFER_INTERVAL": "999"},
        )
        assert run.actions() == []
        result = CliRunner().invoke(app, ["verify", "--local", str(run.only_session())])
        assert result.exit_code == 0
        assert result.output.startswith("VALID")

    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="root writes to read-only directories",
    )
    async def test_on_record_loss_fail_cannot_stop_the_call(self, tmp_path):
        spool = tmp_path / "spool"
        spool.mkdir(mode=0o500)
        inner = HttpIngestClient(
            "https://ingest.example.test/v1",
            "key",
            max_retries=1,
            transport=httpx.MockTransport(lambda request: httpx.Response(503)),
            spool_dir=str(spool),
            on_record_loss="fail",
        )
        client = BufferedIngestClient(inner, flush_interval_seconds=999)
        ctx = SessionContext(agent_id=uuid4())

        @trace(ingest_client=client, session_context=ctx)
        async def pay(amount: int) -> str:
            return "paid"

        try:
            assert await pay(amount=1) == "paid"
            await client.flush()  # the record is lost here, after the call returned
        finally:
            await client.close()
            spool.chmod(0o700)

    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="root writes to read-only files",
    )
    def test_the_failed_write_line(self, tmp_path):
        source = textwrap.dedent(
            """
            import asyncio, os
            import rootsign

            rootsign.init(agent="batch-check")

            @rootsign.trace()
            async def step(n: int) -> int:
                return n

            async def main():
                async with rootsign.session() as ctx:
                    path = os.path.join(
                        os.environ["ROOTSIGN_DATA_DIR"], "sessions", f"{ctx.session_id}.jsonl"
                    )
                    os.chmod(path, 0o400)
                    print(await step(1))
                print(ctx.session_id)

            asyncio.run(main())
            """
        )
        (tmp_path / "lose.py").write_text(source)
        # Log lines cut an exception at 200 characters; pytest's tmp_path is long
        # enough to trip that, a data folder like ~/.rootsign is not.
        data = Path(tempfile.mkdtemp(prefix="rs-", dir="/tmp"))
        env = {
            **os.environ,
            "ROOTSIGN_BACKEND": "jsonl",
            "ROOTSIGN_DATA_DIR": str(data),
            "ROOTSIGN_BUFFERED": "true",
        }
        try:
            result = subprocess.run(
                [sys.executable, "lose.py"], cwd=tmp_path, env=env, capture_output=True, text=True
            )
            assert result.returncode == 0, result.stderr
            printed, session_id = result.stdout.split()
            assert printed == "1"  # the call returned its result
            (shown,) = output_after(PAGE, "the session's client closes are lost:")
            expected = shown.replace("<data_dir>", str(data)).replace("<session_id>", session_id)
            assert expected in result.stderr.splitlines()
        finally:
            shutil.rmtree(data)


async def test_the_cloud_transport_sends_a_write_as_one_request():
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        batch = json.loads(request.content)
        return httpx.Response(
            200,
            json=[{"status": "accepted", "event_id": e["event_id"]} for e in batch],
        )

    inner = HttpIngestClient(
        "https://ingest.example.test/v1", "key", transport=httpx.MockTransport(respond)
    )
    client = BufferedIngestClient(inner, flush_interval_seconds=999)
    ctx = SessionContext(agent_id=uuid4())

    @trace(ingest_client=client, session_context=ctx)
    async def step(n: int) -> int:
        return n

    for n in range(3):
        await step(n)
    await client.flush()
    await client.close()

    assert len(requests) == 1
    assert len(json.loads(requests[0].content)) == 3


def test_the_batched_example_runs_and_verifies(tmp_path):
    run = run_script(code_block(PAGE, "batched.py"), tmp_path, name="batched.py")
    session_id = run.stdout.strip()
    session_file = tmp_path / "audit" / "sessions" / f"{session_id}.jsonl"

    result = CliRunner().invoke(app, ["verify", "--local", str(session_file)])

    assert result.exit_code == 0, result.output
    shown = output_after(PAGE, "rootsign verify --local audit/sessions/<session_id>.jsonl")
    (shown_id,) = re.findall(r"Session:\s+([0-9a-f-]{36})", "\n".join(shown))
    printed = [line.strip() for line in result.output.splitlines() if line.strip()]
    assert printed == [line.replace(shown_id, session_id) for line in shown]
