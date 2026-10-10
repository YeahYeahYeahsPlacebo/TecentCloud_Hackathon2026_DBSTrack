"""
Fake chat client for tests.

Returns canned replies in order, records the messages it received,
and can be told to raise :class:`ModelCallError`.
"""

from __future__ import annotations

from collections.abc import Sequence

from backend.llm.interface import ChatReply, ModelCallError


class FakeChatClient:
    """A scripted :class:`~backend.llm.interface.ChatClient` for tests.

    - ``scripted_replies`` are returned in order, one per ``ask()`` call.
    - If ``raise_on_call`` is set, every ``ask()`` raises
      :class:`ModelCallError` instead of returning a reply.
    - Every message received is recorded in ``received_messages``.
    - Every reply uses a unique conversation_id and request_id.
    """

    def __init__(
        self,
        scripted_replies: Sequence[str] | None = None,
        *,
        raise_on_call: bool = False,
    ) -> None:
        self._replies = list(scripted_replies or [])
        self._raise_on_call = raise_on_call
        self.received_messages: list[str] = []
        self._call_count = 0

    def __repr__(self) -> str:
        return (
            f"FakeChatClient(replies={len(self._replies)}, "
            f"calls={self._call_count}, raise_on_call={self._raise_on_call})"
        )

    def ask(self, message: str) -> ChatReply:
        self.received_messages.append(message)
        self._call_count += 1

        if self._raise_on_call:
            raise ModelCallError("FakeChatClient configured to raise")

        if not self._replies:
            raise ModelCallError("FakeChatClient has no scripted reply left")

        text = self._replies.pop(0)
        # Use call count to make ids unique across calls.
        import uuid

        return ChatReply(
            text=text,
            conversation_id=str(uuid.uuid4()),
            request_id=str(uuid.uuid4()),
        )
