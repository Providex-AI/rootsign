"""Store each session's seal.

Revision ID: 0007_session_seal
Revises: 0006_decision_reasoning_depth
Create Date: 2026-11-27

ADR-016: when a session closes, the store seals it, so a verifier can tell
records cut from the end, an edited open or close, and edited decisions or
approvals. The seal is a first-class column rather than a key inside the
free-form `metadata`, so the verifier and the export read it directly.

Additive and nullable: sessions closed before this revision, and sessions
still running, have no seal, and NULL says exactly that. Forward-only per
project policy.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_session_seal"
down_revision: Union[str, None] = "0006_decision_reasoning_depth"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("seal", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    raise RuntimeError("Downgrade not permitted — forward-only migration policy")
