"""Writing a bundle and checking one that arrived (Sprint B T3.3, ADR-014).

A bundle travels: it is emailed, dropped on a file share, attached to a
ticket. So the interesting tests are not "does the writer write" but "what can
a recipient actually prove", and the honest answer has two halves that this
file keeps carefully apart.

**What `--check` proves:** every file listed in `manifest.json` still hashes to
the value the manifest records, nothing listed is missing, and nothing is
present that the manifest does not name. That is internal consistency.

**What `--check` cannot prove:** that this is the bundle that was generated.
An attacker who edits `timeline.json` *and* updates the manifest passes every
per-file check. `test_an_attacker_who_updates_the_manifest_passes_the_file_check`
is that attack, run for real — it passes the file check, and the manifest hash
moves. Which is why the hash is printed whether or not anything is wrong, and
why the docs tell a recipient to compare it against the value noted out of band
at export time. Without that comparison the check is a formality.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rootsign.sdk.export import (
    MANIFEST_FILE,
    BundleExists,
    check_bundle,
    export_local,
    sha256_text,
    write_bundle,
)
from rootsign.sdk.report import HTML_FILE, MARKDOWN_FILE, attach_reports
from tests.support.session_files import write_session_file


@pytest.fixture
def bundle(tmp_path: Path):
    return attach_reports(export_local(write_session_file(tmp_path / "src", actions=2)))


@pytest.fixture
def written(tmp_path: Path, bundle) -> Path:
    return write_bundle(bundle, tmp_path / "out")


class TestWriting:
    def test_the_bundle_lands_as_one_self_contained_directory(self, written, bundle):
        assert written.name == f"evidence-{bundle.session_id}"
        assert {p.name for p in written.iterdir()} == {
            MANIFEST_FILE,
            "verification.json",
            "timeline.json",
            "redaction.json",
            MARKDOWN_FILE,
            HTML_FILE,
        }

    def test_the_manifest_on_disk_hashes_to_the_anchor_that_was_printed(self, written, bundle):
        """The one equality the whole scheme rests on.

        The digest an auditor writes down at export has to be the digest of the
        bytes they later receive. If the writer serialized the manifest even
        slightly differently from `manifest_hash`, the anchor would be a value
        nobody could ever reproduce — and every check would fail for a reason
        that has nothing to do with tampering.
        """
        on_disk = (written / MANIFEST_FILE).read_text()

        assert sha256_text(on_disk) == bundle.manifest_hash
        assert json.loads(on_disk) == bundle.manifest

    def test_writing_over_an_existing_bundle_is_refused(self, tmp_path, bundle):
        """Evidence directories get re-exported into by accident. A
        half-replaced bundle whose manifest describes the previous run is worse
        than either version alone."""
        write_bundle(bundle, tmp_path / "out")

        with pytest.raises(BundleExists, match="already exists"):
            write_bundle(bundle, tmp_path / "out")

        assert write_bundle(bundle, tmp_path / "out", overwrite=True).exists()


class TestCheckingAnIntactBundle:
    def test_an_untouched_bundle_reports_intact(self, written):
        result = check_bundle(written)

        assert result.intact is True
        assert result.summary.startswith("INTACT")
        assert set(result.verified) == {
            "verification.json",
            "timeline.json",
            "redaction.json",
            MARKDOWN_FILE,
            HTML_FILE,
        }
        assert result.altered == result.missing == result.unexpected == []

    def test_the_manifest_hash_is_reported_even_when_nothing_is_wrong(self, written, bundle):
        """It is the only value a recipient can compare against something the
        bundle did not travel with. Printing it only on failure would mean the
        real check is available exactly when it is least useful."""
        assert check_bundle(written).manifest_hash == bundle.manifest_hash


class TestCheckingATamperedBundle:
    def test_an_edited_file_is_named(self, written):
        target = written / "timeline.json"
        target.write_text(target.read_text().replace("send_email", "refund_card"))

        result = check_bundle(written)

        assert result.intact is False
        assert result.altered == ["timeline.json"]
        assert "timeline.json" in result.summary
        # The others are still fine — the report says which file to look at.
        assert "verification.json" in result.verified

    def test_a_deleted_file_is_named(self, written):
        (written / MARKDOWN_FILE).unlink()

        result = check_bundle(written)

        assert result.intact is False
        assert result.missing == [MARKDOWN_FILE]

    def test_a_file_nobody_listed_is_flagged(self, written):
        """The gap a naive check leaves.

        Re-hashing what the manifest names says nothing about what else is in
        the directory, so an added `addendum.pdf` would ride along looking like
        part of the evidence.
        """
        (written / "addendum.pdf").write_text("please disregard the third action")

        result = check_bundle(written)

        assert result.intact is False
        assert result.unexpected == ["addendum.pdf"]
        assert "unexpected" in result.summary

    def test_an_attacker_who_updates_the_manifest_passes_the_file_check(self, written, bundle):
        """The attack the printed hash exists for — run for real.

        Edit a document, recompute its digest, write it back into the manifest.
        Every per-file check now passes, because the bundle *is* internally
        consistent: it is a coherent bundle describing a session that did not
        happen. Only the manifest's own hash moves, and only someone holding
        the value from export time can see that it moved.
        """
        forged = (written / "timeline.json").read_text().replace("send_email", "refund_card")
        (written / "timeline.json").write_text(forged)

        manifest = json.loads((written / MANIFEST_FILE).read_text())
        manifest["files"]["timeline.json"] = sha256_text(forged)
        (written / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2) + "\n")

        result = check_bundle(written)

        assert result.altered == []
        assert result.intact is True, "the file check cannot see this, by construction"
        assert result.manifest_hash != bundle.manifest_hash, (
            "the anchor did not move — the attack would be undetectable"
        )

    def test_every_kind_of_damage_is_reported_at_once(self, written):
        """One run, one list. An auditor should not have to fix and re-run to
        discover the next problem."""
        (written / "timeline.json").write_text("{}")
        (written / MARKDOWN_FILE).unlink()
        (written / "extra.txt").write_text("hello")

        result = check_bundle(written)

        assert result.altered == ["timeline.json"]
        assert result.missing == [MARKDOWN_FILE]
        assert result.unexpected == ["extra.txt"]
        assert result.summary.startswith("ALTERED")


class TestCheckingSomethingThatIsNotABundle:
    def test_a_directory_without_a_manifest_says_so(self, tmp_path):
        (tmp_path / "loose").mkdir()

        result = check_bundle(tmp_path / "loose")

        assert result.intact is False
        assert result.manifest_hash is None
        assert "no manifest.json" in result.error
        assert result.summary.startswith("UNREADABLE")

    def test_a_corrupt_manifest_is_reported_not_raised(self, written):
        """`--check` runs on files someone else sent. Every malformed thing it
        can be handed has to come back as a result, not a traceback."""
        (written / MANIFEST_FILE).write_text("{not json")

        result = check_bundle(written)

        assert result.intact is False
        assert "not a readable bundle manifest" in result.error
        # The hash is still computed: it is a fact about the bytes received,
        # and it is the value worth comparing even when nothing else parses.
        assert result.manifest_hash == sha256_text("{not json")

    def test_a_file_that_is_not_text_is_checked_like_any_other(self, written):
        """`--check` reads what someone else sent, and that includes bytes that
        are not UTF-8. A binary file has to be hashed and reported, not crash
        the check from the middle of a loop."""
        (written / "scan.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe not text")
        (written / "timeline.json").write_bytes(b"\xff\xfe not text either")

        result = check_bundle(written)

        assert result.unexpected == ["scan.png"]
        assert result.altered == ["timeline.json"]
        assert result.intact is False

    def test_a_manifest_without_a_files_block_is_reported(self, written):
        (written / MANIFEST_FILE).write_text(json.dumps({"bundle_version": "1.0"}) + "\n")

        result = check_bundle(written)

        assert result.intact is False
        assert "not a readable bundle manifest" in result.error
