"""
Parser factory.

Returns the stub parser by default so teammates without API keys
keep working.  When ``PARSER_BACKEND=llm`` is set in the environment,
returns the LLM-backed parser configured with the real ADP client
and the fixture directory.

Usage::

    from backend.parser.factory import get_parser
    parse = get_parser()
    result = parse("Send fifty dollars to John Smith.")
"""

from __future__ import annotations

import os
from typing import Callable

from backend.parser.interface import ParseResult


def get_parser() -> Callable[[str], ParseResult]:
    """Return a ``parse(transcript) -> ParseResult`` callable.

    Defaults to ``backend.parser.stub.parse``.
    When ``PARSER_BACKEND=llm`` is set, returns
    :class:`backend.parser.llm_parser.LlmParser.parse`.
    """
    backend = os.environ.get("PARSER_BACKEND", "").strip().lower()

    if backend == "llm":
        from backend.llm.adp_client import AdpChatClient
        from backend.llm.config import load_llm_settings
        from backend.parser.directory import FixtureDirectory
        from backend.parser.llm_parser import LlmParser

        settings = load_llm_settings(
            env_path=str(_project_root() / ".env")
        )
        client = AdpChatClient(
            app_key=settings.parser_app_key,
            endpoint=settings.adp_endpoint,
            timeout_seconds=settings.adp_timeout_seconds,
        )
        parser = LlmParser(chat_client=client, directory=FixtureDirectory())
        return parser.parse

    from backend.parser.stub import parse as stub_parse

    return stub_parse


def _project_root():
    from pathlib import Path

    return Path(__file__).resolve().parent.parent.parent
