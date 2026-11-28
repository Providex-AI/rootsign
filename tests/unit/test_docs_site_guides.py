"""The "Verify a session" and "Export an evidence bundle" guides stay true.

Both pages show real CLI output captured from one session. These tests make a
fresh session with the quickstart's own `agent.py`, then check every output
block, the CI script and the Python snippet on `guides/verify.mdx`, and every
claim `guides/evidence-bundles.mdx` makes about export, `--check`, the forged-
manifest case, `--redact-previews`, `--format` and `--force`. IDs and hashes
captured on the page are swapped for the ones this run produced.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rootsign.sdk.cli import app
from tests.support.docs_pages import (
    assert_lines_match,
    code_block,
    output_after,
    page_text,
    run_script,
)

VERIFY = "guides/verify.mdx"
BUNDLES = "guides/evidence-bundles.mdx"
# The session ID and record hashes the pages' sample output was captured with.
PAGE_SESSION_ID = "f7feef27-a9ff-452b-a9a7-a373219914ea"
PAGE_HASHES = (
    "3e4497b7b6ead18c78760287b25cfac9952e73ed967174f3cc6473656d0345bf",
    "f0f346e8c4bd21fe64a988811626f1099389e8581c3c4fd055fccaf8d82eaf3d",
)

runner = CliRunner()


@pytest.fixture
def session(tmp_path) -> Path:
    """A two-action session recorded by the quickstart's agent.py."""
    run = run_script(code_block("quickstart.mdx", "agent.py"), tmp_path, name="agent.py")
    return run.only_session()


def _actions(path: Path) -> list[dict]:
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in records if r["event_type"] == "ACTION_RECORD"]


def _tampered(session: Path, tmp_path: Path) -> Path:
    lines = session.read_text().splitlines(keepends=True)
    lines[1] = lines[1].replace("1500.0", "15000.0")
    out = tmp_path / "tampered.jsonl"
    out.write_text("".join(lines))
    return out


def _gap(session: Path, tmp_path: Path) -> Path:
    lines = session.read_text().splitlines(keepends=True)
    del lines[1]
    out = tmp_path / "gap.jsonl"
    out.write_text("".join(lines))
    return out


def _block(page: str, title: str) -> list[str]:
    return [
        line.strip()
        for line in code_block(page, title, language="text").splitlines()
        if line.strip()
    ]


def _manifest_hash(output: str) -> str:
    match = re.search(r"manifest\.json SHA-256:\s+([0-9a-f]{64})", output)
    assert match, output
    return match.group(1)


# --- guides/verify.mdx -------------------------------------------------------


class TestVerifyGuide:
    def test_the_valid_output(self, session):
        result = runner.invoke(app, ["verify", "--local", str(session)])

        assert result.exit_code == 0
        assert_lines_match(
            result.output,
            output_after(VERIFY, "## Verify one session"),
            {PAGE_SESSION_ID: session.stem},
        )

    @pytest.mark.parametrize(
        ("title", "make", "code"),
        [
            ("TAMPERED (exit 1)", _tampered, 1),
            ("INCOMPLETE (exit 2)", _gap, 2),
        ],
    )
    def test_each_failure_looks_as_shown(self, session, tmp_path, title, make, code):
        result = runner.invoke(app, ["verify", "--local", str(make(session, tmp_path))])

        assert result.exit_code == code
        assert_lines_match(result.output, _block(VERIFY, title), {PAGE_SESSION_ID: session.stem})

    def test_nothing_to_verify(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["verify", "--local", "missing.jsonl"])

        assert result.exit_code == 3
        assert_lines_match(result.output, _block(VERIFY, "Nothing to verify (exit 3)"), {})

    def test_verbose_lists_each_record(self, session):
        actions = _actions(session)
        result = runner.invoke(app, ["verify", "--local", str(session), "--verbose"])

        assert result.exit_code == 0
        assert_lines_match(
            result.output,
            output_after(VERIFY, "--verbose\n"),
            {
                PAGE_SESSION_ID: session.stem,
                PAGE_HASHES[0]: actions[0]["self_hash"],
                PAGE_HASHES[1]: actions[1]["self_hash"],
            },
        )

    def test_verify_all_script(self, session, tmp_path):
        data_dir = session.parent.parent
        script = tmp_path / "verify-all.sh"
        script.write_text(code_block(VERIFY, "verify-all.sh", language="bash"))
        env = {
            **os.environ,
            "ROOTSIGN_DATA_DIR": str(data_dir),
            "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
        }

        clean = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
        assert clean.returncode == 0 and clean.stdout == "", clean.stdout + clean.stderr

        bad = _tampered(session, tmp_path)
        shutil.move(bad, data_dir / "sessions" / "f0000000-0000-0000-0000-000000000000.jsonl")
        failed = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)

        assert failed.returncode == 1
        assert_lines_match(
            failed.stdout,
            output_after(VERIFY, "exits `1`"),
            {"<data_dir>": str(data_dir), "<session_id>": "f0000000-0000-0000-0000-000000000000"},
        )

    def test_check_local_py(self, session, tmp_path):
        run_dir = tmp_path / "py"
        run_dir.mkdir()
        (run_dir / "check_local.py").write_text(code_block(VERIFY, "check_local.py"))
        result = subprocess.run(
            [sys.executable, "check_local.py", str(session)],
            cwd=run_dir,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert_lines_match(result.stdout, output_after(VERIFY, "python check_local.py"), {})

    def test_check_db_py_compiles(self):
        compile(code_block(VERIFY, "check_db.py"), "check_db.py", "exec")

    def test_python_caveats_hold(self, tmp_path):
        from rootsign import verify_session_local

        empty = tmp_path / "empty.jsonl"
        empty.write_text("")
        assert verify_session_local(str(empty)).verdict.value == "TAMPERED"
        with pytest.raises(FileNotFoundError):
            verify_session_local(str(tmp_path / "missing.jsonl"))


# --- guides/evidence-bundles.mdx ----------------------------------------------


@pytest.fixture
def exported(session, tmp_path, monkeypatch):
    """Export the session as the page does; return (bundle dir, CLI output)."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["export", "--local", str(session), "--out", "./bundles"])
    assert result.exit_code == 0, result.output
    return tmp_path / "bundles" / f"evidence-{session.stem}", result.output


class TestEvidenceBundleGuide:
    def test_the_export_output(self, session, exported):
        bundle, output = exported
        page_hash = _first_hash(BUNDLES)

        assert_lines_match(
            output,
            output_after(BUNDLES, "## Export a session"),
            {PAGE_SESSION_ID: session.stem, page_hash: _manifest_hash(output)},
        )

    def test_the_bundle_holds_the_listed_files(self, exported):
        bundle, _ = exported
        listed = set(re.findall(r"^\| `([a-z.]+\.(?:json|md|html))`", page_text(BUNDLES), re.M))
        listed |= {"report.md", "report.html"}

        assert {p.name for p in bundle.iterdir()} == listed

    def test_the_manifest_records_the_source_path(self, session, exported):
        bundle, _ = exported
        manifest = json.loads((bundle / "manifest.json").read_text())

        assert manifest["source"]["location"] == str(session)

    def test_each_export_has_its_own_manifest_hash(self, session, exported, tmp_path):
        _, first = exported
        again = runner.invoke(app, ["export", "--local", str(session), "--out", "./again"])

        assert _manifest_hash(again.output) != _manifest_hash(first)

    def test_check_intact(self, exported):
        bundle, output = exported
        result = runner.invoke(app, ["export", "--check", str(bundle)])

        assert result.exit_code == 0
        assert_lines_match(
            result.output,
            output_after(BUNDLES, "rootsign export --check ./bundles"),
            {_first_hash(BUNDLES): _manifest_hash(output)},
        )

    def test_check_altered(self, exported, tmp_path):
        bundle, output = exported
        copy = tmp_path / "altered"
        shutil.copytree(bundle, copy)
        timeline = copy / "timeline.json"
        timeline.write_text(timeline.read_text().replace("acme-corp", "evil-corp"))

        result = runner.invoke(app, ["export", "--check", str(copy)])

        assert result.exit_code == 1
        page_hash = re.search(r"ALTERED.*?SHA-256:\s+([0-9a-f]{64})", page_text(BUNDLES), re.S)
        assert_lines_match(
            result.output,
            output_after(BUNDLES, "names it and exits `1`"),
            {page_hash.group(1): _manifest_hash(output)},
        )

    def test_a_forged_manifest_passes_files_but_not_the_hash(self, exported, tmp_path):
        """The page's warning: INTACT alone does not prove the bundle is genuine."""
        bundle, output = exported
        forged = tmp_path / "forged"
        shutil.copytree(bundle, forged)
        timeline = forged / "timeline.json"
        timeline.write_text(timeline.read_text().replace("acme-corp", "evil-corp"))
        manifest = json.loads((forged / "manifest.json").read_text())
        manifest["files"]["timeline.json"] = hashlib.sha256(timeline.read_bytes()).hexdigest()
        (forged / "manifest.json").write_text(json.dumps(manifest, indent=2))

        result = runner.invoke(app, ["export", "--check", str(forged)])

        assert result.exit_code == 0 and "INTACT" in result.output
        assert _manifest_hash(result.output) != _manifest_hash(output)

    def test_redact_previews_says_what_was_withheld(self, session, tmp_path):
        result = runner.invoke(
            app,
            [
                "export",
                "--local",
                str(session),
                "--out",
                str(tmp_path / "out"),
                "--redact-previews",
            ],
        )
        assert result.exit_code == 0
        report = (tmp_path / "out" / f"evidence-{session.stem}" / "report.md").read_text()

        for line in code_block(BUNDLES, "report.md", language="markdown").splitlines():
            if line.strip():
                assert line.strip() in report

    def test_format_json_writes_only_json(self, session, tmp_path):
        runner.invoke(
            app,
            ["export", "--local", str(session), "--out", str(tmp_path / "j"), "--format", "json"],
        )
        bundle = tmp_path / "j" / f"evidence-{session.stem}"

        assert {p.suffix for p in bundle.iterdir()} == {".json"}

    def test_existing_folder_needs_force(self, session, exported):
        again = runner.invoke(app, ["export", "--local", str(session), "--out", "./bundles"])
        assert again.exit_code == 1 and "Error:" in again.output

        forced = runner.invoke(
            app, ["export", "--local", str(session), "--out", "./bundles", "--force"]
        )
        assert forced.exit_code == 0

    def test_a_tampered_session_still_exports_with_exit_0(self, session, tmp_path):
        bad = _tampered(session, tmp_path)
        result = runner.invoke(app, ["export", "--local", str(bad), "--out", str(tmp_path / "t")])

        assert result.exit_code == 0
        assert result.output.startswith("TAMPERED")
        manifest = json.loads(
            (tmp_path / "t" / f"evidence-{session.stem}" / "manifest.json").read_text()
        )
        assert manifest["verdict"] == "TAMPERED"

    def test_error_cases(self, tmp_path):
        missing = runner.invoke(app, ["export", "--local", str(tmp_path / "nope.jsonl")])
        assert missing.exit_code == 1 and missing.output.startswith("Error:")

        unreadable = runner.invoke(app, ["export", "--check", str(tmp_path / "nodir")])
        assert unreadable.exit_code == 1 and "UNREADABLE" in unreadable.output


def _first_hash(page: str) -> str:
    """The first manifest hash the page shows: the one from its export step."""
    return re.search(r"manifest\.json SHA-256:\s+([0-9a-f]{64})", page_text(page)).group(1)


class TestTheSeal:
    """The "Check the seal" section of the verify guide (ADR-016)."""

    def _edited(self, session: Path, tmp_path: Path, change) -> Path:
        records = [json.loads(line) for line in session.read_text().splitlines() if line.strip()]
        out = tmp_path / "edited.jsonl"
        out.write_text("".join(json.dumps(r) + "\n" for r in change(records)))
        return out

    def _check(self, path: Path, title: str, code: int, *flags: str) -> None:
        result = runner.invoke(app, ["verify", "--local", str(path), *flags])
        assert result.exit_code == code, result.output
        session_id = json.loads(path.read_text().splitlines()[0])["session_id"]
        assert_lines_match(result.output, _block(VERIFY, title), {PAGE_SESSION_ID: session_id})

    def test_records_cut_from_the_end(self, session, tmp_path):
        def drop_last_action(records):
            return [r for r in records if r.get("sequence_number") != 2]

        edited = self._edited(session, tmp_path, drop_last_action)
        self._check(edited, "Records cut from the end (exit 2)", 2)

    def test_an_edited_objective(self, session, tmp_path):
        def edit(records):
            for r in records:
                if r["event_type"] == "SESSION_OPEN":
                    r["payload"]["objective"] = "invoice someone else"
            return records

        self._check(self._edited(session, tmp_path, edit), "An edited objective (exit 1)", 1)

    def test_no_seal_with_require_seal(self, session, tmp_path):
        def unseal(records):
            for r in records:
                if r["event_type"] == "SESSION_CLOSE":
                    r["payload"].pop("seal")
            return records

        edited = self._edited(session, tmp_path, unseal)
        self._check(edited, "No seal, with --require-seal (exit 2)", 2, "--require-seal")
        assert "rootsign verify --local ~/.rootsign/sessions/<session_id>.jsonl --require-seal" in (
            page_text(VERIFY)
        )
