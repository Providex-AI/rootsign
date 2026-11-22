"""The "Cloud and sync" guide (Preview) stays true.

`guides/cloud-and-sync.mdx`: the page's `agent.py` runs against an endpoint
that answers like the default URL does today (404, not deployed) and spools
with exactly the one warning shown; without `agent_id` it fails with the
message shown; the spooled file verifies and dry-runs as shown; `sync` against
a spec-checking endpoint prints what the page shows, moves the file and is safe
to re-run; a mid-session outage leaves a spool file that verifies INCOMPLETE
while the store ends up with the whole chain; a run that spooled stays
spooled; and receipts append. Every endpoint is local: nothing leaves the
machine.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from rootsign.ingest.schemas import EventType
from rootsign.sdk.chain import verify_session_local
from rootsign.sdk.client import HttpIngestClient
from rootsign.sdk.config import SDKSettings
from rootsign.verdict import Verdict
from tests.support.docs_pages import code_block, output_after, page_text
from tests.support.http_endpoints import mock_ingest, not_deployed

PAGE = "guides/cloud-and-sync.mdx"
BIN = Path(sys.executable).parent
SHOWN_ID = "404612c7-0a59-4797-b312-71b5fab47de3"


def _env(tmp_path: Path, url: str, **extra: str) -> dict[str, str]:
    return {
        **{k: v for k, v in os.environ.items() if not k.startswith("ROOTSIGN_")},
        "HOME": str(tmp_path / "home"),
        "ROOTSIGN_BACKEND": "cloud",
        "ROOTSIGN_API_KEY": "test-key",
        "ROOTSIGN_CLOUD_URL": url,
        "ROOTSIGN_DATA_DIR": str(tmp_path / "data"),
        # A cloud user has no local database; nothing here may reach one.
        "DATABASE_URL": "postgresql+asyncpg://rootsign:rootsign@127.0.0.1:1/none",
        "DATABASE_URL_SYNC": "postgresql+psycopg2://rootsign:rootsign@127.0.0.1:1/none",
        **extra,
    }


def _run(env, *argv: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), env=env, cwd=cwd, capture_output=True, text=True)


def _agent(tmp_path: Path, url: str, source: str | None = None) -> subprocess.CompletedProcess:
    (tmp_path / "agent.py").write_text(source or code_block(PAGE, "agent.py"))
    return _run(_env(tmp_path, url), sys.executable, "agent.py", cwd=tmp_path)


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


@pytest.fixture
def spooled(tmp_path):
    """The page's run: the endpoint answers 404, so the session is spooled."""
    with not_deployed() as url:
        result = _agent(tmp_path, url)
    assert result.returncode == 0, result.stderr
    session_id = result.stdout.strip()
    return {
        "result": result,
        "url": url,
        "session_id": session_id,
        "file": tmp_path / "data" / "spool" / "sessions" / f"{session_id}.jsonl",
        "data": tmp_path / "data",
    }


class TestRecordASession:
    def test_the_settings_table_defaults(self):
        fields = SDKSettings.model_fields
        text = page_text(PAGE)
        assert f"| `ROOTSIGN_CLOUD_URL` | `{fields['CLOUD_URL'].default}` |" in text
        assert SDKSettings(DATA_DIR="/d").SPOOL_DIR == "/d/spool"
        assert "| `ROOTSIGN_SPOOL_DIR` | `$ROOTSIGN_DATA_DIR/spool` |" in text

    def test_without_an_agent_id_the_session_does_not_open(self, tmp_path):
        source = code_block(PAGE, "agent.py").replace("agent_id=AGENT_ID, ", "")
        with not_deployed() as url:
            result = _agent(tmp_path, url, source)

        assert result.returncode == 1
        (shown,) = output_after(PAGE, "Without `agent_id`, the session does not open:")
        assert result.stderr.strip().splitlines()[-1] == shown

    def test_the_run_spools_with_the_one_warning_shown(self, spooled):
        assert re.fullmatch(r"[0-9a-f-]{36}", spooled["session_id"])
        assert spooled["file"].exists()
        logged = [
            line for line in spooled["result"].stderr.splitlines() if line.startswith("rootsign")
        ]
        assert len(logged) == 1, logged
        (shown,) = output_after(PAGE, "not one per record:")
        expected = shown.replace("<spool_dir>", str(spooled["data"] / "spool")).replace(
            "<endpoint>", f"{spooled['url']}/ingest"
        )
        assert logged[0] == expected


class TestVerifyOffline:
    def test_a_spooled_file_verifies_as_shown(self, spooled, tmp_path):
        result = _run(
            _env(tmp_path, spooled["url"]),
            str(BIN / "rootsign"),
            "verify",
            "--local",
            str(spooled["file"]),
            cwd=tmp_path,
        )
        assert result.returncode == 0
        shown = output_after(PAGE, "rootsign verify --local ~/.rootsign/spool/sessions/")
        assert _lines(result.stdout) == [
            line.replace(SHOWN_ID, spooled["session_id"]) for line in shown
        ]

    def test_an_outage_mid_session_verifies_incomplete_then_syncs_whole(self, tmp_path):
        source = code_block(PAGE, "agent.py").replace(
            '        await log_payment("acme-corp", 1500.00)\n',
            "        import urllib.request\n"
            "        urllib.request.urlopen(urllib.request.Request(SEVER, method='POST'))\n"
            '        await log_payment("acme-corp", 1500.00)\n'
            '        await log_payment("acme-corp", 1.00)\n',
        )
        with mock_ingest() as (url, store):
            source = source.replace(
                "import rootsign\n", f"import rootsign\n\nSEVER = {url[:-3] + '/_sever'!r}\n", 1
            )
            result = _agent(tmp_path, url, source)
            assert result.returncode == 0, result.stderr
            session_id = result.stdout.strip()
            spool_file = tmp_path / "data" / "spool" / "sessions" / f"{session_id}.jsonl"

            verify = _run(
                _env(tmp_path, url),
                str(BIN / "rootsign"),
                "verify",
                "--local",
                str(spool_file),
                cwd=tmp_path,
            )
            assert verify.returncode == 2
            printed = _lines(verify.stdout)
            shown = output_after(PAGE, "so the file verifies as `INCOMPLETE`:")
            assert printed[0] == shown[0]
            assert re.fullmatch(r"Detail:\s+1 record\(s\) missing at sequence 1", printed[1])
            assert shown[1] == "Detail:   2 record(s) missing at sequence 1-2"

            httpx.post(url[:-3] + "/_restore")
            sync = _run(_env(tmp_path, url), str(BIN / "rootsign-admin"), "sync", cwd=tmp_path)
            assert sync.returncode == 0, sync.stdout + sync.stderr
            whole = store.dump_chain(session_id, tmp_path / "store.jsonl")
            outcome = verify_session_local(str(whole))
            assert outcome.verdict is Verdict.VALID
            assert outcome.record_count == 3


class TestSync:
    def test_dry_run_as_shown(self, spooled, tmp_path):
        result = _run(
            _env(tmp_path, spooled["url"]),
            str(BIN / "rootsign-admin"),
            "sync",
            "--dry-run",
            cwd=tmp_path,
        )
        assert result.returncode == 0
        shown = output_after(PAGE, "```bash\nrootsign-admin sync --dry-run\n```")
        expected = [
            line.replace("<data_dir>", str(spooled["data"])).replace(
                SHOWN_ID, spooled["session_id"]
            )
            for line in shown
        ]
        assert _lines(result.stdout) == expected

    def test_sync_uploads_as_shown_and_is_safe_to_rerun(self, spooled, tmp_path):
        with mock_ingest() as (url, store):
            env = _env(tmp_path, url)
            first = _run(env, str(BIN / "rootsign-admin"), "sync", cwd=tmp_path)
            again = _run(env, str(BIN / "rootsign-admin"), "sync", cwd=tmp_path)

        assert first.returncode == 0, first.stdout + first.stderr
        shown = output_after(PAGE, "```bash\nrootsign-admin sync\n```")
        printed = _lines(first.stdout)
        assert len(printed) == len(shown)
        for line, expected in zip(printed, shown):
            expected = (
                expected.replace("<data_dir>", str(spooled["data"]))
                .replace("<endpoint>", f"{url}/ingest")
                .replace(SHOWN_ID, spooled["session_id"])
            )
            if expected.startswith("receipt SHA-256 "):
                assert re.fullmatch(r"receipt SHA-256 [0-9a-f]{64}", line)
            else:
                assert line == expected
        assert len(store.actions_for(spooled["session_id"])) == 2
        synced = spooled["data"] / "spool" / "synced"
        assert (synced / f"{spooled['session_id']}.jsonl").exists()
        assert (synced / "receipts" / f"{spooled['session_id']}.sync.json").exists()

        assert again.returncode == 0
        spool = spooled["data"] / "spool"
        assert again.stdout.strip() == f"Nothing to sync — no spooled sessions under {spool}."
        assert "it prints `Nothing to sync — no spooled sessions under <spool>.`" in page_text(PAGE)

    def test_a_second_attempt_is_appended_to_the_receipt(self, spooled, tmp_path):
        env = _env(tmp_path, "http://127.0.0.1:1/v1", ROOTSIGN_HTTP_MAX_RETRIES="1")
        for _ in range(2):
            assert _run(env, str(BIN / "rootsign-admin"), "sync", cwd=tmp_path).returncode == 1
        receipt = spooled["data"] / "spool" / "receipts" / f"{spooled['session_id']}.sync.json"
        attempts = json.loads(receipt.read_text())["attempts"]
        assert [a["disposition"] for a in attempts] == ["transport_failed", "transport_failed"]


async def test_once_a_run_spools_it_stays_spooled(tmp_path):
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503)
        batch = json.loads(request.content)
        return httpx.Response(
            200, json=[{"status": "accepted", "event_id": e["event_id"]} for e in batch]
        )

    client = HttpIngestClient(
        "https://ingest.example.test/v1",
        "key",
        max_retries=1,
        transport=httpx.MockTransport(respond),
        spool_dir=str(tmp_path),
    )
    session_id = str(uuid4())

    def envelope() -> dict:
        return {
            "schema_version": "1.0",
            "sdk_version": "0.3.1",
            "event_type": EventType.SESSION_OPEN.value,
            "event_id": str(uuid4()),
            "emitted_at": "2026-11-22T12:00:00+00:00",
            "agent_id": str(uuid4()),
            "session_id": session_id,
            "payload": {},
        }

    await client.handle(envelope())  # fails over
    await client.handle(envelope())  # the endpoint would answer now; it is not asked

    assert len(requests) == 1
    assert client.is_spooling is True
