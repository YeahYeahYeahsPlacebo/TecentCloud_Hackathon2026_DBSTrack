"""
Speech-style English and Singlish input, on both parsers.

Offline: FakeChatClient scripts the LLM parser's model; the stub needs no
model.  The input is the final speech-to-text string plus whatever the user
corrected: lower case, little punctuation, fillers, spoken amounts, a missing
"to".  The server normalises (backend/parser/speech_norm.py); the frontend
passes the text unchanged.

Rules under test
----------------
R1  The draft's ``transcript`` is the exact submitted text, byte for byte.
R2  Never guess a payee: "mum", "my wife" are payee questions.
R3  Money words are exact amounts; "a few hundred", "some" are questions.
R4  Only the listed fillers are removed, and only as standalone words.
R5  A missing "to" is tolerated; the directory must resolve the payee.
R6  No crash on lower case, no punctuation, trailing whitespace.
R7  No new fields: nothing about normalisation is stored.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.llm.fake_client import FakeChatClient  # noqa: E402
from backend.models.draft import TransactionDraft  # noqa: E402
from backend.parser.clarify import resolve_question  # noqa: E402
from backend.parser.directory import FixtureDirectory, PayeeRecord  # noqa: E402
from backend.parser.interface import (  # noqa: E402
    ClarifyingQuestion,
    ParserCannotHandle,
    ParseResult,
    PendingQuestion,
)
from backend.parser.llm_parser import LlmParser  # noqa: E402
from backend.parser.speech_norm import (  # noqa: E402
    FILLERS,
    PERSON_WORDS,
    directory_name_words,
    normalise,
    payee_phrase,
    person_reference,
    spoken_amount,
)
from backend.parser import stub as stub_module  # noqa: E402

SCHEMA = json.loads((ROOT / "contract" / "draft.schema.json").read_text(encoding="utf-8"))
DIRECTORY = FixtureDirectory()


# ── Helpers ───────────────────────────────────────────────────────────


def _reply(mention=None, amount_text=None, amount_value=None, **extra) -> str:
    intent = {
        "intent_type": "transfer",
        "payee_mention": mention,
        "amount_text": amount_text,
        "amount_value": amount_value,
        "currency": None,
        "source_account_mention": None,
        "ticker_mention": None,
        "order_type": None,
        "uncertain_fields": [],
    }
    intent.update(extra)
    return json.dumps({"intents": [intent]})


def _llm(reply: str, directory=None):
    client = FakeChatClient([reply])
    return LlmParser(chat_client=client, directory=directory or DIRECTORY), client


def _llm_parse(text: str, reply: str, directory=None) -> ParseResult:
    parser, _ = _llm(reply, directory)
    return parser.parse(text)


def _check_result(result: ParseResult, submitted: str) -> object:
    """The checks every successful parse must pass; returns the one item."""
    assert len(result.items) == result.intents_detected == 1
    item = result.items[0]
    if isinstance(item, TransactionDraft):
        # R1: byte for byte.
        assert item.transcript == submitted
        assert item.transcript.encode("utf-8") == submitted.encode("utf-8")
        jsonschema.validate(
            instance=item.model_dump(mode="json", exclude_none=True), schema=SCHEMA,
        )
        TransactionDraft(**item.model_dump(mode="json", exclude_none=True))
    else:
        assert isinstance(item, ClarifyingQuestion)
        # A question never carries a draft, and the server-side state
        # keeps the raw wording (R1).
        pending = result.pending[item.question_id]
        assert isinstance(pending, PendingQuestion)
        assert pending.original_transcript == submitted
    return item


# The listed cases.  Each row: (submitted text, expected, model reply).
#   expected = ("draft", payee_id, amount) | ("question", field)
CASES = [
    pytest.param(
        "send fifty dollars john smith",
        ("draft", "payee-001", "50.00"),
        _reply("john smith", "fifty dollars", "50.00"),
        id="missing-to",
    ),
    pytest.param(
        "Send a grand to Jane.",
        ("draft", "payee-002", "1000.00"),
        _reply("Jane", "a grand", "1000.00"),
        id="a-grand",
    ),
    pytest.param(
        "send one thousand dollars to jane lah",
        ("draft", "payee-002", "1000.00"),
        _reply("jane", "one thousand dollars", "1000.00"),
        id="singlish-lah",
    ),
    pytest.param(
        "Send fifteen dollars to mum.",
        ("question", "payee"),
        _reply("mum", "fifteen dollars", "15.00"),
        id="mum",
    ),
    pytest.param(
        "transfer five hundred dollars to my wife",
        ("question", "payee"),
        _reply("my wife", "five hundred dollars", "500.00"),
        id="my-wife",
    ),
    pytest.param(
        "Send some money to Jane.",
        ("question", "amount"),
        # The model "helpfully" guesses: it must be ignored.
        _reply("Jane", "some money", "100.00"),
        id="some-money",
    ),
    pytest.param(
        "send a few hundred to jane",
        ("question", "amount"),
        _reply("jane", "a few hundred", "300.00"),
        id="a-few-hundred",
    ),
    pytest.param(
        "uh send um fifty dollars to john smith",
        ("draft", "payee-001", "50.00"),
        _reply("john smith", "fifty dollars", "50.00"),
        id="fillers-uh-um",
    ),
    pytest.param(
        "SEND FIFTY DOLLARS TO JANE",
        ("draft", "payee-002", "50.00"),
        _reply("JANE", "FIFTY DOLLARS", "50.00"),
        id="upper-case",
    ),
    pytest.param(
        "send fifty dollars to jane   ",
        ("draft", "payee-002", "50.00"),
        _reply("jane", "fifty dollars", "50.00"),
        id="trailing-spaces",
    ),
]


def _assert_expected(item, expected) -> None:
    if expected[0] == "draft":
        assert isinstance(item, TransactionDraft), item
        assert item.unresolved == []
        assert item.payee.id == expected[1]
        assert item.amount.value == expected[2]
        assert item.amount.currency == "SGD"
    else:
        assert isinstance(item, ClarifyingQuestion), item
        assert item.field == expected[1]


# ═══════════════════════════════════════════════════════════════════════
# The listed cases, on both parsers
# ═══════════════════════════════════════════════════════════════════════


class TestListedCasesLlmParser:
    @pytest.mark.parametrize("text,expected,reply", CASES)
    def test_case(self, text, expected, reply):
        result = _llm_parse(text, reply)
        item = _check_result(result, text)
        _assert_expected(item, expected)


class TestListedCasesStub:
    @pytest.mark.parametrize("text,expected,reply", CASES)
    def test_case(self, text, expected, reply):
        item = _check_result(stub_module.parse(text), text)
        _assert_expected(item, expected)


class TestRawTranscriptIsUntouched:
    """R1, across the awkward inputs: whatever normalisation does, the
    draft and the pending question keep the submitted bytes."""

    SUBMITTED = [
        "send fifty dollars to jane   ",
        "   send fifty dollars to jane",
        "send   fifty  dollars   to jane",
        "send fifty dollars to jane\n",
        "send fifty dollars\tto jane",
        "Send fifty dollars to Ja​ne.",
        "uh send um fifty dollars to jane lah.",
        "SEND FIFTY DOLLARS TO JANE",
        "send like fifty dollars to jane",
    ]

    @pytest.mark.parametrize("text", SUBMITTED)
    def test_llm_draft_keeps_the_submitted_text(self, text):
        result = _llm_parse(text, _reply("jane", "fifty dollars", "50.00"))
        _check_result(result, text)

    @pytest.mark.parametrize("text", SUBMITTED)
    def test_stub_draft_keeps_the_submitted_text(self, text):
        _check_result(stub_module.parse(text), text)

    @pytest.mark.parametrize("text", [
        "send some money to jane  ",
        "uh send a few hundred to jane lah",
        "Send fifteen dollars to mum   ",
    ])
    def test_pending_question_keeps_the_submitted_text(self, text):
        for result in (
            _llm_parse(text, _reply("jane" if "mum" not in text else "mum", None, None)),
            stub_module.parse(text),
        ):
            item = _check_result(result, text)
            assert isinstance(item, ClarifyingQuestion)

    def test_normalised_form_never_reaches_the_draft(self):
        text = "uh send um fifty dollars to john smith lah"
        draft = _llm_parse(text, _reply("john smith", "fifty dollars", "50.00")).items[0]
        assert draft.transcript == text
        assert "uh" in draft.transcript and "lah" in draft.transcript

    def test_the_model_sees_the_working_text_not_the_fillers(self):
        text = "uh send um fifty dollars to john smith lah"
        parser, client = _llm(_reply("john smith", "fifty dollars", "50.00"))
        parser.parse(text)
        prompt = client.received_messages[0]
        assert "send fifty dollars to john smith" in prompt
        assert text not in prompt


# ═══════════════════════════════════════════════════════════════════════
# R4: fillers
# ═══════════════════════════════════════════════════════════════════════


class TestFillers:
    def test_the_listed_fillers_are_exactly_these(self):
        assert FILLERS == ("uh", "um", "lah", "lor", "leh", "meh", "hor", "ah", "like")

    @pytest.mark.parametrize("filler", ["uh", "um", "lah", "lor", "leh", "meh", "hor", "ah"])
    def test_each_listed_filler_is_removed(self, filler):
        assert normalise(f"send {filler} fifty dollars to jane").text == "send fifty dollars to jane"
        assert normalise(f"send fifty dollars to jane {filler}").text == "send fifty dollars to jane"
        assert normalise(f"{filler} send fifty dollars to jane").text == "send fifty dollars to jane"

    def test_like_is_removed_only_between_words(self):
        assert normalise("send like fifty dollars to jane").text == "send fifty dollars to jane"
        assert normalise("send fifty dollars to jane like lah").text == "send fifty dollars to jane"
        # At either end it is a content word.
        assert normalise("Like fifty dollars").text == "Like fifty dollars"
        assert normalise("send fifty dollars to jane like").text == "send fifty dollars to jane like"

    def test_filler_case_and_punctuation(self):
        assert normalise("send fifty dollars to Jane LAH.").text == "send fifty dollars to Jane."
        assert normalise("send fifty to jane, lor!").text == "send fifty to jane!"
        assert normalise("Uh, send fifty dollars to jane").text == "send fifty dollars to jane"

    @pytest.mark.parametrize("word", [
        "Sarah", "Allah", "Lahore", "likely", "likes", "umbrella", "umm", "uhh",
        "hormone", "ahead", "meh-meh", "ah-beng", "lah's", "well", "so", "actually",
        "hmm", "okay", "please", "just", "maybe",
    ])
    def test_nothing_else_is_removed(self, word):
        text = f"send fifty dollars to {word}"
        assert normalise(text).text == text

    def test_content_words_survive(self):
        text = "please send exactly fifty dollars to the person called jane"
        assert normalise(text).text == text

    def test_a_filler_that_is_part_of_a_payee_name_is_protected(self):
        class _WithAhBeng(FixtureDirectory):
            _PAYEES = FixtureDirectory._PAYEES + (
                PayeeRecord(id="payee-401", display_name="Ah Beng", masked_account="****0401"),
            )

        d = _WithAhBeng()
        assert {"ah", "beng"} <= directory_name_words(d)
        text = "send fifty dollars to ah beng lah"
        assert normalise(text, protected_words=directory_name_words(d)).text == (
            "send fifty dollars to ah beng"
        )
        # Without the protection the name would be mangled.
        assert normalise(text).text == "send fifty dollars to beng"

    def test_protected_name_end_to_end_llm_parser(self):
        class _WithAhBeng(FixtureDirectory):
            _PAYEES = FixtureDirectory._PAYEES + (
                PayeeRecord(id="payee-401", display_name="Ah Beng", masked_account="****0401"),
            )

        text = "send fifty dollars to ah beng lah"
        parser, client = _llm(_reply("ah beng", "fifty dollars", "50.00"), _WithAhBeng())
        draft = parser.parse(text).items[0]
        assert isinstance(draft, TransactionDraft)
        assert draft.payee.id == "payee-401"
        assert "to ah beng" in client.received_messages[0]
        assert draft.transcript == text

    def test_protected_name_end_to_end_stub(self, monkeypatch):
        class _WithAhBeng(FixtureDirectory):
            _PAYEES = FixtureDirectory._PAYEES + (
                PayeeRecord(id="payee-401", display_name="Ah Beng", masked_account="****0401"),
            )

        monkeypatch.setattr(stub_module, "FixtureDirectory", _WithAhBeng)
        draft = stub_module.parse("send fifty dollars to ah beng lah").items[0]
        assert isinstance(draft, TransactionDraft) and draft.payee.id == "payee-401"

    def test_rules_applied_are_reported_and_the_input_is_not_changed(self):
        text = "uh send um fifty dollars to jane lah"
        result = normalise(text)
        assert result.original == text
        assert result.changed
        assert result.rules == ("remove_filler:uh", "remove_filler:um", "remove_filler:lah")

    def test_nothing_to_do(self):
        result = normalise("send fifty dollars to jane")
        assert result.rules == () and not result.changed

    def test_only_fillers_leaves_nothing(self):
        assert normalise("uh um lah").text == ""
        assert normalise("").text == ""
        assert normalise(None).text == ""  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════
# R3: money words
# ═══════════════════════════════════════════════════════════════════════


class TestSpokenAmountExact:
    @pytest.mark.parametrize("text,value", [
        # The brief's list.
        ("a grand", "1000.00"),
        ("fifteen", "15.00"),
        ("fifty", "50.00"),
        ("one thousand", "1000.00"),
        ("five hundred", "500.00"),
        ("two hundred fifty", "250.00"),
        # Around it.
        ("two hundred and fifty", "250.00"),
        ("one hundred and twenty five", "125.00"),
        ("twenty-five", "25.00"),
        ("twenty five", "25.00"),
        ("a hundred", "100.00"),
        ("a thousand", "1000.00"),
        ("two grand", "2000.00"),
        ("fifty thousand", "50000.00"),
        ("one thousand five hundred", "1500.00"),
        ("ten", "10.00"),
        # Digits.
        ("50", "50.00"),
        ("50.50", "50.50"),
        ("$1,000", "1000.00"),
        ("50,000", "50000.00"),
        ("5k", "5000.00"),
        ("2 grand", "2000.00"),
        ("5 hundred", "500.00"),
    ])
    def test_exact(self, text, value):
        reading = spoken_amount(f"send {text} dollars to jane")
        assert (reading.status, reading.value) == ("exact", value)

    def test_the_rule_is_reported_for_logs_only(self):
        reading = spoken_amount("send a grand to jane")
        assert reading.phrase == "a grand"
        assert reading.rule == "'a grand' -> 1000.00"

    def test_digits_after_an_account_keyword_are_not_an_amount(self):
        reading = spoken_amount("send fifty dollars to john smith account ending 8892")
        assert (reading.status, reading.value) == ("exact", "50.00")


class TestSpokenAmountIsNeverAGuess:
    @pytest.mark.parametrize("text", [
        "a few hundred", "a few", "some money", "some", "a bit", "a lot",
        "a lot of money", "a couple hundred", "a couple of hundred", "several hundred",
        "hundreds", "thousands", "a bunch", "a little", "plenty", "loads",
        "about fifty", "around fifty dollars", "roughly a hundred", "maybe fifty",
        "fifty or so", "fifty-ish", "fifty odd", "nearly a hundred",
        "two fifty", "five five", "thirteen hundred", "a million", "one million",
        "half a grand", "a quarter", "fifty point five",
        "hundred", "grand", "thousand",
    ])
    def test_vague(self, text):
        reading = spoken_amount(f"send {text} to jane")
        assert reading.status == "vague", (text, reading)
        assert reading.value is None

    @pytest.mark.parametrize("text", ["send one to jane", "send five to jane"])
    def test_a_lone_small_number_word_needs_a_currency_word(self, text):
        assert spoken_amount(text).status == "vague"

    @pytest.mark.parametrize("text,value", [
        ("send one dollar to jane", "1.00"),
        ("send five bucks to jane", "5.00"),
        ("send sgd five to jane", "5.00"),
    ])
    def test_beside_a_currency_word_it_is_exact(self, text, value):
        assert spoken_amount(text).value == value

    def test_two_numbers_are_not_resolved_by_picking_one(self):
        assert spoken_amount("send fifty dollars to jane actually five hundred").status == "multiple"
        assert spoken_amount("send fifty dollars at 5 pm").status == "multiple"

    @pytest.mark.parametrize("text", ["send money to jane", "send to jane", "hello", "", "   "])
    def test_no_amount_at_all(self, text):
        assert spoken_amount(text).status == "none"

    def test_robust_to_odd_input(self):
        for text in (None, 0, "   ", "\n", "$", "-", "..."):
            assert spoken_amount(text).status == "none"  # type: ignore[arg-type]


class TestVagueAmountsNeverBecomeANumber:
    """The parser asks, however helpful the model tries to be."""

    @pytest.mark.parametrize("text,guess", [
        ("send a few hundred to jane", "300.00"),
        ("send a few hundred to jane", "100.00"),
        ("Send some money to Jane.", "50.00"),
        ("send a bit to jane", "10.00"),
        ("send a lot to jane", "1000.00"),
        ("send about fifty dollars to jane", "50.00"),
        ("send two fifty to jane", "250.00"),
    ])
    def test_llm_parser_asks_whatever_the_model_returned(self, text, guess):
        result = _llm_parse(text, _reply("jane" if "Jane" not in text else "Jane", None, guess))
        item = _check_result(result, text)
        assert isinstance(item, ClarifyingQuestion) and item.field == "amount"
        partial = result.pending[item.question_id].partial
        assert "amount" not in partial                       # no number leaked
        assert partial["payee"]["id"] == "payee-002"         # the payee still carried

    @pytest.mark.parametrize("text", [
        "send a few hundred to jane", "Send some money to Jane.",
        "send a bit to jane", "send about fifty dollars to jane",
    ])
    def test_stub_asks(self, text):
        item = _check_result(stub_module.parse(text), text)
        assert isinstance(item, ClarifyingQuestion) and item.field == "amount"


class TestExactAmountIsFixedByCode:
    def test_the_model_cannot_change_an_exact_amount(self):
        text = "send one thousand dollars to jane"
        result = _llm_parse(text, _reply("jane", "one thousand dollars", "100.00"))
        item = _check_result(result, text)
        # Two readings disagree: ask, do not pick one.
        assert isinstance(item, ClarifyingQuestion) and item.field == "amount"

    def test_the_model_returning_nothing_does_not_stop_a_clear_amount(self):
        text = "Send a grand to Jane."
        draft = _llm_parse(text, _reply("Jane", "a grand", None)).items[0]
        assert isinstance(draft, TransactionDraft)
        assert draft.amount.value == "1000.00"

    def test_a_model_that_says_uncertain_does_not_stop_a_clear_amount(self):
        text = "Send a grand to Jane."
        draft = _llm_parse(
            text, _reply("Jane", "a grand", None, uncertain_fields=["amount"]),
        ).items[0]
        assert isinstance(draft, TransactionDraft) and draft.amount.value == "1000.00"

    def test_an_invented_amount_text_is_still_refused(self):
        text = "send fifty dollars to jane"
        item = _llm_parse(text, _reply("jane", "five hundred dollars", "500.00")).items[0]
        assert isinstance(item, ClarifyingQuestion) and item.field == "amount"

    def test_equity_notional_follows_the_exact_amount(self):
        reply = json.dumps({"intents": [{
            "intent_type": "equity_purchase", "payee_mention": None,
            "amount_text": "a grand", "amount_value": None, "currency": None,
            "source_account_mention": None, "ticker_mention": "DBS",
            "order_type": "market", "uncertain_fields": [],
        }]})
        text = "buy a grand of DBS shares at market"
        draft = _llm_parse(text, reply).items[0]
        assert isinstance(draft, TransactionDraft)
        assert (draft.amount.value, draft.notional_amount) == ("1000.00", "1000.00")

    def test_negative_and_over_precise_amounts_still_ask(self):
        for text in ("send minus fifty dollars to jane", "Send 50.555 dollars to Jane."):
            item = _llm_parse(text, _reply("jane", "fifty dollars", "50.00")).items[0]
            assert isinstance(item, ClarifyingQuestion) and item.field == "amount"
            assert isinstance(stub_module.parse(text).items[0], ClarifyingQuestion)

    def test_a_non_english_amount_is_left_to_the_existing_path(self):
        """No English number words -> status "none" -> the model's value,
        checked as before (the stricter rules above are English-only)."""
        text = "转五十块给Jane"
        assert spoken_amount(text).status == "none"
        draft = _llm_parse(text, _reply("Jane", "五十块", "50.00")).items[0]
        assert isinstance(draft, TransactionDraft) and draft.amount.value == "50.00"


class TestClarificationAnswers:
    """R3 holds when the amount arrives as an answer to a question."""

    def _pending(self):
        parser, _ = _llm(_reply("john smith", None, None))
        result = parser.parse("Send some money to John Smith.")
        item = result.items[0]
        return result.pending[item.question_id]

    def _answer(self, answer, model_reply):
        client = FakeChatClient([model_reply])
        return resolve_question(self._pending(), answer, client, DIRECTORY)

    def _model(self, value=None, text=None, resolved=True):
        if not resolved:
            return json.dumps({"resolved": False})
        return json.dumps({"resolved": True, "amount_text": text, "amount_value": value})

    @pytest.mark.parametrize("answer,guess", [
        ("a few hundred", "300.00"), ("some", "10.00"), ("a lot", "1000.00"),
        ("about fifty", "50.00"), ("two fifty", "250.00"),
    ])
    def test_a_vague_answer_asks_again_and_never_calls_the_model(self, answer, guess):
        client = FakeChatClient([self._model(guess, answer)])
        result, record = resolve_question(self._pending(), answer, client, DIRECTORY)
        item = result.items[0]
        assert isinstance(item, ClarifyingQuestion) and item.field == "amount"
        assert client.received_messages == []
        assert record.answer == answer

    def test_an_exact_slang_answer_is_fixed_by_code(self):
        result, _ = self._answer("a grand", self._model("1000.00", "a grand"))
        draft = result.items[0]
        assert isinstance(draft, TransactionDraft)
        assert (draft.amount.value, draft.payee.id) == ("1000.00", "payee-001")

    def test_the_model_not_understanding_slang_does_not_stop_it(self):
        result, _ = self._answer("a grand", self._model(resolved=False))
        assert result.items[0].amount.value == "1000.00"

    def test_fillers_in_an_answer_are_ignored(self):
        result, _ = self._answer("uh five hundred dollars lah", self._model("500.00", "five hundred dollars"))
        assert result.items[0].amount.value == "500.00"

    def test_a_disagreeing_model_asks_again(self):
        result, _ = self._answer("five hundred", self._model("50.00", "five hundred"))
        assert isinstance(result.items[0], ClarifyingQuestion)

    def test_the_draft_transcript_is_the_original_not_the_answer(self):
        result, record = self._answer("a grand", self._model("1000.00", "a grand"))
        assert result.items[0].transcript == "Send some money to John Smith."
        assert record.answer == "a grand"


# ═══════════════════════════════════════════════════════════════════════
# R2: person references
# ═══════════════════════════════════════════════════════════════════════


class TestPersonReferences:
    def test_the_listed_words(self):
        assert PERSON_WORDS == (
            "mum", "mom", "ma", "dad", "wife", "husband", "boss", "brother", "sister",
        )

    @pytest.mark.parametrize("word", PERSON_WORDS)
    def test_bare_and_my_forms_are_detected(self, word):
        assert person_reference(word) == word
        assert person_reference(f"my {word}") == f"my {word}"
        assert person_reference(f"My {word.upper()}") == f"my {word}"
        assert person_reference(f"{word}'s account") == word

    @pytest.mark.parametrize("phrase", [
        "jane", "john smith", "mark", "madam", "mama", "manager", "mumbai",
        "brothers", "my friend", "", None,
    ])
    def test_names_and_other_words_are_not_references(self, phrase):
        assert person_reference(phrase) is None

    @pytest.mark.parametrize("word", PERSON_WORDS)
    @pytest.mark.parametrize("form", ["{w}", "my {w}"])
    def test_a_reference_never_becomes_a_draft_on_either_parser(self, word, form):
        phrase = form.format(w=word)
        text = f"send fifteen dollars to {phrase}"
        for result in (
            _llm_parse(text, _reply(phrase, "fifteen dollars", "15.00")),
            stub_module.parse(text),
        ):
            item = _check_result(result, text)
            assert isinstance(item, ClarifyingQuestion) and item.field == "payee"
            assert result.pending[item.question_id].partial["amount"] == {
                "value": "15.00", "currency": "SGD",
            }
            assert "payee" not in result.pending[item.question_id].partial

    def test_the_question_names_the_relation_not_a_guess(self):
        item = stub_module.parse("transfer five hundred dollars to my wife").items[0]
        assert 'my wife' in item.question and "saved payees" in item.question

    @pytest.mark.parametrize("phrase", [
        "mum", "ma", "my mom", "the boss", "my dad", "wife", "my husband",
        "my brother", "my sister",
    ])
    def test_no_directory_entry_is_ever_chosen_for_a_relation(self, phrase):
        """Whatever the relation, none of the four payees is picked for it."""
        text = f"send fifty dollars to {phrase}"
        stub_item = stub_module.parse(text).items[0]
        assert isinstance(stub_item, ClarifyingQuestion) and stub_item.field == "payee"
        mention = phrase.replace("the ", "") if phrase.startswith("the ") else phrase
        llm_item = _llm_parse(text, _reply(mention, "fifty dollars", "50.00")).items[0]
        assert isinstance(llm_item, ClarifyingQuestion) and llm_item.field == "payee"

    def test_a_model_that_hallucinates_a_name_for_a_relation_is_refused(self):
        text = "Send fifteen dollars to mum."
        item = _llm_parse(text, _reply("Jane Tan", "fifteen dollars", "15.00")).items[0]
        assert isinstance(item, ClarifyingQuestion) and item.field == "payee"

    def test_a_model_that_finds_no_recipient_for_a_relation_still_asks(self):
        text = "Send fifteen dollars to mum."
        item = _llm_parse(text, _reply(None, "fifteen dollars", "15.00")).items[0]
        assert isinstance(item, ClarifyingQuestion) and item.field == "payee"
        assert "mum" in item.question

    def test_a_relation_that_IS_a_directory_name_is_a_direct_match_not_a_mapping(self):
        """Only a payee whose name is literally "Ma" can be reached by "ma"."""
        class _WithMa(FixtureDirectory):
            _PAYEES = FixtureDirectory._PAYEES + (
                PayeeRecord(id="payee-501", display_name="Ma Li", masked_account="****0501"),
            )

        text = "send fifty dollars to ma"
        draft = _llm_parse(text, _reply("ma", "fifty dollars", "50.00"), _WithMa()).items[0]
        assert isinstance(draft, TransactionDraft) and draft.payee.id == "payee-501"
        # With the default directory the same words are a question.
        item = _llm_parse(text, _reply("ma", "fifty dollars", "50.00")).items[0]
        assert isinstance(item, ClarifyingQuestion)

    def test_an_amount_question_still_comes_first(self):
        item = stub_module.parse("send some money to my wife").items[0]
        assert isinstance(item, ClarifyingQuestion) and item.field == "amount"


# ═══════════════════════════════════════════════════════════════════════
# R5: a missing "to"
# ═══════════════════════════════════════════════════════════════════════


class TestMissingTo:
    @pytest.mark.parametrize("text,phrase", [
        ("send fifty dollars to john smith", "john smith"),
        ("send fifty dollars john smith", "john smith"),
        ("send john smith fifty dollars", "john smith"),
        ("pay jane fifty dollars", "jane"),
        ("send a grand jane", "jane"),
        ("send $50 to jane please", "jane"),
        ("I want to send fifty dollars to jane", "jane"),
        ("fifty dollars to jane", "jane"),
        ("send fifteen dollars to mum.", "mum"),
        ("send fifty dollars to john smith account ending 8892", "john smith account ending 8892"),
        ("send money", None),
        ("send fifty dollars", None),
    ])
    def test_payee_phrase(self, text, phrase):
        assert payee_phrase(text) == phrase

    @pytest.mark.parametrize("text,payee_id", [
        ("send fifty dollars john smith", "payee-001"),
        ("send a grand jane", "payee-002"),
        ("pay jane fifty dollars", "payee-002"),
        ("send john smith fifty dollars", "payee-001"),
    ])
    def test_unique_directory_match_drafts_on_both_parsers(self, text, payee_id):
        # The model found no recipient at all: the words left over decide.
        for result in (
            _llm_parse(text, _reply(None, None, None)),
            stub_module.parse(text),
        ):
            item = _check_result(result, text)
            assert isinstance(item, TransactionDraft)
            assert item.payee.id == payee_id and item.unresolved == []

    @pytest.mark.parametrize("text", [
        "send fifty dollars jon smith",      # a near-miss is not a match
        "send fifty dollars johnny",
        "send fifty dollars bob",
        "send fifty dollars smith lee",
        "send fifty dollars",
    ])
    def test_no_unique_match_is_a_payee_question(self, text):
        for result in (
            _llm_parse(text, _reply(None, None, None)),
            stub_module.parse(text),
        ):
            item = _check_result(result, text)
            assert isinstance(item, ClarifyingQuestion) and item.field == "payee"

    def test_an_ambiguous_name_is_an_unresolved_draft_never_a_chosen_payee(self):
        """CONTRACT.md §2: ambiguity is a draft with payee unresolved and
        candidates, which can never be signed; the user picks one."""
        text = "send fifty dollars john"
        llm = _llm_parse(text, _reply(None, None, None)).items[0]
        stub = stub_module.parse(text).items[0]
        for draft in (llm, stub):
            assert isinstance(draft, TransactionDraft)
            assert draft.unresolved == ["payee"] and draft.payee is None
            assert len(draft.payee_candidates) >= 2

    def test_the_account_clue_still_decides_without_a_to(self):
        text = "send fifty dollars john smith account ending 8892"
        for result in (_llm_parse(text, _reply(None, None, None)), stub_module.parse(text)):
            item = result.items[0]
            # 8892 is John Lee's account, not John Smith's: never drafted.
            assert isinstance(item, ClarifyingQuestion) and item.field == "payee"

    def test_a_model_mention_not_in_the_text_is_never_rescued_by_the_fallback(self):
        text = "send fifty dollars john smith"
        item = _llm_parse(text, _reply("Jane Tan", "fifty dollars", "50.00")).items[0]
        assert isinstance(item, ClarifyingQuestion) and item.field == "payee"


# ═══════════════════════════════════════════════════════════════════════
# R6: robustness
# ═══════════════════════════════════════════════════════════════════════


class TestRobustness:
    INPUTS = [
        "send fifty dollars to jane",
        "SEND FIFTY DOLLARS TO JANE",
        "send fifty dollars to jane   ",
        "   send fifty dollars to jane",
        "send fifty dollars to jane\n\n",
        "send\tfifty\tdollars\tto\tjane",
        "send fifty dollars to jane.....",
        "!!! send fifty dollars to jane ???",
        "um",
        "uh um lah",
        "...",
        "   ",
        "",
        "a",
        "to",
        "send",
        "dollars",
        "send to",
        "send fifty",
        "$",
        "-",
        "​​",
        "ａｂｃ 五十",
        "send " + "fifty " * 80 + "dollars to jane",
    ]

    @pytest.mark.parametrize("text", INPUTS)
    def test_stub_never_crashes(self, text):
        try:
            result = stub_module.parse(text)
        except ParserCannotHandle:
            return
        _check_result(result, text)

    @pytest.mark.parametrize("text", INPUTS)
    def test_llm_parser_never_crashes(self, text):
        parser, _ = _llm(_reply("jane", "fifty dollars", "50.00"))
        try:
            result = parser.parse(text)
        except ParserCannotHandle:
            return
        _check_result(result, text)

    @pytest.mark.parametrize("text", ["", "   ", "uh um lah", "...", "​"])
    def test_nothing_to_parse_is_declined_not_sent_to_the_model(self, text):
        parser, client = _llm(_reply("jane", "fifty dollars", "50.00"))
        with pytest.raises(ParserCannotHandle):
            parser.parse(text)
        assert client.received_messages == []
        with pytest.raises(ParserCannotHandle):
            stub_module.parse(text)

    def test_the_length_limit_is_measured_before_normalisation(self):
        text = "um " * 300 + "send fifty dollars to jane"
        parser, client = _llm(_reply("jane", "fifty dollars", "50.00"))
        with pytest.raises(ParserCannotHandle) as exc:
            parser.parse(text)
        assert exc.value.reason == "request too long"
        assert client.received_messages == []


# ═══════════════════════════════════════════════════════════════════════
# R7: nothing new is stored
# ═══════════════════════════════════════════════════════════════════════


class TestNothingNewIsStored:
    def test_no_draft_field_was_added(self):
        assert set(TransactionDraft.model_fields) <= set(SCHEMA["properties"])

    def test_a_draft_carries_nothing_about_normalisation(self):
        text = "uh send a grand to jane lah"
        draft = _llm_parse(text, _reply("jane", "a grand", "1000.00")).items[0]
        dumped = draft.model_dump(mode="json", exclude_none=True)
        assert set(dumped) <= set(SCHEMA["properties"])
        blob = json.dumps(dumped).lower()
        for leaked in ("remove_filler", "normalis", "rules", "1000.00 <-", "->"):
            assert leaked not in blob

    def test_the_result_has_no_new_field(self):
        assert set(ParseResult.model_fields) == {"items", "intents_detected", "pending"}

    def test_the_pending_question_has_no_new_field(self):
        assert set(PendingQuestion.model_fields) == {
            "question_id", "field", "original_transcript", "partial",
            "created_at", "expires_at",
        }


class TestOriginalAlwaysSurvives:
    @pytest.mark.parametrize("text", [
        "uh send um a grand to jane lah  ",
        "SEND FIFTY DOLLARS TO JANE",
        "send fifty dollars john smith",
    ])
    def test_normalise_returns_a_new_value_and_keeps_the_original(self, text):
        result = normalise(text)
        assert result.original is text or result.original == text
        assert result.original.encode() == text.encode()
