"""RedactionConfig — per-field regex redaction for tool payloads.

The decorator passes input args and tool return values through this layer
before hashing or persisting. Matched values are replaced with the literal
[REDACTED] string in a *copy* of the payload — the original dict is never
mutated.

Field paths support dot notation:

    RedactionConfig({"user.email": r"[^@]+@[^@]+\\.[^@]+"})

would redact `payload["user"]["email"]` when its value matches the pattern.
Unconfigured fields pass through unchanged.

This is intentionally a Sprint 1 minimum:
  - dict-typed payloads only (lists/sets land in Sprint 3 if needed)
  - regex on the VALUE, not on the field path itself
  - whole-value replacement, not partial mask

A richer scheme (typed redactors, partial substring masking, allow-lists)
is in scope for the compliance dashboard in Phase 2.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED_PLACEHOLDER = "[REDACTED]"


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
        return self._redact_dict(payload, path="")

    def _redact_dict(self, obj: dict[str, Any], path: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for k, v in obj.items():
            full_path = f"{path}.{k}" if path else k
            result[k] = self._redact_value(full_path, v)
        return result

    def _redact_value(self, path: str, value: Any) -> Any:
        if path in self._rules and isinstance(value, str):
            if self._rules[path].search(value):
                return REDACTED_PLACEHOLDER
        if isinstance(value, dict):
            return self._redact_dict(value, path)
        return value
