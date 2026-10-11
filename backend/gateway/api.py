"""
Every endpoint in CONTRACT.md §4, wired together into one object.

This is the server-side surface: a real HTTP layer (FastAPI, Flask, …) would
translate requests into these calls and status codes back out — 422 for any
``{"result": "rejected", ...}``, 200 otherwise. Nothing here knows about HTTP,
so the whole gateway is testable without a server.

The pieces it wires:

- :class:`~backend.gateway.message.MessageService` — `/api/message`, `/api/clarify`
- :class:`~backend.gateway.credential.CredentialService` — `/api/webauthn/*`
- :class:`~backend.gateway.execute.ExecuteGateway` — `/api/execute`
- :class:`~backend.audit.AuditLog` — `/api/audit/verify`, and an entry for
  every outcome, not just the successful ones

Two stores are shared on purpose. The draft store is written by `/api/message`
and `/api/clarify` and read by `/api/execute`, which is how the gateway ends up
executing a copy it stored itself. The verdict store is filled when the server
runs the validator over a draft, and read by `/api/execute` check 11.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Mapping, MutableMapping, Optional

from backend.audit import AuditLog
from backend.gateway.credential import CredentialService
from backend.bank import seed_bank
from backend.gateway.execute import ExecuteGateway
from backend.gateway.message import MessageService
from backend.ledger import InMemoryLedger
from backend.parser.directory import FixtureDirectory
from backend.policy import Limits, PolicyEngine
from backend.validator.factory import get_validator


_UNSET = object()
"""Sentinel so ``bank=None`` ("no money moves") differs from omitting it
("seed the default accounts")."""


class GatewayAPI:
    """The whole backend surface. One instance serves one account."""

    def __init__(
        self,
        *,
        parser: Callable[[str], Any],
        draft_store: Optional[MutableMapping[str, Any]] = None,
        credentials: Optional[MutableMapping[str, Any]] = None,
        # Any object with a ``validate(draft, transcript, clarification=...)``
        # method (backend/validator). Omit it and the gateway builds one via
        # ``get_validator()``: VALIDATOR_BACKEND=llm selects the real
        # validator, anything else gives one that freezes. There is no
        # placeholder and no callable fallback — nothing here can produce a
        # pass that was not actually checked.
        validator: Optional[Any] = None,
        policy: Optional[PolicyEngine] = None,
        limits: Optional[Limits] = None,
        audit: Optional[AuditLog] = None,
        ledger: Optional[Any] = None,
        clock: Optional[Callable[[], datetime]] = None,
        known_payees: Any = (),
        rp_id: str = "localhost",
        origin: str = "http://localhost:8000",
        bank: Any = _UNSET,
        directory: Optional[Any] = None,
        client: Optional[Any] = None,
    ) -> None:
        # Mock bank. Seeded by default so a fresh server has known balances.
        # Pass your own Bank to control balances, or bank=None to run with no
        # money moving at all. None and "not passed" mean different things,
        # hence the sentinel rather than a plain default.
        self.bank = seed_bank() if bank is _UNSET else bank
        self.store: MutableMapping[str, Any] = draft_store if draft_store is not None else {}
        self.credentials: MutableMapping[str, Any] = (
            credentials if credentials is not None else {}
        )
        self.verdicts: MutableMapping[str, str] = {}
        self.audit = audit if audit is not None else AuditLog()
        self.ledger = ledger if ledger is not None else InMemoryLedger()
        self.clock = clock
        self.known_payees = set(known_payees)
        self.policy = policy or PolicyEngine(limits or Limits(), bank=self.bank)
        # Built once, here. VALIDATOR_BACKEND=llm selects the real validator;
        # anything else gives UnconfiguredValidator, which returns
        # freeze/validator_not_configured and never pass. There is no default
        # that can produce pass without saying so.
        self.validator = validator if validator is not None else get_validator()
        # /api/clarify resolves payees and free-text answers through Member 1's
        # functions, which need a directory (and, for the question form, a chat
        # client). The fixture directory is the default for the same reason the
        # validator factory defaults to it: a server with no directory cannot
        # resolve anything, and says so loudly rather than guessing.
        self.directory = directory if directory is not None else FixtureDirectory()
        self.client = client

        self.messages = MessageService(
            parser=parser,
            store=self.store,
            validator=self.validator,
            policy=self.policy,
            audit=self.audit,
            clock=clock,
            ledger=self.ledger,
            known_payees=self.known_payees,
            verdicts=self.verdicts,
            directory=self.directory,
            client=self.client,
        )
        self.webauthn = CredentialService(
            store=self.store,
            credentials=self.credentials,
            rp_id=rp_id,
            audit=self.audit,
            clock=clock,
            policy=self._policy_call,
            verdicts=self.verdicts,
        )
        self.executor = ExecuteGateway(
            draft_store=self.store,
            credentials=self.credentials,
            verdicts=self.verdicts,
            policy=self._policy_call,
            ledger=self.ledger,
            clock=clock,
            rp_id=rp_id,
            origin=origin,
            bank=self.bank,
        )

    def _known(self) -> set:
        """Payees already paid (from the ledger) plus any pre-declared ones."""
        known = set(self.known_payees)
        for entry in self.ledger.entries:
            payee = (entry.get("draft") or {}).get("payee")
            if isinstance(payee, Mapping) and payee.get("id"):
                known.add(payee["id"])
        return known

    def _policy_call(self, draft: Mapping[str, Any], digest: Optional[str]) -> Any:
        return self.policy.evaluate(
            draft,
            digest,
            posted=self.ledger.entries,
            known_payees=self._known(),
            bank=self.bank,
        )

    # ── POST /api/message ─────────────────────────────────────────────

    def message(self, body: Mapping[str, Any]) -> dict:
        return self.messages.message(body)

    # ── POST /api/clarify ─────────────────────────────────────────────

    def clarify(self, body: Mapping[str, Any]) -> dict:
        return self.messages.clarify(body)

    # ── POST /api/webauthn/challenge ──────────────────────────────────

    def challenge(self, body: Mapping[str, Any]) -> dict:
        return self.webauthn.challenge(body)

    # ── POST /api/webauthn/register ───────────────────────────────────

    def register(self, body: Mapping[str, Any]) -> dict:
        return self.webauthn.register(body)

    # ── POST /api/execute ─────────────────────────────────────────────

    def execute(self, body: Mapping[str, Any]) -> dict:
        """Record the outcome either way.

        A rejected execution is worth auditing as much as a successful one —
        more, arguably: it is the record that someone tried.
        """
        result = self.executor.execute(body)
        self.audit.append("execute", self._execute_payload(body, result))
        return result

    @staticmethod
    def _execute_payload(body: Any, result: Mapping[str, Any]) -> dict:
        """Omit rather than send null: the canonical form forbids null, and an
        audit row that cannot be canonicalised cannot be hashed."""
        payload: dict = {}
        if isinstance(body, Mapping):
            for key in ("draft_id", "idempotency_key"):
                if body.get(key) is not None:
                    payload[key] = body[key]
        payload["result"] = result.get("result")
        if result.get("reason_code") is not None:
            payload["reason_code"] = result["reason_code"]
        return payload

    # ── GET /api/audit/verify ─────────────────────────────────────────

    def audit_verify(self) -> dict:
        return self.audit.verify_chain().to_response()
