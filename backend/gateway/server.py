"""
The HTTP layer: one FastAPI app in front of :class:`GatewayAPI`.

``backend/gateway/api.py`` deliberately knows nothing about HTTP, so the whole
gateway is testable without a server. This module is the only place that does
know, and it is thin on purpose — it translates, it does not decide:

- a dict with ``{"result": "rejected", ...}`` becomes **422** with that body
- anything else becomes **200** with that body
- anything that raises becomes **500**, carrying the same three fields as a
  rejection (CONTRACT.md §5): ``result``, ``reason_code``, ``message`` — with
  ``result`` ``"error"`` rather than ``"rejected"``, because a 500 means *we*
  could not answer, not that the request was judged. There is no reason code
  naming "our server is not wired up"; ``internal_error`` says it is ours and
  ``message`` says which one.

Run it::

    uvicorn backend.gateway.server:app --reload

or::

    python -m backend.gateway.server

The frontend is served from the same origin when ``DCTA_SERVE_FRONTEND`` is
not "0". frontend/app.js calls ``fetch("/api/message")`` with a relative path,
so it has to be same-origin or nothing works.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.audit import AuditLog
from backend.bank import seed_bank
from backend.gateway.api import GatewayAPI
from backend.gateway.errors import error_response
from backend.gateway.message import (
    ParserContractViolation,
    ServerNotConfigured,
    ValidatorUnusable,
)
from backend.ledger import FileLedger, InMemoryLedger
from backend.parser.directory import FixtureDirectory
from backend.parser.factory import get_parser
from backend.policy import Limits, PolicyEngine


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


# WebAuthn is bound to a host and an origin, both of which a deployment has
# to be able to name: the browser will not produce a credential for an rpId
# it is not on, and /api/execute compares the origin in clientDataJSON to
# this one literally. Localhost is the development default, not a setting.
DEFAULT_RP_ID = "localhost"
DEFAULT_ORIGIN = "http://localhost:8000"


def _env_value(name: str, default: str) -> str:
    """``name`` if it is set to something, else ``default``.

    Deliberately treats "set but empty" as unset: a stray ``DCTA_ORIGIN=``
    in a shell would otherwise make every signature fail with
    ``signature_invalid`` for a reason nobody can see.
    """
    return os.environ.get(name, "").strip() or default


def _rp_id() -> str:
    """The WebAuthn relying-party id the browser must be on."""
    return _env_value("DCTA_RP_ID", DEFAULT_RP_ID)


def _origin() -> str:
    """The browser origin ``clientDataJSON`` must carry (scheme, host, port)."""
    return _env_value("DCTA_ORIGIN", DEFAULT_ORIGIN)


def _chat_client() -> Optional[Any]:
    """The chat client the question form of ``/api/clarify`` needs.

    ``resolve_question`` calls a model to pull one field out of a free-text
    answer, so the free-text form cannot work without a client. Passing none
    is what made it a 500; this is what the 500 was asking for.

    Keyed off the parser's ADP application: the answer-extraction prompt is
    parser-side work, not validator work. With no ADP keys configured there is
    no client to pass, and :func:`_answer_resolver` then switches the endpoint
    to the code-only rules rather than inventing an answer —
    ``DCTA_ANSWER_BACKEND=model`` opts back into requiring the model, and
    ``DCTA_FAKE_CLIENT=1`` opts into the scripted fake for local wiring
    checks. Both say what they are in ``/api/health``.
    """
    if _flag("DCTA_NO_CLIENT"):
        return None

    if _flag("DCTA_FAKE_CLIENT"):
        # Explicit opt-in, before any key is looked at: "I have no model, give
        # me the scripted one so I can exercise the wiring".
        from backend.llm.fake_client import FakeChatClient

        return FakeChatClient()

    try:
        from backend.llm.adp_client import AdpChatClient
        from backend.llm.config import load_llm_settings

        settings = load_llm_settings(env_path=str(_project_root() / ".env"))
    except RuntimeError:
        # Missing ADP_* / PARSER_* / VALIDATOR_* variable. No keys, no model,
        # no answer: fail closed instead of guessing.
        return None

    return AdpChatClient(
        app_key=settings.parser_app_key,
        endpoint=settings.adp_endpoint,
        timeout_seconds=settings.adp_timeout_seconds,
    )


def _answer_resolver(client: Optional[Any]) -> Optional[Any]:
    """How the free-text form of ``/api/clarify`` should read an answer.

    Returns ``None`` for "with the model" (which needs *client*, and is a
    loud 500 without one) or
    :func:`backend.parser.clarify.resolve_answer_rules` for "with code".

    The default follows the client: a server with ADP keys answers through
    the model, and one without answers by the same rules the stub parser
    already uses — reading only what the user said, and asking again
    rather than guessing. That is what closes the gap where the free-text
    form could only return a 500.

    ``DCTA_ANSWER_BACKEND`` overrides it either way:
    ``model`` to require the model (and fail loud without one), ``rules``
    to force the code path even when keys are configured.
    """
    from backend.parser.clarify import resolve_answer_rules

    choice = os.environ.get("DCTA_ANSWER_BACKEND", "").strip().lower()

    if choice == "model":
        return None
    if choice == "rules":
        return resolve_answer_rules
    if choice:
        raise RuntimeError(
            f"DCTA_ANSWER_BACKEND={choice!r} is not valid; use 'model' or 'rules'"
        )
    return None if client is not None else resolve_answer_rules


def build_api() -> GatewayAPI:
    """Wire the real pieces together. The only place that does so.

    Every dependency is chosen here and nowhere else, so there is exactly one
    answer to "what is this server actually running?" — and it can be read
    from this function instead of from a pile of defaults.

    Environment:
        PARSER_BACKEND=llm        real LLM parser, else the stub
        VALIDATOR_BACKEND=llm     real validator, else freeze
        DCTA_AUDIT_PATH           append-only audit file (default: memory)
        DCTA_LEDGER_PATH          ledger file (default: memory)
        DCTA_NO_BANK=1            run with no money moving at all
        DCTA_NO_CLIENT=1          no chat client (free-text clarify reads
                                  answers with code, or 500s if
                                  DCTA_ANSWER_BACKEND=model)
        DCTA_FAKE_CLIENT=1        scripted client, for local wiring only
        DCTA_ANSWER_BACKEND       model | rules (default: model when ADP
                                  keys are configured, else rules)
        DCTA_RP_ID                WebAuthn rpId (default: localhost)
        DCTA_ORIGIN               origin clientDataJSON must carry
                                  (default: http://localhost:8000)
    """
    audit_path = os.environ.get("DCTA_AUDIT_PATH", "").strip()
    ledger_path = os.environ.get("DCTA_LEDGER_PATH", "").strip()

    bank = None if _flag("DCTA_NO_BANK") else seed_bank()
    client = _chat_client()

    return GatewayAPI(
        parser=get_parser(),
        directory=FixtureDirectory(),
        client=client,
        answer_resolver=_answer_resolver(client),
        audit=AuditLog(path=audit_path or None),
        ledger=FileLedger(ledger_path) if ledger_path else InMemoryLedger(),
        bank=bank,
        rp_id=_rp_id(),
        origin=_origin(),
    )


def create_app(api: Optional[GatewayAPI] = None, *, serve_frontend: bool = True) -> FastAPI:
    """Build the FastAPI app around *api* (or a freshly wired one)."""
    app = FastAPI(
        title="DCTA gateway",
        version="0.1.0",
        description="Direct Conversational Transaction Agent — draft, verify, sign, execute.",
    )
    gateway = api if api is not None else build_api()
    app.state.gateway = gateway

    def call(method: str, body: Any) -> Any:
        return getattr(gateway, method)(body)

    # ── Routes ────────────────────────────────────────────────────────

    @app.post("/api/message")
    async def message(request: Request) -> Any:
        return _respond(call("message", await _json(request)))

    @app.post("/api/clarify")
    async def clarify(request: Request) -> Any:
        return _respond(call("clarify", await _json(request)))

    @app.post("/api/webauthn/register")
    async def register(request: Request) -> Any:
        return _respond(call("register", await _json(request)))

    @app.post("/api/webauthn/challenge")
    async def challenge(request: Request) -> Any:
        return _respond(call("challenge", await _json(request)))

    @app.post("/api/execute")
    async def execute(request: Request) -> Any:
        return _respond(call("execute", await _json(request)))

    @app.get("/api/audit/verify")
    async def audit_verify() -> Any:
        return _respond(gateway.audit_verify())

    @app.get("/api/health")
    async def health() -> dict:
        """What this server is actually running. Read it before demoing."""
        return {
            "ok": True,
            "parser": type(gateway.messages.parser).__name__
            if getattr(gateway.messages, "parser", None) is not None
            else None,
            "validator": type(gateway.validator).__name__,
            "client": type(gateway.client).__name__ if gateway.client is not None else None,
            # "model" or "rules": which of the two reads a free-text
            # clarification answer on this server. "rules" means no model
            # is involved, and is what a server with no ADP keys reports.
            "answer_backend": "rules"
            if gateway.answer_resolver is not None
            else "model",
            "bank": "none" if gateway.bank is None else f"{len(gateway.bank.accounts())} accounts",
            "drafts": len(gateway.store),
            "credentials": len(gateway.credentials),
        }

    # ── Errors that are ours, not the user's ──────────────────────────

    @app.exception_handler(ParserContractViolation)
    async def _contract_violation(request: Request, exc: ParserContractViolation) -> JSONResponse:
        return JSONResponse(status_code=500, content=error_response(str(exc), bug="parser"))

    @app.exception_handler(ServerNotConfigured)
    async def _not_configured(request: Request, exc: ServerNotConfigured) -> JSONResponse:
        return JSONResponse(status_code=500, content=error_response(str(exc), bug="wiring"))

    @app.exception_handler(ValidatorUnusable)
    async def _validator_unusable(request: Request, exc: ValidatorUnusable) -> JSONResponse:
        return JSONResponse(status_code=500, content=error_response(str(exc), bug="validator"))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        """Anything we did not anticipate is still our error, so it still
        carries the contract's three fields rather than a bare traceback.

        The type is in ``message`` — hiding it would make the one case nobody
        predicted the hardest one to debug."""
        return JSONResponse(
            status_code=500,
            content=error_response(f"{type(exc).__name__}: {exc}", bug="unhandled"),
        )

    # ── Frontend, same origin, last so it cannot shadow the API ───────

    if serve_frontend:
        frontend = _project_root() / "frontend"
        if frontend.is_dir():
            app.mount("/", StaticFiles(directory=str(frontend), html=True), name="frontend")

    return app


async def _json(request: Request) -> Any:
    """Empty or non-JSON bodies still reach the gateway.

    A body of ``null`` is what lets step 1 produce ``malformed_request``
    instead of a 500 from the JSON parser: the gateway owns that decision, so
    the HTTP layer must not make it first.
    """
    try:
        return await request.json()
    except Exception:
        return None


def _respond(result: Any) -> Any:
    """Rejection -> 422, anything else -> 200, body unchanged either way.

    CONTRACT.md §5: every rejection is a 422 carrying ``result``,
    ``reason_code`` and ``message``. The gateway already produced that dict;
    this only picks the status code, so the payload is never reshaped here.
    """
    if isinstance(result, Mapping) and result.get("result") == "rejected":
        return JSONResponse(status_code=422, content=dict(result))
    return result


app = create_app(serve_frontend=_flag("DCTA_SERVE_FRONTEND", default=True))


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "backend.gateway.server:app",
        host=os.environ.get("DCTA_HOST", "127.0.0.1"),
        port=int(os.environ.get("DCTA_PORT", "8000")),
        reload=_flag("DCTA_RELOAD"),
    )
