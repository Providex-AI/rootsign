"""Initial schema — 7 entities + TimescaleDB hypertable on actions.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-05-19

This migration creates the full Phase 0 schema in one shot. Per the spec:
  * `actions` is converted to a TimescaleDB hypertable partitioned by `timestamp`
  * Compression policy applied to chunks > 30 days
  * downgrade() raises — forward-only migration policy
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")

    # ---------- agents ----------
    op.create_table(
        "agents",
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("owner", sa.String(200), nullable=False),
        sa.Column("description", sa.String(2000), nullable=True),
        sa.Column("environment", sa.String(32), nullable=False),
        sa.Column("risk_tier", sa.String(32), nullable=False),
        sa.Column(
            "permitted_tools",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "regulatory_categories",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("framework", sa.String(32), nullable=False),
        sa.Column("model_version", sa.String(100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.UniqueConstraint("name", name="uq_agents_name"),
        sa.CheckConstraint(
            "environment IN ('development','staging','production')",
            name="ck_agents_environment",
        ),
        sa.CheckConstraint(
            "risk_tier IN ('low','medium','high','critical')",
            name="ck_agents_risk_tier",
        ),
        sa.CheckConstraint(
            "framework IN ('langgraph','crewai','autogen','custom','unknown')",
            name="ck_agents_framework",
        ),
    )
    op.create_index(
        "ix_agents_environment_is_active", "agents", ["environment", "is_active"]
    )
    op.create_index("ix_agents_risk_tier", "agents", ["risk_tier"])
    op.create_index("ix_agents_is_active", "agents", ["is_active"])

    # ---------- policies (created early — Action.policy_id FKs into it) ----------
    op.create_table(
        "policies",
        sa.Column("policy_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("rule_text", sa.String(50000), nullable=False),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "version", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column("regulatory_refs", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column(
            "enforcement_mode",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'log_only'"),
        ),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.UniqueConstraint("name", "version", name="uq_policies_name_version"),
        sa.CheckConstraint(
            "scope IN ('agent','session','tool','global')", name="ck_policies_scope"
        ),
        sa.CheckConstraint(
            "enforcement_mode IN ('log_only','require_approval','block')",
            name="ck_policies_enforcement_mode",
        ),
        sa.CheckConstraint("version >= 1", name="ck_policies_version_positive"),
    )

    # ---------- sessions ----------
    op.create_table(
        "sessions",
        sa.Column("session_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.agent_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.String(200), nullable=True),
        sa.Column("objective", sa.String(2000), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "start_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "action_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "decision_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("chain_head_hash", sa.String(64), nullable=True),
        sa.Column("chain_tail_hash", sa.String(64), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.CheckConstraint(
            "status IN ('running','completed','failed','abandoned')",
            name="ck_sessions_status",
        ),
        sa.CheckConstraint(
            "action_count >= 0", name="ck_sessions_action_count_nonneg"
        ),
        sa.CheckConstraint(
            "decision_count >= 0", name="ck_sessions_decision_count_nonneg"
        ),
        sa.CheckConstraint(
            "end_time IS NULL OR end_time >= start_time",
            name="ck_sessions_end_after_start",
        ),
    )
    op.create_index("ix_sessions_agent_id_status", "sessions", ["agent_id", "status"])
    op.create_index("ix_sessions_start_time_desc", "sessions", ["start_time"])

    # ---------- decisions ----------
    op.create_table(
        "decisions",
        sa.Column("decision_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sessions.session_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "policy_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("policies.policy_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("inputs_summary", sa.String(5000), nullable=True),
        sa.Column("reasoning_summary", sa.String(10000), nullable=True),
        sa.Column("selected_action", sa.String(200), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "alternatives_considered", postgresql.ARRAY(sa.String()), nullable=True
        ),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "reasoning_captured",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)",
            name="ck_decisions_confidence_range",
        ),
    )
    op.create_index(
        "ix_decisions_session_timestamp", "decisions", ["session_id", "timestamp"]
    )

    # ---------- actions (hypertable) ----------
    op.create_table(
        "actions",
        sa.Column("action_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sessions.session_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("decision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "policy_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("policies.policy_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("tool_name", sa.String(200), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("output_hash", sa.String(64), nullable=True),
        sa.Column("input_redacted", postgresql.JSONB(), nullable=True),
        sa.Column("output_redacted", postgresql.JSONB(), nullable=True),
        sa.Column("prev_action_hash", sa.String(64), nullable=True),
        sa.Column("self_hash", sa.String(64), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("authorization_status", sa.String(32), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        # Composite PK includes `timestamp` — required by TimescaleDB.
        sa.PrimaryKeyConstraint("action_id", "timestamp", name="pk_actions"),
        sa.CheckConstraint(
            "authorization_status IN ("
            "'auto_authorized','human_approved','human_rejected','pending','bypassed')",
            name="ck_actions_authorization_status",
        ),
        sa.CheckConstraint("sequence_number >= 1", name="ck_actions_seq_positive"),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_actions_duration_nonneg",
        ),
        sa.CheckConstraint(
            "length(input_hash) = 64", name="ck_actions_input_hash_length"
        ),
        sa.CheckConstraint(
            "length(self_hash) = 64", name="ck_actions_self_hash_length"
        ),
    )
    op.create_index("ix_actions_session_seq", "actions", ["session_id", "sequence_number"])
    op.create_index("ix_actions_session_timestamp", "actions", ["session_id", "timestamp"])
    op.create_index("ix_actions_tool_name", "actions", ["tool_name"])
    op.create_index("ix_actions_authorization_status", "actions", ["authorization_status"])

    # Convert to TimescaleDB hypertable + compression policy.
    op.execute(
        "SELECT create_hypertable('actions', 'timestamp', "
        "chunk_time_interval => INTERVAL '7 days', if_not_exists => TRUE)"
    )
    op.execute(
        "ALTER TABLE actions SET ("
        "timescaledb.compress, "
        "timescaledb.compress_segmentby = 'session_id')"
    )
    op.execute(
        "SELECT add_compression_policy('actions', INTERVAL '30 days', "
        "if_not_exists => TRUE)"
    )

    # ---------- approvals ----------
    # action_id is NOT a SQL FK because actions has a composite PK (action_id, timestamp)
    # — referential integrity is enforced at the CRUD layer.
    op.create_table(
        "approvals",
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("action_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approver_id", sa.String(200), nullable=False),
        sa.Column("approver_type", sa.String(32), nullable=False),
        sa.Column("context_presented", postgresql.JSONB(), nullable=False),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("decision_reason", sa.String(2000), nullable=True),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("response_latency_ms", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "approver_type IN ("
            "'human','automated_policy','timeout_auto_approved','timeout_auto_rejected')",
            name="ck_approvals_approver_type",
        ),
        sa.CheckConstraint(
            "decision IN ('approved','rejected','escalated')",
            name="ck_approvals_decision",
        ),
        sa.CheckConstraint(
            "response_latency_ms IS NULL OR response_latency_ms >= 0",
            name="ck_approvals_latency_nonneg",
        ),
    )
    op.create_index("ix_approvals_action_id", "approvals", ["action_id"])
    op.create_index(
        "ix_approvals_decision_timestamp", "approvals", ["decision", "timestamp"]
    )

    # ---------- incidents ----------
    op.create_table(
        "incidents",
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("trigger_type", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "linked_session_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "linked_action_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=True,
        ),
        sa.Column(
            "linked_decision_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=True,
        ),
        sa.Column("investigator_id", sa.String(200), nullable=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.String(10000), nullable=True),
        sa.Column("resolution", sa.String(5000), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "trigger_type IN ("
            "'anomaly_detected','policy_violation','manual','authorization_bypass')",
            name="ck_incidents_trigger_type",
        ),
        sa.CheckConstraint(
            "severity IN ('info','low','medium','high','critical')",
            name="ck_incidents_severity",
        ),
        sa.CheckConstraint(
            "status IN ('open','investigating','resolved','closed','false_positive')",
            name="ck_incidents_status",
        ),
    )
    op.create_index("ix_incidents_severity", "incidents", ["severity"])
    op.create_index("ix_incidents_status", "incidents", ["status"])


def downgrade() -> None:
    raise RuntimeError("Downgrade not permitted — forward-only migration policy")
