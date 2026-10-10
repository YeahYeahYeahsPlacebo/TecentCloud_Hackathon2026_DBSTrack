"""
LLM client interface.

Defines the data structures and protocol that every chat client must
implement.  No transport logic lives here — see
:mod:`backend.llm.adp_client` and :mod:`backend.llm.fake_client`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ChatReply:
    """The result of a single ``ask()`` call.

    Attributes:
        text: The full reply text assembled from the streaming response.
        conversation_id: The conversation id used for this call.  Every
            call uses a fresh id; nothing carries over.
        request_id: The request id sent with this call, for correlation
            with server-side logs.
    """

    text: str
    conversation_id: str
    request_id: str


class ModelCallError(Exception):
    """Raised when a chat call fails for any reason.

    Covers timeouts, HTTP errors, auth errors, malformed streams,
    server-side error events, and empty replies.

    The error message must **never** contain the AppKey or any other
    secret value.
    """


@runtime_checkable
class ChatClient(Protocol):
    """A stateless chat client.

    Every ``ask()`` starts a NEW conversation with a fresh conversation
    id and visitor id.  Nothing from one call may carry into the next.
    """

    def ask(self, message: str) -> ChatReply: ...
