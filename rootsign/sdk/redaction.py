"""RedactionConfig — per-field regex redaction for tool payloads.

The decorator passes input args and tool return values through this layer
before hashing or persisting. Matched values are replaced with the literal
[REDACTED] string in a *copy* of the payload — the original dict is never
mutated.

## Matching semantics (audit fix — leaf-key default)

Two rule-key shapes are supported:

* **Bare names** (no dot) → **leaf-key match.** Rule `"email"` fires on
  any field named `email` regardless of nesting. So a payload
  `{"args": [], "kwargs": {"user": {"email": "alice@..."}}}` redacts
  `kwargs.user.email` because the leaf is `email`.

* **Dotted paths** → **exact-path match.** Rule `"user.email"` fires only
  when the field path is exactly `user.email` — top-level under key
  `user` only.

The decorator's payload envelope is `{"args": [...], "kwargs": {...}}`,
so leaf-key matching is the only way pre-built configs (`email`,
`phone`, `ssn`) hit real PII coming through `kwargs.*`. Path-key
matching survives for advanced users with strict nesting requirements.

`match_mode="path"` opts out of leaf inference entirely — every rule
key is treated as a full path, even bare names. Useful when you need
to distinguish `user.email` from `audit.email` and don't want bare
`email` to redact both.

Sprint 3 hardening (ADR-006), Sprint 4 audit:
  - List items walked recursively (lists of dicts redact through).
  - Depth-limited at 5 levels — fails CLOSED past the bound (returns
    `[REDACTED]`, not the raw subtree).
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

    def __init__(
        self,
        rules: dict[str, str],
        *,
        match_mode: str = "leaf",
    ):
        """rules: {field_key: regex_pattern}

        field_key: either a bare name (`"email"`) for leaf-key matching,
            or a dotted path (`"user.email"`) for exact-path matching.
            See module docstring for the full semantics.
        regex_pattern: if the field VALUE matches this pattern, redact it.
        match_mode: ``"leaf"`` (default) — bare keys = leaf-key match,
            dotted keys = path match. ``"path"`` — every key is treated
            as an exact path, no leaf inference.

        Examples::

            # Leaf-key match: redacts `email` anywhere in the payload tree
            RedactionConfig({"email": r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}"})

            # Strict path-only mode for `audit.email`, leaves `user.email` alone
            RedactionConfig({"audit.email": r".+@.+"}, match_mode="path")
        """
        if match_mode not in ("leaf", "path"):
            raise ValueError(
                f"match_mode must be 'leaf' or 'path', got {match_mode!r}"
            )
        self._match_mode = match_mode
        self._leaf_rules: dict[str, re.Pattern[str]] = {}
        self._path_rules: dict[str, re.Pattern[str]] = {}
        for key, pattern in rules.items():
            compiled = re.compile(pattern)
            if match_mode == "path" or "." in key:
                # Dotted keys are always paths regardless of mode. Bare
                # keys become paths only when the user explicitly opts in
                # via match_mode="path".
                self._path_rules[key] = compiled
            else:
                self._leaf_rules[key] = compiled

    @property
    def match_mode(self) -> str:
        return self._match_mode

    def _has_rules(self) -> bool:
        return bool(self._leaf_rules) or bool(self._path_rules)

    def redact(self, payload: Any) -> Any:
        """Return a new payload with matching fields redacted.

        Does not mutate the input. None and non-dict payloads pass through
        unchanged. Empty rule set is a no-op even for dicts (avoids the
        deep-copy round trip).
        """
        if not isinstance(payload, dict):
            return payload
        if not self._has_rules():
            return payload
        return self._redact_dict(payload, path="", depth=0)

    def _redact_dict(self, obj: dict[str, Any], path: str, depth: int) -> Any:
        # Fail CLOSED past the depth limit — return the redaction
        # placeholder rather than the raw subtree. Audit fix.
        if depth > MAX_REDACTION_DEPTH:
            return REDACTED_PLACEHOLDER
        result: dict[str, Any] = {}
        for k, v in obj.items():
            full_path = f"{path}.{k}" if path else k
            result[k] = self._redact_value(full_path, v, depth + 1)
        return result

    def _matched_rule(self, path: str) -> re.Pattern[str] | None:
        """Resolve which rule (if any) applies to a given field path.

        Path-keyed rules always win over leaf-keyed rules when both
        match — explicit beats inferred. This matters for the audit-fix
        scenario where a user adds a tighter `kwargs.email` path rule
        alongside a broad leaf `email` rule.
        """
        if path in self._path_rules:
            return self._path_rules[path]
        if self._leaf_rules:
            leaf = path.rsplit(".", 1)[-1] if "." in path else path
            if leaf in self._leaf_rules:
                return self._leaf_rules[leaf]
        return None

    def _redact_value(self, path: str, value: Any, depth: int) -> Any:
        # Same fail-closed rule as _redact_dict — see note above.
        if depth > MAX_REDACTION_DEPTH:
            return REDACTED_PLACEHOLDER
        # Rule matches only fire on string values — applying a regex to
        # anything else would crash, so silently skip mismatched types
        # instead of throwing.
        rule = self._matched_rule(path)
        if rule is not None and isinstance(value, str):
            if rule.search(value):
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
