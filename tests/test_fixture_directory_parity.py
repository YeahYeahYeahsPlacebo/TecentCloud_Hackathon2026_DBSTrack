"""
Fixtures must not disagree with the payee directory about who a payee is.

The directory (``backend/parser/directory.py``) is the source of truth —
CONTRACT.md §4, and it is what ``resolve_payee`` fills the executed draft
from. A fixture that labels a candidate differently is not a cosmetic
drift: the candidate list is what the user is shown and clicks, so the
user picks one name and the money goes to another.

That is what happened to ``payee-102``: the directory knew it as "John
Lee" while ``ambiguous_payee.json`` still called it "John Smith", and
nothing in the suite compared the two. PR #24 aligned the fixture with
the directory; these tests are the comparison that keeps it aligned.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.parser.directory import FixtureDirectory  # noqa: E402

FIXTURES = ROOT / "fixtures" / "drafts"


def _fixture_paths():
    return sorted(FIXTURES.glob("*.json"))


def _directory_by_id():
    return {p.id: p for p in FixtureDirectory().payees()}


def _payee_shapes(data):
    """Every place a fixture can name a payee, as (where, id, name)."""
    payee = data.get("payee")
    if isinstance(payee, dict) and payee.get("id"):
        yield ("payee", payee["id"], payee.get("display_name"))
    for candidate in data.get("payee_candidates") or []:
        if candidate.get("id"):
            yield ("payee_candidates", candidate["id"], candidate.get("display_name"))


@pytest.mark.parametrize("path", _fixture_paths(), ids=lambda p: p.name)
def test_a_fixture_names_no_payee_the_directory_does_not_know(path):
    """An id the directory lacks cannot be resolved, so the draft is a
    trap: it renders, it is clickable, and /api/clarify refuses it."""
    data = json.loads(path.read_text(encoding="utf-8"))
    known = _directory_by_id()
    for where, payee_id, _ in _payee_shapes(data):
        assert payee_id in known, f"{path.name}: {where} names {payee_id!r}"


@pytest.mark.parametrize("path", _fixture_paths(), ids=lambda p: p.name)
def test_a_fixture_labels_every_payee_the_way_the_directory_does(path):
    """The name the user clicks is the name the draft must carry."""
    data = json.loads(path.read_text(encoding="utf-8"))
    known = _directory_by_id()
    for where, payee_id, name in _payee_shapes(data):
        assert name == known[payee_id].display_name, (
            f"{path.name}: {where} shows {payee_id!r} as {name!r} but the "
            f"directory says {known[payee_id].display_name!r}"
        )


def test_the_ambiguous_payee_fixture_offers_two_different_johns():
    """Pinned deliberately: the ambiguity is "which John", not "which
    account of one John". If a future edit collapses the two Johns into
    one person, the demo loses the case it exists to demonstrate."""
    data = json.loads((FIXTURES / "ambiguous_payee.json").read_text(encoding="utf-8"))
    names = [c["display_name"] for c in data["payee_candidates"]]
    assert len(names) == 2
    assert all(name.startswith("John") for name in names)
    assert len(set(names)) == 2, f"the two Johns collapsed into {names!r}"
