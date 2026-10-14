"""
The rejection value and the standard error response shape (CONTRACT.md §5).

Every rejection is HTTP 422 with the same three fields::

    {"result": "rejected", "reason_code": "...", "message": "..."}

``reason_code`` is machine-readable and is the only field the client should
branch on. ``message`` is for the developer log, not for the end user.

A 500 uses the same three fields with ``result: "error"`` — see
:func:`error_response`. §5 fixes the shape of *every* error response, so ours
carry a reason code too (``internal_error``) instead of a bare ``detail``.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.gateway import reason_codes


@dataclass(frozen=True)
class Rejection:
    """A failed check. Carrying one is how a check says "stop here"."""

    reason_code: str
    message: str = ""

    def __post_init__(self) -> None:
        if self.reason_code not in reason_codes.ALL_REASON_CODES:
            raise ValueError(f"unknown reason_code: {self.reason_code!r}")

    def to_response(self) -> dict:
        """The wire shape every rejection uses."""
        return {
            "result": "rejected",
            "reason_code": self.reason_code,
            "message": self.message,
        }


def rejected(reason_code: str, message: str = "") -> Rejection:
    """Shorthand for constructing a Rejection."""
    return Rejection(reason_code=reason_code, message=message)


def rejection_response(reason_code: str, message: str = "") -> dict:
    """The wire shape, for endpoints that return a response body directly.

    Endpoint methods answer with a plain dict — a 200 payload or a 422
    rejection — so the HTTP layer only has to pick the status code.
    """
    return rejected(reason_code, message).to_response()


def error_response(message: str, *, bug: str) -> dict:
    """The wire shape for a 500 (CONTRACT.md §5).

    Same three fields as a rejection — ``result``, ``reason_code``,
    ``message`` — because §5 fixes the shape of every error response and
    does not exempt the 500s. ``result`` is ``"error"`` rather than
    ``"rejected"``: a 500 means *we* could not answer, not that the request
    was judged and refused, and a client that branched on "rejected" would
    otherwise show the user an error that is not theirs.

    ``bug`` is ours, not part of the contract: which of our own failures it
    was, for whoever reads the log.
    """
    return {
        "result": "error",
        "reason_code": reason_codes.INTERNAL_ERROR,
        "message": message,
        "bug": bug,
    }
