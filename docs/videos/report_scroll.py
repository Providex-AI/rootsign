"""Record a scroll through an evidence bundle's report.html as an MP4 segment.

    .venv/bin/python docs/videos/report_scroll.py <bundle_dir> <out.mp4>

Headless Chrome renders the report once, full length. The segment then scrolls
that image down the page, stopping on each section heading, at the size and
frame rate of the vhs clips so render.sh can join them. A strip across the top
names the file, so the cut from the terminal reads as "opened in a browser".
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
WIDTH, HEIGHT, FPS = 1280, 720, 25
SCALE = 1.6  # device pixels per CSS pixel: an 800px-wide page, legible at 1280
BAR = 48  # height of the file-name strip
VIEW = HEIGHT - BAR
TALL = 6000  # device pixels; taller than any report this records
FONT = "/System/Library/Fonts/Menlo.ttc"

# Where to stop, by section heading, and how long to hold there (seconds).
STOPS = [
    ("Session", 4.0),
    ("Chain verification", 4.0),
    ("What happened", 4.5),
    ("Bundle contents", 4.0),
    ("Verifying this bundle", 4.0),
]
SCROLL_SECONDS = 1.2

_OFFSETS = """<script>
document.body.dataset.offsets = JSON.stringify(
  [...document.querySelectorAll("h2")].map(h => [h.textContent.trim(), h.offsetTop]));
</script>"""


def _chrome(*args: str) -> str:
    window = f"--window-size={round(WIDTH / SCALE)},{round(TALL / SCALE)}"
    common = ["--headless", "--disable-gpu", "--hide-scrollbars", window]
    common.append(f"--force-device-scale-factor={SCALE}")
    run = subprocess.run([CHROME, *common, *args], capture_output=True, text=True, check=True)
    return run.stdout


def _heading_offsets(report: Path, scratch: Path) -> dict[str, int]:
    """Device-pixel y of each <h2>, read from a copy of the report with one script added."""
    probe = scratch / "probe.html"
    probe.write_text(report.read_text(encoding="utf-8").replace("</body>", _OFFSETS + "</body>"))
    dom = _chrome("--dump-dom", probe.as_uri())
    raw = re.search(r'data-offsets="([^"]*)"', dom)
    assert raw, "Chrome did not run the offset probe"
    pairs = json.loads(raw.group(1).replace("&quot;", '"'))
    return {name: round(top * SCALE) for name, top in pairs}


def _screenshot(report: Path, scratch: Path) -> Image.Image:
    shot = scratch / "report.png"
    _chrome(f"--screenshot={shot}", report.as_uri())
    page = Image.open(shot).convert("RGB")
    # Trim the empty background below the last line of the report.
    background = page.getpixel((2, page.height - 2))
    bottom = page.height - 1
    while bottom > 0 and all(
        page.getpixel((x, bottom)) == background for x in range(0, page.width, 5)
    ):
        bottom -= 1
    return page.crop((0, 0, page.width, min(page.height, bottom + 60)))


def _bar(label: str) -> Image.Image:
    bar = Image.new("RGB", (WIDTH, BAR), (30, 30, 46))
    draw = ImageDraw.Draw(bar)
    draw.text((28, 13), label, font=ImageFont.truetype(FONT, 18), fill=(205, 214, 244))
    return bar


def _ease(t: float) -> float:
    return t * t * (3 - 2 * t)


def main(bundle: Path, out: Path) -> None:
    report = bundle / "report.html"
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        offsets = _heading_offsets(report, scratch)
        page = _screenshot(report, scratch)
    assert page.width == WIDTH, f"screenshot is {page.width}px wide, expected {WIDTH}"
    last = page.height - VIEW
    # The first stop is the top of the page, so the verdict banner shows first.
    stops = [
        (0 if i == 0 else min(last, offsets[name] - 24), hold)
        for i, (name, hold) in enumerate(STOPS)
    ]

    positions: list[int] = []
    previous = stops[0][0]
    for y, hold in stops:
        moves = round(SCROLL_SECONDS * FPS) if y != previous else 0
        positions += [
            round(previous + (y - previous) * _ease(i / moves)) for i in range(1, moves + 1)
        ]
        positions += [y] * round(hold * FPS)
        previous = y

    bar = _bar(f"{bundle.parent.name}/{bundle.name}/report.html")
    ffmpeg = subprocess.Popen(
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            f"{WIDTH}x{HEIGHT}",
            "-r",
            str(FPS),
            "-i",
            "-",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(FPS),
            str(out),
        ],
        stdin=subprocess.PIPE,
    )
    assert ffmpeg.stdin
    frame = Image.new("RGB", (WIDTH, HEIGHT))
    frame.paste(bar, (0, 0))
    for y in positions:
        frame.paste(page.crop((0, y, WIDTH, y + VIEW)), (0, BAR))
        ffmpeg.stdin.write(frame.tobytes())
    ffmpeg.stdin.close()
    assert ffmpeg.wait() == 0


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
