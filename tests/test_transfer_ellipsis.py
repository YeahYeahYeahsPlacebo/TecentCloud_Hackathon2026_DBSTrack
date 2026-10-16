"""Narrow shared-verb transfers. Canned model extraction, never injected pass.

Real parser/validator classes compare the verbatim clauses independently.
Actual LLM/browser checks are reported separately. No execution calls.
"""
import json
import sys
from pathlib import Path

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.canonical import draft_hash
from backend.gateway.api import GatewayAPI
from backend.gateway.message import ParserContractViolation
from backend.llm.fake_client import FakeChatClient
from backend.parser.directory import FixtureDirectory, match_payees
from backend.parser.independent import independent_transfer_clauses
from backend.parser.llm_parser import LlmParser
from backend.validator.factory import UnconfiguredValidator
from backend.validator.llm_validator import LlmValidator

TEXT = 'Send fifty dollars to John Smith and thirty dollars to Jane.'
FIRST, SECOND = 'Send fifty dollars to John Smith', 'thirty dollars to Jane.'


def gateway(*, first_name='John Smith', second_name='Jane', second_value='30.00', validator=None, tamper=None):
    directory = FixtureDirectory()
    parser_client = FakeChatClient([json.dumps({'intents': [{'intent_type': 'transfer', 'payee_mention': name,
        'amount_text': money, 'amount_value': value, 'currency': 'SGD'}]}) for name, money, value in
        [(first_name, 'fifty dollars', '50.00'), (second_name, 'thirty dollars' if second_value=='30.00' else 'six thousand dollars', second_value)]])
    parser = LlmParser(chat_client=parser_client, directory=directory)
    def parse(text):
        result = parser.parse(text)
        if tamper: tamper(text, result)
        return result
    validator_client = FakeChatClient([json.dumps({'intents': [{'intent_type': 'transfer', 'payee_mention': name,
        'amount_value': value, 'currency': 'SGD'}]}) for name,value in [(first_name,'50.00'),(second_name,second_value)]])
    api=GatewayAPI(parser=parse, directory=directory, validator=validator or LlmValidator(client=validator_client,directory=directory), bank=None)
    return api


def test_exact_requested_sentence_has_two_isolated_verbatim_drafts():
    api=gateway();body=api.message({'transcript':TEXT})
    assert body['intents_detected']==len(body['items'])==2 and body['source_transcript']==TEXT
    first,second=body['items']
    assert [x['draft']['transcript'] for x in body['items']]==[FIRST,SECOND]
    assert [x['draft']['amount']['value'] for x in body['items']]==['50.00','30.00']
    assert [x['draft']['payee']['display_name'] for x in body['items']]==['John Smith','Jane Tan']
    assert len({x['draft']['id'] for x in body['items']})==len({x['draft']['nonce'] for x in body['items']})==2
    for x in body['items']:
        h=draft_hash(x['draft']);assert x['validator']['draft_hash']==h and api.verdicts[h]=='pass'
        assert x['policy']['decision']=='allow'
    assert api.audit.rows[-1].payload['transcript']==TEXT


@pytest.mark.parametrize('text,clauses',[
    (TEXT,(FIRST,SECOND)),
    ('Send fifty dollars to John Smith and send thirty dollars to Jane.',(FIRST,'send thirty dollars to Jane.')),
    ('Transfer $50 to John and $30 to Jane.',('Transfer $50 to John','$30 to Jane.')),
    ('Send one hundred and fifty dollars to John Smith and one hundred and thirty dollars to Jane.',('Send one hundred and fifty dollars to John Smith','one hundred and thirty dollars to Jane.')),
    ('Send fifty dollars to John and thirty dollars to Jane.',('Send fifty dollars to John',SECOND)),
])
def test_unique_safe_boundary_preserves_names_and_number_phrases(text,clauses):
    assert independent_transfer_clauses(text,FixtureDirectory())==clauses


@pytest.mark.parametrize('text',[
    'Send fifty dollars to John Smith and then thirty dollars to Jane.',
    'Send fifty dollars to John Smith and thirty dollars to Jane after that.',
    'Send fifty dollars to John Smith and the remaining thirty dollars to Jane.',
    'Send fifty dollars to John Smith and about thirty dollars to Jane.',
    'Send fifty dollars to John Smith and some dollars to Jane.',
    'Send fifty dollars to John Smith and thirty dollars to Mom.',
    'Send fifty dollars to John Smith and thirty dollars to the same person.',
    'Send fifty dollars to John Smith and thirty dollars from another account to Jane.',
    'Send fifty dollars to John Smith and thirty USD to Jane.',
    'Send fifty dollars to John Smith and -30 dollars to Jane.',
    'Send fifty dollars to John Smith and 30.555 dollars to Jane.',
    'Send fifty dollars to John Smith and thirty dollars to Jane and ten dollars to John Doe.',
    'Send fifty dollars to John Smith and thirty dollars to Jane or John Doe.',
    'Send fifty dollars to John Smith and zero thirty dollars to Jane.',
])
def test_dependent_uncertain_or_unknown_wording_creates_nothing(text):
    api=gateway();body=api.message({'transcript':text})
    assert body['reason_code']=='multi_intent_unsupported'
    assert not api.store and not api.verdicts


@pytest.mark.parametrize('field',['amount','payee','transcript'])
def test_cross_clause_swaps_never_publish_a_draft_or_verdict(field):
    first=None
    def swap(text,result):
        nonlocal first
        draft=result.items[0]
        if first is None:first=draft
        else:result.items[0]=draft.model_copy(update={field:getattr(first,field)})
    api=gateway(tamper=swap)
    with pytest.raises(ParserContractViolation):api.message({'transcript':TEXT})
    assert not api.store and not api.verdicts


def test_ambiguous_first_is_held_and_manual_clarification_preserves_jane():
    api=gateway(first_name='John')
    items=api.message({'transcript':TEXT.replace('John Smith','John')})['items']
    first,second=items;assert first['draft']['unresolved']==['payee'] and first['policy']['decision']=='hold'
    before=dict(api.store);verdicts=dict(api.verdicts)
    # The clarification validator gets its own extraction of the original clause.
    api.messages.validator=LlmValidator(client=FakeChatClient([json.dumps({'intents':[{'intent_type':'transfer','payee_mention':'John','amount_value':'50.00','currency':'SGD'}]})]),directory=FixtureDirectory())
    picked=first['draft']['payee_candidates'][0]['id']
    new=api.clarify({'draft_id':first['draft']['id'],'field':'payee','answer':picked})['items'][0]
    assert new['draft']['id']!=first['draft']['id'] and new['draft']['payee']['id']==picked
    assert new['validator']['verdict']=='pass' and new['validator']['draft_hash']==draft_hash(new['draft'])
    assert new['draft']['transcript']==first['draft']['transcript']
    assert api.store[second['draft']['id']] is before[second['draft']['id']]
    assert api.verdicts[draft_hash(second['draft'])]==verdicts[draft_hash(second['draft'])]


def test_ambiguous_bare_second_stays_unresolved_and_cannot_guess():
    api=gateway(first_name='Jane',second_name='John')
    # Supply matching explicit amounts/names without relying on model invention.
    text='Send fifty dollars to Jane and thirty dollars to John.'
    items=api.message({'transcript':text})['items']
    assert items[0]['draft']['payee']['display_name']=='Jane Tan'
    assert items[1]['draft']['unresolved']==['payee'] and 'payee' not in items[1]['draft']
    assert items[1]['policy']['decision']=='hold'


def test_default_validator_freezes_both_and_policy_is_per_item():
    items=gateway(validator=UnconfiguredValidator()).message({'transcript':TEXT})['items']
    assert [x['validator']['verdict'] for x in items]==['freeze','freeze']
    over=TEXT.replace('thirty dollars','six thousand dollars')
    items=gateway(second_value='6000.00').message({'transcript':over})['items']
    assert [x['policy']['decision'] for x in items]==['allow','block']


def test_existing_unique_jane_resolution_is_unchanged():
    assert [p.display_name for p in match_payees('Jane',FixtureDirectory())]==['Jane Tan']


def test_parser_without_fragment_support_rejects_whole_request_without_publication():
    from backend.parser.stub import parse
    api=GatewayAPI(parser=parse, directory=FixtureDirectory(), validator=UnconfiguredValidator(), bank=None)
    result=api.message({'transcript':TEXT})
    assert result['result']=='rejected'
    assert not api.store and not api.verdicts
