"""A deliberately narrow grammar for two independent transfer requests.

This is not general multi-step planning. No ordering, conditions, references,
bills, equities, source-account overrides or model-authored segmentation is
accepted. Each returned clause is an unchanged substring of the user's text.
The existing parser and validator must still process each clause separately.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Optional, NoReturn

from backend.llm.text import sanitise
from backend.models.draft import TransactionDraft
from backend.parser.amount_rules import resolve_currency
from backend.parser.directory import PayeeDirectory, match_payees
from backend.parser.interface import ParserCannotHandle
from backend.parser.speech_norm import spoken_amount

_CONNECTOR = re.compile(r"\s+and\s+(?=(?:send|transfer)\b)", re.IGNORECASE)
_SHARED_VERB_CLAUSE = re.compile(r"(?:(?:send|transfer)\s+)?(.+?)\s+to\s+(.+)", re.IGNORECASE)
_AND = re.compile(r"\s+and\s+", re.IGNORECASE)
_CLAUSE = re.compile(r"(?:send|transfer)\s+(.+?)\s+to\s+(.+)", re.IGNORECASE)
_DEPENDENCY = re.compile(
    r"\b(?:then|after|before|if|unless|when|until|once|otherwise|remaining|remainder|rest|"
    r"balance|proceeds|same|that|it|them|respectively|split|between|first|second|again|"
    r"also|only|except|cancel|instead|either|or|both|together|total|another|each|from|using)\b",
    re.IGNORECASE,
)
_NUMBER_WORDS = set((
    "zero one two three four five six seven eight nine ten eleven twelve thirteen "
    "fourteen fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty "
    "sixty seventy eighty ninety hundred thousand and a grand dollar dollars singapore sgd"
).split())


def _refuse() -> NoReturn:
    raise ParserCannotHandle(
        "Only two independent send/transfer requests with exact SGD amounts and "
        "saved payee names are supported together. Send other requests separately.",
        reason="multi-intent unsupported",
    )


def independent_transfer_clauses(transcript: str, directory: Optional[PayeeDirectory]) -> Optional[tuple[str, str]]:
    """Return two safe verbatim clauses (a shared second verb is allowed), or None for the existing single path.

    A recognized but unsupported combination is rejected as a whole; it is
    never shortened to one draft. A partial name may produce payee candidates,
    but must actually name a payee in the authoritative directory.
    """
    parts = _CONNECTOR.split(transcript)
    if len(parts) > 2:
        _refuse()
    if len(parts) == 2:
        candidates = [tuple(part.strip() for part in parts)]
    else:
        # Consider only an explicit first transfer and an amount-to-payee
        # second clause. Enumerate boundaries rather than splitting number
        # phrases such as "one hundred and fifty" at their internal "and".
        if not re.match(r"(?:send|transfer)\s+", transcript.lstrip(), re.IGNORECASE):
            return None
        candidates = []
        for connector in _AND.finditer(transcript):
            first, second = transcript[:connector.start()].strip(), transcript[connector.end():].strip()
            if _CLAUSE.fullmatch(first) and _SHARED_VERB_CLAUSE.fullmatch(second):
                candidates.append((first, second))
        if not candidates:
            return None
    if directory is None or len(sanitise(transcript)) > 500 or _DEPENDENCY.search(transcript):
        _refuse()
    accepted = []
    for first, second in candidates:
        try:
            _check_clause(first, directory, shared_verb=False)
            _check_clause(second, directory, shared_verb=True)
        except ParserCannotHandle:
            continue
        accepted.append((first, second))
    if len(accepted) != 1:
        _refuse()
    return accepted[0]


def _check_clause(clause: str, directory: PayeeDirectory, *, shared_verb: bool) -> None:
    match = (_SHARED_VERB_CLAUSE if shared_verb else _CLAUSE).fullmatch(clause)
    if match is None:
        _refuse()
    money, recipient = match.groups()
    # Limit the accepted language before interpreting it. For example,
    # "fifty dollars after ..." must not become an independent payment.
    if not re.fullmatch(r"[\w\s.$]+", money):
        _refuse()
    tokens = re.findall(r"[a-z_]+|\d+(?:\.\d+)?|\$", money.lower())
    if any(token not in _NUMBER_WORDS and token != "$" and not re.fullmatch(r"\d+(?:\.\d{1,2})?", token) for token in tokens):
        _refuse()
    reading = spoken_amount(money)
    if reading.status != "exact" or resolve_currency(money) != "SGD" or Decimal(reading.value) <= 0:
        _refuse()
    # The amount reader must consume the entire numeric phrase, not
    # ignore a stray word such as "zero" in "zero fifty dollars".
    start, end = reading.span
    if money[:start].strip().lower() not in ("", "sgd", "$", "sgd $") or money[end:].strip().lower() not in (
        "", "dollar", "dollars", "sgd", "singapore dollar", "singapore dollars",
    ):
        _refuse()
    recipient = recipient.rstrip(".,").strip()
    offered = match_payees(recipient, directory, context=clause)
    phrase = recipient.casefold()
    if not offered or not any(
        phrase == payee.display_name.casefold() or phrase in payee.display_name.casefold().split()
        for payee in offered
    ):
        _refuse()


def independent_draft_matches(clause: str, draft: TransactionDraft, directory: PayeeDirectory) -> bool:
    """Bind parser output to this clause before any draft is published.

    The independent validator still runs. This extra deterministic boundary
    prevents a parser from swapping amounts/payees or guessing an ambiguous
    name, even if a later validator were to miss that mismatch.
    """
    money, recipient = _SHARED_VERB_CLAUSE.fullmatch(clause).groups()
    offered = match_payees(recipient.rstrip(".,").strip(), directory, context=clause)
    expected = {p.id: {"id": p.id, "display_name": p.display_name, "masked_account": p.masked_account} for p in offered}
    if (draft.transcript != clause or draft.intent_type != "transfer"
            or draft.source_account != directory.default_source_account()
            or draft.amount.value != spoken_amount(money).value or draft.amount.currency != "SGD"):
        return False
    if len(offered) == 1:
        return (not draft.unresolved and draft.payee is not None and not draft.payee_candidates
                and draft.payee.model_dump() == expected[offered[0].id])
    actual = draft.payee_candidates or []
    return (draft.unresolved == ["payee"] and draft.payee is None and len(actual) >= 2
            and len({p.id for p in actual}) == len(actual)
            and all(p.model_dump() == expected.get(p.id) for p in actual))
