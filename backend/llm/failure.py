"""
Redacted descriptions of model-call failures.

A fail-closed path (the validator freezing because the model could not
answer) must say WHY, so an operator can tell a rate limit from a
malformed reply.  The description is safe to log and to show in a
verdict reason: it never contains a key, a request body or a reply
body.

- The text of a :class:`~backend.llm.interface.ModelCallError` is
  already free of the AppKey (``adp_client`` never puts it there), but
  it is redacted again here: whitespace collapsed, control characters
  removed, anything that looks like a token (a long run of letters,
  digits, ``-`` or ``_``) replaced, and the result capped.
- For an invalid reply only the error CLASS and, for a schema error,
  the field locations and error types are reported -- never the
  offending value, which would be a slice of the model's reply.
"""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from backend.llm.interface import ModelCallError

_MAX_MESSAGE_LEN = 100

# 20+ characters of key-like alphabet: an AppKey, a bearer token, a
# request id.  Normal English words and "HTTP 429" are far shorter.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{20,}")
_SPACE_RE = re.compile(r"\s+")


def redact(text: str, limit: int = _MAX_MESSAGE_LEN) -> str:
    """Collapse whitespace, drop control characters, mask token-like
    strings, and cap the length."""
    cleaned = "".join(ch if ch.isprintable() else " " for ch in str(text))
    cleaned = _SPACE_RE.sub(" ", cleaned).strip()
    cleaned = _TOKEN_RE.sub("<redacted>", cleaned)
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 3] + "..."
    return cleaned


def describe_failure(exc: BaseException) -> str:
    """Describe why a model call or its reply failed, without secrets.

    Examples::

        ModelCallError (ADP returned HTTP 429)
        invalid JSON after retry
        reply did not match the schema after retry (intents.0.foo: extra_forbidden)
    """
    if isinstance(exc, ModelCallError):
        message = redact(str(exc))
        return f"ModelCallError ({message})" if message else "ModelCallError"
    if isinstance(exc, json.JSONDecodeError):
        return "invalid JSON after retry"
    if isinstance(exc, ValidationError):
        parts = [
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['type']}"
            for err in exc.errors()[:3]
        ]
        detail = f" ({redact('; '.join(parts))})" if parts else ""
        return f"reply did not match the schema after retry{detail}"
    return f"{type(exc).__name__} after retry"
