"""Single source of truth for the package version string.

Reads from the installed distribution metadata (`importlib.metadata`),
which is populated from `pyproject.toml`'s `[project] version` at install
time. This is the only place a version string is hardcoded fallback — every
other site (`rootsign.__version__`, `SDK_VERSION` in decorator.py /
session.py, and the test envelope helper) imports from here.

Tracking the version in one place fixes rootsign_review.md finding #12
("SDK_VERSION is hand-pinned rather than read from package metadata —
these will drift on the next release bump"). After bumping
`pyproject.toml`, run `pip install -e .` (or rebuild the wheel) so the
distribution metadata picks up the new version.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("rootsign")
except PackageNotFoundError:
    # The package isn't installed (e.g. running tests with PYTHONPATH=.
    # before `pip install -e .`). Keep a stable sentinel rather than
    # raising — envelopes validate sdk_version as a non-empty string and
    # don't care what value it holds. Reinstall to surface the real one.
    __version__ = "0.0.0+unknown"

SDK_VERSION = __version__
