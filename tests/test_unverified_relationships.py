"""No Mom alias was authorized: unknown/guessed relationships must fail closed.

Canned model replies test the safety boundary, not real LLM accuracy. No execute.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.gateway.api import GatewayAPI
from backend.llm.fake_client import FakeChatClient
from backend.models.draft import Payee
from backend.parser.directory import FixtureDirectory, match_payees
from backend.parser.llm_parser import LlmParser
from backend.parser.stub import parse
from backend.validator.llm_validator import LlmValidator
from backend.validator.stub_validator import StubValidator


def test_mom_alone_remains_a_question_with_original_words_and_no_draft():
    directory = FixtureDirectory()
    gateway = GatewayAPI(parser=parse, directory=directory, validator=StubValidator(directory=directory), bank=None)
    result = gateway.message({'transcript': 'Send thirty dollars to Mom'})
    assert result['items'][0]['kind'] == 'question' and result['items'][0]['field'] == 'payee'
    assert not gateway.store and not gateway.verdicts
    assert gateway.audit.rows[-1].payload['transcript'] == 'Send thirty dollars to Mom'


@pytest.mark.parametrize('text', [
    'Send fifty dollars to John Smith and send thirty dollars to Mom.',
    'Send fifty dollars to John Smith and then send thirty dollars to Jane Tan.',
    'Send fifty dollars to John Smith and then send thirty dollars to Mom.',
])
def test_unknown_mom_and_ordering_are_separate_whole_request_refusals(text):
    directory = FixtureDirectory()
    gateway = GatewayAPI(parser=parse, directory=directory, validator=StubValidator(directory=directory), bank=None)
    result = gateway.message({'transcript': text})
    assert result['reason_code'] == 'multi_intent_unsupported'
    assert not gateway.store and not gateway.verdicts


@pytest.mark.parametrize('guessed_name', [None, 'Mom', 'Jane Tan', 'John Smith'])
def test_parser_cannot_invent_a_mom_mapping(guessed_name):
    reply = json.dumps({'intents': [{'intent_type': 'transfer', 'payee_mention': guessed_name,
                                   'amount_text': 'thirty dollars', 'amount_value': '30.00', 'currency': 'SGD'}]})
    parser = LlmParser(chat_client=FakeChatClient([reply]), directory=FixtureDirectory())
    item = parser.parse('Send thirty dollars to Mom').items[0]
    assert not hasattr(item, 'payee') and item.field == 'payee'


@pytest.mark.parametrize('payee_id', ['payee-001', 'payee-002', 'payee-101', 'payee-102'])
def test_validator_freezes_every_guessed_or_changed_mom_recipient(payee_id):
    directory = FixtureDirectory()
    record = next(p for p in directory.payees() if p.id == payee_id)
    original = parse('Send thirty dollars to John Smith').items[0]
    draft = original.model_copy(update={'transcript': 'Send thirty dollars to Mom',
                                       'payee': Payee(id=record.id, display_name=record.display_name, masked_account=record.masked_account)})
    extraction = json.dumps({'intents': [{'intent_type': 'transfer', 'payee_mention': 'Mom',
                                        'amount_value': '30.00', 'currency': 'SGD'}]})
    validator = LlmValidator(client=FakeChatClient([extraction]), directory=directory)
    verdict = validator.validate(draft, draft.transcript)
    assert verdict.verdict == 'freeze' and 'payee' in verdict.discrepancies
    assert not match_payees('Mom', directory, context=draft.transcript)
