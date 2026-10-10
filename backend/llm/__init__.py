"""LLM client plumbing for the Tencent Cloud ADP Chat API.

This package contains only transport and configuration code — no parser
or validator logic.  It provides a :class:`~backend.llm.interface.ChatClient`
protocol with two implementations:

* :class:`~backend.llm.adp_client.AdpChatClient` — the real HTTP SSE client.
* :class:`~backend.llm.fake_client.FakeChatClient` — a scripted fake for tests.

The module must never import the ledger or gateway (enforced by
``tests/test_import_boundaries.py``).
"""
