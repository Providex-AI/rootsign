"""Positional arguments are redacted by parameter name (ADR-006).

Redaction rules match field names. A traced call is recorded as
`{"args": [...], "kwargs": {...}}`, and a positional argument has no name in
that payload, so `email_customer("alice@example.com")` used to store the
address in the clear while `email_customer(email="alice@example.com")`
redacted it. The chain still verified and `redaction.json` reported nothing
missed. These tests pin the fix: a call redacts the same way however it was
made, the recorded shape is unchanged, and an unreadable signature fails
closed.

Also: `FinancialPIIConfig` and `HealthcarePIIConfig` accept `extra_rules`, as
the README always said they did.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from rootsign.sdk.context import SessionContext
from rootsign.sdk.decorator import _positional_names, _redacted_call_input, trace
from rootsign.sdk.hashing import compute_payload_hash
from rootsign.sdk.redaction import (
    REDACTED_PLACEHOLDER,
    FinancialPIIConfig,
    HealthcarePIIConfig,
    RedactionConfig,
    StandardPIIConfig,
)

EMAIL = "alice@example.com"


def email_customer(email: str, subject: str) -> str:
    return f"sent to {email}"


class TestPositionalNames:
    def test_names_follow_the_signature(self):
        assert _positional_names(email_customer, 2) == ["email", "subject"]
        assert _positional_names(email_customer, 1) == ["email"]

    def test_star_args_take_the_parameter_name(self):
        def notify(channel, *emails):
            pass

        assert _positional_names(notify, 3) == ["channel", "emails", "emails"]

    def test_keyword_only_parameters_stop_the_list(self):
        def f(a, *, email):
            pass

        assert _positional_names(f, 1) == ["a"]

    def test_an_unreadable_signature_returns_none(self):
        class NoSignature:
            @property
            def __signature__(self):
                raise ValueError("no signature")

            def __call__(self, *args):
                pass

        assert _positional_names(NoSignature(), 1) is None


class TestRedactCall:
    def test_positional_and_keyword_calls_redact_the_same(self):
        config = StandardPIIConfig()

        positional = _redacted_call_input(email_customer, (EMAIL, "Invoice"), {}, config)
        keyword = _redacted_call_input(
            email_customer, (), {"email": EMAIL, "subject": "Invoice"}, config
        )

        assert positional == {"args": [REDACTED_PLACEHOLDER, "Invoice"], "kwargs": {}}
        assert keyword == {
            "args": [],
            "kwargs": {"email": REDACTED_PLACEHOLDER, "subject": "Invoice"},
        }

    def test_the_recorded_shape_is_unchanged(self):
        payload = _redacted_call_input(email_customer, (EMAIL, "Invoice"), {}, StandardPIIConfig())

        assert set(payload) == {"args", "kwargs"}
        assert len(payload["args"]) == 2

    def test_a_value_that_does_not_match_the_pattern_is_kept(self):
        payload = _redacted_call_input(
            email_customer, ("not an address", "Invoice"), {}, StandardPIIConfig()
        )

        assert payload["args"] == ["not an address", "Invoice"]

    def test_star_args_are_matched_under_their_name(self):
        def notify(channel, *emails):
            pass

        config = RedactionConfig({"emails": r".+@.+"})
        payload = _redacted_call_input(notify, ("sms", EMAIL, "bob@example.com"), {}, config)

        assert payload["args"] == ["sms", REDACTED_PLACEHOLDER, REDACTED_PLACEHOLDER]

    def test_a_path_rule_written_for_kwargs_also_covers_positional(self):
        config = RedactionConfig({"kwargs.email": r".+"}, match_mode="path")

        payload = _redacted_call_input(email_customer, (EMAIL, "Invoice"), {}, config)

        assert payload["args"][0] == REDACTED_PLACEHOLDER

    def test_dict_arguments_still_redact_by_inner_key(self):
        def update(customer):
            pass

        payload = _redacted_call_input(
            update, ({"name": "Alice", "email": EMAIL},), {}, StandardPIIConfig()
        )

        assert payload["args"][0] == {"name": "Alice", "email": REDACTED_PLACEHOLDER}

    def test_an_unreadable_signature_fails_closed(self):
        payload = StandardPIIConfig().redact_call(
            {"args": [EMAIL, "Invoice", {"note": "x"}, None], "kwargs": {}}, None
        )

        assert payload["args"] == [REDACTED_PLACEHOLDER, REDACTED_PLACEHOLDER, {"note": "x"}, None]

    def test_no_config_records_the_call_as_is(self):
        payload = _redacted_call_input(email_customer, (EMAIL, "Invoice"), {}, None)

        assert payload == {"args": [EMAIL, "Invoice"], "kwargs": {}}


class TestThroughTheDecorator:
    async def test_a_positional_pii_argument_is_never_recorded_raw(self):
        client = AsyncMock()
        client.handle.return_value = MagicMock(status="accepted", entity_id=uuid4())
        ctx = SessionContext(agent_id=uuid4())

        @trace(ingest_client=client, session_context=ctx, redaction_config=StandardPIIConfig())
        async def send(email: str, subject: str) -> str:
            return "sent"

        await send(EMAIL, "Invoice")

        payload = client.handle.call_args[0][0]["payload"]
        assert payload["input_redacted"] == {
            "args": [REDACTED_PLACEHOLDER, "Invoice"],
            "kwargs": {},
        }
        assert EMAIL not in str(payload)
        # Redaction runs before hashing: the hash is of the redacted payload.
        assert payload["input_hash"] == compute_payload_hash(payload["input_redacted"])


class TestExtraRulesOnPrebuiltConfigs:
    @pytest.mark.parametrize("config_cls", [FinancialPIIConfig, HealthcarePIIConfig])
    def test_extra_rules_are_added(self, config_cls):
        config = config_cls(extra_rules={"policy_number": r"POL-\d{6}"})

        redacted = config.redact({"policy_number": "POL-123456", "email": EMAIL})

        assert redacted == {"policy_number": REDACTED_PLACEHOLDER, "email": REDACTED_PLACEHOLDER}

    def test_built_in_rules_are_kept(self):
        assert FinancialPIIConfig(extra_rules={"x": "y"}).redact(
            {"iban": "GB29NWBK60161331926819"}
        ) == {"iban": REDACTED_PLACEHOLDER}
        assert HealthcarePIIConfig(extra_rules={"x": "y"}).redact({"mrn": "MRN12345678"}) == {
            "mrn": REDACTED_PLACEHOLDER
        }

    @pytest.mark.parametrize("config_cls", [FinancialPIIConfig, HealthcarePIIConfig])
    def test_no_arguments_still_works(self, config_cls):
        assert config_cls().redact({"email": EMAIL}) == {"email": REDACTED_PLACEHOLDER}
