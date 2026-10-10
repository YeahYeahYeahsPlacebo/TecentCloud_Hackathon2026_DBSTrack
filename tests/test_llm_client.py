"""
Tests for the LLM client plumbing.

Uses httpx.MockTransport to simulate ADP SSE responses — no real
network calls anywhere.  Also tests the FakeChatClient and the
config loader.

SSE sample text is built from the documented format at:
    https://www.tencentcloud.com/document/product/1254/81449
    "Chat API Documentation (HTTP SSE)"
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.llm.adp_client import (  # noqa: E402
    DEFAULT_ENDPOINT,
    AdpChatClient,
    build_request_body,
    parse_sse_stream,
)
from backend.llm.config import load_llm_settings  # noqa: E402
from backend.llm.fake_client import FakeChatClient  # noqa: E402
from backend.llm.interface import ChatClient, ModelCallError  # noqa: E402

DUMMY_KEY = "test-app-key-1234567890abcdef"


# ── Helpers ──────────────────────────────────────────────────────────


def make_sse_stream(
    *,
    text_chunks: list[str] | None = None,
    conversation_id: str = "conv-001",
    error: dict | None = None,
    include_done: bool = True,
) -> str:
    """Build a realistic SSE stream from documented event format.

    Follows the response example in the ADP HTTP SSE docs:
        event: request_ack
        data: {\"Type\": \"request_ack\", ...}

        event: message.added
        data: {\"Type\": \"message.added\", \"Message\": {\"Type\": \"reply\", ...}}

        event: text.delta
        data: {\"Type\": \"text.delta\", \"Text\": \"Hello\", \"MessageId\": \"rpl-001\"}

        event: response.completed
        data: {\"Type\": \"response.completed\", \"Response\": {...}}

        event: done
        data: [DONE]
    """
    reply_msg_id = "rpl-001"
    parts: list[str] = []

    # request_ack event
    parts.append(
        'event: request_ack\n'
        f'data: {json.dumps({"Type": "request_ack", "RequestAck": {"ConversationId": conversation_id, "Status": "success"}})}'
    )

    # message.added event announcing the reply message
    if error is None:
        parts.append(
            'event: message.added\n'
            f'data: {json.dumps({"Type": "message.added", "Message": {"Type": "reply", "MessageId": reply_msg_id, "Name": "reply", "Status": "processing"}})}'
        )

    # text.delta events — linked to the reply message
    if error is None and text_chunks:
        for chunk in text_chunks:
            parts.append(
                'event: text.delta\n'
                f'data: {json.dumps({"Type": "text.delta", "Text": chunk, "MessageId": reply_msg_id})}'
            )

    # response.completed event
    if error is None:
        full_text = "".join(text_chunks or [])
        parts.append(
            'event: response.completed\n'
            f'data: {json.dumps({"Type": "response.completed", "Response": {"ConversationId": conversation_id, "Status": "success", "Messages": [{"Type": "reply", "MessageId": reply_msg_id, "Contents": [{"Type": "text", "Text": full_text}]}]}})}'
        )

    # error event
    if error is not None:
        parts.append(
            'event: error\n'
            f'data: {json.dumps({"Type": "error", "Error": error})}'
        )

    # done event
    if include_done:
        parts.append('event: done\ndata: [DONE]')

    return "\n\n".join(parts) + "\n\n"


def make_reasoning_stream(
    *,
    reasoning_chunks: list[str],
    reply_chunks: list[str],
    conversation_id: str = "conv-reason-001",
) -> str:
    """Build an SSE stream with reasoning (thought) content followed by a reply.

    Follows the documented format where reasoning models emit:
      1. message.added with Message.Type = "thought" (reasoning)
      2. text.delta events for the reasoning text (MessageId = thought id)
      3. message.added with Message.Type = "reply" (final answer)
      4. text.delta events for the reply text (MessageId = reply id)
      5. response.completed with Messages array containing both thought
         and reply messages

    Doc reference:
        https://www.tencentcloud.com/document/product/1254/81449
        "Chat API Documentation (HTTP SSE)" — message.added event,
        response.completed Messages array.
    """
    thought_id = "tht-001"
    reply_id = "rpl-001"
    parts: list[str] = []

    # request_ack
    parts.append(
        'event: request_ack\n'
        f'data: {json.dumps({"Type": "request_ack", "RequestAck": {"ConversationId": conversation_id, "Status": "success"}})}'
    )

    # message.added for thought (reasoning)
    parts.append(
        'event: message.added\n'
        f'data: {json.dumps({"Type": "message.added", "Message": {"Type": "thought", "MessageId": thought_id, "Name": "thought", "Title": "Reasoning", "Status": "processing"}})}'
    )

    # text.delta for reasoning content
    for chunk in reasoning_chunks:
        parts.append(
            'event: text.delta\n'
            f'data: {json.dumps({"Type": "text.delta", "Text": chunk, "MessageId": thought_id})}'
        )

    # message.added for reply
    parts.append(
        'event: message.added\n'
        f'data: {json.dumps({"Type": "message.added", "Message": {"Type": "reply", "MessageId": reply_id, "Name": "reply", "Status": "processing"}})}'
    )

    # text.delta for reply content
    for chunk in reply_chunks:
        parts.append(
            'event: text.delta\n'
            f'data: {json.dumps({"Type": "text.delta", "Text": chunk, "MessageId": reply_id})}'
        )

    # response.completed with both thought and reply messages
    full_reasoning = "".join(reasoning_chunks)
    full_reply = "".join(reply_chunks)
    completed_data = {
        "Type": "response.completed",
        "Response": {
            "ConversationId": conversation_id,
            "Status": "success",
            "Messages": [
                {
                    "Type": "thought",
                    "MessageId": thought_id,
                    "Contents": [{"Type": "text", "Text": full_reasoning}],
                },
                {
                    "Type": "reply",
                    "MessageId": reply_id,
                    "Contents": [{"Type": "text", "Text": full_reply}],
                },
            ],
        },
    }
    parts.append(
        'event: response.completed\n'
        f'data: {json.dumps(completed_data)}'
    )

    # done
    parts.append('event: done\ndata: [DONE]')

    return "\n\n".join(parts) + "\n\n"


def make_mock_transport(sse_content: str, status: int = 200) -> httpx.MockTransport:
    """Create a MockTransport that returns the given SSE content."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            content=sse_content.encode("utf-8"),
            headers={"Content-Type": "text/event-stream"},
        )
    return httpx.MockTransport(handler)


# ── build_request_body ───────────────────────────────────────────────


class TestBuildRequestBody:
    def test_contains_required_fields(self):
        body = build_request_body(
            app_key="mykey",
            message="Hello",
            conversation_id="conv-1",
            visitor_id="vis-1",
            request_id="req-1",
        )
        assert body["AppKey"] == "mykey"
        assert body["ConversationId"] == "conv-1"
        assert body["VisitorId"] == "vis-1"
        assert body["RequestId"] == "req-1"
        assert body["Contents"] == [{"Type": "text", "Text": "Hello"}]
        assert body["Incremental"] is True
        assert body["Stream"] == "enable"

    def test_key_never_appears_in_repr_of_client(self):
        client = AdpChatClient(app_key="super-secret-key-12345")
        r = repr(client)
        assert "super-secret-key-12345" not in r
        assert "<hidden>" in r


# ── parse_sse_stream ─────────────────────────────────────────────────


class TestParseSseStream:
    def test_multi_chunk_assembles_full_text(self):
        stream = make_sse_stream(
            text_chunks=["Hello! ", "How can I ", "help you?"],
            conversation_id="conv-abc",
        )
        deltas, completed, conv_id, error = parse_sse_stream(stream)
        assert "".join(deltas) == "Hello! How can I help you?"
        assert conv_id == "conv-abc"
        assert error is None

    def test_error_event_returns_error(self):
        stream = make_sse_stream(
            error={"Code": 460020, "Message": "Model request timeout."},
        )
        deltas, completed, conv_id, error = parse_sse_stream(stream)
        assert error is not None
        assert error.get("Error", {}).get("Code") == 460020

    def test_empty_stream_returns_no_parts(self):
        stream = make_sse_stream(text_chunks=[], conversation_id="conv-x")
        deltas, completed, conv_id, error = parse_sse_stream(stream)
        assert error is None

    def test_done_event_is_ignored(self):
        stream = 'event: done\ndata: [DONE]\n\n'
        deltas, completed, conv_id, error = parse_sse_stream(stream)
        assert deltas == []
        assert completed is None
        assert conv_id is None
        assert error is None

    def test_text_replace_replaces_previous(self):
        stream = (
            'event: message.added\n'
            'data: {"Type": "message.added", "Message": {"Type": "reply", "MessageId": "rpl-001"}}\n\n'
            'event: text.replace\n'
            'data: {"Type": "text.replace", "Text": "final text", "MessageId": "rpl-001"}\n\n'
        )
        deltas, completed, conv_id, error = parse_sse_stream(stream)
        assert deltas == ["final text"]


# ── AdpChatClient with MockTransport ──────────────────────────────────


class TestAdpChatClient:
    def test_multi_chunk_stream_assembles_full_text(self):
        sse = make_sse_stream(
            text_chunks=["Hello! ", "How can I ", "help you?"],
            conversation_id="conv-123",
        )
        client = AdpChatClient(
            app_key=DUMMY_KEY,
            timeout_seconds=5,
            _transport=make_mock_transport(sse),
        )
        reply = client.ask("Hi")
        assert reply.text == "Hello! How can I help you?"
        assert reply.conversation_id == "conv-123"
        assert reply.request_id

    def test_error_event_raises_model_call_error(self):
        sse = make_sse_stream(
            error={"Code": 460020, "Message": "Model request timeout."},
        )
        client = AdpChatClient(
            app_key=DUMMY_KEY,
            timeout_seconds=5,
            _transport=make_mock_transport(sse),
        )
        with pytest.raises(ModelCallError) as exc_info:
            client.ask("Hi")
        assert "460020" in str(exc_info.value)
        assert DUMMY_KEY not in str(exc_info.value)

    def test_empty_reply_raises_model_call_error(self):
        sse = make_sse_stream(
            text_chunks=[],
            conversation_id="conv-empty",
        )
        client = AdpChatClient(
            app_key=DUMMY_KEY,
            timeout_seconds=5,
            _transport=make_mock_transport(sse),
        )
        with pytest.raises(ModelCallError) as exc_info:
            client.ask("Hi")
        assert "no reply text" in str(exc_info.value).lower()
        assert DUMMY_KEY not in str(exc_info.value)

    def test_non_2xx_status_raises_model_call_error(self):
        client = AdpChatClient(
            app_key=DUMMY_KEY,
            timeout_seconds=5,
            _transport=make_mock_transport("Internal Server Error", status=500),
        )
        with pytest.raises(ModelCallError) as exc_info:
            client.ask("Hi")
        assert "500" in str(exc_info.value)
        assert DUMMY_KEY not in str(exc_info.value)

    def test_two_calls_use_different_conversation_ids(self):
        sse1 = make_sse_stream(text_chunks=["Reply one"], conversation_id="conv-A")
        sse2 = make_sse_stream(text_chunks=["Reply two"], conversation_id="conv-B")
        responses = [sse1, sse2]

        def handler(request: httpx.Request) -> httpx.Response:
            sse = responses.pop(0)
            return httpx.Response(
                200,
                content=sse.encode("utf-8"),
                headers={"Content-Type": "text/event-stream"},
            )

        client = AdpChatClient(
            app_key=DUMMY_KEY,
            timeout_seconds=5,
            _transport=httpx.MockTransport(handler),
        )
        reply1 = client.ask("First")
        reply2 = client.ask("Second")
        assert reply1.text == "Reply one"
        assert reply2.text == "Reply two"
        assert reply1.conversation_id != reply2.conversation_id
        assert reply1.request_id != reply2.request_id

    def test_key_never_in_repr(self):
        client = AdpChatClient(app_key="super-secret-key-67890")
        r = repr(client)
        assert "super-secret-key-67890" not in r

    def test_key_never_in_model_call_error_message(self):
        sse = make_sse_stream(
            error={"Code": 4505004, "Message": "APPKEY is invalid."},
        )
        client = AdpChatClient(
            app_key="super-secret-key-67890",
            timeout_seconds=5,
            _transport=make_mock_transport(sse),
        )
        with pytest.raises(ModelCallError) as exc_info:
            client.ask("Hi")
        assert "super-secret-key-67890" not in str(exc_info.value)

    def test_key_never_in_log_output(self, caplog):
        sse = make_sse_stream(
            error={"Code": 460020, "Message": "timeout"},
        )
        client = AdpChatClient(
            app_key="super-secret-key-99999",
            timeout_seconds=5,
            _transport=make_mock_transport(sse),
        )
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(ModelCallError):
                client.ask("Hi")
        for record in caplog.records:
            assert "super-secret-key-99999" not in record.getMessage()

    def test_adp_client_is_chat_client(self):
        client = AdpChatClient(app_key=DUMMY_KEY)
        assert isinstance(client, ChatClient)


# ── Fail-closed message type filtering ───────────────────────────────
# Only text from messages explicitly announced as Type "reply" (via
# message.added) may become ChatReply.text.  Deltas whose MessageId was
# never announced, or whose type is anything other than "reply" (thought,
# tool_call, unknown), are excluded.  If no reply text remains, raise
# ModelCallError.
#
# Doc reference:
#   https://www.tencentcloud.com/document/product/1254/81449
#   "Chat API Documentation (HTTP SSE)" — message.added event,
#   response.completed Messages array.


class TestReasoningExclusion:
    def test_reasoning_events_excluded_from_reply(self):
        """A stream with reasoning (thought) deltas followed by reply
        deltas must return only the reply text."""
        stream = make_reasoning_stream(
            reasoning_chunks=[
                "We need to reply with the single word OK. ",
                "No extra text.",
            ],
            reply_chunks=["OK"],
        )
        client = AdpChatClient(
            app_key=DUMMY_KEY,
            timeout_seconds=5,
            _transport=make_mock_transport(stream),
        )
        reply = client.ask("Reply with the single word OK.")
        assert reply.text == "OK"
        assert "We need to reply" not in reply.text
        assert "No extra text" not in reply.text

    def test_no_duplication_when_deltas_and_completed_both_carry_reply(self):
        """When both text.delta events and response.completed carry the
        reply text, the client must not concatenate them — it must
        prefer response.completed's reply text."""
        # make_sse_stream produces both text.delta events AND a
        # response.completed with the same reply text.
        stream = make_sse_stream(
            text_chunks=["OK"],
            conversation_id="conv-dup-001",
        )
        client = AdpChatClient(
            app_key=DUMMY_KEY,
            timeout_seconds=5,
            _transport=make_mock_transport(stream),
        )
        reply = client.ask("Hi")
        # Must be "OK", not "OKOK"
        assert reply.text == "OK"
        assert len(reply.text) == 2

    def test_reasoning_in_completed_messages_ignored(self):
        """response.completed carries both thought and reply messages;
        only the reply message text is used."""
        stream = make_reasoning_stream(
            reasoning_chunks=["Thinking about the answer..."],
            reply_chunks=["OK"],
        )
        client = AdpChatClient(
            app_key=DUMMY_KEY,
            timeout_seconds=5,
            _transport=make_mock_transport(stream),
        )
        reply = client.ask("Hi")
        assert reply.text == "OK"
        assert "Thinking about" not in reply.text

    def test_parse_sse_stream_skips_thought_deltas(self):
        """parse_sse_stream returns empty reply_deltas for thought
        messages and a non-None completed_reply from the reply message."""
        stream = make_reasoning_stream(
            reasoning_chunks=["reasoning here"],
            reply_chunks=["OK"],
        )
        deltas, completed, conv_id, error = parse_sse_stream(stream)
        # Reasoning deltas must be skipped.
        assert "reasoning" not in "".join(deltas)
        # Reply text comes from response.completed.
        assert completed == "OK"
        assert conv_id == "conv-reason-001"
        assert error is None

    def test_reply_only_via_deltas_no_completed(self):
        """If response.completed has no reply message, the client
        falls back to reply deltas (not thought deltas)."""
        thought_id = "tht-001"
        reply_id = "rpl-001"
        # Build a stream with thought + reply deltas but
        # response.completed has only a thought message (no reply).
        completed_no_reply = json.dumps({
            "Type": "response.completed",
            "Response": {
                "ConversationId": "conv-x",
                "Messages": [
                    {"Type": "thought", "Contents": [{"Type": "text", "Text": "reasoning"}]},
                ],
            },
        })
        stream = (
            'event: request_ack\n'
            f'data: {json.dumps({"Type": "request_ack", "RequestAck": {"ConversationId": "conv-x", "Status": "success"}})}\n\n'
            'event: message.added\n'
            f'data: {json.dumps({"Type": "message.added", "Message": {"Type": "thought", "MessageId": thought_id}})}\n\n'
            'event: text.delta\n'
            f'data: {json.dumps({"Type": "text.delta", "Text": "reasoning", "MessageId": thought_id})}\n\n'
            'event: message.added\n'
            f'data: {json.dumps({"Type": "message.added", "Message": {"Type": "reply", "MessageId": reply_id}})}\n\n'
            'event: text.delta\n'
            f'data: {json.dumps({"Type": "text.delta", "Text": "OK", "MessageId": reply_id})}\n\n'
            f'event: response.completed\ndata: {completed_no_reply}\n\n'
            'event: done\ndata: [DONE]\n\n'
        )
        client = AdpChatClient(
            app_key=DUMMY_KEY,
            timeout_seconds=5,
            _transport=make_mock_transport(stream),
        )
        reply = client.ask("Hi")
        assert reply.text == "OK"
        assert "reasoning" not in reply.text


# ── Fail-closed: unannounced / tool_call / unknown message types ─────


class TestFailClosedMessageTypes:
    def test_unannounced_message_id_excluded(self):
        """A text.delta whose MessageId was never announced via
        message.added is excluded — fail closed."""
        stream = (
            'event: request_ack\n'
            f'data: {json.dumps({"Type": "request_ack", "RequestAck": {"ConversationId": "conv-u", "Status": "success"}})}\n\n'
            'event: text.delta\n'
            f'data: {json.dumps({"Type": "text.delta", "Text": "phantom text", "MessageId": "never-announced"})}\n\n'
            'event: response.completed\n'
            f'data: {json.dumps({"Type": "response.completed", "Response": {"ConversationId": "conv-u", "Status": "success", "Messages": []}})}\n\n'
            'event: done\ndata: [DONE]\n\n'
        )
        client = AdpChatClient(
            app_key=DUMMY_KEY,
            timeout_seconds=5,
            _transport=make_mock_transport(stream),
        )
        with pytest.raises(ModelCallError) as exc_info:
            client.ask("Hi")
        assert "no reply text" in str(exc_info.value).lower()

    def test_tool_call_message_excluded(self):
        """A message.added with Type "tool_call" and its text deltas
        are excluded — only reply text is returned."""
        stream = (
            'event: request_ack\n'
            f'data: {json.dumps({"Type": "request_ack", "RequestAck": {"ConversationId": "conv-tc", "Status": "success"}})}\n\n'
            'event: message.added\n'
            f'data: {json.dumps({"Type": "message.added", "Message": {"Type": "tool_call", "MessageId": "tc-001", "Name": "tool_call"}})}\n\n'
            'event: text.delta\n'
            f'data: {json.dumps({"Type": "text.delta", "Text": "calling tool...", "MessageId": "tc-001"})}\n\n'
            'event: message.added\n'
            f'data: {json.dumps({"Type": "message.added", "Message": {"Type": "reply", "MessageId": "rpl-001", "Name": "reply"}})}\n\n'
            'event: text.delta\n'
            f'data: {json.dumps({"Type": "text.delta", "Text": "OK", "MessageId": "rpl-001"})}\n\n'
            'event: response.completed\n'
            f'data: {json.dumps({"Type": "response.completed", "Response": {"ConversationId": "conv-tc", "Status": "success", "Messages": [{"Type": "reply", "MessageId": "rpl-001", "Contents": [{"Type": "text", "Text": "OK"}]}]}})}\n\n'
            'event: done\ndata: [DONE]\n\n'
        )
        client = AdpChatClient(
            app_key=DUMMY_KEY,
            timeout_seconds=5,
            _transport=make_mock_transport(stream),
        )
        reply = client.ask("Hi")
        assert reply.text == "OK"
        assert "calling tool" not in reply.text

    def test_unknown_message_type_excluded(self):
        """A message.added with an unknown Type (e.g. "unknown_type")
        and its text deltas are excluded — fail closed on anything
        that is not exactly "reply"."""
        stream = (
            'event: request_ack\n'
            f'data: {json.dumps({"Type": "request_ack", "RequestAck": {"ConversationId": "conv-unk", "Status": "success"}})}\n\n'
            'event: message.added\n'
            f'data: {json.dumps({"Type": "message.added", "Message": {"Type": "unknown_type", "MessageId": "unk-001", "Name": "unknown"}})}\n\n'
            'event: text.delta\n'
            f'data: {json.dumps({"Type": "text.delta", "Text": "mystery text", "MessageId": "unk-001"})}\n\n'
            'event: response.completed\n'
            f'data: {json.dumps({"Type": "response.completed", "Response": {"ConversationId": "conv-unk", "Status": "success", "Messages": []}})}\n\n'
            'event: done\ndata: [DONE]\n\n'
        )
        client = AdpChatClient(
            app_key=DUMMY_KEY,
            timeout_seconds=5,
            _transport=make_mock_transport(stream),
        )
        with pytest.raises(ModelCallError) as exc_info:
            client.ask("Hi")
        assert "no reply text" in str(exc_info.value).lower()


# ── FakeChatClient ────────────────────────────────────────────────────


class TestFakeChatClient:
    def test_returns_scripted_replies_in_order(self):
        fake = FakeChatClient(["Hello", "World"])
        r1 = fake.ask("msg1")
        r2 = fake.ask("msg2")
        assert r1.text == "Hello"
        assert r2.text == "World"

    def test_records_received_messages(self):
        fake = FakeChatClient(["OK", "OK"])
        fake.ask("first message")
        fake.ask("second message")
        assert fake.received_messages == ["first message", "second message"]

    def test_can_raise_model_call_error(self):
        fake = FakeChatClient(raise_on_call=True)
        with pytest.raises(ModelCallError):
            fake.ask("test")

    def test_no_replies_left_raises(self):
        fake = FakeChatClient(["only one"])
        fake.ask("msg1")
        with pytest.raises(ModelCallError):
            fake.ask("msg2")

    def test_two_calls_use_different_ids(self):
        fake = FakeChatClient(["a", "b"])
        r1 = fake.ask("x")
        r2 = fake.ask("y")
        assert r1.conversation_id != r2.conversation_id
        assert r1.request_id != r2.request_id

    def test_fake_client_is_chat_client(self):
        fake = FakeChatClient([])
        assert isinstance(fake, ChatClient)

    def test_multi_intent_list_never_returns_result(self):
        """No sentence in the multi-intent list ever returns a ParseResult.

        This is a cross-check that the parser still fails closed.
        """
        from backend.parser.interface import ParserCannotHandle
        from backend.parser.stub import parse

        multi_intent = [
            "Send fifty dollars to John Smith and buy one thousand dollars of DBS shares.",
            "Send fifty dollars to John Smith, also pay my bill.",
            "Send fifty dollars to John Smith then buy fifty dollars of shares.",
            "Send fifty dollars and send one thousand dollars to John Smith.",
            "Pay my bill and send money to Jane.",
        ]
        for sentence in multi_intent:
            with pytest.raises(ParserCannotHandle):
                parse(sentence)


# ── Config loader ────────────────────────────────────────────────────


class TestConfigLoader:
    def test_missing_key_raises_naming_variable(self, monkeypatch):
        for var in (
            "PARSER_APPKEY", "VALIDATOR_APPKEY",
            "PARSER_MODEL_LABEL", "VALIDATOR_MODEL_LABEL",
            "ADP_ENDPOINT", "ADP_TIMEOUT_SECONDS",
        ):
            monkeypatch.delenv(var, raising=False)

        with pytest.raises(RuntimeError) as exc_info:
            load_llm_settings()
        assert "PARSER_APPKEY" in str(exc_info.value)
        assert "secret" not in str(exc_info.value).lower()

    def test_identical_keys_raise(self, monkeypatch):
        monkeypatch.setenv("PARSER_APPKEY", "same-key-1234567890")
        monkeypatch.setenv("VALIDATOR_APPKEY", "same-key-1234567890")
        monkeypatch.setenv("PARSER_MODEL_LABEL", "DeepSeek-V3.2")
        monkeypatch.setenv("VALIDATOR_MODEL_LABEL", "Tencent-Hy3")

        with pytest.raises(RuntimeError) as exc_info:
            load_llm_settings()
        assert "PARSER_APPKEY" in str(exc_info.value)
        assert "VALIDATOR_APPKEY" in str(exc_info.value)
        assert "differ" in str(exc_info.value).lower()

    def test_identical_labels_raise(self, monkeypatch):
        monkeypatch.setenv("PARSER_APPKEY", "parser-key-aaaaaaaa")
        monkeypatch.setenv("VALIDATOR_APPKEY", "validator-key-bb")
        monkeypatch.setenv("PARSER_MODEL_LABEL", "Same-Model")
        monkeypatch.setenv("VALIDATOR_MODEL_LABEL", "Same-Model")

        with pytest.raises(RuntimeError) as exc_info:
            load_llm_settings()
        assert "PARSER_MODEL_LABEL" in str(exc_info.value)
        assert "VALIDATOR_MODEL_LABEL" in str(exc_info.value)
        assert "differ" in str(exc_info.value).lower()

    def test_valid_settings_load(self, monkeypatch):
        monkeypatch.setenv("PARSER_APPKEY", "parser-key-aaa")
        monkeypatch.setenv("VALIDATOR_APPKEY", "validator-key-bbb")
        monkeypatch.setenv("PARSER_MODEL_LABEL", "DeepSeek-V3.2")
        monkeypatch.setenv("VALIDATOR_MODEL_LABEL", "Tencent-Hy3")
        monkeypatch.delenv("ADP_ENDPOINT", raising=False)
        monkeypatch.delenv("ADP_TIMEOUT_SECONDS", raising=False)

        settings = load_llm_settings()
        assert settings.parser_app_key == "parser-key-aaa"
        assert settings.validator_app_key == "validator-key-bbb"
        assert settings.parser_model_label == "DeepSeek-V3.2"
        assert settings.validator_model_label == "Tencent-Hy3"
        assert settings.adp_endpoint == DEFAULT_ENDPOINT
        assert settings.adp_timeout_seconds == 30
