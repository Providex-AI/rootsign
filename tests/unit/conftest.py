"""Fixtures shared by the spool-replay suites.

`test_spool_sync.py` (ADR-013 D4) and `test_sync_receipts.py` (ADR-015) both
drive the real `rootsign-admin sync` against a real spool tree. The fixtures
live here rather than being imported across test modules — importing a fixture
makes every test parameter of the same name read as a redefinition (ruff
F811), and the shadowing is genuine, not a lint quirk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.spool_tree import API_KEY, spool_a_session


@pytest.fixture
def spool(tmp_path: Path) -> Path:
    return tmp_path / "spool"


@pytest.fixture
def spooled_session(spool: Path) -> tuple[str, Path]:
    session_id = spool_a_session(spool)
    return session_id, spool / "sessions" / f"{session_id}.jsonl"


@pytest.fixture
def cloud_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROOTSIGN_API_KEY", API_KEY)
