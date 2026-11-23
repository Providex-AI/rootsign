"""The Configuration and Errors reference pages stay true to the code.

`reference/configuration.mdx` lists every environment variable with its
default and allowed values, and says where settings come from and when they
are read. `reference/errors.mdx` lists every exception with its attributes and
message. These tests hold both to the code in both directions (nothing
documented that does not exist, nothing that exists left out) and run the
behaviours the pages describe in fresh processes, the way a reader meets them.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import typing
from pathlib import Path
from uuid import uuid4

import pytest

from rootsign import __all__ as ROOTSIGN_EXPORTS
from rootsign import errors, verify_session_local
from rootsign.config import Settings
from rootsign.sdk.config import SDKSettings
from tests.support.docs_pages import output_after, page_text

CONFIG = "reference/configuration.mdx"
ERRORS = "reference/errors.mdx"
REPO = Path(__file__).resolve().parents[2]

#: Root writes to read-only files, so a simulated disk failure does not happen.
needs_permissions = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root writes to read-only files, so the disk failure cannot be simulated",
)

#: Settings the page leaves out on purpose: RootSign's own test suite uses them.
INTERNAL = {"TEST_DATABASE_URL", "TEST_DATABASE_URL_SYNC"}
#: Accepted for old .env files, read by nothing.
UNUSED = {"WAL_PATH", "MAX_RETRIES", "RETRY_BASE_DELAY", "RETRY_MAX_DELAY"}


def _section(page: str, heading: str) -> str:
    text = page_text(page)
    start = text.index(f"\n{heading}\n")
    level = heading.split(" ")[0]
    end = re.search(rf"\n#{{1,{len(level)}}} ", text[start + 1 :])
    return text[start : start + 1 + end.start()] if end else text[start:]


def _fields(page: str) -> dict[str, dict[str, str]]:
    """Every ResponseField on a page: name -> {type, default}."""
    found = {}
    for attrs in re.findall(r"<ResponseField ([^>]+)>", page_text(page)):
        parsed = dict(re.findall(r'(\w+)="([^"]*)"', attrs))
        found[parsed.pop("name")] = parsed
    return found


def _env_name(model: type, field: str) -> str:
    return f"ROOTSIGN_{field}" if model is SDKSettings else field


def _documented_default(model: type, field: str) -> str | None:
    default = model.model_fields[field].default
    if default == "":
        return None
    if isinstance(default, bool):
        return str(default).lower()
    return str(getattr(default, "value", default))


def _no_rootsign_env() -> dict[str, str]:
    """This process's environment minus every RootSign setting."""
    clean = {k: v for k, v in os.environ.items() if not k.upper().startswith("ROOTSIGN_")}
    clean.pop("ENVIRONMENT", None)
    return clean


def _python(source: str, cwd: Path, env: dict[str, str] | None = None):
    """Run `source` in a fresh interpreter: no inherited ROOTSIGN_ settings,
    cwd away from the repo's .env."""
    clean = _no_rootsign_env()
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        cwd=cwd,
        env={**clean, "ROOTSIGN_DATA_DIR": str(cwd / "data"), **(env or {})},
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Configuration: the variables
# ---------------------------------------------------------------------------

SETTINGS = [
    (model, field)
    for model in (SDKSettings, Settings)
    for field in model.model_fields
    if field not in INTERNAL | UNUSED
]


def test_every_setting_is_documented_and_nothing_else_is():
    documented = set(_fields(CONFIG))
    real = {_env_name(m, f) for m, f in SETTINGS}
    assert documented == real


def test_the_unused_settings_are_listed_and_really_unused():
    section = _section(CONFIG, "## Accepted but unused")
    for name in UNUSED:
        assert f"`ROOTSIGN_{name}`" in section
        assert name in SDKSettings.model_fields
        readers = [
            p
            for p in (REPO / "rootsign").rglob("*.py")
            if p.name != "config.py" and re.search(rf"\.{name}\b", p.read_text())
        ]
        assert readers == [], f"{name} is read by {readers}"


@pytest.mark.parametrize(("model", "field"), SETTINGS, ids=[f for _, f in SETTINGS])
def test_each_documented_default_is_the_real_one(model, field):
    documented = _fields(CONFIG)[_env_name(model, field)].get("default")
    if field == "SPOOL_DIR":
        # Empty means "derived", and the page says from what.
        assert documented == "$ROOTSIGN_DATA_DIR/spool"
        assert SDKSettings(_env_file=None, DATA_DIR="/d").SPOOL_DIR == "/d/spool"
    else:
        assert documented == _documented_default(model, field)


@pytest.mark.parametrize(
    "field",
    [
        f
        for f, info in SDKSettings.model_fields.items()
        if typing.get_origin(info.annotation) is typing.Literal
        or hasattr(info.annotation, "__members__")
    ],
)
def test_allowed_values_match(field):
    annotation = SDKSettings.model_fields[field].annotation
    values = (
        [m.value for m in annotation]
        if hasattr(annotation, "__members__")
        else list(typing.get_args(annotation))
    )
    if field == "BACKEND":
        values.remove("local")  # documented as a deprecated alias instead
        assert "`local` is a deprecated name for `postgres`" in page_text(CONFIG)
    assert _fields(CONFIG)[f"ROOTSIGN_{field}"]["type"] == " | ".join(values)


def test_the_fsync_table_matches_the_chain_critical_records():
    from rootsign.sdk.jsonl_client import _CHAIN_CRITICAL

    assert _CHAIN_CRITICAL == {"ACTION_RECORD", "APPROVAL_RECORD"}
    assert "| `chain` | Each action and approval record," in page_text(CONFIG)


# ---------------------------------------------------------------------------
# Configuration: how settings are read
# ---------------------------------------------------------------------------

DATA_DIR = "from rootsign.sdk.config import SDKSettings; print(SDKSettings().DATA_DIR)"


class TestSources:
    def test_dotenv_in_the_working_directory_is_read(self, tmp_path):
        (tmp_path / ".env").write_text("ROOTSIGN_DATA_DIR=/from-dotenv\n")
        result = subprocess.run(
            [sys.executable, "-c", DATA_DIR],
            cwd=tmp_path,
            env=_no_rootsign_env(),
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "/from-dotenv"

    def test_an_environment_variable_beats_dotenv(self, tmp_path):
        (tmp_path / ".env").write_text("ROOTSIGN_DATA_DIR=/from-dotenv\n")
        result = _python(DATA_DIR, tmp_path, {"ROOTSIGN_DATA_DIR": "/from-env"})
        assert result.stdout.strip() == "/from-env"

    def test_dotenv_next_to_the_script_is_not_read(self, tmp_path):
        project, elsewhere = tmp_path / "project", tmp_path / "elsewhere"
        project.mkdir(), elsewhere.mkdir()
        (project / ".env").write_text("ROOTSIGN_DATA_DIR=/from-dotenv\n")
        (project / "show.py").write_text(DATA_DIR)
        result = subprocess.run(
            [sys.executable, str(project / "show.py")],
            cwd=elsewhere,
            env=_no_rootsign_env(),
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "~/.rootsign"

    def test_names_are_not_case_sensitive(self, tmp_path):
        result = subprocess.run(
            [sys.executable, "-c", DATA_DIR],
            cwd=tmp_path,
            env={**_no_rootsign_env(), "rootsign_data_dir": "/lower"},
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "/lower"
        assert "Names are not case-sensitive." in page_text(CONFIG)


def test_setting_one_in_code_before_init_works(tmp_path):
    """The page's snippet, followed by a decision: it must be recorded."""
    snippet = re.search(
        r"```python\n(import os\n.*?)\n```", _section(CONFIG, "## How settings are read"), re.S
    ).group(1)
    source = snippet + textwrap.dedent(
        """
            import asyncio

            async def main():
                async with rootsign.session(objective="config page") as ctx:
                    print(await ctx.record_decision(selected_action="x", confidence=1.0))

            asyncio.run(main())
            """
    )
    result = _python(source, tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() != "None"


def test_an_invalid_value_stops_the_import(tmp_path):
    result = _python("import rootsign", tmp_path, {"ROOTSIGN_BACKEND": "sqlite"})

    assert result.returncode == 1
    printed = result.stderr.splitlines()
    for line in output_after(CONFIG, "stops `import rootsign` with a pydantic"):
        assert line in [p.strip() for p in printed], result.stderr


def test_a_plain_postgresql_url_is_refused(tmp_path):
    result = _python(
        "import rootsign.database",
        tmp_path,
        {"DATABASE_URL": "postgresql://rootsign:rootsign@localhost:1/x"},
    )
    # SQLAlchemy 2.0 maps a plain URL to psycopg2 (synchronous); 2.1 maps it
    # to psycopg 3, which RootSign does not install. Either way it fails.
    assert result.returncode == 1
    assert (
        "The asyncio extension requires an async driver to be used." in result.stderr
        or "No module named 'psycopg'" in result.stderr
    ), result.stderr
    assert "Use the `postgresql+asyncpg://` form" in page_text(CONFIG)


def test_the_production_guard_message(tmp_path):
    # The guard fires on the default URLs, so make sure nothing overrides them.
    env = {k: v for k, v in _no_rootsign_env().items() if not k.startswith("DATABASE_URL")}
    result = subprocess.run(
        [sys.executable, "-c", "import rootsign.config"],
        cwd=tmp_path,
        env={**env, "ENVIRONMENT": "production"},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    (expected,) = output_after(CONFIG, "RootSign then refuses to use the default database URLs")
    assert expected in result.stderr


@pytest.mark.parametrize(
    ("depth", "summary_chars", "alternatives"),
    [("minimal", None, False), ("summary", 500, False), ("full", 10_000, True)],
)
def test_the_reasoning_depth_table(tmp_path, depth, summary_chars, alternatives):
    row = re.search(rf"^  \| `{depth}` \| (.+?) \| (.+?) \|$", page_text(CONFIG), re.M)
    documented_summary, documented_alternatives = row.group(1), row.group(2)
    assert documented_summary == (
        "Not kept" if summary_chars is None else f"First {summary_chars:,} characters"
    )
    assert documented_alternatives == ("Kept" if alternatives else "Not kept")

    source = """
        import asyncio, rootsign
        rootsign.init(agent="depth-check")

        async def main():
            async with rootsign.session(objective="depth") as ctx:
                await ctx.record_decision(
                    selected_action="x", reasoning_summary="r" * 20_000,
                    alternatives_considered=["a", "b"], confidence=1.0,
                )

        asyncio.run(main())
    """
    result = _python(
        source,
        tmp_path,
        {"ROOTSIGN_CAPTURE_DECISIONS": "true", "ROOTSIGN_REASONING_DEPTH": depth},
    )
    assert result.returncode == 0, result.stderr
    (session,) = (tmp_path / "data" / "sessions").glob("*.jsonl")
    records = [json.loads(line) for line in session.read_text().splitlines()]
    (decision,) = [r["payload"] for r in records if r["event_type"] == "DECISION_RECORD"]
    summary = decision["reasoning_summary"]
    assert (None if summary is None else len(summary)) == summary_chars
    assert bool(decision["alternatives_considered"]) is alternatives


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def _subclasses(cls: type) -> list[type]:
    return [c for c in cls.__subclasses__() if c.__module__ == "rootsign.errors"]


def test_the_tree_is_the_real_hierarchy():
    drawn = output_after(ERRORS, "except RootSignError as error:")
    names = [re.sub(r"^[│├└─\s]+", "", line) for line in drawn]

    expected = ["RootSignError"]
    for child in _subclasses(errors.RootSignError):
        expected.append(child.__name__)
        if child is not errors.IngestError:
            expected += [g.__name__ for g in _subclasses(child)]
    assert sorted(names) == sorted(expected)
    assert "│   └── HiTLPersistenceError" in "\n".join(drawn)


def test_the_top_level_imports_listed_are_the_real_ones():
    sentence = re.search(
        r"(`RootSignError`.+?) can also be imported from `rootsign`", page_text(ERRORS)
    ).group(1)
    listed = set(re.findall(r"`(\w+)`", sentence))
    exported = {n for n in ROOTSIGN_EXPORTS if isinstance(getattr(errors, n, None), type)}
    assert listed == exported


@pytest.mark.parametrize(
    ("exception", "instance"),
    [
        ("HiTLRejectedError", errors.HiTLRejectedError(uuid4(), "r")),
        ("HiTLTimeoutError", errors.HiTLTimeoutError(uuid4(), 300)),
        ("HiTLUnsupportedBackendError", errors.HiTLUnsupportedBackendError("t")),
        ("HiTLPersistenceError", errors.HiTLPersistenceError("d", tool_name="t")),
        ("RootSignNotInitializedError", errors.RootSignNotInitializedError("s")),
    ],
)
def test_each_exception_documents_exactly_its_attributes(exception, instance):
    documented = set(
        re.findall(r'<ResponseField name="(\w+)"', _section(ERRORS, f"### {exception}"))
    )
    assert documented == set(vars(instance)) - {"args"}


@pytest.mark.parametrize(
    ("exception", "message"),
    [
        (
            "HiTLRejectedError",
            str(
                errors.HiTLRejectedError(
                    "7d8d56ec-805c-430d-ae2f-ff1141a17f30", "Above the unattended mandate"
                )
            ),
        ),
        (
            "HiTLTimeoutError",
            str(errors.HiTLTimeoutError("b5e0e3d3-fa5e-4cb4-9462-b1048fd54c83", 300)),
        ),
        ("HiTLUnsupportedBackendError", str(errors.HiTLUnsupportedBackendError("wire_transfer"))),
        (
            "RootSignPostgresExtraRequired",
            str(errors.RootSignPostgresExtraRequired("missing module: sqlalchemy")),
        ),
        (
            "RootSignCloudExtraRequired",
            str(errors.RootSignCloudExtraRequired("missing module: httpx")),
        ),
    ],
)
def test_message_formats(exception, message):
    assert output_after(ERRORS, f"### {exception}\n") == [message]


def test_the_cloud_approval_message():
    cloud = str(errors.HiTLUnsupportedBackendError("wire_transfer", backend="cloud"))
    assert output_after(ERRORS, "On the cloud transport:") == [cloud]


def test_the_own_transaction_approval_message():
    message = str(errors.HiTLUnsupportedBackendError("wire_transfer", backend="transaction"))
    assert output_after(ERRORS, "With a client bound to your own transaction:") == [message]


def test_not_initialised_is_raised_with_the_documented_message(tmp_path):
    source = """
        import asyncio, rootsign

        @rootsign.trace()
        async def lookup(q: str) -> str:
            return q

        try:
            asyncio.run(lookup(q="x"))
        except rootsign.RootSignError as error:
            print(type(error).__name__)
            print(error)
    """
    result = _python(source, tmp_path)

    lines = result.stdout.splitlines()
    assert lines[0] == "RootSignNotInitializedError"
    expected = output_after(ERRORS, "### RootSignNotInitializedError\n")
    assert [line.strip() for line in lines[1:]] == expected


@pytest.mark.parametrize(
    ("missing", "source", "exception"),
    [
        (
            "sqlalchemy",
            """
            import asyncio, rootsign
            rootsign.init(agent="extra-check")
            async def main():
                async with rootsign.session(objective="x"):
                    pass
            asyncio.run(main())
            """,
            "RootSignPostgresExtraRequired",
        ),
        (
            "httpx",
            """
            from rootsign import HttpIngestClient
            HttpIngestClient("https://ingest.example.test/v1", "k")
            """,
            "RootSignCloudExtraRequired",
        ),
    ],
)
def test_a_missing_extra_is_reported(tmp_path, missing, source, exception):
    blocked = f"import sys; sys.modules[{missing!r}] = None\n" + textwrap.dedent(source)
    env = {"ROOTSIGN_BACKEND": "postgres"} if missing == "sqlalchemy" else {}
    result = _python(blocked, tmp_path, env)

    (expected,) = output_after(ERRORS, f"### {exception}\n")
    assert result.stderr.strip().splitlines()[-1] == f"rootsign.errors.{exception}: {expected}"


@needs_permissions
class TestPersistence:
    """The cloud transport with the endpoint down and an unwritable spool."""

    SOURCE = """
        import asyncio, sys, rootsign
        from rootsign.sdk.client import HttpIngestClient
        rootsign.init(agent="loss-check")
        HITL, MODE = sys.argv[1] == "hitl", sys.argv[2]

        async def main():
            client = HttpIngestClient(
                "http://localhost:1/v1", "k", max_retries=1, spool_dir="ro", on_record_loss=MODE
            )
            try:
                async with rootsign.session(client=client, objective="x") as ctx:
                    @rootsign.trace(ingest_client=client, session_context=ctx, require_approval=HITL)
                    async def pay(amount: int) -> str:
                        print("TOOL RAN")
                        return "paid"
                    await pay(amount=1)
            except rootsign.RootSignError as error:
                print(f"{type(error).__name__}: {error}")

        asyncio.run(main())
    """

    def _run(self, tmp_path, hitl: str, mode: str) -> str:
        (tmp_path / "ro").mkdir(mode=0o500)
        (tmp_path / "loss.py").write_text(textwrap.dedent(self.SOURCE))
        result = subprocess.run(
            [sys.executable, "loss.py", hitl, mode],
            cwd=tmp_path,
            env={**_no_rootsign_env(), "ROOTSIGN_DATA_DIR": str(tmp_path / "data")},
            capture_output=True,
            text=True,
        )
        return result.stdout

    def _pattern(self, exception: str) -> re.Pattern[str]:
        (message,) = output_after(ERRORS, f"### {exception}\n")
        parts = re.split(r"(<[^>]+>)", message)
        return re.compile("".join(".+?" if p.startswith("<") else re.escape(p) for p in parts))

    def test_record_persistence_error(self, tmp_path):
        out = self._run(tmp_path, "telemetry", "fail")
        (line,) = [x for x in out.splitlines() if x.startswith("RecordPersistenceError: ")]
        assert self._pattern("RecordPersistenceError").fullmatch(line.split(": ", 1)[1]), line

    # A held call on the cloud transport now stops at HiTLUnsupportedBackendError
    # before any record is written, so the page shows this error on JSONL: the
    # session file becomes unwritable before the held call, a terminal is
    # present (patched), and the pending record cannot be stored.
    HELD = """
        import asyncio, os
        from unittest.mock import patch
        import rootsign
        rootsign.init(agent="loss-check")

        @rootsign.trace(require_approval=True)
        async def pay(amount: int) -> str:
            print("TOOL RAN")
            return "paid"

        async def main():
            async with rootsign.session(objective="x") as ctx:
                path = os.path.join(os.environ["ROOTSIGN_DATA_DIR"], "sessions", f"{ctx.session_id}.jsonl")
                os.chmod(path, 0o400)
                try:
                    with patch("sys.stdin.isatty", return_value=True):
                        await pay(amount=1)
                except rootsign.RootSignError as error:
                    print(f"{type(error).__name__}: {error}")
                os.chmod(path, 0o600)

        asyncio.run(main())
    """

    def test_hitl_persistence_error_stops_the_tool(self, tmp_path):
        result = _python(self.HELD, tmp_path)
        assert "TOOL RAN" not in result.stdout
        (line,) = [x for x in result.stdout.splitlines() if x.startswith("HiTLPersistenceError: ")]
        assert self._pattern("HiTLPersistenceError").fullmatch(line.split(": ", 1)[1]), line


def test_escalation_depth_is_never_raised():
    """The page says this version never raises it. Keep that true, or fix the page."""
    raisers = [
        p
        for p in (REPO / "rootsign").rglob("*.py")
        if p.name != "errors.py" and "EscalationDepthExceededError(" in p.read_text()
    ]
    assert raisers == []
    assert "This version never raises it." in _section(ERRORS, "### EscalationDepthExceededError")


def test_the_ingest_error_table():
    rows = dict(re.findall(r"^\| `(\w+Error)` \| `(\w+)` \|$", page_text(ERRORS), re.M))
    real = {c.__name__: c.error_code for c in _subclasses(errors.IngestError)}
    assert rows == real
    for cls in _subclasses(errors.IngestError):
        assert isinstance(cls.retryable, bool)


def test_reinit_value_error_message(tmp_path):
    source = """
        import rootsign
        rootsign.init(agent="invoice-agent")
        rootsign.init(agent="billing-agent")
    """
    result = _python(source, tmp_path)
    expected = output_after(ERRORS, "\nFor example:")
    assert result.stderr.strip().splitlines()[-1] == expected[0]


def test_verify_session_local_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        verify_session_local(str(tmp_path / "missing.jsonl"))


@needs_permissions
def test_a_failed_write_is_logged_not_raised(tmp_path, caplog):
    """The WARNING the page shows, from a real unwritable session file."""
    from rootsign.sdk.context import SessionContext
    from rootsign.sdk.decorator import trace
    from rootsign.sdk.jsonl_client import JsonlIngestClient

    # The log line shortens long values, so keep the path as short as a real
    # ~/.rootsign one rather than pytest's deep temporary folder.
    short = Path(tempfile.mkdtemp(prefix="rs-", dir="/tmp"))
    client = JsonlIngestClient(data_dir=short)
    ctx = SessionContext(agent_id=uuid4(), session_id=uuid4())
    path = client._session_path(str(ctx.session_id))
    path.parent.mkdir(parents=True)
    path.touch(mode=0o400)

    @trace(ingest_client=client, session_context=ctx)
    async def lookup(q: str) -> str:
        return q.upper()

    import asyncio

    with caplog.at_level(logging.WARNING, logger="rootsign"):
        assert asyncio.run(lookup("x")) == "X"

    (expected,) = output_after(ERRORS, "For example, on the JSONL backend:")
    level, rest = expected.split(" ", 1)
    record = caplog.records[-1]
    logged = f"{record.name}: {record.getMessage()}".replace(
        str(path), "<data_dir>/sessions/<session_id>.jsonl"
    ).replace(str(short), "<data_dir>")
    assert record.levelname == level
    assert logged == rest
    path.chmod(0o600)
    shutil.rmtree(short)
