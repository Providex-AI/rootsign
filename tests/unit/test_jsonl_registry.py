"""Unit tests for the JSONL agent registry (ADR-011, T2.4). DB-free."""

from __future__ import annotations

from rootsign.sdk.jsonl_registry import find_agent, get_or_create_agent


def test_create_then_get_is_idempotent(tmp_path):
    a = get_or_create_agent(tmp_path, name="invoice-agent", environment="development", owner="me")
    assert a["agent_id"] and a["name"] == "invoice-agent"
    # Re-running never re-registers — same agent_id back.
    b = get_or_create_agent(tmp_path, name="invoice-agent", environment="development")
    assert b["agent_id"] == a["agent_id"]


def test_same_name_different_environment_is_distinct(tmp_path):
    dev = get_or_create_agent(tmp_path, name="invoice-agent", environment="development")
    prod = get_or_create_agent(tmp_path, name="invoice-agent", environment="production")
    assert dev["agent_id"] != prod["agent_id"]  # identity is (name, environment)


def test_find_agent_returns_none_when_absent(tmp_path):
    assert find_agent(tmp_path, name="nope", environment="development") is None


def test_attrs_stored_on_create_ignored_on_get(tmp_path):
    a = get_or_create_agent(
        tmp_path, name="x", environment="development", risk_tier="high", owner="alice"
    )
    assert a["risk_tier"] == "high"
    # A get with different attrs returns the stored record unchanged.
    b = get_or_create_agent(tmp_path, name="x", environment="development", risk_tier="low")
    assert b["risk_tier"] == "high"  # stored value kept
    assert b["agent_id"] == a["agent_id"]
