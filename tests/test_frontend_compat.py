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

It also pins one thing that is NOT a protocol detail but would look like
one to a user: the name on the candidate button is the name on the confirmed
draft. The resolved payee comes from the directory (`FixtureDirectory`),
which is authoritative per CONTRACT.md §4, so the stub parser must build its
candidates from that same directory. It used to hard-code payee-102 as
"John Smith" while the directory knows it as "John Lee"; the user clicked
one name and the draft showed another. The stub now takes its candidates
from the directory, and `test_resolved_payee_comes_from_the_directory_not_the_candidate_label`
asserts the two agree (it was skipped while they did not).

The payee pick also reaches the validator with a ClarificationRecord
(`resolve_payee` returns one), so the validator can check the draft against
the payee the user actually picked.
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
    # The label the user clicked is the name the draft shows. This was a
    # pytest.skip while the stub offered payee-102 as "John Smith".
    assert shown_name == directory_name
    assert shown_name == "John Lee"


# ── The payee pick reaches the validator, end to end ──────────────────

import json  # noqa: E402

from backend.llm.fake_client import FakeChatClient  # noqa: E402
from backend.validator.llm_validator import LlmValidator  # noqa: E402


def _api(validator):
    from backend.gateway.api import GatewayAPI  # noqa: E402
    from backend.parser.factory import get_parser  # noqa: E402

    return GatewayAPI(
        parser=get_parser(), directory=FixtureDirectory(), validator=validator,
    )


def _extraction(mention="John"):
    return json.dumps({"intents": [{
        "intent_type": "transfer", "payee_mention": mention,
        "amount_value": "50.00", "currency": "SGD",
        "source_account_mention": None, "ticker_mention": None, "order_type": None,
    }]})


def _pick(api, answer="payee-102"):
    old = api.message({"transcript": "Send fifty dollars to John."})["items"][0]["draft"]
    response = api.clarify({"draft_id": old["id"], "field": "payee", "answer": answer})
    return old, response


def test_the_gateway_hands_the_validator_the_pick_record():
    from gateway_fakes import FakeValidator  # noqa: E402

    validator = FakeValidator("pass")
    api = _api(validator)
    old, response = _pick(api)
    new = response["items"][0]["draft"]

    # One call at /api/message (the ambiguous draft, no record), one at
    # /api/clarify (the resolved draft, with the pick).
    assert len(validator.calls) == 2
    assert validator.calls[0][2] is None
    draft, transcript, record = validator.calls[-1]
    assert draft.id == new["id"]
    assert transcript == "Send fifty dollars to John."     # the original, not the answer
    assert record is not None
    assert record.field == "payee"
    assert record.answer == "payee-102"
    assert record.answer_kind == "candidate_id"
    assert record.question_id == old["id"]
    assert record.original_transcript == "Send fifty dollars to John."


def test_a_picked_draft_passes_the_real_validator_and_executes():
    """message -> clarify (pick) -> challenge -> signed execute, with the
    real LlmValidator and a scripted model. Nothing here tested a clarified
    draft all the way to execution before."""
    from gateway_fakes import new_key, sign_assertion  # noqa: E402
    from backend.gateway.webauthn import public_key_to_pem  # noqa: E402

    client = FakeChatClient([_extraction(), _extraction()])
    api = _api(LlmValidator(client=client, directory=FixtureDirectory()))
    _, response = _pick(api)
    item = response["items"][0]
    assert item["validator"]["verdict"] == "pass", item["validator"]

    # The second model call carried the pick in its own ANSWER block,
    # labelled "payee", never merged into the transcript.
    prompt = client.received_messages[-1]
    assert "Clarification answer for field <<<FIELD>>>payee<<<FIELD>>>" in prompt
    assert "<<<ANSWER>>>\npayee-102\n<<<END>>>" in prompt

    key = new_key()
    credential_id = api.register(
        {"user_id": "u", "public_key": public_key_to_pem(key.public_key())}
    )["credential_id"]
    draft_id = item["draft"]["id"]
    challenge = api.challenge({"draft_id": draft_id, "credential_id": credential_id})
    assert challenge["draft_id"] == draft_id
    result = api.execute({
        "draft_id": draft_id,
        "credential_id": credential_id,
        "idempotency_key": "key-pick-1",
        "assertion": sign_assertion(key, credential_id, challenge["challenge"]),
    })
    assert result["result"] == "executed", result


def test_the_verdict_for_a_picked_draft_is_stored_under_its_own_hash():
    from backend.canonical import draft_hash  # noqa: E402

    api = _api(LlmValidator(
        client=FakeChatClient([_extraction(), _extraction()]),
        directory=FixtureDirectory(),
    ))
    old, response = _pick(api)
    new_id = response["items"][0]["draft"]["id"]

    def h(draft_id):
        return draft_hash(api.store[draft_id].model_dump(mode="json", exclude_none=True))

    assert api.verdicts[h(new_id)] == "pass"
    assert h(new_id) != h(old["id"])


def test_a_real_validator_does_not_pass_a_pick_it_cannot_verify():
    """If the model is unreachable at clarify time, the picked draft is
    frozen with a reason, not passed."""
    api = _api(LlmValidator(
        client=FakeChatClient([_extraction()]),   # no reply left for /api/clarify
        directory=FixtureDirectory(),
    ))
    _, response = _pick(api)
    verdict = response["items"][0]["validator"]
    assert verdict["verdict"] == "freeze"
    assert verdict["reason"].startswith("validator_unavailable")
