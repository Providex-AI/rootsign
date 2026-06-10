"""Sprint 3 redaction hardening tests — pre-built PII configs, nested /
list traversal, depth limit, and the golden-vector contract that proves
PII is redacted BEFORE hashing (ADR-006).
"""

from __future__ import annotations

import json
from pathlib import Path

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

    def test_max_depth_bounded(self):
        cfg = RedactionConfig({"a.b.c.d.e.f.g": ".*"})
        # Build a payload one level past MAX_REDACTION_DEPTH; the inner
        # value should NOT be redacted because traversal halts.
        nested = {}
        cur = nested
        keys = ["a", "b", "c", "d", "e", "f", "g"]
        assert len(keys) > MAX_REDACTION_DEPTH
        for k in keys[:-1]:
            cur[k] = {}
            cur = cur[k]
        cur[keys[-1]] = "should_not_be_redacted"

        out = cfg.redact(nested)
        # Walk down to leaf in the output; the value is unchanged.
        cur_out = out
        for k in keys[:-1]:
            cur_out = cur_out[k]
        assert cur_out[keys[-1]] == "should_not_be_redacted"


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
