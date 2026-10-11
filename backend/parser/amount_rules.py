"""
Deterministic amount rules shared by the parser, clarify and the validator.

These are stated PRODUCT RULES, applied by code.  The model's
``currency`` field is never used, and a model can never drop a minus
sign on the user's behalf.

CURRENCY RULE (DCTA is a Singapore app; its home currency is SGD)
------------------------------------------------------------------

The currency of a request is read from the user's own words:

- "dollar", "dollars", "bucks", "$", "SGD", "S$", "块", "元"  ->  SGD
- "US dollars", "USD", "US$"                                 ->  USD
- "RMB", "人民币"                                             ->  CNY
- no currency word at all                                    ->  SGD
  (the home currency, the same default the overlay shows)
- any other currency ("euros", "A$", "HKD", "日元", ...), or
  mentions of two different currencies                       ->  unclear

When the currency is unclear, the parser asks for the amount again and
the validator freezes.  Neither ever guesses.

Qualified forms are matched before the bare words they contain, so
"US dollars" is USD (not SGD + USD) and "美元" is "other" (not SGD).

NEGATIVE AMOUNTS
----------------

A minus sign (ASCII hyphen-minus, U+2212, dash variants) or the words
"minus" / "negative" attached to an amount make the request negative.
The parser asks for the amount; the validator freezes.  A hyphen inside
a word or number ("acct-001-2345", "2026-10-03") is not a sign.
"""

from __future__ import annotations

import re

HOME_CURRENCY = "SGD"

_OTHER = "OTHER"

# One alternation, scanned left to right with finditer.  At any
# position the first matching branch wins, so qualified forms (USD,
# other) are listed before the bare SGD words they contain.
_CURRENCY_RE = re.compile(
    r"""
    (?P<USD>
        \bus\s*dollars?\b | \busd\b | \bus\$
    )
  | (?P<CNY>
        \brmb\b | 人民币
    )
  | (?P<OTHER>
        \b(?:a|au|c|ca|hk|nz|nt|b|bn)\$
      | \b(?:australian|aussie|canadian|hong\s*kong|hk|new\s+zealand|nz
            |taiwan(?:ese)?|brunei|american|zimbabwe(?:an)?)\s+dollars?\b
      | \b(?:eur|euros?|gbp|pounds?|sterling|quid|jpy|yen|cny|yuan|renminbi
            |myr|ringgit|thb|baht|inr|rupees?|idr|rupiah|krw|php|pesos?
            |chf|francs?|aud|cad|hkd|nzd|twd|bnd|vnd|btc|bitcoins?|usdt)\b
      | [€£¥₹₩฿₱₫₽]
      | 美元 | 美金 | 日元 | 日币 | 欧元 | 港元 | 港币 | 澳元 | 加元 | 英镑
      | 台币 | 马币 | 令吉 | 韩元 | 泰铢 | 卢比 | 比索
    )
  | (?P<SGD>
        \bsgd\b | \bs\$ | \bsingapore\s+dollars?\b | \bdollars?\b
      | \bbucks?\b | \$ | 块 | 元
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def currency_mentions(*texts: str) -> list[str]:
    """Return the currency code of every currency mention, in order.

    Codes are ``"SGD"``, ``"USD"``, ``"CNY"`` or ``"OTHER"``.
    """
    found: list[str] = []
    for text in texts:
        if not text:
            continue
        for m in _CURRENCY_RE.finditer(text):
            found.append(m.lastgroup or _OTHER)
    return found


def resolve_currency(*texts: str) -> str | None:
    """Apply the currency rule to the user's own words.

    Pass every text the user supplied for this request (the transcript
    and, after a clarification, the answer).  Returns the ISO code, or
    ``None`` when the currency is unclear (an unsupported currency, or
    two different currencies).  No mention at all gives
    :data:`HOME_CURRENCY`.
    """
    codes = set(currency_mentions(*texts))
    if not codes:
        return HOME_CURRENCY
    if _OTHER in codes or len(codes) > 1:
        return None
    return codes.pop()


# ── Negative amounts ─────────────────────────────────────────────────

_MINUS_CHARS = "-−‐‑‒–—﹣－"
_CURRENCY_PREFIX = r"(?:us\$|s\$|sgd|usd|rmb|\$)?\s*"
_NUMBER_WORD = (
    r"(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven"
    r"|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen"
    r"|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety"
    r"|hundred|thousand|million|a\s+hundred|a\s+thousand|a\s+grand)\b"
)

_NEGATIVE_RE = re.compile(
    # A minus sign not preceded by a letter or digit, then (optionally)
    # a currency prefix, then a digit: "-50", "$-50", "-$50", "−50".
    rf"(?<![\w])[{_MINUS_CHARS}]\s*{_CURRENCY_PREFIX}\d"
    # A currency prefix directly followed by the sign: "S$-50".
    rf"|(?:us\$|s\$|\$)\s*[{_MINUS_CHARS}]\s*\d"
    # The words "minus" / "negative" before an amount.
    rf"|\b(?:minus|negative)\s+{_CURRENCY_PREFIX}(?:\d|{_NUMBER_WORD})"
    # Chinese: 负 before a digit or a numeral.
    r"|负\s*[\d零一二两三四五六七八九十百千万]",
    re.IGNORECASE,
)


def is_negative_amount(text: str) -> bool:
    """Return True if *text* attaches a minus sign or minus word to an amount."""
    if not text:
        return False
    return bool(_NEGATIVE_RE.search(text))


# ── Excess decimal places ────────────────────────────────────────────
#
# Money has two decimal places.  An amount said with more ("50.555")
# is never rounded, truncated or drafted: a model that quietly turns it
# into 50.56 or 50.55 would move money the user did not ask for.  The
# parser and clarify ask again; the validator freezes.  The rule reads
# the user's own text, so it does not depend on what the model returned.
# "50.500" is also asked about -- three decimals is not a money amount
# and the code never decides which trailing digits "don't count".
# The money pattern in the schema and the draft model is unchanged.

_EXCESS_DECIMALS_RE = re.compile(r"[0-9]\.[0-9]{3,}")


def has_excess_decimals(text: str) -> bool:
    """Return True if *text* has a number with more than two decimals."""
    if not text:
        return False
    return bool(_EXCESS_DECIMALS_RE.search(text))


def is_negative_value(value: str | None) -> bool:
    """Return True if an extracted amount value carries a minus sign."""
    if not value or not value.strip():
        return False
    v = value.strip().lower()
    return v[0] in _MINUS_CHARS or v.startswith(("minus", "negative"))
