"""The Python API reference page stays true to the code.

`reference/python-api.mdx` documents the core calls parameter by parameter.
These tests hold it to the real signatures, defaults, allowed values and
length limits, run both examples the page shows and compare their output,
compare each error message with a real one, and check that every name
`rootsign` exports is covered by this page or the Errors page, so a new export
cannot go undocumented.
"""

from __future__ import annotations

import inspect
import os
import re
import textwrap
from uuid import uuid4

import pytest
import rootsign
from rootsign.schemas.agent import AgentCreate, AgentEnvironment, AgentFramework, AgentRiskTier
from rootsign.sdk import facade
from rootsign.sdk.context import SessionContext
from tests.support.docs_pages import code_block, output_after, page_text, run_script

PAGE = "reference/python-api.mdx"


def _section(heading: str) -> str:
    text = page_text(PAGE)
    start = text.index(f"\n{heading}\n")
    level = heading.split(" ")[0]
    end = re.search(rf"\n#{{1,{len(level)}}} ", text[start + 1 :])
    return text[start : start + 1 + end.start()] if end else text[start:]


def _fields(heading: str) -> dict[str, dict[str, str]]:
    found = {}
    for attrs in re.findall(r"<ResponseField ([^>]+)>", _section(heading)):
        parsed = dict(re.findall(r'(\w+)="([^"]*)"', attrs))
        found[parsed.pop("name")] = parsed
    return found


def _parameters(func) -> dict[str, inspect.Parameter]:
    return dict(inspect.signature(func).parameters)


@pytest.fixture(autouse=True)
def _fresh_init(monkeypatch, tmp_path):
    monkeypatch.setenv("ROOTSIGN_BACKEND", "jsonl")
    monkeypatch.setenv("ROOTSIGN_DATA_DIR", str(tmp_path))
    facade._reset_init_config()
    yield
    facade._reset_init_config()


# ---------------------------------------------------------------------------
# The examples run as shown
# ---------------------------------------------------------------------------


def test_the_core_example(tmp_path):
    run = run_script(code_block(PAGE, "core.py"), tmp_path, name="core.py")

    assert run.stdout.splitlines() == output_after(PAGE, "```python core.py")


def test_the_wrap_tools_example(tmp_path):
    pytest.importorskip("langchain_core")
    source = code_block(PAGE, "langchain_tools.py")
    run = run_script(source, tmp_path, name="langchain_tools.py")

    assert run.stdout.splitlines() == output_after(PAGE, "```python langchain_tools.py")


# ---------------------------------------------------------------------------
# init()
# ---------------------------------------------------------------------------


def test_init_documents_exactly_its_parameters():
    assert set(_fields("## rootsign.init()")) == set(_parameters(rootsign.init))
    for name in _parameters(rootsign.init):
        assert f"    {name}=None," in _section("## rootsign.init()")


@pytest.mark.parametrize(
    ("field", "enum"),
    [
        ("environment", AgentEnvironment),
        ("risk_tier", AgentRiskTier),
        ("framework", AgentFramework),
    ],
)
def test_init_allowed_values_and_defaults(field, enum):
    documented = _fields("## rootsign.init()")[field]
    assert documented["type"] == " | ".join(m.value for m in enum)

    rootsign.init(agent="defaults-check")
    assert getattr(facade.get_init_config(), field) == documented["default"]


def test_init_length_limits_are_the_schema_s():
    def limits(field: str) -> tuple[int | None, int | None]:
        meta = AgentCreate.model_fields[field].metadata
        low = next((m.min_length for m in meta if hasattr(m, "min_length")), None)
        high = next((m.max_length for m in meta if hasattr(m, "max_length")), None)
        return low, high

    fields = _fields("## rootsign.init()")
    section = _section("## rootsign.init()")
    assert limits("name") == (2, 200)
    assert limits("owner") == (2, 200)
    assert "2 to 200 characters" in section.split('name="agent"')[1].split("</ResponseField>")[0]
    assert "2 to 200 characters" in section.split('name="owner"')[1].split("</ResponseField>")[0]
    assert limits("description")[1] == 2000 and "Up to 2,000 characters." in section
    assert limits("model_version")[1] == 100 and "up to 100 characters" in section
    assert set(fields) >= {"agent", "owner", "description", "model_version"}


def test_init_value_error_message():
    with pytest.raises(ValueError) as raised:
        rootsign.init(agent="a")

    assert output_after(PAGE, "length limits above, or when") == [str(raised.value)]


def test_init_does_not_need_a_session_or_io(tmp_path):
    rootsign.init(agent="io-check")
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# session(), SessionContext, record_decision()
# ---------------------------------------------------------------------------


def test_session_documents_exactly_its_parameters():
    assert set(_fields("## rootsign.session()")) == set(_parameters(rootsign.session))


def test_session_context_attributes_exist():
    section = _section("## rootsign.session()")
    yields = section[section.index("**Yields**") : section.index("**Raises**")]
    names = re.findall(r"^\| `(\w+)(?:\(\.\.\.\))?` \|", yields, re.M)
    assert names == ["session_id", "agent_id", "current_sequence", "record_decision"]
    ctx = SessionContext(agent_id=uuid4())
    for name in names:
        assert hasattr(ctx, name), name


def test_record_decision_signature():
    documented = re.findall(r"^    (\w+)(?:=|,)", _section("### ctx.record_decision()"), re.M)
    real = [
        p for p in _parameters(SessionContext.record_decision) if p not in ("self", "ingest_client")
    ]
    assert documented == real


@pytest.mark.parametrize(
    ("raises", "close_status"),
    [(False, "completed"), (True, "failed")],
)
def test_the_close_status(tmp_path, raises, close_status):
    source = textwrap.dedent(
        f"""
        import asyncio, json, os
        import rootsign

        rootsign.init(agent="close-check")


        async def main() -> None:
            try:
                async with rootsign.session(objective="x") as ctx:
                    if {raises}:
                        raise RuntimeError("boom")
            except RuntimeError:
                pass
            path = os.path.join(os.environ["ROOTSIGN_DATA_DIR"], "sessions", f"{{ctx.session_id}}.jsonl")
            last = json.loads(open(path).read().splitlines()[-1])
            print(last["payload"]["status"])


        asyncio.run(main())
        """
    )
    assert run_script(source, tmp_path).stdout.strip() == close_status
    assert "recorded as `failed`" in _section("## rootsign.session()")


# ---------------------------------------------------------------------------
# @trace()
# ---------------------------------------------------------------------------


def test_trace_documents_exactly_its_parameters_and_defaults():
    fields = _fields("## @rootsign.trace()")
    params = _parameters(rootsign.trace)
    assert set(fields) == set(params)
    for name in ("require_approval", "poll_interval_seconds", "timeout_seconds"):
        assert fields[name]["default"] == str(params[name].default)
        assert f"    {name}={params[name].default}," in _section("## @rootsign.trace()")


def test_a_raising_function_is_still_recorded_without_a_result(tmp_path):
    source = textwrap.dedent(
        """
        import asyncio, json, os
        import rootsign

        rootsign.init(agent="raise-check")


        @rootsign.trace()
        async def charge(amount: float) -> str:
            raise RuntimeError("card declined")


        async def main() -> None:
            try:
                async with rootsign.session(objective="x") as ctx:
                    await charge(amount=1.0)
            except RuntimeError as error:
                print("raised:", error)
            path = os.path.join(os.environ["ROOTSIGN_DATA_DIR"], "sessions", f"{ctx.session_id}.jsonl")
            action = [json.loads(l) for l in open(path) if '"ACTION_RECORD"' in l][0]
            print(action["output_hash"], action["output_redacted"])


        asyncio.run(main())
        """
    )
    assert run_script(source, tmp_path).stdout.splitlines() == [
        "raised: card declined",
        "None None",
    ]
    assert "The call is still recorded, with no result." in _section("## @rootsign.trace()")


# ---------------------------------------------------------------------------
# wrap_tools()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("wrap", [rootsign.wrap_tools, rootsign.wrap_crewai_tools])
def test_wrap_tools_documents_exactly_its_parameters(wrap):
    assert set(_fields("## rootsign.wrap_tools()")) == set(_parameters(wrap))


def test_wrap_tools_type_error_message():
    def lookup(q: str) -> str:
        return q

    with pytest.raises(TypeError) as raised:
        rootsign.wrap_tools([lookup])

    assert output_after(PAGE, "Nothing in the list is wrapped then") == [str(raised.value)]


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def test_verify_parameters():
    fields = set(_fields("## Verify a session"))
    assert fields == set(_parameters(rootsign.verify_session_local)) | set(
        _parameters(rootsign.verify_session)
    )


def test_verify_result_fields_listed():
    listed = re.search(
        r"\*\*Returns\*\* a `VerifyResult`: (.+?)\. ", _section("## Verify a session")
    )
    names = set(re.findall(r"`(\w+)`", listed.group(1))) - {"VALID", "TAMPERED", "INCOMPLETE"}
    real = {n for n in dir(rootsign.VerifyResult) if not n.startswith("_")}
    real |= set(getattr(rootsign.VerifyResult, "__dataclass_fields__", {}))
    assert names <= real
    assert {"verdict", "valid", "record_count", "error", "summary"} <= names


# ---------------------------------------------------------------------------
# Every export is covered
# ---------------------------------------------------------------------------


def test_every_export_is_documented_here_or_on_the_errors_page():
    here = page_text(PAGE)
    errors_page = page_text("reference/errors.mdx")
    missing = [
        name
        for name in rootsign.__all__
        if f"`{name}`" not in here
        and f"`{name}()`" not in here
        and f"rootsign.{name}()" not in here
        and f"### {name}" not in errors_page
        and f"`{name}`" not in errors_page
    ]
    assert missing == []


def test_the_beyond_table_names_real_exports():
    table = _section("## Beyond the core calls")
    for name in re.findall(r"`(\w+)(?:\(\))?`", table.split("| --- | --- |")[1]):
        assert hasattr(rootsign, name), name


# ---------------------------------------------------------------------------
# The Errors page's session-open message
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root writes to read-only folders, so the failure cannot be simulated",
)
def test_the_session_open_message_on_the_errors_page(tmp_path):
    data = tmp_path / "data"
    data.mkdir(mode=0o500)
    source = textwrap.dedent(
        """
        import asyncio
        import rootsign

        rootsign.init(agent="open-check")


        async def main() -> None:
            try:
                async with rootsign.session(objective="x"):
                    pass
            except rootsign.RootSignError as error:
                print(error)


        asyncio.run(main())
        """
    )
    run = run_script(source, tmp_path, extra_env={"ROOTSIGN_DATA_DIR": str(data)})

    (expected,) = output_after("reference/errors.mdx", "Nothing in the `async with` block runs.")
    assert run.stdout.strip() == expected.replace("<data_dir>", str(data))
