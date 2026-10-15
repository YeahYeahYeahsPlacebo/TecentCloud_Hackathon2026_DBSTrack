"""Independent-message coverage; no injected pass or execution calls.

These use the real stub parser and explicit deterministic validator. Browser
verification against the real LLM configuration is a separate required check.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.gateway.api import GatewayAPI
from backend.gateway.server import create_app
from backend.parser.directory import FixtureDirectory
from backend.parser.interface import ParserCannotHandle
from backend.parser.stub import parse
from backend.validator.stub_validator import StubValidator
from fastapi.testclient import TestClient

TEXT = "Send fifty dollars to John Smith and send thirty dollars to Jane Tan."


def api(parser=parse, validator=None):
    directory = FixtureDirectory()
    return GatewayAPI(parser=parser, validator=validator or StubValidator(directory=directory), directory=directory, bank=None)


def test_two_drafts_have_verbatim_clauses_and_separate_security_results():
    gateway = api()
    response = TestClient(create_app(gateway, serve_frontend=False)).post('/api/message', json={'transcript': TEXT})
    assert response.status_code == 200
    body = response.json()
    assert body['intents_detected'] == len(body['items']) == 2
    assert body['source_transcript'] == TEXT
    assert body['request_mode'] == 'independent_transfers'
    drafts = [item['draft'] for item in body['items']]
    assert [d['transcript'] for d in drafts] == ['Send fifty dollars to John Smith', 'send thirty dollars to Jane Tan.']
    assert [d['amount']['value'] for d in drafts] == ['50.00', '30.00']
    assert [d['payee']['display_name'] for d in drafts] == ['John Smith', 'Jane Tan']
    assert len({d['id'] for d in drafts}) == len({d['nonce'] for d in drafts}) == 2
    assert len({item['validator']['draft_hash'] for item in body['items']}) == 2
    assert all(item['validator']['verdict'] == 'pass' and item['policy']['decision'] == 'allow' for item in body['items'])
    assert set(gateway.store) == {d['id'] for d in drafts}


@pytest.mark.parametrize('text', [
    'Send fifty dollars to John Smith and then send thirty dollars to Jane Tan.',
    'Send fifty dollars to John Smith and send the remaining thirty dollars to Jane Tan.',
    'If I have enough money send fifty dollars to John Smith and send thirty dollars to Jane Tan.',
    'Send fifty dollars to John Smith and send thirty dollars to the same person.',
    'Send fifty dollars to John Smith and send thirty dollars to Jane Tan after that.',
    'Send fifty dollars to John Smith and send thirty dollars to Jane Tan and send ten dollars to John Doe.',
    'Send fifty dollars to John Smith and buy thirty dollars of shares.',
    'Send fifty dollars to John Smith and send thirty dollars from another account to Jane Tan.',
    'Send fifty dollars to John Smith and send some dollars to Jane Tan.',
    'Send fifty dollars to John Smith and send -30 dollars to Jane Tan.',
    'Send fifty dollars to John Smith and send 30.555 dollars to Jane Tan.',
    'Send fifty dollars to John Smith and send thirty dollars to an unknown stranger.',
])
def test_unsupported_or_dependent_requests_do_not_create_partial_drafts(text):
    gateway = api()
    response = TestClient(create_app(gateway, serve_frontend=False)).post('/api/message', json={'transcript': text})
    assert response.status_code == 422
    assert response.json()['reason_code'] == 'multi_intent_unsupported'
    assert not gateway.store


def test_second_parse_failure_never_stores_the_first_draft():
    def failing(text):
        if 'Jane' in text:
            raise ParserCannotHandle('second clause refused', reason='multi-intent unsupported')
        return parse(text)
    gateway = api(parser=failing)
    response = gateway.message({'transcript': TEXT})
    assert response['reason_code'] == 'multi_intent_unsupported'
    assert not gateway.store


def test_ambiguous_first_draft_can_be_clarified_without_changing_second():
    gateway = api()
    body = gateway.message({'transcript': 'Send fifty dollars to John and send thirty dollars to Jane Tan.'})
    first, second = body['items']
    assert first['draft']['unresolved'] == ['payee']
    answer = first['draft']['payee_candidates'][0]['id']
    result = gateway.clarify({'draft_id': first['draft']['id'], 'field': 'payee', 'answer': answer})
    assert result['items'][0]['draft']['id'] != first['draft']['id']
    assert result['items'][0]['draft']['transcript'] == 'Send fifty dollars to John'
    assert gateway.store[second['draft']['id']].model_dump(mode='json', exclude_none=True) == second['draft']


def test_default_validator_still_freezes_each_independent_draft():
    from backend.validator.factory import UnconfiguredValidator
    gateway = api(validator=UnconfiguredValidator())
    items = gateway.message({'transcript': TEXT})['items']
    assert len(items) == 2
    assert all(item['validator']['verdict'] == 'freeze' for item in items)
    for item in items:
        assert gateway.challenge({'draft_id': item['draft']['id']})['reason_code'] == 'validator_frozen'


def test_validator_checks_both_clauses_and_freezes_only_the_tampered_draft():
    from backend.models.draft import Money
    def tampered_parser(text):
        result = parse(text)
        if 'Jane' in text:
            draft = result.items[0]
            result.items[0] = draft.model_copy(update={'amount': Money(value='31.00', currency='SGD')})
        return result
    items = api(parser=tampered_parser).message({'transcript': TEXT})['items']
    assert [item['validator']['verdict'] for item in items] == ['pass', 'freeze']
    assert items[0]['draft']['amount']['value'] == '50.00'
    assert items[1]['draft']['amount']['value'] == '31.00'


def test_policy_blocks_one_transfer_without_relabelling_the_other():
    items = api().message({'transcript': 'Send fifty dollars to John Smith and send six thousand dollars to Jane Tan.'})['items']
    assert [item['validator']['verdict'] for item in items] == ['pass', 'pass']
    assert [item['policy']['decision'] for item in items] == ['allow', 'block']


def test_parser_cannot_replace_a_childs_verbatim_source_transcript():
    from backend.gateway.message import ParserContractViolation
    def wrong_transcript(text):
        result = parse(text)
        result.items[0] = result.items[0].model_copy(update={'transcript': TEXT})
        return result
    gateway = api(parser=wrong_transcript)
    with pytest.raises(ParserContractViolation):
        gateway.message({'transcript': TEXT})
    assert not gateway.store
