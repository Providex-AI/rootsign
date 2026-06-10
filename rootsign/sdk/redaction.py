"""RedactionConfig — per-field regex redaction for tool payloads.

The decorator passes input args and tool return values through this layer
before hashing or persisting. Matched values are replaced with the literal
[REDACTED] string in a *copy* of the payload — the original dict is never
mutated.

Field paths support dot notation:

    RedactionConfig({"user.email": r"[^@]+@[^@]+\\.[^@]+"})

would redact `payload["user"]["email"]` when its value matches the pattern.
Unconfigured fields pass through unchanged.

Sprint 3 hardening (ADR-006):
  - List items walked recursively (lists of dicts redact through).
  - Depth-limited at 5 levels to bound recursion on adversarial payloads.
  - Pre-built `StandardPIIConfig`, `FinancialPIIConfig`,
    `HealthcarePIIConfig` for common patterns — design partners can extend
    via `extra_rules=`.

ADR-006: redaction runs BEFORE hashing. The hash is always of the redacted
payload, so PII cannot be re-identified from stored hashes.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED_PLACEHOLDER = "[REDACTED]"

# Maximum recursion depth for nested dicts / lists. Anything deeper is
# returned unchanged. Five levels covers every realistic tool payload
# (LangChain / CrewAI tools rarely nest beyond 2–3) while blocking
# pathological inputs from blowing the stack. See ADR-006.
MAX_REDACTION_DEPTH = 5


class RedactionConfig:
    """Per-field regex redaction policy. Immutable after construction."""

    def __init__(self, rules: dict[str, str]):
        """rules: {field_path: regex_pattern}

        field_path supports dot notation (e.g. 'user.email').
        regex_pattern: if the field VALUE matches this pattern, redact it.

        Examples::

            RedactionConfig({"email": r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}"})
            RedactionConfig({"ssn":   r"\\d{3}-\\d{2}-\\d{4}"})
        """
        self._rules = {path: re.compile(pattern) for path, pattern in rules.items()}

    def redact(self, payload: Any) -> Any:
        """Return a new payload with matching fields redacted.

        Does not mutate the input. None and non-dict payloads pass through
        unchanged. Empty rule set is a no-op even for dicts (avoids the
        deep-copy round trip).
        """
        if not isinstance(payload, dict):
            return payload
        if not self._rules:
            return payload
        return self._redact_dict(payload, path="", depth=0)

    def _redact_dict(self, obj: dict[str, Any], path: str, depth: int) -> dict[str, Any]:
        if depth > MAX_REDACTION_DEPTH:
            return obj
        result: dict[str, Any] = {}
        for k, v in obj.items():
            full_path = f"{path}.{k}" if path else k
            result[k] = self._redact_value(full_path, v, depth + 1)
        return result

    def _redact_value(self, path: str, value: Any, depth: int) -> Any:
        if depth > MAX_REDACTION_DEPTH:
            return value
        # Path-keyed rule matches only fire on string values — applying a
        # regex to anything else would crash, so silently skip mismatched
        # types instead of throwing.
        if path in self._rules and isinstance(value, str):
            if self._rules[path].search(value):
                return REDACTED_PLACEHOLDER
        if isinstance(value, dict):
            return self._redact_dict(value, path, depth)
        if isinstance(value, list):
            # Walk list items individually — items keep the parent's path
            # for rule lookup so `payload.emails` redaction applies to each
            # email string in `{"emails": ["a@b", "c@d"]}`.
            return [self._redact_value(path, item, depth + 1) for item in value]
        return value


# ---------------------------------------------------------------------------
# Pre-built PII configurations (Sprint 3 §4.3)
# ---------------------------------------------------------------------------

# Field-name → regex pattern. Pragmatic regexes — they aim to catch the
# common shape and accept some false positives over false negatives. Tighten
# per design-partner feedback as real PII patterns surface.
STANDARD_PII_PATTERNS: dict[str, str] = {
    "email": r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
    "phone": r"(\+?\d[\s\-\.]?){7,15}",
    "ssn": r"\d{3}[\-\s]?\d{2}[\-\s]?\d{4}",
    "credit_card": r"\b(?:\d[\s\-]?){13,16}\b",
    "uk_ni": r"[A-Z]{2}\d{6}[A-Z]",
}


class StandardPIIConfig(RedactionConfig):
    """Pre-built redaction config for common PII patterns.

    Covers: email, phone, US SSN, credit card, UK NI number.

    Usage::

        from rootsign import StandardPIIConfig
        config = StandardPIIConfig()
        # Or with extra patterns:
        config = StandardPIIConfig(extra_rules={"mrn": r"MRN\\d{8}"})
    """

    def __init__(self, extra_rules: dict[str, str] | None = None):
        rules = dict(STANDARD_PII_PATTERNS)
        if extra_rules:
            rules.update(extra_rules)
        super().__init__(rules=rules)


class FinancialPIIConfig(StandardPIIConfig):
    """StandardPIIConfig + financial field names commonly seen in fintech agents."""

    def __init__(self):
        super().__init__(
            extra_rules={
                "account_number": r"\d{8,17}",
                "routing_number": r"\d{9}",
                "iban": r"[A-Z]{2}\d{2}[A-Z0-9]{4}\d{7}([A-Z0-9]?){0,16}",
            }
        )


class HealthcarePIIConfig(StandardPIIConfig):
    """StandardPIIConfig + healthcare field names (HIPAA-relevant)."""

    def __init__(self):
        super().__init__(
            extra_rules={
                "mrn": r"MRN\d{6,10}",
                "npi": r"\d{10}",
                "dob": r"\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}",
            }
        )
