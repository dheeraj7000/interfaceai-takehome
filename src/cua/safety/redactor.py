"""
PII & Sensitive Data Redactor.

Ensures that credentials, tokens, full PII (SSNs, account numbers, etc.)
are never persisted into artifacts, logs, or evidence. This is regulated
financial data — leaking it is not an option.

Approach:
1. Input parameters marked as `sensitive=True` in the artifact schema
   have their values replaced with "[REDACTED]" in all logs and evidence.
2. Known PII patterns (SSN, account numbers, etc.) are detected and
   redacted from free-text fields (visible text captures, error messages).
3. Screenshots can optionally have sensitive regions blurred (not implemented
   in this version — noted as a future enhancement).

The redactor is applied:
- When recording steps during discovery (before saving the artifact)
- When building step traces during replay (before saving evidence)
- When logging any action that involves sensitive parameters
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# PII patterns — regex patterns for common sensitive data formats
# ---------------------------------------------------------------------------

PII_PATTERNS: list[tuple[str, str]] = [
    # SSN (full and partial)
    (r"\b\d{3}-\d{2}-\d{4}\b", "[SSN-REDACTED]"),
    (r"\b\d{9}\b", "[SSN-REDACTED]"),  # SSN without dashes (9 consecutive digits)
    # Credit card numbers (basic)
    (r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b", "[CARD-REDACTED]"),
    # Account numbers (common formats)
    (r"\b\d{4}-[A-Z]{3}-\d{3}\b", "[ACCT-REDACTED]"),  # Our mock format: 1001-SAV-001
    # Email addresses
    (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", "[EMAIL-REDACTED]"),
    # Phone numbers
    (r"\b\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b", "[PHONE-REDACTED]"),
    # Routing numbers (9 digits starting with 0-3)
    (r"\b[0-3]\d{8}\b", "[ROUTING-REDACTED]"),
]

# Values that should always be redacted if they appear
SENSITIVE_KEYWORDS = [
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "credential",
]


class Redactor:
    """Redacts sensitive data from text and parameter maps."""

    def __init__(
        self,
        sensitive_param_names: set[str] | None = None,
        additional_patterns: list[tuple[str, str]] | None = None,
        redact_pii: bool = True,
    ):
        """
        Args:
            sensitive_param_names: Names of parameters marked as sensitive
                in the artifact schema (their values will be redacted everywhere)
            additional_patterns: Extra regex patterns to redact
            redact_pii: Whether to scan for and redact PII patterns
        """
        self._sensitive_params = sensitive_param_names or set()
        self._patterns = list(PII_PATTERNS)
        if additional_patterns:
            self._patterns.extend(additional_patterns)
        self._redact_pii = redact_pii
        # Cache of sensitive values seen (so we can redact them in free text)
        self._sensitive_values: set[str] = set()

    def register_sensitive_value(self, value: str) -> None:
        """Register a value that should be redacted wherever it appears."""
        if value and len(value) >= 3:  # Don't redact very short strings
            self._sensitive_values.add(value)

    def redact_params(self, params: dict[str, str]) -> dict[str, str]:
        """Redact sensitive parameter values. Returns a new dict."""
        result = {}
        for key, value in params.items():
            if key in self._sensitive_params or self._is_sensitive_key(key):
                result[key] = "[REDACTED]"
                self.register_sensitive_value(value)
            else:
                result[key] = value
        return result

    def redact_text(self, text: str) -> str:
        """Redact PII patterns and known sensitive values from text."""
        if not text:
            return text

        result = text

        # Redact known sensitive values
        for sensitive_val in self._sensitive_values:
            result = result.replace(sensitive_val, "[REDACTED]")

        # Redact PII patterns
        if self._redact_pii:
            for pattern, replacement in self._patterns:
                result = re.sub(pattern, replacement, result)

        return result

    def redact_log_entry(self, entry: dict) -> dict:
        """Redact sensitive data from a log entry dict (recursive)."""
        return self._redact_dict(entry)

    def _redact_dict(self, d: dict) -> dict:
        """Recursively redact sensitive values in a dict."""
        result = {}
        for key, value in d.items():
            if self._is_sensitive_key(key):
                result[key] = "[REDACTED]"
            elif isinstance(value, str):
                result[key] = self.redact_text(value)
            elif isinstance(value, dict):
                result[key] = self._redact_dict(value)
            elif isinstance(value, list):
                result[key] = [
                    self._redact_dict(item) if isinstance(item, dict)
                    else self.redact_text(item) if isinstance(item, str)
                    else item
                    for item in value
                ]
            else:
                result[key] = value
        return result

    @staticmethod
    def _is_sensitive_key(key: str) -> bool:
        """Check if a key name suggests sensitive content."""
        key_lower = key.lower()
        return any(kw in key_lower for kw in SENSITIVE_KEYWORDS)
