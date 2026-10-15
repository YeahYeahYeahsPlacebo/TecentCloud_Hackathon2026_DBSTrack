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
from backend.parser.amount_rules import resolve_currency
from backend.parser.directory import PayeeDirectory, match_payees
from backend.parser.interface import ParserCannotHandle
from backend.parser.speech_norm import spoken_amount

_CONNECTOR = re.compile(r"\s+and\s+(?=(?:send|transfer)\b)", re.IGNORECASE)
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
    """Return two safe verbatim clauses, or None for the existing single path.

    A recognized but unsupported combination is rejected as a whole; it is
    never shortened to one draft. A partial name may produce payee candidates,
    but must actually name a payee in the authoritative directory.
    """
    parts = _CONNECTOR.split(transcript)
    if len(parts) == 1:
        return None
    if directory is None or len(parts) != 2 or len(sanitise(transcript)) > 500 or _DEPENDENCY.search(transcript):
        _refuse()
    clauses = tuple(part.strip() for part in parts)
    for clause in clauses:
        match = _CLAUSE.fullmatch(clause)
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
        recipient = recipient.rstrip(".,").strip()
        offered = match_payees(recipient, directory, context=clause)
        phrase = recipient.casefold()
        if not offered or not any(
            phrase == payee.display_name.casefold() or phrase in payee.display_name.casefold().split()
            for payee in offered
        ):
            _refuse()
    return clauses[0], clauses[1]
