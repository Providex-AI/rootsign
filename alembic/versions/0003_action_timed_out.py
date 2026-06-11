"""Add 'timed_out' to actions.authorization_status CHECK constraint.

Revision ID: 0003_action_timed_out
Revises: 0002_approval_parent_id
Create Date: 2026-06-11

Sprint 4 HiTL checkpoint introduces a fourth terminal authorization state:
when the SDK's async poll loop expires without a human decision, the Action
is marked 'timed_out' and a sibling APPROVAL_RECORD with
approver_type='timeout_auto_rejected' is written.

'timeout_auto_rejected' is already a member of approvals.approver_type (see
ck_approvals_approver_type in 0001_initial_schema). Only the actions-side
constraint needs widening.

The constraint is a single-table CHECK on a String(32) column — not a PG
ENUM type — so the migration is a straight DROP + ADD rather than
ALTER TYPE ... ADD VALUE. Forward-only per project policy.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0003_action_timed_out"
down_revision: Union[str, None] = "0002_approval_parent_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NEW_CHECK = (
    "authorization_status IN ("
    "'auto_authorized','human_approved','human_rejected',"
    "'pending','bypassed','timed_out')"
)


def upgrade() -> None:
    op.drop_constraint(
        "ck_actions_authorization_status", "actions", type_="check"
    )
    op.create_check_constraint(
        "ck_actions_authorization_status", "actions", _NEW_CHECK
    )


def downgrade() -> None:
    raise RuntimeError("Downgrade not permitted — forward-only migration policy")
