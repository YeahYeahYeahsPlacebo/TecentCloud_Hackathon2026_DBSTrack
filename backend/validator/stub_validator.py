"""Deterministic validator: ``VALIDATOR_BACKEND=stub``.

Why this exists
---------------
Without ADP keys the only validator is :class:`UnconfiguredValidator`,
which returns ``pass`` with the reason ``"unvalidated (dev only)"`` when
``DCTA_ALLOW_UNVALIDATED=true``.  That is honest, and it is also the one
line on a demo screen that says out loud that nothing was checked.  The
LLM validator cannot run offline at all.

This backend fills the gap *without pretending to be a model*.  It reads
the transcript with the same code the parser trusts —
:func:`~backend.parser.speech_norm.spoken_amount` for the amount, the
stub parser for who was named, the directory for what a name resolves
to — and hands that reading to :func:`_compare`, the same comparison
:class:`~backend.validator.llm_validator.LlmValidator` uses.  So:

* a draft that disagrees with the transcript **freezes**, with the same
  discrepancies and the same kind of reason the model path produces;
* a draft that agrees **passes**, with reason ``"The draft matches the
  transcript."`` — which is exactly what happened, and does not claim a
  model looked at it;
* the entry checks (length, negative amount, more than two decimals)
  are :func:`_entry_freeze`, shared with the LLM path, so a check cannot
  hold for one backend and not the other.

What it is not
--------------
It is not an independent second opinion in the way the LLM validator is.
The re-reading goes through the parser, so a mistake the parser and this
validator share is not caught — the model path is the one that can
disagree with the parser.  What this backend catches is a draft that no
longer matches what the user said: a tampered amount, a swapped payee, a
currency that drifted.  Those are the cases a demo actually shows, and
the dev bypass currently passes all of them.

It is also not the default.  ``VALIDATOR_BACKEND=stub`` has to be asked
for, and ``/api/health`` reports ``StubValidator`` so the stage cannot
mistake it for the model.
"""

from __future__ import annotations

from typing import Any, Optional

from backend.canonical import draft_hash
from backend.llm.text import sanitise
from backend.models.draft import TransactionDraft
from backend.parser.directory import FixtureDirectory, PayeeDirectory
from backend.parser.interface import ClarificationRecord, ParserCannotHandle
from backend.parser.speech_norm import normalise, spoken_amount
from backend.parser.stub import parse as stub_parse
from backend.validator.interface import ValidatorVerdict
from backend.validator.llm_validator import (
    _compare,
    _entry_freeze,
    _ExtractedIntent,
    _ExtractionResult,
    _payee_pick,
)

__all__ = ["StubValidator"]


# ── Reading the transcript with code ──────────────────────────────────


def _read_amount(text: str) -> Optional[str]:
    """The amount said in *text*, or None when it does not say one.

    Only an exact reading counts.  "a few hundred" is not an amount here
    any more than it is in the parser.
    """
    if not text:
        return None
    reading = spoken_amount(normalise(text).text)
    if reading.status != "exact":
        return None
    return reading.value


def _reparsed_drafts(transcript: str, parser: Any) -> list[TransactionDraft]:
    """Every draft the stub parser reads out of *transcript*.

    More than one means the transcript asked for more than one thing,
    which :func:`_compare` freezes on.  Zero means the parser could not
    resolve the transcript into a draft at all (it asked a question
    instead) — the fields are then read straight from the text.

    A transcript the parser refuses is not an error here.  It means the
    code reading has nothing to stand on, and the caller gets no drafts:
    the draft under test is then compared against an extraction that
    says nothing, which freezes.  Letting :class:`ParserCannotHandle`
    escape would turn "I cannot check this" into a 500.
    """
    try:
        result = parser(transcript)
    except ParserCannotHandle:
        return []
    return [item for item in result.items if isinstance(item, TransactionDraft)]


def _payee_mention(
    clarification: Optional[ClarificationRecord],
    reparse: Optional[TransactionDraft],
    clean_answer: Optional[str],
    directory: PayeeDirectory,
) -> Optional[str]:
    """What the user named as the payee, as text.

    Three sources, in order of how directly the user stated it:

    1. a free-text answer to a payee question — the answer itself, which
       :func:`match_payees` reads against the directory;
    2. the transcript alone — what the re-parse resolved it to.

    A candidate pick deliberately yields None: the mention has to be
    something the *user* said, and a pick is the user choosing from a
    list, not naming someone.  Inventing the mention from the picked
    record would make the check vacuous — it would always match itself.
    :func:`_check_payee_pick` then falls back to asking whether the
    transcript names the picked payee, and :func:`_extra_checks` asks
    the sharper question: was the picked payee one of the candidates
    this transcript could mean at all.

    None when the user never said who.  :func:`_compare` then refuses a
    draft that names a payee anyway, which is the right answer.
    """
    if clarification is not None and clarification.field == "payee":
        return clean_answer

    if reparse is not None and reparse.payee is not None:
        return reparse.payee.display_name

    return None


def _amount_said(
    clarification: Optional[ClarificationRecord],
    reparse: Optional[TransactionDraft],
    clean_transcript: str,
    clean_answer: Optional[str],
) -> Optional[str]:
    """The amount the user said.

    When the amount was the clarified field it was said in the answer,
    not in the transcript — reading the transcript there would report
    that the user never named an amount.
    """
    if clarification is not None and clarification.field == "amount":
        return _read_amount(clean_answer or "")

    if reparse is not None and reparse.amount is not None:
        return reparse.amount.value

    return _read_amount(clean_transcript)


def _source_account_said(
    reparse: Optional[TransactionDraft],
    directory: PayeeDirectory,
) -> Optional[str]:
    """The source account the user named, or None for the default."""
    if reparse is None:
        return None
    default = directory.default_source_account()
    return reparse.source_account if reparse.source_account != default else None


def _build_intent(
    draft: TransactionDraft,
    reparse: Optional[TransactionDraft],
    mention: Optional[str],
    amount_value: Optional[str],
    source_account_mention: Optional[str],
) -> _ExtractedIntent:
    """One intent for :func:`_compare`, built entirely from code.

    ``currency`` is left None on purpose: :func:`_compare` applies the
    product currency rule to the user's texts itself and never trusts
    what the extractor said.
    """
    return _ExtractedIntent(
        intent_type=(
            reparse.intent_type.value
            if reparse is not None
            else draft.intent_type.value
        ),
        payee_mention=mention,
        amount_value=amount_value,
        currency=None,
        source_account_mention=source_account_mention,
        ticker_mention=reparse.ticker if reparse is not None else None,
        order_type=(
            reparse.order_type.value
            if reparse is not None and reparse.order_type is not None
            else None
        ),
    )


def _picked_is_a_candidate(
    clarification: Optional[ClarificationRecord],
    reparse: Optional[TransactionDraft],
    directory: PayeeDirectory,
) -> Optional[str]:
    """Why a payee pick is not one of the candidates this transcript fits.

    "Pay John two hundred dollars." could mean John Doe or John Lee and
    nobody else.  A draft that names John Smith because someone picked
    him must not pass: the pick is the only evidence of who was meant,
    so the pick has to be one of the candidates the transcript allows.
    """
    pick = _payee_pick(clarification)
    if pick is None or reparse is None:
        return None

    candidates = getattr(reparse, "payee_candidates", None) or []
    ids = {
        c.id if hasattr(c, "id") else c["id"]
        for c in candidates
    }
    if not ids:
        # The transcript named one payee outright, so there was nothing
        # to pick from and nothing to check here.
        return None
    if pick.answer in ids:
        return None

    named = next((p for p in directory.payees() if p.id == pick.answer), None)
    who = named.display_name if named is not None else pick.answer
    return (
        f"The payee that was picked ({who}) is not one of the payees "
        f"this transcript could mean."
    )


def _extra_checks(
    draft: TransactionDraft,
    reparse: Optional[TransactionDraft],
    clarification: Optional[ClarificationRecord],
    directory: PayeeDirectory,
) -> list[tuple[str, str]]:
    """Field checks :func:`_compare` cannot make from a mention.

    ``_compare`` resolves a ticker *mention* through the directory.  The
    re-parse has already resolved it, and passing a resolved ticker back
    through :func:`resolve_ticker` yields nothing — so the ticker and
    the order type are compared here instead.  The payee-pick check is
    here for the same reason: what makes a pick wrong is the candidate
    list, not a mention.
    """
    problems: list[tuple[str, str]] = []

    why = _picked_is_a_candidate(clarification, reparse, directory)
    if why is not None:
        problems.append(("payee", why))

    if reparse is None or draft.intent_type.value != "equity_purchase":
        return problems

    if reparse.ticker is not None and reparse.ticker != draft.ticker:
        problems.append(
            (
                "ticker",
                f"The ticker in the draft ({draft.ticker}) does not match "
                f"what was said ({reparse.ticker}).",
            )
        )
    if (
        reparse.order_type is not None
        and draft.order_type is not None
        and reparse.order_type != draft.order_type
    ):
        problems.append(
            (
                "order_type",
                f"The order type in the draft ({draft.order_type.value}) "
                f"does not match what was said ({reparse.order_type.value}).",
            )
        )
    return problems


# ── The validator ─────────────────────────────────────────────────────


class StubValidator:
    """A validator that reads the transcript with code, not a model.

    Implements the same protocol as
    :class:`~backend.validator.llm_validator.LlmValidator` and uses the
    same comparison, so the gateway cannot tell them apart and the audit
    record reads the same either way.
    """

    def __init__(
        self,
        directory: Optional[PayeeDirectory] = None,
        parser: Any = None,
    ) -> None:
        self._directory = directory if directory is not None else FixtureDirectory()
        self._parser = parser if parser is not None else stub_parse

    def validate(
        self,
        draft: TransactionDraft,
        transcript: str,
        clarification: Optional[ClarificationRecord] = None,
    ) -> ValidatorVerdict:
        """Validate *draft* against *transcript*, with no model call."""
        h = draft_hash(draft.model_dump(mode="json", exclude_none=True))

        clean_transcript = sanitise(transcript or "")
        clean_answer = (
            sanitise(clarification.answer) if clarification is not None else None
        )

        # The amount was said in the answer when the amount was the
        # clarified field, otherwise in the transcript.
        amount_text = (
            clean_answer
            if clarification is not None and clarification.field == "amount"
            else clean_transcript
        )
        early = _entry_freeze(clean_transcript, amount_text, h)
        if early is not None:
            return early

        drafts = _reparsed_drafts(clean_transcript, self._parser)
        reparse = drafts[0] if drafts else None

        # One intent per thing the parser read: two means the transcript
        # asked for two things, which _compare freezes on.
        intents = [
            _build_intent(
                draft,
                one,
                _payee_mention(clarification, one, clean_answer, self._directory)
                if one is reparse
                else (
                    one.payee.display_name if one.payee is not None else None
                ),
                _amount_said(clarification, one, clean_transcript, clean_answer)
                if one is reparse
                else (one.amount.value if one.amount is not None else None),
                _source_account_said(one, self._directory),
            )
            for one in drafts
        ]
        if not intents:
            # The parser asked a question instead of drafting anything,
            # so everything is read straight from the text.
            intents = [
                _build_intent(
                    draft,
                    None,
                    _payee_mention(
                        clarification, None, clean_answer, self._directory
                    ),
                    _amount_said(
                        clarification, None, clean_transcript, clean_answer
                    ),
                    None,
                )
            ]
        extraction = _ExtractionResult(intents=intents)

        user_texts = (clean_transcript,) + (
            (clean_answer,) if clean_answer is not None else ()
        )
        discrepancies, reason, should_freeze = _compare(
            draft, extraction, self._directory, user_texts, clarification
        )

        for field, why in _extra_checks(draft, reparse, clarification, self._directory):
            if field not in discrepancies:
                discrepancies.append(field)
            reason = f"{reason} {why}".strip()

        if should_freeze or discrepancies:
            return ValidatorVerdict(
                verdict="freeze",
                discrepancies=discrepancies,
                reason=reason,
                draft_hash=h,
            )

        return ValidatorVerdict(
            verdict="pass",
            discrepancies=[],
            reason=reason,
            draft_hash=h,
        )

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """The callable form is intentionally disabled.

        ``__call__`` would drop the ``ClarificationRecord`` that
        ``validate()`` needs to pass through.  Call ``validate()``
        directly instead.
        """
        raise TypeError(
            "Call validate(draft, transcript, clarification=record); "
            "the callable form drops the ClarificationRecord."
        )
