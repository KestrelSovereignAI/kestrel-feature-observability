"""Recursive denylist redaction for inbound event ``metadata``.

Q2 (resolved): walk the ``metadata`` JSON recursively and replace any value
whose **key** matches a secret-ish pattern with ``"[REDACTED]"``. Mirrors the
key-name pattern set / behaviour of ``_SENSITIVE_KEY`` in the per-agent
``kestrel-feature-observability`` producer so the fleet consumer scrubs the same
things. Self-contained (no heavy dependency); no value-regex scrubbing in v1.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

# Key-name denylist. Covers the resolved pattern set — ``*token*``, ``*secret*``,
# ``*password*``, ``*api*key*``, ``authorization``, ``*credential*``,
# ``*private*key*`` — plus the extra aliases the producer's ``_SENSITIVE_KEY``
# already scrubs (auth/passwd/cookie/session-token/bearer) for consistency.
_SENSITIVE_KEY = re.compile(
    r"(api[_-]?key|secret|password|passwd|token|authorization|auth|"
    r"credential|private[_-]?key|cookie|session[_-]?token|bearer)",
    re.IGNORECASE,
)


def is_secret_key(key: Any) -> bool:
    """Return True when ``key`` looks like a secret-bearing field name."""
    return isinstance(key, str) and _SENSITIVE_KEY.search(key) is not None


def redact_metadata(metadata: Any) -> Any:
    """Return a copy of ``metadata`` with secret-keyed values redacted.

    Recurses through nested dicts and lists. Any dict value whose key matches
    the denylist is replaced with :data:`REDACTED`; everything else is walked
    and preserved. Scalars pass through unchanged.
    """
    if isinstance(metadata, dict):
        out: dict[Any, Any] = {}
        for key, value in metadata.items():
            if is_secret_key(key):
                out[key] = REDACTED
            else:
                out[key] = redact_metadata(value)
        return out
    if isinstance(metadata, list):
        return [redact_metadata(v) for v in metadata]
    return metadata


__all__ = ["redact_metadata", "is_secret_key", "REDACTED"]
