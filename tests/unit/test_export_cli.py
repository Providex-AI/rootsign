"""`rootsign export` on the developer CLI (Sprint B T3.5, ADR-014 Decision 1).

The command is the only part of the export story a user ever touches, so these
tests are about the contract it offers rather than the assembly underneath
(that is T3.1–T3.4). Three things it has to get right:

* **The anchor reaches the human.** The manifest hash is printed on every
  successful export, with the instruction to record it outside the bundle. A
  bundle whose anchor stayed in the process is a bundle nobody can later prove
  anything about.
* **`--local` needs no database.** It is the bare-install path and the only way
  a cloud-mode user exports a spooled session, so its import graph must stay
  DB-free (the packaging contract test proves the graph; this proves the
  behavior).
* **Failures are one actionable line.** Every way a user can point this command
  at the wrong thing — bad UUID, missing file, unknown session, existing
  directory — ends in a sentence and exit 1, never a traceback.

The Postgres-backed path lives in `tests/integration/test_export_cli_postgres.py`
because it needs a database; everything here runs on files.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rootsign.sdk.cli import app
from rootsign.sdk.export import MANIFEST_FILE, check_bundle, export_local, write_bundle
from rootsign.sdk.report import HTML_FILE, MARKDOWN_FILE, attach_reports
from tests.support.session_files import damage_action, drop_action, write_session_file

runner = CliRunner()


@pytest.fixture
def session_file(tmp_path: Path) -> Path:
    return write_session_file(tmp_path / "src", actions=3, approval=True, decision=True)


def _export(session_file: Path, out: Path, *args: str):
    return runner.invoke(app, ["export", "--local", str(session_file), "--out", str(out), *args])


def _bundle_dir(out: Path) -> Path:
    return next(out.glob("evidence-*"))


class TestExportingLocally:
    def test_a_bundle_is_written_and_the_anchor_is_printed(self, session_file, tmp_path):
        result = _export(session_file, tmp_path / "out")

        assert result.exit_code == 0, result.output
        directory = _bundle_dir(tmp_path / "out")
        printed = check_bundle(directory).manifest_hash

        assert printed in result.output, "the manifest hash never reached the operator"
        assert "Record that hash outside the bundle" in result.output
        assert (directory / MANIFEST_FILE).is_file()

    def test_the_verdict_leads_the_output(self, session_file, tmp_path):
        """Same rule as the report: the answer comes before the inventory."""
        result = _export(session_file, tmp_path / "out")

        assert result.output.splitlines()[0].startswith("VALID")

    @pytest.mark.parametrize(
        ("damage", "verdict"), [("tamper", "TAMPERED"), ("drop", "INCOMPLETE")]
    )
    def test_a_damaged_session_still_exports_and_says_so(self, tmp_path, damage: str, verdict: str):
        """Exporting a broken chain is not an error — it is the case the bundle
        matters most for. The verdict is the finding; `rootsign verify` is where
        a script asks for a pass/fail exit code.
        """
        path = write_session_file(tmp_path / "src", actions=4, previews=False)
        if damage == "tamper":
            damage_action(path, 2, "tool_name", "SOMETHING_ELSE")
        else:
            drop_action(path, 2)

        result = _export(path, tmp_path / "out")

        assert result.exit_code == 0, result.output
        assert result.output.splitlines()[0].startswith(verdict)
        verification = json.loads((_bundle_dir(tmp_path / "out") / "verification.json").read_text())
        assert verification["verdict"] == verdict

    @pytest.mark.parametrize(
        ("fmt", "expected"),
        [
            ("all", {MARKDOWN_FILE, HTML_FILE}),
            ("md", {MARKDOWN_FILE}),
            ("html", {HTML_FILE}),
            ("json", set()),
        ],
    )
    def test_format_selects_the_renderings_never_the_evidence(
        self, session_file, tmp_path, fmt: str, expected: set[str]
    ):
        """`--format` drops human renderings only. A bundle without
        `verification.json` would be a report, not evidence."""
        result = _export(session_file, tmp_path / "out", "--format", fmt)

        assert result.exit_code == 0, result.output
        present = {p.name for p in _bundle_dir(tmp_path / "out").iterdir()}

        assert present & {MARKDOWN_FILE, HTML_FILE} == expected
        assert {"verification.json", "timeline.json", "redaction.json", MANIFEST_FILE} <= present

    def test_redact_previews_reaches_the_bundle(self, session_file, tmp_path):
        result = _export(session_file, tmp_path / "out", "--redact-previews")

        assert result.exit_code == 0, result.output
        timeline = json.loads((_bundle_dir(tmp_path / "out") / "timeline.json").read_text())

        assert timeline["previews"]["included"] is False
        assert "eu-west-1" not in (_bundle_dir(tmp_path / "out") / MARKDOWN_FILE).read_text()

    def test_a_written_bundle_passes_its_own_check(self, session_file, tmp_path):
        """The round trip a recipient performs, done against a bundle this
        command wrote — the two halves have to agree about serialization or the
        anchor is unusable."""
        _export(session_file, tmp_path / "out")

        result = runner.invoke(app, ["export", "--check", str(_bundle_dir(tmp_path / "out"))])

        assert result.exit_code == 0, result.output
        assert "INTACT" in result.output


class TestChecking:
    def test_a_tampered_bundle_exits_one_and_names_the_file(self, session_file, tmp_path):
        _export(session_file, tmp_path / "out")
        directory = _bundle_dir(tmp_path / "out")
        target = directory / "timeline.json"
        target.write_text(target.read_text().replace("send_email", "refund_card"))

        result = runner.invoke(app, ["export", "--check", str(directory)])

        assert result.exit_code == 1
        assert "ALTERED" in result.output
        assert "timeline.json" in result.output

    def test_the_manifest_hash_is_printed_whether_or_not_anything_is_wrong(
        self, session_file, tmp_path
    ):
        """The check the file comparison cannot perform.

        An attacker who edits a file and updates the manifest passes every
        per-file check — so the value a recipient compares out of band has to be
        on screen in both outcomes, with the caveat next to it.
        """
        _export(session_file, tmp_path / "out")
        directory = _bundle_dir(tmp_path / "out")
        intact = runner.invoke(app, ["export", "--check", str(directory)])
        (directory / "extra.txt").write_text("hello")
        altered = runner.invoke(app, ["export", "--check", str(directory)])

        for result in (intact, altered):
            assert "manifest.json SHA-256:" in result.output
            assert "cannot detect an edit that also updated the manifest" in result.output
        assert "unexpected:  extra.txt" in altered.output

    def test_checking_something_that_is_not_a_bundle_is_one_clean_line(self, tmp_path):
        (tmp_path / "empty").mkdir()

        result = runner.invoke(app, ["export", "--check", str(tmp_path / "empty")])

        assert result.exit_code == 1
        assert "UNREADABLE" in result.output
        assert "Traceback" not in result.output


class TestBadInput:
    def test_no_target_asks_for_one(self):
        result = runner.invoke(app, ["export"])

        assert result.exit_code == 1
        assert "provide a session_id, --local <path>, or --check <dir>" in result.output

    def test_a_bad_uuid_is_rejected_before_anything_is_opened(self):
        result = runner.invoke(app, ["export", "not-a-uuid"])

        assert result.exit_code == 1
        assert "not a valid UUID" in result.output
        assert "Traceback" not in result.output

    def test_a_missing_session_file_says_which_path(self, tmp_path):
        result = runner.invoke(app, ["export", "--local", str(tmp_path / "nope.jsonl")])

        assert result.exit_code == 1
        assert "Session file not found" in result.output
        assert "Traceback" not in result.output

    def test_an_unknown_format_lists_the_valid_ones(self, session_file, tmp_path):
        result = _export(session_file, tmp_path / "out", "--format", "pdf")

        assert result.exit_code == 1
        assert "all, json, md, html" in result.output

    def test_exporting_over_an_existing_bundle_needs_force(self, session_file, tmp_path):
        """Refusing is the right default for a directory of evidence; `--force`
        is there for the operator who meant it."""
        assert _export(session_file, tmp_path / "out").exit_code == 0

        second = _export(session_file, tmp_path / "out")
        assert second.exit_code == 1
        assert "--force" in second.output

        assert _export(session_file, tmp_path / "out", "--force").exit_code == 0


class TestSpoolAndBareInstall:
    def test_a_spooled_session_exports_like_any_other(self, tmp_path):
        """The cloud-mode user's path: records that never reached the server
        still produce evidence someone can hand over (ADR-013 D4 / ADR-014 D1)."""
        spool = tmp_path / "spool"
        path = write_session_file(spool, actions=2, previews=False)

        result = _export(path, tmp_path / "out")

        assert result.exit_code == 0, result.output
        manifest = json.loads((_bundle_dir(tmp_path / "out") / MANIFEST_FILE).read_text())
        assert manifest["source"]["backend"] == "jsonl"
        assert str(spool) in manifest["source"]["location"]

    def test_the_local_path_never_touches_the_database(self, session_file, tmp_path, monkeypatch):
        """`--local` is the bare-install path. If it resolved a session factory
        it would die on a machine that has no `postgres` extra — the exact
        defect class the ADR-011 console-script rule exists to prevent."""

        def explode():
            raise AssertionError("--local resolved a database session factory")

        monkeypatch.setattr("rootsign.sdk.cli._session_factory", explode)

        assert _export(session_file, tmp_path / "out").exit_code == 0

    def test_check_never_touches_the_database_either(self, session_file, tmp_path, monkeypatch):
        bundle = attach_reports(export_local(session_file))
        directory = write_bundle(bundle, tmp_path / "out")

        def explode():
            raise AssertionError("--check resolved a database session factory")

        monkeypatch.setattr("rootsign.sdk.cli._session_factory", explode)

        assert runner.invoke(app, ["export", "--check", str(directory)]).exit_code == 0
