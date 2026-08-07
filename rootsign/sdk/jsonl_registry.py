"""JSONL agent registry — get-or-create keyed on (name, environment) (ADR-011).

The database-free counterpart to `crud.agent`: one JSON object per agent in
`$ROOTSIGN_DATA_DIR/agents.jsonl`. Re-running a script never re-registers — an
existing `(name, environment)` is returned as-is (attribute drift is the
caller's concern: `init()`/`get_or_register_agent` log a WARNING and keep the
stored values, ADR-012 Decision 2).

Identity is `(name, environment)` across both backends — the same name may
exist independently per environment (matches the W3 Postgres
`uq_agents_name_environment` migration).

Single-writer contract (ADR-011 Decision 4): no cross-process file locking. A
process that get-or-creates concurrently with another writer on the same file
is out of contract; teams needing that graduate to `postgres`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4


def _registry_path(data_dir: str | os.PathLike[str]) -> Path:
    return Path(data_dir).expanduser() / "agents.jsonl"


def find_agent(
    data_dir: str | os.PathLike[str], *, name: str, environment: str
) -> dict[str, Any] | None:
    """Return the stored agent for (name, environment), or None."""
    path = _registry_path(data_dir)
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        agent = json.loads(line)
        if agent.get("name") == name and agent.get("environment") == environment:
            return agent
    return None


def get_or_create_agent(
    data_dir: str | os.PathLike[str],
    *,
    name: str,
    environment: str,
    **attrs: Any,
) -> dict[str, Any]:
    """Return the existing agent for (name, environment) or append a new one.

    A new agent gets a generated `agent_id` (uuid4). `**attrs` (owner,
    risk_tier, framework, …) are stored on creation and ignored on a
    get — the caller decides how to surface drift.
    """
    existing = find_agent(data_dir, name=name, environment=environment)
    if existing is not None:
        return existing

    agent: dict[str, Any] = {
        "agent_id": str(uuid4()),
        "name": name,
        "environment": environment,
        **attrs,
    }
    path = _registry_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(agent, ensure_ascii=True, separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return agent
