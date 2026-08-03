"""Sprint 3 redaction hardening tests — pre-built PII configs, nested /
list traversal, depth limit, and the golden-vector contract that proves
PII is redacted BEFORE hashing (ADR-006).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rootsign.sdk.hashing import compute_payload_hash
from rootsign.sdk.redaction import (
    MAX_REDACTION_DEPTH,
    REDACTED_PLACEHOLDER,
    FinancialPIIConfig,
    HealthcarePIIConfig,
    RedactionConfig,
    StandardPIIConfig,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "redaction_vectors.json"


def _resolve_config(spec):
    if isinstance(spec, str):
        return {
            "StandardPIIConfig": StandardPIIConfig,
            "FinancialPIIConfig": FinancialPIIConfig,
            "HealthcarePIIConfig": HealthcarePIIConfig,
        }[spec]()
    return RedactionConfig(spec)


class TestStandardPIIConfig:
    def test_instantiable_with_zero_args(self):
        cfg = StandardPIIConfig()
        assert isinstance(cfg, RedactionConfig)

    def test_redacts_email(self):
        cfg = StandardPIIConfig()
        assert cfg.redact({"email": "alice@example.com"}) == {
            "email": REDACTED_PLACEHOLDER
        }

    def test_redacts_phone(self):
        cfg = StandardPIIConfig()
        assert cfg.redact({"phone": "+1 415 555 1234"}) == {
            "phone": REDACTED_PLACEHOLDER
        }

    def test_redacts_ssn(self):
        cfg = StandardPIIConfig()
        assert cfg.redact({"ssn": "123-45-6789"}) == {"ssn": REDACTED_PLACEHOLDER}

    def test_redacts_credit_card(self):
        cfg = StandardPIIConfig()
        assert cfg.redact({"credit_card": "4111 1111 1111 1111"}) == {
            "credit_card": REDACTED_PLACEHOLDER
        }

    def test_redacts_uk_ni(self):
        cfg = StandardPIIConfig()
        assert cfg.redact({"uk_ni": "AB123456C"}) == {"uk_ni": REDACTED_PLACEHOLDER}

    def test_extra_rules_compose(self):
        cfg = StandardPIIConfig(extra_rules={"mrn": r"MRN\d{8}"})
        assert cfg.redact({"mrn": "MRN12345678", "email": "x@y.com"}) == {
            "mrn": REDACTED_PLACEHOLDER,
            "email": REDACTED_PLACEHOLDER,
        }

    def test_redacts_pii_inside_decorator_envelope_shape(self):
        """REGRESSION (audit finding #3): the decorator's input payload is
        `{"args": [...], "kwargs": {...}}`. Pre-audit, `StandardPIIConfig`
        was keyed by bare names (`email`, `ssn`, ...) but the matcher
        only fired on top-level path keys, so PII at `kwargs.email`
        never matched and the config was effectively a no-op for every
        real tool call.

        Post-audit fix: leaf-key matching is the default — a rule keyed
        `email` fires on any field named `email` regardless of nesting.
        This test mirrors what `_emit_action_record` actually produces.
        """
        cfg = StandardPIIConfig()
        envelope = {
            "args": [],
            "kwargs": {
                "customer_id": "acme",
                "email": "alice@example.com",
                "phone": "+1-415-555-1234",
                "ssn": "123-45-6789",
            },
        }
        out = cfg.redact(envelope)
        assert out["kwargs"]["email"] == REDACTED_PLACEHOLDER
        assert out["kwargs"]["phone"] == REDACTED_PLACEHOLDER
        assert out["kwargs"]["ssn"] == REDACTED_PLACEHOLDER
        # Non-PII fields pass through unchanged.
        assert out["kwargs"]["customer_id"] == "acme"
        assert out["args"] == []

    def test_redacts_pii_deeply_nested_in_envelope(self):
        """Leaf-key matching must walk the full nesting depth — a tool
        that passes a structured user object should still get its
        email/phone redacted."""
        cfg = StandardPIIConfig()
        envelope = {
            "args": [],
            "kwargs": {
                "user": {
                    "profile": {
                        "email": "alice@example.com",
                        "phone": "+14155551234",
                    },
                    "id": "u-7",
                },
            },
        }
        out = cfg.redact(envelope)
        assert out["kwargs"]["user"]["profile"]["email"] == REDACTED_PLACEHOLDER
        assert out["kwargs"]["user"]["profile"]["phone"] == REDACTED_PLACEHOLDER
        assert out["kwargs"]["user"]["id"] == "u-7"

    def test_path_mode_does_not_apply_leaf_inference(self):
        """`match_mode="path"` opts out of leaf inference — bare names
        match only top-level paths, NOT nested ones. Useful when you
        want to distinguish `audit.email` from `user.email`."""
        cfg = RedactionConfig({"email": r".+@.+"}, match_mode="path")
        out = cfg.redact(
            {"kwargs": {"email": "alice@example.com"}}
        )
        # Under path mode, the bare rule "email" only matches path "email"
        # at the top level — kwargs.email does NOT match.
        assert out == {"kwargs": {"email": "alice@example.com"}}

    def test_path_keyed_rule_always_takes_precedence_over_leaf(self):
        """When both a path rule and a leaf rule could match, the explicit
        path-keyed rule wins. This lets users tighten a broad leaf rule
        with a narrow path-specific one (e.g. allow `audit.email` to use
        a different pattern than the generic `email` leaf rule)."""
        cfg = RedactionConfig(
            {
                "email": r"BROAD.+",  # leaf rule — wouldn't match
                "kwargs.email": r".+@.+",  # path rule — matches
            }
        )
        out = cfg.redact({"kwargs": {"email": "alice@example.com"}})
        assert out["kwargs"]["email"] == REDACTED_PLACEHOLDER


class TestFinancialAndHealthcareConfigs:
    def test_financial_redacts_iban(self):
        cfg = FinancialPIIConfig()
        out = cfg.redact({"iban": "DE89370400440532013000"})
        assert out == {"iban": REDACTED_PLACEHOLDER}

    def test_financial_inherits_email_redaction(self):
        cfg = FinancialPIIConfig()
        out = cfg.redact({"email": "x@y.com", "iban": "DE89370400440532013000"})
        assert out == {"email": REDACTED_PLACEHOLDER, "iban": REDACTED_PLACEHOLDER}

    def test_healthcare_redacts_mrn(self):
        cfg = HealthcarePIIConfig()
        assert cfg.redact({"mrn": "MRN12345678"}) == {"mrn": REDACTED_PLACEHOLDER}

    def test_healthcare_redacts_dob(self):
        cfg = HealthcarePIIConfig()
        assert cfg.redact({"dob": "1/15/1980"}) == {"dob": REDACTED_PLACEHOLDER}


class TestListAndDepth:
    def test_list_items_redacted(self):
        cfg = StandardPIIConfig()
        out = cfg.redact({"email": ["a@b.com", "c@d.com"]})
        assert out == {"email": [REDACTED_PLACEHOLDER, REDACTED_PLACEHOLDER]}

    def test_list_of_dicts_redacted(self):
        cfg = RedactionConfig({"users.email": ".*@.*"})
        out = cfg.redact(
            {"users": [{"email": "a@b.com", "name": "A"}, {"email": "c@d.com", "name": "C"}]}
        )
        assert out == {
            "users": [
                {"email": REDACTED_PLACEHOLDER, "name": "A"},
                {"email": REDACTED_PLACEHOLDER, "name": "C"},
            ]
        }

    def test_max_depth_fails_closed(self):
        """Audit fix: past MAX_REDACTION_DEPTH the redactor must FAIL
        CLOSED — return the redaction placeholder, never the raw subtree.
        Prior behaviour returned `obj`/`value` unchanged, which silently
        let raw PII past the privacy boundary on deeply nested payloads.
        """
        from rootsign.sdk.redaction import REDACTED_PLACEHOLDER

        cfg = RedactionConfig({"a.b.c.d.e.f.g": ".*"})
        # Build a payload that pushes the recursion past MAX_REDACTION_DEPTH.
        nested: dict[str, Any] = {}
        cur = nested
        keys = ["a", "b", "c", "d", "e", "f", "g"]
        assert len(keys) > MAX_REDACTION_DEPTH
        for k in keys[:-1]:
            cur[k] = {}
            cur = cur[k]
        cur[keys[-1]] = "should_not_be_visible_in_output"

        out = cfg.redact(nested)
        # The raw PII string MUST NOT appear anywhere in the output —
        # the depth-limit bail-out replaced the subtree with the
        # placeholder, so a serialised form of `out` should contain
        # neither the raw value nor any path beyond the limit.
        import json
        rendered = json.dumps(out)
        assert "should_not_be_visible_in_output" not in rendered
        assert REDACTED_PLACEHOLDER in rendered


class TestRedactionGoldenVectors:
    """The golden contract from ADR-006.

    Each vector pins a (config, input) → redacted_output mapping, and we
    also assert the hash of the redacted payload is the same regardless of
    the original PII — i.e. two distinct PII inputs that redact to the
    same structure hash to the same value. This is the binding statement
    that hashes never carry PII.
    """

    def test_vectors_match_expected_output(self):
        data = json.loads(FIXTURES.read_text())
        for v in data["vectors"]:
            cfg = _resolve_config(v["config"])
            assert cfg.redact(v["input"]) == v["expected_after_redaction"], (
                f"vector failed: {v['description']}"
            )

    def test_pii_inputs_collide_on_hash_after_redaction(self):
        """Two different PII values redacted by the same rule produce the
        same hash. Proves the hash carries no PII signal."""
        cfg = StandardPIIConfig()
        a = cfg.redact({"email": "alice@example.com", "name": "Alice"})
        b = cfg.redact({"email": "bob@example.com", "name": "Alice"})
        assert compute_payload_hash(a) == compute_payload_hash(b)

    def test_raw_pii_inputs_do_not_collide(self):
        """Control: WITHOUT redaction, two distinct PII inputs hash
        differently. Lets the previous test's claim stand."""
        a = {"email": "alice@example.com", "name": "Alice"}
        b = {"email": "bob@example.com", "name": "Alice"}
        assert compute_payload_hash(a) != compute_payload_hash(b)


class TestRedactionRunsBeforeHashing:
    """Direct simulation of the decorator pipeline. Covers ADR-006 §
    'raw_payload → redact() → redacted_payload → compute_payload_hash()'.
    """

    def test_pipeline_hashes_redacted_not_raw(self):
        raw = {"email": "leaky@example.com", "kwargs": {"q": "hello"}}
        cfg = StandardPIIConfig()
        redacted = cfg.redact(raw)

        hash_of_redacted = compute_payload_hash(redacted)
        hash_of_raw = compute_payload_hash(raw)

        assert hash_of_redacted != hash_of_raw
        # And the redacted hash carries no email signal — same structure
        # with any other email collides.
        other_raw = {"email": "totally@different.org", "kwargs": {"q": "hello"}}
        assert hash_of_redacted == compute_payload_hash(cfg.redact(other_raw))
