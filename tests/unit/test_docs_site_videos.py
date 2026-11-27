"""The docs videos show the commands their pages show, and every video is there.

The clips on the quickstart, evidence-bundle and human-approval pages are
recorded from the vhs tapes in `docs/videos/` (`docs/videos/render.sh`). A
recording cannot be re-checked in CI, but the tapes can: every `rootsign`,
`pip` and `python` command a tape types must be a command the page itself
shows, so a page edit that renames a flag or a file fails here until the clip
is re-rendered. Shell helpers the tapes use to move around (`cp`, `sed`, `ls`,
`ID=$(...)`) are not checked.

Session and action IDs differ between the page and a recording, so both sides
are compared with IDs normalised: a UUID, `<session_id>`, `<action_id>`, a
`*` glob standing for one session, and `$ID` all read as `<id>`.
"""

from __future__ import annotations

import re

import pytest

from tests.support.docs_pages import REPO, SITE, page_text

TAPES = REPO / "docs" / "videos"
VIDEOS = SITE / "videos"
# Page -> (the video it embeds, the tapes that record it).
CLIPS = {
    "quickstart.mdx": ("quickstart.mp4", ["quickstart.tape"]),
    "guides/evidence-bundles.mdx": (
        "evidence-bundle.mp4",
        ["evidence-bundle-1.tape", "evidence-bundle-2.tape"],
    ),
    "guides/human-approval.mdx": ("human-approval.mp4", ["human-approval.tape"]),
}
CHECKED = ("rootsign ", "rootsign-admin ", "pip ", "python ")
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
MAX_BYTES = 5_000_000


def _normalise(command: str) -> str:
    command = re.sub(UUID, "<id>", command)
    command = re.sub(r"<session_id>|<action_id>|\$ID", "<id>", command)
    return command.replace("*", "<id>").strip()


def _typed(tape: str) -> list[str]:
    """The commands a tape types while recording (between Show and the end)."""
    lines = (TAPES / tape).read_text(encoding="utf-8").splitlines()
    typed: list[str] = []
    showing = True
    for line in lines:
        if line.strip() in ("Hide", "Show"):
            showing = line.strip() == "Show"
            continue
        match = re.match(r"""Type (["'])(.*)\1$""", line.strip())
        if showing and match:
            typed.append(match.group(2))
    return typed


def _page_commands(page: str) -> set[str]:
    blocks = re.findall(r"```bash[^\n]*\n(.*?)\n\s*```", page_text(page), re.DOTALL)
    return {_normalise(line) for block in blocks for line in block.splitlines() if line.strip()}


@pytest.mark.parametrize("page", CLIPS)
def test_every_command_a_clip_types_is_on_its_page(page: str) -> None:
    _, tapes = CLIPS[page]
    shown = _page_commands(page)
    checked = [c for tape in tapes for c in _typed(tape) if c.startswith(CHECKED)]
    assert checked, f"the tapes for {page} type no rootsign/pip/python commands"
    missing = [c for c in checked if _normalise(c) not in shown]
    assert not missing, f"{page} does not show these commands its clip types: {missing}"


@pytest.mark.parametrize("page", CLIPS)
def test_each_page_embeds_its_clip(page: str) -> None:
    video, _ = CLIPS[page]
    sources = re.findall(r'<video\s[^>]*?src="/videos/([^"]+)"', page_text(page), re.DOTALL)
    assert sources == [video], f"{page} embeds {sources}, expected [{video!r}]"
    # A muted, inline clip is what lets autoPlay start on phones and in Safari.
    tag = re.search(r"<video\s.*?>", page_text(page), re.DOTALL).group(0)
    for attribute in ("autoPlay", "muted", "loop", "playsInline", "controls"):
        assert re.search(rf"\b{attribute}\b", tag), f"{page}: <video> lacks {attribute}"


def test_every_tape_records_to_the_video_its_page_embeds() -> None:
    """A tape writing to a renamed or missing file would leave the page's clip stale."""
    render = (TAPES / "render.sh").read_text(encoding="utf-8")
    for video, tapes in CLIPS.values():
        outputs = [
            line.split(maxsplit=1)[1].strip('"')
            for tape in tapes
            for line in (TAPES / tape).read_text(encoding="utf-8").splitlines()
            if line.startswith("Output ")
        ]
        target = f"docs/site/videos/{video}"
        assert target in outputs or target in render, f"nothing renders {target}"


def test_video_files_are_the_ones_the_pages_use_and_small() -> None:
    used = {video for video, _ in CLIPS.values()}
    on_disk = {path.name for path in VIDEOS.iterdir() if not path.name.startswith(".")}
    assert on_disk == used, f"docs/site/videos holds {sorted(on_disk)}, pages use {sorted(used)}"
    for name in used:
        data = (VIDEOS / name).read_bytes()
        assert data[4:8] == b"ftyp", f"{name} is not an MP4 file"
        assert len(data) < MAX_BYTES, f"{name} is {len(data):,} bytes; keep clips under 5 MB"


def test_readme_gif_is_the_quickstart_clip() -> None:
    """GitHub and PyPI cannot play an MP4 from the repo, so the README shows a GIF of it."""
    gif = REPO / "docs" / "demo.gif"
    assert gif.read_bytes()[:6] in (b"GIF87a", b"GIF89a")
    assert gif.stat().st_size < MAX_BYTES
    assert "docs/site/videos/quickstart.mp4" in (TAPES / "render.sh").read_text(encoding="utf-8")
    assert "docs/demo.gif" in (TAPES / "render.sh").read_text(encoding="utf-8")
