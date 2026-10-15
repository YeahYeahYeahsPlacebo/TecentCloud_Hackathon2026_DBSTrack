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


def test_validator_verdicts_remain_independent():
    directory = FixtureDirectory()
    class FreezeSecond:
        def validate(self, draft, transcript, clarification=None):
            result = StubValidator(directory=directory).validate(draft, transcript, clarification=clarification)
            return result.model_copy(update={'verdict': 'freeze'}) if 'Jane' in transcript else result
    items = api(validator=FreezeSecond()).message({'transcript': TEXT})['items']
    assert [item['validator']['verdict'] for item in items] == ['pass', 'freeze']


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


@pytest.mark.parametrize('change', ['amount', 'payee', 'guess', 'source', 'candidate', 'id', 'nonce'])
def test_parser_cross_clause_changes_never_publish_either_draft(change):
    from backend.models.draft import Money, Payee
    from backend.gateway.message import ParserContractViolation
    first = None
    def changed(text):
        nonlocal first
        result = parse(text)
        d = result.items[0]
        if first is None:
            first = d
            if change == 'guess':
                result.items[0] = d.model_copy(update={'payee': Payee(id='payee-101', display_name='John Doe', masked_account='****4521'), 'payee_candidates': None, 'unresolved': []})
            elif change == 'candidate':
                result.items[0] = d.model_copy(update={'payee_candidates': [c.model_copy(update={'masked_account': '****0000'}) for c in d.payee_candidates]})
        elif change == 'amount':
            result.items[0] = d.model_copy(update={'amount': first.amount})
        elif change == 'payee':
            result.items[0] = d.model_copy(update={'payee': first.payee})
        elif change == 'source':
            result.items[0] = d.model_copy(update={'source_account': 'other-account'})
        elif change in ('id', 'nonce'):
            result.items[0] = d.model_copy(update={change: getattr(first, change)})
        return result
    gateway = api(parser=changed)
    text = TEXT if change not in ('guess', 'candidate') else TEXT.replace('John Smith', 'John')
    with pytest.raises(ParserContractViolation):
        gateway.message({'transcript': text})
    assert not gateway.store and not gateway.verdicts


@pytest.mark.parametrize('money', ['zero fifty dollars', 'fifty and dollars', 'fifty . dollars', 'about fifty dollars', '50 30 dollars', 'fifty or thirty dollars', '50 USD', '50 dollars maybe'])
def test_uncertain_or_unconsumed_amount_words_fail_closed(money):
    gateway = api()
    result = gateway.message({'transcript': TEXT.replace('fifty dollars', money)})
    assert result['reason_code'] == 'multi_intent_unsupported'
    assert not gateway.store and not gateway.verdicts


@pytest.mark.parametrize('stage', ['validator', 'policy', 'audit', 'store', 'verdict_store'])
def test_partial_failure_leaves_no_new_draft_or_passing_verdict(stage):
    gateway = api()
    # An existing unrelated draft and verdict must survive rollback.
    gateway.message({'transcript': 'Send ten dollars to John Smith.'})
    before_store, before_verdicts = dict(gateway.store), dict(gateway.verdicts)
    if stage == 'validator':
        class BrokenValidator:
            def validate(self, draft, transcript, clarification=None):
                assert gateway.store == before_store and gateway.verdicts == before_verdicts
                if 'Jane' in transcript: raise RuntimeError('validation failed')
                return StubValidator(directory=FixtureDirectory()).validate(draft, transcript, clarification=clarification)
        gateway.messages.validator = BrokenValidator()
    elif stage == 'policy':
        original = gateway.messages.policy
        class BrokenPolicy:
            def evaluate(self, draft, *args, **kwargs):
                assert gateway.store == before_store and gateway.verdicts == before_verdicts
                if 'Jane' in draft['transcript']: raise RuntimeError('policy failed')
                return original.evaluate(draft, *args, **kwargs)
        gateway.messages.policy = BrokenPolicy()
    elif stage == 'audit':
        class BrokenAudit:
            def append(self, *args): raise RuntimeError('audit failed')
        gateway.messages.audit = BrokenAudit()
    else:
        class BrokenStore(dict):
            writes = 0
            def __setitem__(self, key, value):
                self.writes += 1
                if self.writes == 2: raise RuntimeError('publication failed')
                super().__setitem__(key, value)
        if stage == 'store': gateway.store = gateway.messages.store = BrokenStore(before_store)
        else: gateway.verdicts = gateway.messages.verdicts = BrokenStore(before_verdicts)
    response = TestClient(create_app(gateway, serve_frontend=False), raise_server_exceptions=False).post('/api/message', json={'transcript': TEXT})
    assert response.status_code == 500
    assert response.json()['reason_code'] == 'internal_error'
    assert gateway.store == before_store and gateway.verdicts == before_verdicts


def test_original_combined_transcript_is_audited_and_clarify_preserves_other_binding():
    from backend.canonical import draft_hash
    gateway = api()
    text = TEXT.replace('John Smith', 'John')
    body = gateway.message({'transcript': text})
    row = gateway.audit.rows[-1]
    assert row.payload['transcript'] == text
    assert row.payload['drafts'] == [x['draft']['id'] for x in body['items']]
    first, second = body['items']
    other_id = second['draft']['id']
    other_hash = draft_hash(second['draft'])
    other_draft = gateway.store[other_id]
    other_verdict = gateway.verdicts[other_hash]
    result = gateway.clarify({'draft_id': first['draft']['id'], 'field': 'payee', 'answer': first['draft']['payee_candidates'][0]['id']})
    new = result['items'][0]
    assert new['draft']['id'] != first['draft']['id']
    assert new['validator']['draft_hash'] == draft_hash(new['draft'])
    assert gateway.store[other_id] is other_draft and gateway.verdicts[other_hash] == other_verdict
    assert gateway.policy.evaluate(second['draft'], other_hash, posted=[], known_payees=set()).to_response() == second['policy']


def test_wrong_hash_verdict_freezes_only_its_own_binding():
    from backend.canonical import draft_hash
    directory = FixtureDirectory()
    class WrongSecondHash:
        def validate(self, draft, transcript, clarification=None):
            result = StubValidator(directory=directory).validate(draft, transcript, clarification=clarification)
            return result.model_copy(update={'draft_hash': '0' * 64}) if 'Jane' in transcript else result
    gateway = api(validator=WrongSecondHash())
    items = gateway.message({'transcript': TEXT})['items']
    hashes = [draft_hash(x['draft']) for x in items]
    assert gateway.verdicts == {hashes[0]: 'pass', hashes[1]: 'freeze'}
    assert [x['validator']['verdict'] for x in items] == ['pass', 'freeze']
    assert '0' * 64 not in gateway.verdicts
