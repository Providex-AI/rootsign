"""`rootsign verify --local`: what it says when there is nothing to verify.

Exit codes 0/1/2 are verdicts (VALID / TAMPERED / INCOMPLETE). Pointing the
command at the wrong thing is not a verdict, so it exits 3 with one line:

* a path that does not exist (was a traceback and exit 1, i.e. "TAMPERED"),
* a directory,
* an empty file (was "TAMPERED — chain broken at record #None"),
* no arguments at all.

`--verbose` lists each record and the hashes that link it; it used to print
"(per-record detail not yet implemented for DB path)" even on the local path.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from rootsign.sdk.cli import EXIT_CANNOT_VERIFY, app
from tests.support.session_files import damage_action, drop_action, write_session_file

runner = CliRunner()


def _verify(*args: str):
    return runner.invoke(app, ["verify", *args])


def test_exit_3_is_not_a_verdict_code():
    from rootsign.verdict import EXIT_CODES

    assert EXIT_CANNOT_VERIFY == 3
    assert EXIT_CANNOT_VERIFY not in EXIT_CODES.values()


def _file(tmp, name: str, content: str):
    path = tmp / name
    path.write_text(content)
    return path


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda tmp: tmp / "missing.jsonl", "no session file at"),
        (lambda tmp: tmp, "no session file at"),
        (lambda tmp: _file(tmp, "empty.jsonl", ""), "is empty"),
        (lambda tmp: _file(tmp, "blank.jsonl", "\n \n"), "is empty"),
    ],
    ids=["missing", "directory", "empty", "blank-lines"],
)
def test_nothing_to_verify_exits_3_with_one_line(tmp_path, make, message):
    result = _verify("--local", str(make(tmp_path)))

    assert result.exit_code == 3, result.output
    assert message in result.output
    assert "Traceback" not in result.output
    assert "TAMPERED" not in result.output and "VALID" not in result.output


def test_no_arguments_exits_3():
    result = _verify()

    assert result.exit_code == 3
    assert "provide a session_id or --local path" in result.output


def test_verdict_codes_are_unchanged(tmp_path):
    valid = write_session_file(tmp_path / "valid", actions=3)
    tampered = write_session_file(tmp_path / "tampered", actions=3)
    damage_action(tampered, 2, "tool_name", "something_else")
    incomplete = write_session_file(tmp_path / "incomplete", actions=3)
    drop_action(incomplete, 2)

    assert _verify("--local", str(valid)).exit_code == 0
    assert _verify("--local", str(tampered)).exit_code == 1
    assert _verify("--local", str(incomplete)).exit_code == 2


def test_verbose_lists_each_record_and_its_link(tmp_path):
    import json

    session = write_session_file(tmp_path / "ok", actions=2)
    actions = [
        r
        for r in (json.loads(line) for line in session.read_text().splitlines() if line.strip())
        if r.get("event_type") == "ACTION_RECORD"
    ]

    result = _verify("--local", str(session), "--verbose")

    assert result.exit_code == 0, result.output
    assert "not yet implemented" not in result.output
    assert "prev_action_hash  (none: first record)" in result.output
    for record in actions:
        assert f"#{record['sequence_number']}  {record['tool_name']}" in result.output
        assert record["self_hash"] in result.output
    assert f"prev_action_hash  {actions[0]['self_hash']}" in result.output


@pytest.mark.parametrize(
    "argv",
    [["verify", "--lcal", "x.jsonl"], ["verfy"], ["--bogus"]],
    ids=["mistyped-option", "mistyped-command", "unknown-top-level-option"],
)
def test_usage_errors_exit_3_not_click_default_2(argv):
    """Click exits 2 on a usage error, which is verify's INCOMPLETE code."""
    result = runner.invoke(app, argv)

    assert result.exit_code == 3, result.output


def test_help_still_exits_0():
    assert runner.invoke(app, ["verify", "--help"]).exit_code == 0
