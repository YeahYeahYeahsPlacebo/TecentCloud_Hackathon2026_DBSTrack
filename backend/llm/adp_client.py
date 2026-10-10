"""
Tencent Cloud ADP Chat API client over HTTP SSE.

Implements :class:`~backend.llm.interface.ChatClient` using httpx to
POST to the ADP ``/adp/v2/chat`` endpoint and read the SSE stream.

Documentation reference:
    https://www.tencentcloud.com/document/product/1254/81449
    "Chat API Documentation (HTTP SSE)"

Request body fields (from the docs):
    RequestId        — String, optional.  32-64 chars, UUID recommended.
    ConversationId   — String, required.   32-64 chars, UUID recommended.
    AppKey           — String, required.   Application key.
    VisitorId        — String, required.   Visitor ID.
    Contents         — Array of Content.   Each item: {Type: "text", Text: "..."}.
    Incremental      — Boolean, optional.  Default false.  When true,
                       text is streamed as ``text.delta`` events.

SSE event types consumed:
    request_ack        — request confirmed.
    message.added      — announces a new message; carries ``Message.Type``
                         which is ``"thought"`` for reasoning content or
                         ``"reply"`` for the final reply.
    text.delta         — incremental text fragment (``Text`` field,
                         ``MessageId`` links to the owning message).
    text.replace       — full replacement text (``Text`` field).
    response.completed — final response with ``Messages`` array; each
                         message has a ``Type`` (``"thought"``,
                         ``"reply"``, ``"tool_call"``, etc.).
    error              — server-side error (``Error.Code``, ``Error.Message``).
    done               — stream terminator (data is ``[DONE]``).

Reasoning / thinking content is marked by ``Message.Type == "thought"``.
The client discards all non-reply content and returns only text from
messages explicitly announced as ``Type: "reply"``.  Deltas whose
``MessageId`` was never announced (or whose type is ``"thought"``,
``"tool_call"``, unknown, etc.) are excluded — fail closed.
See: https://www.tencentcloud.com/document/product/1254/81449
     "Chat API Documentation (HTTP SSE)" — message.added event,
     response.completed Messages array.

This module contains no parser or validator logic — it is plumbing only.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from backend.llm.interface import ChatReply, ModelCallError

# ── Constants ────────────────────────────────────────────────────────

DEFAULT_ENDPOINT = "https://wss.lke.tencentcloud.com/adp/v2/chat"
DEFAULT_TIMEOUT = 30

# Wait this many seconds before retrying after an empty reply or a
# rate-limit error from ADP.
_RETRY_WAIT_SECONDS = 2

# SSE event types that matter for text assembly and error handling.
_EVENT_MESSAGE_ADDED = "message.added"
_EVENT_TEXT_DELTA = "text.delta"
_EVENT_TEXT_REPLACE = "text.replace"
_EVENT_RESPONSE_COMPLETED = "response.completed"
_EVENT_ERROR = "error"
_EVENT_DONE = "done"

# Message Type values that distinguish reasoning from reply content.
_MSG_TYPE_THOUGHT = "thought"
_MSG_TYPE_REPLY = "reply"

# ADP error codes that indicate rate limiting.  Retrying after a short
# wait often succeeds.  Codes observed in practice:
#   460014 — request frequency limit (QPS exceeded)
#   460015 — request frequency limit (daily quota)
#
# These codes are matched as strings so both int and str values from
# JSON work correctly.
_RATE_LIMIT_CODES: set[str] = {"460014", "460015"}


# ── Request building ─────────────────────────────────────────────────


def build_request_body(
    *,
    app_key: str,
    message: str,
    conversation_id: str,
    visitor_id: str,
    request_id: str,
) -> dict[str, Any]:
    """Build the JSON body for an ADP chat request.

    Uses the exact field names from the docs: ``RequestId``,
    ``ConversationId``, ``AppKey``, ``VisitorId``, ``Contents``,
    ``Incremental``, ``Stream``.

    This function is pure and can be tested in isolation.
    """
    return {
        "RequestId": request_id,
        "ConversationId": conversation_id,
        "AppKey": app_key,
        "VisitorId": visitor_id,
        "Contents": [
            {
                "Type": "text",
                "Text": message,
            }
        ],
        "Incremental": True,
        "Stream": "enable",
    }


# ── SSE event parsing ────────────────────────────────────────────────


def parse_sse_stream(
    raw_stream: str,
) -> tuple[list[str], str | None, str | None, dict | None]:
    """Parse an SSE stream and return (reply_deltas, completed_reply, conv_id, error).

    Splits the raw text into events (separated by ``\\n\\n``), extracts
    the event type from the ``event:`` line and the JSON payload from
    the ``data:`` line.

    **Fail-closed on message types.**  Only text from messages
    explicitly announced as ``Type: "reply"`` (via ``message.added``
    events) may become reply text.  Deltas whose ``MessageId`` was
    never announced, or whose type is anything other than ``"reply"``
    (``"thought"``, ``"tool_call"``, ``"unknown"``, etc.) are excluded.

    Returns:
        reply_deltas: text fragments from ``text.delta`` /
            ``text.replace`` events whose ``MessageId`` belongs to a
            reply-announced message.
        completed_reply: the full reply text from the
            ``response.completed`` event's ``Type: "reply"`` message,
            or ``None`` if not present.
        conversation_id: from ``response.completed``.
        error: the error dict if an ``error`` event was received.

    The caller prefers ``completed_reply`` when it is non-empty;
    otherwise it joins ``reply_deltas``.  The two are never
    concatenated together — that would duplicate text when both
    deltas and the completed event carry the reply.
    """
    reply_deltas: list[str] = []
    completed_reply: str | None = None
    conversation_id: str | None = None
    error_data: dict | None = None

    # MessageIds explicitly announced as Type "reply" via message.added.
    # Only deltas whose MessageId is in this set are accepted.
    # This is fail-closed: unannounced, thought, tool_call, and unknown
    # type MessageIds are all excluded.
    reply_message_ids: set[str] = set()

    # SSE events are separated by double newlines.
    events = raw_stream.split("\n\n")
    for event_block in events:
        event_type: str | None = None
        data_line: str | None = None

        for line in event_block.strip().splitlines():
            if line.startswith("event:"):
                event_type = line[len("event:"):].strip().strip('"')
            elif line.startswith("data:"):
                data_line = line[len("data:"):].strip()

        if event_type is None or data_line is None:
            continue

        # The ``done`` event has data ``[DONE]`` — not JSON.
        if event_type == _EVENT_DONE:
            continue

        try:
            data = json.loads(data_line)
        except json.JSONDecodeError:
            # Malformed event data — skip but don't crash; the caller
            # will raise if the stream ends without any text.
            continue

        if event_type == _EVENT_ERROR:
            error_data = data
            break

        # ── Track reply-announced messages by their MessageId ──────
        if event_type == _EVENT_MESSAGE_ADDED:
            msg = data.get("Message", {})
            if msg.get("Type") == _MSG_TYPE_REPLY:
                mid = msg.get("MessageId")
                if mid:
                    reply_message_ids.add(mid)
            continue

        # ── Only accept text from reply-announced messages ─────────
        if event_type in (_EVENT_TEXT_DELTA, _EVENT_TEXT_REPLACE):
            mid = data.get("MessageId")
            if mid is None or mid not in reply_message_ids:
                # Unannounced, thought, tool_call, or unknown — discard.
                continue

            text = data.get("Text", "")
            if not text:
                continue

            if event_type == _EVENT_TEXT_REPLACE:
                # text.replace carries the full accumulated text so far
                # for this message — replace previous reply deltas.
                reply_deltas = [text]
            else:
                reply_deltas.append(text)

        # ── Extract reply text from response.completed ──────────────
        if event_type == _EVENT_RESPONSE_COMPLETED:
            response = data.get("Response", {})
            cid = response.get("ConversationId")
            if cid:
                conversation_id = cid
            messages = response.get("Messages", [])
            for msg in messages:
                if msg.get("Type") == _MSG_TYPE_REPLY:
                    contents = msg.get("Contents", [])
                    for content in contents:
                        if content.get("Type") == "text":
                            t = content.get("Text", "")
                            if t:
                                completed_reply = (
                                    completed_reply + t
                                    if completed_reply is not None
                                    else t
                                )

    return reply_deltas, completed_reply, conversation_id, error_data


@dataclass
class SseEventTrace:
    """One row in the debug event trace for ``--debug`` output.

    Attributes:
        event_type: The SSE ``event:`` value.
        message_type: The ``Message.Type`` value if the event carries a
            Message object (e.g. ``message.added``, ``response.completed``),
            otherwise ``None``.
        text_len: Character count of the ``Text`` field if present,
            otherwise 0.
    """

    event_type: str
    message_type: str | None
    text_len: int


def parse_sse_stream_debug(raw_stream: str) -> list[SseEventTrace]:
    """Parse an SSE stream and return a trace of every event.

    For each event, records the event type, the Message.Type (if the
    event carries a Message object), and the character count of any
    Text field.  Used by ``scripts/smoke_adp.py --debug`` to diagnose
    reasoning vs reply content.

    Never returns the Text content itself — only the length — so it
    is safe to print without leaking prompt or reply text.
    """
    traces: list[SseEventTrace] = []
    events = raw_stream.split("\n\n")
    for event_block in events:
        event_type: str | None = None
        data_line: str | None = None

        for line in event_block.strip().splitlines():
            if line.startswith("event:"):
                event_type = line[len("event:"):].strip().strip('"')
            elif line.startswith("data:"):
                data_line = line[len("data:"):].strip()

        if event_type is None or data_line is None:
            continue

        if event_type == _EVENT_DONE:
            traces.append(SseEventTrace(event_type=event_type, message_type=None, text_len=0))
            continue

        try:
            data = json.loads(data_line)
        except json.JSONDecodeError:
            traces.append(SseEventTrace(event_type=event_type, message_type=None, text_len=0))
            continue

        # Extract Message.Type if present.
        msg = data.get("Message")
        if msg is None and event_type == _EVENT_RESPONSE_COMPLETED:
            # response.completed has Messages array, not a single Message.
            # We record the message types separately below.
            pass
        message_type = msg.get("Type") if isinstance(msg, dict) else None

        # Extract Text length.
        text = data.get("Text", "")
        text_len = len(text) if isinstance(text, str) else 0

        traces.append(SseEventTrace(
            event_type=event_type,
            message_type=message_type,
            text_len=text_len,
        ))

        # For response.completed, also trace each message in the array.
        if event_type == _EVENT_RESPONSE_COMPLETED:
            response = data.get("Response", {})
            messages = response.get("Messages", [])
            for m in messages:
                m_type = m.get("Type")
                m_text_len = 0
                for content in m.get("Contents", []):
                    t = content.get("Text", "")
                    if isinstance(t, str):
                        m_text_len += len(t)
                traces.append(SseEventTrace(
                    event_type="  (msg)",
                    message_type=m_type,
                    text_len=m_text_len,
                ))

    return traces


# ── Client ───────────────────────────────────────────────────────────


class AdpChatClient:
    """HTTP SSE client for the Tencent Cloud ADP Chat API.

    Every ``ask()`` starts a NEW conversation with a fresh conversation
    id and visitor id.  The client is stateless: nothing from one call
    may carry into the next.

    The AppKey is never printed, logged, or included in error messages.
    """

    def __init__(
        self,
        app_key: str,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout_seconds: int = DEFAULT_TIMEOUT,
        *,
        _transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._app_key = app_key
        self._endpoint = endpoint
        self._timeout = timeout_seconds
        self._transport = _transport

    def __repr__(self) -> str:
        return (
            f"AdpChatClient(app_key=<hidden>, endpoint={self._endpoint!r}, "
            f"timeout_seconds={self._timeout})"
        )

    def ask(self, message: str) -> ChatReply:
        """Send a message and return the full reply text.

        Raises :class:`ModelCallError` on timeouts, HTTP errors, auth
        errors, malformed streams, server error events, and empty
        replies.

        **Retry policy.** If ADP returns an empty reply (stream ended
        with no reply text) or a rate-limit error event, the client
        waits ``_RETRY_WAIT_SECONDS`` and retries once.  If the retry
        also fails, the error is raised (fail closed).  All other
        errors (timeouts, auth, HTTP non-200, non-rate-limit error
        events) are raised immediately without retry.
        """
        reply, _ = self._ask_internal(message)
        return reply

    def _ask_internal(self, message: str) -> tuple[ChatReply, str]:
        """Send a message and return (ChatReply, raw_sse_stream).

        The raw stream is exposed so ``--debug`` tools can trace
        events without re-issuing the request.
        """
        try:
            full_text, raw_stream, resp_conversation_id, request_id = (
                self._attempt(message)
            )
        except ModelCallError as exc:
            if not _should_retry(exc):
                raise
            # Retry once after a short wait.
            time.sleep(_RETRY_WAIT_SECONDS)
            full_text, raw_stream, resp_conversation_id, request_id = (
                self._attempt(message)
            )

        return (
            ChatReply(
                text=full_text,
                conversation_id=resp_conversation_id,
                request_id=request_id,
            ),
            raw_stream,
        )

    def _attempt(
        self,
        message: str,
    ) -> tuple[str, str, str, str]:
        """One attempt: build request, call ADP, parse SSE.

        Returns ``(full_text, raw_stream, resp_conversation_id,
        request_id)`` on success, or raises :class:`ModelCallError`.

        Never includes the AppKey or request body in the error
        message.  Includes the HTTP status, and the error event's Code
        and Message when available.
        """
        conversation_id = str(uuid.uuid4())
        visitor_id = str(uuid.uuid4())
        request_id = str(uuid.uuid4())

        body = build_request_body(
            app_key=self._app_key,
            message=message,
            conversation_id=conversation_id,
            visitor_id=visitor_id,
            request_id=request_id,
        )

        http_status: int | None = None
        try:
            client_kwargs: dict[str, Any] = {"timeout": self._timeout}
            if self._transport is not None:
                client_kwargs["transport"] = self._transport
            with httpx.Client(**client_kwargs) as client:
                with client.stream(
                    "POST",
                    self._endpoint,
                    json=body,
                    headers={"Content-Type": "application/json"},
                ) as response:
                    http_status = response.status_code
                    if response.status_code != 200:
                        raise ModelCallError(
                            f"ADP returned HTTP {response.status_code}"
                        )

                    raw_stream = response.read().decode("utf-8")
        except httpx.TimeoutException:
            raise ModelCallError("ADP request timed out")
        except httpx.HTTPError as exc:
            # Never include the key — httpx errors won't contain it,
            # but be defensive.
            raise ModelCallError(f"ADP HTTP error: {type(exc).__name__}")
        except ModelCallError:
            raise
        except Exception as exc:
            raise ModelCallError(f"ADP unexpected error: {type(exc).__name__}")

        reply_deltas, completed_reply, resp_conversation_id, error_data = (
            parse_sse_stream(raw_stream)
        )

        if error_data is not None:
            error = error_data.get("Error", {})
            code = error.get("Code", "unknown")
            # The server-side Message may reference the app or request
            # but never the raw AppKey value.
            msg = error.get("Message", "ADP error event received")
            raise ModelCallError(f"ADP error {code}: {msg}")

        # Prefer the completed reply text when available; otherwise
        # assemble from reply deltas.  Never concatenate the two —
        # that would duplicate text when both carry the reply.
        if completed_reply is not None and completed_reply.strip():
            full_text = completed_reply
        else:
            full_text = "".join(reply_deltas)

        if not full_text.strip():
            # Include whatever ADP sent back: the HTTP status (known
            # to be 200 at this point since non-200 raised above), and
            # a note that no reply-announced message was found.  Never
            # include the key or request body.
            status_str = str(http_status) if http_status is not None else "unknown"
            raise ModelCallError(
                f"ADP returned no reply text "
                f"(HTTP {status_str}, no messages announced as Type 'reply')"
            )

        return full_text, raw_stream, resp_conversation_id or conversation_id, request_id


def _should_retry(exc: ModelCallError) -> bool:
    """Return True if the error is retryable (empty reply or rate-limit).

    Empty replies and rate-limit error events are transient — retrying
    after a short wait often succeeds.  All other errors (timeouts,
    auth, HTTP non-200, non-rate-limit error events) are not retried.
    """
    msg = str(exc).lower()

    # Empty reply — the stream ended without reply text.
    if "no reply text" in msg:
        return True

    # Rate-limit error events carry the code in the message.
    # _should_retry checks the message text because ModelCallError
    # stores everything as a string.
    if "rate" in msg or "frequency" in msg or "quota" in msg:
        return True

    # Check known rate-limit codes.
    for code in _RATE_LIMIT_CODES:
        if code in str(exc):
            return True

    return False
