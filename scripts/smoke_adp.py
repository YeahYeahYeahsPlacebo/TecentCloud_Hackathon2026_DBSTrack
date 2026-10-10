#!/usr/bin/env python3
"""
Manual smoke test for the Tencent Cloud ADP Chat API.

This script is NOT run by pytest.  It loads real settings from .env,
sends each ADP application the message "Reply with the single word OK.",
and prints which app answered, the reply text and the request id.

It must never print the AppKey or the request body.

Usage:
    .venv/bin/python scripts/smoke_adp.py
    .venv/bin/python scripts/smoke_adp.py --debug

With --debug, prints for each app:
  - the sequence of SSE event types received
  - the message Types seen (thought, reply, tool_call, etc.)
  - character counts for each event's Text field

Exit code 0 if both apps replied; 1 otherwise.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.llm.adp_client import AdpChatClient, parse_sse_stream_debug  # noqa: E402
from backend.llm.config import load_llm_settings  # noqa: E402
from backend.llm.interface import ModelCallError  # noqa: E402

PROMPT = "Reply with the single word OK."


def main() -> int:
    parser = argparse.ArgumentParser(description="ADP smoke test")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print SSE event types, message Types, and char counts.",
    )
    args = parser.parse_args()

    settings = load_llm_settings(env_path=str(ROOT / ".env"))

    apps = [
        ("dcta-parser (DeepSeek-V3.2)", settings.parser_app_key),
        ("dcta-validator (Tencent Hy3)", settings.validator_app_key),
    ]

    all_ok = True

    for name, app_key in apps:
        print(f"\n--- {name} ---")
        client = AdpChatClient(
            app_key=app_key,
            endpoint=settings.adp_endpoint,
            timeout_seconds=settings.adp_timeout_seconds,
        )
        print(f"endpoint: {settings.adp_endpoint}")
        print(f"prompt:   {PROMPT}")
        try:
            if args.debug:
                reply, raw_stream = client._ask_internal(PROMPT)
                traces = parse_sse_stream_debug(raw_stream)
                print(f"\n  SSE event trace ({len(traces)} events):")
                print(f"  {'event_type':<25} {'msg_type':<15} {'chars':>6}")
                print(f"  {'-' * 25} {'-' * 15} {'-' * 6}")
                for t in traces:
                    mt = t.message_type or "-"
                    print(f"  {t.event_type:<25} {mt:<15} {t.text_len:>6}")
                msg_types = [
                    t.message_type for t in traces if t.message_type
                ]
                print(f"\n  message types seen: {msg_types}")
                print(f"  reply text: {reply.text!r}")
                print(f"  reply chars: {len(reply.text)}")
                print(f"  conv id:     {reply.conversation_id}")
                print(f"  req id:      {reply.request_id}")
            else:
                reply = client.ask(PROMPT)
                print(f"reply:    {reply.text!r}")
                print(f"conv id:  {reply.conversation_id}")
                print(f"req id:   {reply.request_id}")
        except ModelCallError as exc:
            print(f"ERROR:    {exc}")
            all_ok = False

    print()
    if all_ok:
        print("Both apps responded successfully.")
        return 0
    else:
        print("One or more apps failed.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
