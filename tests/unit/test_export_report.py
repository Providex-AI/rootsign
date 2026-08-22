"""The rendered reports (Sprint B T3.2, ADR-014 Decisions 1 and 3).

A rendered report is what a compliance officer actually opens, so the tests
here are about what that person sees and in what order — and about the ways a
document meant to travel by email could betray the person reading it.

Four claims carry the weight:

* **The verdict is the first visible element** in both formats. Everything
  else in the bundle is context for it.
* **Both renderings are pure functions of the bundle's JSON.** The round-trip
  test serializes the documents and renders from the parsed copy, so a
  renderer that reached back into the store or the bundle object would produce
  different output and fail.
* **The HTML is inert and self-contained**: no scripts, no external requests,
  no framework. It has to render identically from a file share, an attachment,
  or an air-gapped machine.
* **Everything is escaped.** The content originates as data an agent handled —
  which is precisely the data an attacker chooses.
"""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path

import pytest

from rootsign.sdk.export import export_local
from rootsign.sdk.report import (
    HTML_FILE,
    MARKDOWN_FILE,
    attach_reports,
    bundle_documents,
    render_html,
    render_markdown,
)
from tests.support.session_files import damage_action, drop_action, write_session_file

VOID_TAGS = {"meta", "br", "hr", "img", "input", "link", "source"}


class _TagBalance(HTMLParser):
    """Enough of a parser to catch a template that lost a closing tag."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.unbalanced: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.unbalanced.append(tag)
        else:
            self.stack.pop()

    def handle_data(self, data):
        self.text.append(data)


def _parse(markup: str) -> _TagBalance:
    parser = _TagBalance()
    parser.feed(markup)
    return parser


@pytest.fixture
def documents(tmp_path: Path):
    return bundle_documents(export_local(write_session_file(tmp_path, actions=2)))


@pytest.fixture
def markdown(documents) -> str:
    return render_markdown(documents)


@pytest.fixture
def page(documents) -> str:
    return render_html(documents)


class TestVerdictComesFirst:
    def test_markdown_opens_with_the_verdict(self, markdown):
        """Not the title, not the session id — the answer."""
        assert markdown.splitlines()[0].startswith("# VALID")

    def test_html_shows_the_verdict_before_anything_else(self, page):
        """Whoever opens this needs to know whether the chain holds before
        they read a word of narrative. Burying it under an identity block
        would be a layout that flatters the product."""
        body = page[page.index("<body>") :]
        assert body.index("VALID") < body.index("Session")
        assert body.index('class="verdict') < body.index("<h2>")

    @pytest.mark.parametrize(
        ("damage", "verdict", "phrase"),
        [
            (None, "VALID", "chain is intact"),
            ("tamper", "TAMPERED", "was altered"),
            ("drop", "INCOMPLETE", "Records are missing"),
        ],
        ids=["valid", "tampered", "incomplete"],
    )
    def test_each_verdict_is_explained_in_an_auditors_terms(
        self, tmp_path, damage, verdict, phrase
    ):
        """The vocabulary is three-valued from bundle v1.0 (ADR-014 Decision 2),
        and each verdict means something different to the person holding the
        bundle: intact, altered, or incomplete. A renderer that mapped two of
        them to "invalid" would throw away the distinction the sprint added."""
        path = write_session_file(tmp_path, actions=4, previews=False)
        if damage == "tamper":
            damage_action(path, 2, "tool_name", "SOMETHING_ELSE")
        elif damage == "drop":
            drop_action(path, 2)

        docs = bundle_documents(export_local(path))
        markdown, page = render_markdown(docs), render_html(docs)

        assert markdown.splitlines()[0] == f"# {verdict} — RootSign evidence bundle"
        assert phrase in markdown
        assert phrase in page
        assert f'class="verdict {verdict.lower()}"' in page

    def test_an_incomplete_bundle_names_the_missing_range(self, tmp_path):
        path = write_session_file(tmp_path, actions=4, previews=False)
        drop_action(path, 2)

        docs = bundle_documents(export_local(path))

        assert "Missing sequence numbers: **2**" in render_markdown(docs)
        assert "Missing sequence numbers: <strong>2</strong>" in render_html(docs)


class TestRenderedFromTheJsonOnly:
    def test_a_round_trip_through_json_renders_identically(self, documents):
        """The structural version of "if it is not in the JSON it cannot be in
        the HTML". Serializing and re-parsing drops every Python object the
        renderer might have leaned on; identical output proves it leaned on
        none of them.
        """
        reloaded = json.loads(json.dumps(documents, default=str))

        assert render_markdown(reloaded) == render_markdown(documents)
        assert render_html(reloaded) == render_html(documents)

    def test_attaching_puts_both_reports_under_the_manifest(self, tmp_path):
        """A rendering is bundle content, so it answers to the same integrity
        claim as the JSON it was made from."""
        bundle = attach_reports(export_local(write_session_file(tmp_path, actions=1)))

        assert set(bundle.rendered) == {MARKDOWN_FILE, HTML_FILE}
        assert MARKDOWN_FILE in bundle.manifest["files"]
        assert HTML_FILE in bundle.manifest["files"]

    def test_the_json_documents_content_hashes_are_in_the_report(self, tmp_path):
        """A reader should not have to open `manifest.json` to see the
        fingerprints of the documents this report was rendered from."""
        bundle = attach_reports(export_local(write_session_file(tmp_path, actions=1)))
        markdown, page = bundle.rendered[MARKDOWN_FILE], bundle.rendered[HTML_FILE]

        for name in ("verification.json", "timeline.json", "redaction.json"):
            digest = bundle.manifest["files"][name]
            assert digest in markdown, name
            assert digest in page, name

    def test_the_report_cannot_and_does_not_quote_its_own_digest(self, tmp_path):
        """The one hash a document can never contain.

        `manifest.json` hashes the report, so a report quoting its own digest —
        or the manifest's, which is derived from it — would invalidate the value
        the moment it was written. Both live in `manifest.json`, and the report
        says so and points at the real check.
        """
        bundle = attach_reports(export_local(write_session_file(tmp_path, actions=1)))
        markdown = bundle.rendered[MARKDOWN_FILE]

        assert bundle.manifest_hash not in markdown
        assert bundle.manifest["files"][MARKDOWN_FILE] not in markdown
        assert bundle.manifest["files"][HTML_FILE] not in markdown
        assert "cannot quote its own digest" in markdown
        assert "rootsign export --check" in markdown


class TestTheHtmlIsInertAndSelfContained:
    def test_no_scripts_and_no_external_requests(self, page):
        """It has to open the same way on an air-gapped review machine as it
        does on the machine that made it (ADR-014 Decision 3)."""
        lowered = page.lower()

        assert "<script" not in lowered
        assert "javascript:" not in lowered
        assert "<link" not in lowered
        assert "http://" not in lowered and "https://" not in lowered
        assert "onerror=" not in lowered and "onload=" not in lowered

    def test_it_parses_with_balanced_tags(self, page):
        parser = _parse(page)

        assert parser.unbalanced == []
        assert parser.stack == []
        assert "VALID" in "".join(parser.text)

    def test_hostile_content_is_escaped_not_executed(self, tmp_path):
        """A tool name is data an agent handled, and an agent handles what it
        is given. A bundle that executed markup out of its own evidence would
        be a report an attacker gets to write."""
        path = write_session_file(tmp_path, actions=2, previews=False)
        payload = "<script>alert('pwned')</script>"
        damage_action(path, 1, "tool_name", payload)

        page = render_html(bundle_documents(export_local(path)))

        assert payload not in page
        assert "&lt;script&gt;" in page
        assert _parse(page).unbalanced == []


class TestWhatTheReportSays:
    def test_the_chain_table_carries_every_record_and_its_hashes(self, documents, markdown):
        records = documents["verification.json"]["records"]

        for record in records:
            assert record["self_hash"] in markdown
        assert markdown.count("| verified |") == len(records)

    def test_records_after_a_break_are_shown_as_unverified(self, tmp_path):
        """The report must not launder the distinction the bundle draws: the
        verifier stopped at the break and proved nothing after it."""
        path = write_session_file(tmp_path, actions=4, previews=False)
        damage_action(path, 2, "tool_name", "SOMETHING_ELSE")

        docs = bundle_documents(export_local(path))

        assert "| failed |" in render_markdown(docs)
        assert 'class="status-unverified"' in render_html(docs)

    def test_the_narrative_lists_every_event(self, tmp_path):
        path = write_session_file(tmp_path, actions=2, approval=True, decision=True)
        docs = bundle_documents(export_local(path))

        markdown, page = render_markdown(docs), render_html(docs)

        for kind in ("SESSION_OPEN", "DECISION", "ACTION", "APPROVAL", "SESSION_CLOSE"):
            assert kind in markdown
            assert kind in page
        # The approval is the human moment in the story; the reason is the
        # part an auditor quotes.
        assert "amount exceeds mandate" in markdown
        assert "rejected by sile" in markdown

    def test_previews_appear_only_when_the_session_kept_them(self, tmp_path):
        """Previews are fields of the action they belong to, not a separate
        appendix — so a reader sees the payload next to the hash it produced."""
        with_previews = bundle_documents(export_local(write_session_file(tmp_path / "a")))
        without = bundle_documents(export_local(write_session_file(tmp_path / "b", previews=False)))

        rendered = render_markdown(with_previews)
        assert "`input_preview`" in rendered
        assert "eu-west-1" in rendered

        stripped = render_markdown(without)
        assert "input_preview" not in stripped
        assert "payload previews not retained for this session" in stripped

    def test_every_field_of_every_event_is_rendered(self, tmp_path):
        """The report must not curate. A field the renderer omits is a field an
        auditor never learns exists — and a per-type whitelist would silently
        drop whatever a later schema version adds.
        """
        docs = bundle_documents(
            export_local(write_session_file(tmp_path, actions=2, approval=True, decision=True))
        )
        markdown, page = render_markdown(docs), render_html(docs)

        for event in docs["timeline.json"]["events"]:
            for key in event:
                if key in ("type", "timestamp"):
                    continue
                assert f"`{key}`" in markdown, f"{event['type']}.{key} missing from markdown"
                assert f"<code>{key}</code>" in page, f"{event['type']}.{key} missing from html"

    def test_structured_values_render_as_json_not_python(self, tmp_path):
        """`context_presented` is a dict. Rendered with `str()` it would come out
        as `{'tool_name': ...}` — Python's repr, which is not what the stored
        record says and not something an auditor can paste anywhere."""
        docs = bundle_documents(
            export_local(write_session_file(tmp_path, actions=1, approval=True))
        )

        markdown = render_markdown(docs)

        assert '{"input_summary": "...", "tool_name": "send_email"}' in markdown
        assert "'tool_name'" not in markdown

    def test_redacted_previews_say_so_in_both_formats(self, tmp_path):
        docs = bundle_documents(
            export_local(
                write_session_file(tmp_path, actions=2, approval=True, decision=True),
                redact_previews=True,
            )
        )
        markdown, page = render_markdown(docs), render_html(docs)

        for rendered in (markdown, page):
            assert "--redact-previews" in rendered
            # Named, not merely missing: absence a reader cannot interpret is
            # worse than a hole they can ask about.
            assert "context_presented" in rendered
            assert "Ask the exporter for an unredacted bundle" in rendered
            assert "eu-west-1" not in rendered
            assert "amount over threshold" not in rendered

    def test_the_redaction_section_reports_paths_and_its_own_limits(self, documents):
        markdown = render_markdown(documents)

        assert "meta.account" in markdown
        assert "cc[1]" in markdown
        assert "not derivable from stored records" in markdown

    def test_a_file_sourced_bundle_says_what_it_does_not_know_about_the_agent(self, markdown):
        assert "identity details not recorded in this source" in markdown
