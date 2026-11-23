"""Every SDK setting is read at the same moment: when `rootsign.init()` runs.

`ROOTSIGN_CAPTURE_DECISIONS` and `ROOTSIGN_REASONING_DEPTH` used to be read
once, when `rootsign` was imported, while every other setting was read by
`init()`. A script that set them in code after `import rootsign` (a notebook,
a test, a launcher) silently recorded no decisions: `record_decision()`
returned None and no DECISION_RECORD was written, while `ROOTSIGN_DATA_DIR`
set on the line next to it was honoured.

Each case runs as its own Python process, so `import rootsign` happens exactly
once, the way it does for a user.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

from tests.support.docs_pages import run_script

AGENT = textwrap.dedent(
    """
    import asyncio
    import os

    import rootsign

    os.environ.update({env})
    rootsign.init(agent="settings-timing")
    os.environ.update({after_init})


    @rootsign.trace()
    async def lookup(q: str) -> str:
        return q.upper()


    async def main() -> None:
        async with rootsign.session(objective="settings timing") as ctx:
            decision_id = await ctx.record_decision(
                selected_action="lookup", reasoning_summary="because", confidence=0.9
            )
            await lookup(q="x")
        print(decision_id)


    asyncio.run(main())
    """
)


def _decisions(tmp_path, env: dict[str, str], after_init: dict[str, str] | None = None):
    source = AGENT.format(env=repr(env), after_init=repr(after_init or {}))
    run = run_script(source, tmp_path, name="agent.py")
    lines = run.only_session().read_text().splitlines()
    records = [json.loads(line) for line in lines if line.strip()]
    return run.stdout.strip(), [r for r in records if r["event_type"] == "DECISION_RECORD"]


def test_capture_set_in_code_after_import_is_honoured(tmp_path):
    printed, decisions = _decisions(tmp_path, {"ROOTSIGN_CAPTURE_DECISIONS": "true"})

    assert printed != "None"
    assert len(decisions) == 1


def test_reasoning_depth_set_in_code_after_import_is_honoured(tmp_path):
    _, (decision,) = _decisions(
        tmp_path,
        {"ROOTSIGN_CAPTURE_DECISIONS": "true", "ROOTSIGN_REASONING_DEPTH": "minimal"},
    )

    assert decision["payload"]["reasoning_summary"] is None


def test_init_fixes_the_settings_like_every_other_one(tmp_path):
    """Changing the variable after init() changes nothing, exactly as for
    ROOTSIGN_BACKEND or ROOTSIGN_DATA_DIR."""
    printed, decisions = _decisions(
        tmp_path,
        {"ROOTSIGN_CAPTURE_DECISIONS": "true"},
        after_init={"ROOTSIGN_CAPTURE_DECISIONS": "false"},
    )

    assert printed != "None"
    assert len(decisions) == 1


def test_capture_stays_off_by_default(tmp_path):
    printed, decisions = _decisions(tmp_path, {})

    assert printed == "None"
    assert decisions == []


def test_the_local_alias_warning_is_shown_by_default(tmp_path):
    """DeprecationWarning is hidden outside __main__ by Python's default
    filters, so nobody using the alias ever saw it."""
    env = {**os.environ, "ROOTSIGN_BACKEND": "local"}
    env.pop("PYTHONWARNINGS", None)
    result = subprocess.run(
        [sys.executable, "-c", "from rootsign.sdk.config import SDKSettings; SDKSettings()"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "ROOTSIGN_BACKEND=local is deprecated; use 'postgres'" in result.stderr


# The session's client used to be built from settings read when the session
# opened, while the agent registry used the ones init() read. A data folder
# changed after init() split one run across two folders: agents.jsonl in the
# old one, the session file in the new one.
CLIENT = textwrap.dedent(
    """
    import asyncio
    import os

    import rootsign
    from rootsign.sdk.facade import current_session

    rootsign.init(agent="settings-timing")
    os.environ.update({after_init})


    @rootsign.trace()
    async def lookup(q: str) -> str:
        return q.upper()


    async def main() -> None:
        async with rootsign.session(objective="settings timing"):
            _, client = current_session()
            print(type(client).__name__)
            await lookup(q="x")


    asyncio.run(main())
    """
)


def _client_run(tmp_path, after_init: dict[str, str]):
    return run_script(CLIENT.format(after_init=repr(after_init)), tmp_path, name="agent.py")


def test_data_dir_changed_after_init_is_ignored(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    run = _client_run(tmp_path, {"ROOTSIGN_DATA_DIR": str(elsewhere)})

    assert len(run.actions()) == 1  # the session file is in the init() folder
    assert (run.data_dir / "agents.jsonl").exists()
    assert not elsewhere.exists()


def test_buffering_changed_after_init_is_ignored(tmp_path):
    run = _client_run(tmp_path, {"ROOTSIGN_BUFFERED": "true"})

    assert run.stdout.strip() == "JsonlIngestClient"


def test_backend_changed_after_init_is_ignored(tmp_path):
    run = _client_run(tmp_path, {"ROOTSIGN_BACKEND": "cloud", "ROOTSIGN_API_KEY": "key"})

    assert run.stdout.strip() == "JsonlIngestClient"
    assert len(run.actions()) == 1


def test_buffering_set_before_init_still_applies(tmp_path):
    run = run_script(
        CLIENT.format(after_init="{}"),
        tmp_path,
        name="agent.py",
        extra_env={"ROOTSIGN_BUFFERED": "true"},
    )

    assert run.stdout.strip() == "BufferedIngestClient"
    assert len(run.actions()) == 1
