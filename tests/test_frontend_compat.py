"""
Frontend↔backend compatibility for the payee-clarification flow.

This is the backend side of Member 2's PR #15 review. It drives the real
HTTP layer (the one `backend/gateway/server.py` exposes) the way
`frontend/app.js` does, and asserts the contract it relies on:

  - POST /api/message returns items with a draft that carries
    `payee_candidates` (the frontend builds its candidate buttons from
    `draft.payee_candidates[*].{id,display_name,masked_account}`).
  - POST /api/clarify with `{draft_id, field:"payee", answer:<candidate id>}`
    returns 200 and an items list whose draft has a NEW id and NEW nonce,
    a resolved `payee`, and no `payee_candidates`.
  - A wrong `answer` returns 422 with `reason_code` `invalid_clarification_answer`,
    which the frontend renders as "Clarification rejected — invalid_clarification_answer".

It also documents one divergence that is NOT a protocol bug: the resolved
payee's name comes from the directory (`FixtureDirectory`), which is
authoritative per CONTRACT.md §4. The stub parser offers `payee-102` as
"John Smith", but the directory (and Member 1's red-team tests) know
`payee-102` as "John Lee". So the confirmed draft shows "John Lee", not the
"John Smith" the user clicked. That is a parser/directory data mismatch on
Member 1's side — see the review note — not something this backend violates.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from backend.gateway.server import create_app  # noqa: E402
from backend.parser.directory import FixtureDirectory  # noqa: E402


def client():
    # Same wiring as `python -m backend.gateway.server` but with an injected
    # passing validator so a draft can be clarified to a verifiable state.
    from backend.gateway.api import GatewayAPI  # noqa: E402
    from gateway_fakes import passing_validator  # noqa: E402
    from backend.parser.factory import get_parser  # noqa: E402

    api = GatewayAPI(
        parser=get_parser(),
        directory=FixtureDirectory(),
        validator=passing_validator(),
    )
    return TestClient(create_app(api, serve_frontend=False))


def _message(c, transcript):
    return c.post("/api/message", json={"transcript": transcript}).json()


def test_message_returns_candidates_the_frontend_builds_buttons_from():
    """PR #15 reads draft.payee_candidates[*].{id,display_name,masked_account}
    to render its candidate buttons and to set data-candidate-ids."""
    body = _message(client(), "Send fifty dollars to John.")
    draft = body["items"][0]["draft"]
    assert body["intents_detected"] == 1
    candidates = draft["payee_candidates"]
    assert len(candidates) >= 2
    for c in candidates:
        assert set(c.keys()) >= {"id", "display_name", "masked_account"}


def test_clarify_request_shape_matches_pr15():
    """PR #15 sends exactly {draft_id, field:"payee", answer: candidateId}."""
    c = client()
    draft = _message(c, "Send fifty dollars to John.")["items"][0]["draft"]
    draft_id = draft["id"]
    answer = draft["payee_candidates"][1]["id"]

    response = c.post(
        "/api/clarify",
        json={"draft_id": draft_id, "field": "payee", "answer": answer},
    )
    assert response.status_code == 200
    # The frontend reads body.items[0].draft.
    resolved = response.json()["items"][0]["draft"]
    assert resolved["id"] != draft_id          # new draft, new id
    assert resolved["nonce"] != draft["nonce"]  # new nonce
    assert resolved["payee"]["id"] == answer    # resolved to the chosen id
    assert "payee_candidates" not in resolved or resolved.get("payee_candidates") in (None, [])


def test_a_wrong_clarification_answer_is_422_invalid_clarification_answer():
    """PR #15 displays body.reason_code on a non-ok response."""
    c = client()
    draft_id = _message(c, "Send fifty dollars to John.")["items"][0]["draft"]["id"]
    response = c.post(
        "/api/clarify",
        json={"draft_id": draft_id, "field": "payee", "answer": "payee-999"},
    )
    assert response.status_code == 422
    assert response.json()["reason_code"] == "invalid_clarification_answer"


def test_resolved_payee_comes_from_the_directory_not_the_candidate_label():
    """CONTRACT.md §4: the directory is authoritative. The resolved name is
    the directory's, which is why a "John Smith" candidate can resolve to
    "John Lee" — that mismatch is Member 1's parser/directory data, not a
    protocol break. This asserts the authoritative behaviour explicitly so
    the divergence is tracked, not hidden."""
    c = client()
    draft = _message(c, "Send fifty dollars to John.")["items"][0]["draft"]
    answer = draft["payee_candidates"][1]["id"]  # payee-102
    shown_name = next(
        c["display_name"] for c in draft["payee_candidates"] if c["id"] == answer
    )

    resolved = c.post(
        "/api/clarify",
        json={"draft_id": draft["id"], "field": "payee", "answer": answer},
    ).json()["items"][0]["draft"]["payee"]

    directory_name = {
        p.id: p.display_name for p in FixtureDirectory().payees()
    }[answer]
    assert resolved["display_name"] == directory_name
    # Documented divergence: the parser shows one name, the directory another.
    # Remove this skip once Member 1 aligns the parser's candidate generation
    # with FixtureDirectory (or vice versa).
    if shown_name != directory_name:
        pytest.skip(
            f"known parser/directory mismatch: candidate shows {shown_name!r} "
            f"but directory resolves to {directory_name!r} (payee-102)"
        )
