#!/usr/bin/env python3
"""
Dress rehearsal: walk the whole story over real HTTP, and prove the gate holds.

NOT run by pytest. This is the script to run immediately before a live demo:
it registers a fresh ES256 credential, sends a transcript, answers whatever
the parser asks, signs, executes, and finally verifies the audit chain. Then
it attacks its own transaction — re-signs a *different* draft with the same
key — and asserts the gateway refuses it.

Exit code 0 means the whole story worked and the attack was refused. Anything
else means do not go on stage yet.

The point of the last step is that it is the product. "The AI can draft and
cannot spend" is not a claim in a README; it is check 8 failing on a
signature over different bytes. Demo that, not the chat UI.

Usage:
    # offline, no keys — needs the server started with DCTA_ALLOW_UNVALIDATED=true
    python scripts/demo.py

    # the real thing
    PARSER_BACKEND=llm VALIDATOR_BACKEND=llm python -m backend.gateway.server
    python scripts/demo.py

    python scripts/demo.py --transcript "Send fifty dollars to John Doe."
    python scripts/demo.py --base http://localhost:8000 --origin http://localhost:8000

    # the free-text form: the parser asks, and the answer is typed, not picked
    python scripts/demo.py --transcript "Send fifty dollars to mum." --answer "John Lee"

Options:
    --transcript TEXT   What the user says (default: the ambiguous-John case).
    --answer TEXT       The free-text answer, if the parser asks. Default:
                        by field — "John Lee" for a payee, "fifty dollars"
                        for an amount.
    --base URL          Server root (default: http://localhost:8000).
    --origin URL        Origin put in clientDataJSON. Must match DCTA_ORIGIN,
                        port included, or every signature fails.
    --pick N            Which payee candidate to choose, 1-based (default: 2).

Never prints an AppKey. Never reads .env.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from backend.gateway.execute import (  # noqa: E402
    FLAG_USER_PRESENT,
    FLAG_USER_VERIFIED,
    b64url_encode,
)
from backend.gateway.webauthn import der_to_raw_signature  # noqa: E402

DEFAULT_TRANSCRIPT = "Pay John two hundred dollars."

# What the user says back when the parser asks. A payee question needs a
# name the directory knows; an amount question needs a number. Guessing
# "fifty dollars" at a payee question is answered with a rejection, since
# it changes a field that is already resolved.
DEFAULT_ANSWERS = {
    "payee": "John Lee",
    "amount": "fifty dollars",
    "ticker": "D05.SI",
    "order_type": "market",
}


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class Server:
    def __init__(self, base: str, origin: str) -> None:
        self.base = base.rstrip("/")
        self.origin = origin

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            try:
                return exc.code, json.loads(payload)
            except json.JSONDecodeError:
                return exc.code, {"message": payload.decode(errors="replace")}
        except urllib.error.URLError as exc:
            return 0, {"message": f"cannot reach {self.base}: {exc.reason}"}

    def get(self, path: str) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(self.base + path) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())
        except urllib.error.URLError as exc:
            return 0, {"message": f"cannot reach {self.base}: {exc.reason}"}


def sign(private_key, credential_id: str, challenge: str, rp_id: str, origin: str) -> dict:
    """An assertion over ``authenticator_data || SHA256(client_data_json)``.

    Inlined rather than imported from tests/ so this script keeps working
    when the test doubles change.
    """
    client_data = json.dumps(
        {"type": "webauthn.get", "challenge": challenge, "origin": origin}
    ).encode()
    authenticator_data = (
        hashlib.sha256(rp_id.encode()).digest()
        + bytes([FLAG_USER_PRESENT | FLAG_USER_VERIFIED])
        + (1).to_bytes(4, "big")
    )
    signed = authenticator_data + hashlib.sha256(client_data).digest()
    return {
        "credential_id": credential_id,
        "authenticator_data": b64url_encode(authenticator_data),
        "client_data_json": b64url_encode(client_data),
        "signature": b64url_encode(
            der_to_raw_signature(private_key.sign(signed, ec.ECDSA(hashes.SHA256())))
        ),
    }


def step(number: int, text: str) -> None:
    print(f"\n{number}. {text}")


def outcome(status: int, body: dict) -> str:
    """One line saying what happened, in the response's own vocabulary."""
    if body.get("result") == "error":
        return f"{status} {body.get('reason_code')} — {body.get('message')}"
    if body.get("result") == "rejected":
        return f"{status} rejected: {body.get('reason_code')} — {body.get('message')}"
    return f"{status} ok"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--transcript", default=DEFAULT_TRANSCRIPT)
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--origin", default=None)
    ap.add_argument("--pick", type=int, default=2, help="1-based payee candidate")
    ap.add_argument(
        "--answer",
        default=None,
        help="the free-text answer, if the parser asks (default: by field)",
    )
    args = ap.parse_args()

    server = Server(args.base, args.origin or args.base)
    key = ec.generate_private_key(ec.SECP256R1())

    print(f"server  {server.base}")
    print(f"origin  {server.origin}   (must equal DCTA_ORIGIN)")

    status, health = server.get("/api/health")
    if status == 0:
        print(f"\n[FAIL] {health['message']}")
        print("Start it first:  python -m backend.gateway.server")
        return 1
    print(f"health  parser={health.get('parser')} validator={health.get('validator')} "
          f"client={health.get('client')}")
    print(f"        free-text answers are read by: {health.get('answer_backend')} "
          "('rules' = no model involved)")

    # ── 1. register ─────────────────────────────────────────────────
    step(1, "register a credential the browser minted")
    credential_id = b64url(os.urandom(32))
    public_key = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    status, body = server.post(
        "/api/webauthn/register",
        {"user_id": "demo", "credential_id": credential_id, "public_key": public_key},
    )
    print(f"        {outcome(status, body)}")
    if body.get("status") != "registered":
        return 1
    print(f"        id {credential_id[:24]}… source={body.get('credential_id_source')}")

    # ── 2. the user speaks ──────────────────────────────────────────
    step(2, f"the user says: {args.transcript!r}")
    status, body = server.post("/api/message", {"transcript": args.transcript})
    print(f"        {outcome(status, body)}")
    if status != 200:
        return 1

    item = body["items"][0]

    # ── 3. clarification, if the draft is not finished ──────────────
    if item.get("kind") == "question":
        field = item["field"]
        answer = args.answer or DEFAULT_ANSWERS.get(field, "fifty dollars")
        step(3, "the parser needs a free-text answer")
        print(f"        asks ({field}): {item['question']}")
        print(f"        answers: {answer!r}")
        status, body = server.post(
            "/api/clarify",
            {"question_id": item["question_id"], "answer": answer},
        )
        print(f"        {outcome(status, body)}")
        if status != 200:
            return 1
        item = body["items"][0]
        if item.get("kind") == "question":
            # The answer did not settle it: the question is asked again.
            print(f"        asked again ({item['field']}): {item['question']}")
            print("\n[FAIL] the answer was not enough. Try --answer (see --help).")
            return 1

    draft = item["draft"]
    if draft.get("unresolved"):
        step(3, f"the draft is unfinished: {draft['unresolved']}")
        candidates = draft.get("payee_candidates", [])
        for n, candidate in enumerate(candidates, start=1):
            print(f"        [{n}] {candidate['id']}  {candidate['display_name']}  "
                  f"{candidate['masked_account']}")
        if not candidates:
            print("        [FAIL] unresolved with no candidates: nothing to answer with")
            return 1
        chosen = candidates[min(args.pick, len(candidates)) - 1]
        print(f"        picking {chosen['id']} ({chosen['display_name']})")
        status, body = server.post(
            "/api/clarify",
            {"draft_id": draft["id"], "field": "payee", "answer": chosen["id"]},
        )
        print(f"        {outcome(status, body)}")
        if status != 200:
            return 1
        draft = body["items"][0]["draft"]
    else:
        step(3, "the draft is complete — nothing to clarify")

    print(f"        payee={draft['payee']['display_name']} "
          f"amount={draft['amount']['value']} {draft['amount']['currency']}")

    # ── 4. challenge ────────────────────────────────────────────────
    step(4, "ask for a challenge")
    status, challenge = server.post(
        "/api/webauthn/challenge", {"draft_id": draft["id"]}
    )
    print(f"        {outcome(status, challenge)}")
    if status != 200:
        return 1
    print(f"        draft_hash {challenge['draft_hash'][:16]}… rp_id={challenge['rp_id']}")
    print(f"        allow_credentials has our id: "
          f"{credential_id in challenge['allow_credentials']}")

    # ── 5. sign ─────────────────────────────────────────────────────
    step(5, "sign the challenge with the registered key")
    assertion = sign(key, credential_id, challenge["challenge"],
                     challenge["rp_id"], server.origin)
    print(f"        signature {assertion['signature'][:24]}…")

    # ── 6. execute ──────────────────────────────────────────────────
    step(6, "execute")
    idempotency_key = f"demo-{os.getpid()}"
    status, body = server.post(
        "/api/execute",
        {
            "draft_id": draft["id"],
            "credential_id": credential_id,
            "idempotency_key": idempotency_key,
            "assertion": assertion,
        },
    )
    print(f"        {outcome(status, body)}")
    if body.get("result") != "executed":
        return 1
    print(f"        {body['transaction_id']} at {body['executed_at']}")

    # ── 7. the point of the whole thing ─────────────────────────────
    step(7, "now attack it: sign a *different* draft with the same key")
    status, other = server.post("/api/message", {"transcript": "Send fifty dollars to John Doe."})
    if status != 200 or other["items"][0].get("kind") != "draft":
        print("        [skip] no second draft to attack with")
        return 0
    other_draft = other["items"][0]["draft"]
    status, other_challenge = server.post(
        "/api/webauthn/challenge", {"draft_id": other_draft["id"]}
    )
    if status != 200:
        print(f"        [skip] second draft got no challenge: {outcome(status, other_challenge)}")
        return 0
    swapped = sign(key, credential_id, other_challenge["challenge"],
                   other_challenge["rp_id"], server.origin)
    # The signature is valid; it is simply over different bytes.
    status, body = server.post(
        "/api/execute",
        {
            "draft_id": draft["id"],  # the first draft
            "credential_id": credential_id,
            "idempotency_key": f"{idempotency_key}-attack",
            "assertion": swapped,     # signed for the second one
        },
    )
    print(f"        {outcome(status, body)}")
    if body.get("result") != "rejected":
        print("\n[FAIL] the gateway accepted a signature over a different draft.")
        return 1
    print(f"        refused: {body.get('reason_code')}")

    # ── 8. audit ────────────────────────────────────────────────────
    step(8, "the audit chain still verifies")
    status, body = server.get("/api/audit/verify")
    print(f"        {body.get('rows_checked', '?')} rows checked, "
          f"intact={body.get('intact')}")
    if not body.get("intact"):
        print(f"\n[FAIL] the audit chain is broken: {body.get('broken_row')}")
        return 1

    print("\n[OK] the story works, and the gate holds.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
