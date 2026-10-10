"""
Validator factory.

Returns :class:`LlmValidator` when ``VALIDATOR_BACKEND=llm``.  Otherwise
returns :class:`UnconfiguredValidator`, which always returns freeze with
reason ``"validator_not_configured"``, unless ``DCTA_ALLOW_UNVALIDATED``
is set to ``"true"`` (case-insensitive), in which case it returns pass
with reason ``"unvalidated (dev only)"``.

It must never pass silently by default.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from backend.canonical import draft_hash
from backend.llm.config import load_llm_settings
from backend.llm.interface import ChatClient
from backend.models.draft import TransactionDraft
from backend.parser.directory import PayeeDirectory, FixtureDirectory
from backend.parser.interface import ClarificationRecord
from backend.validator.interface import ValidatorVerdict


class UnconfiguredValidator:
    """A validator that always freezes (or passes in dev mode).

    Used when ``VALIDATOR_BACKEND`` is not set or not ``"llm"``.  It
    never passes silently by default — it returns a freeze verdict with
    reason ``"validator_not_configured"``.  When the environment
    variable ``DCTA_ALLOW_UNVALIDATED`` is set to ``"true"``
    (case-insensitive), it returns pass with reason ``"unvalidated
    (dev only)"``.
    """

    def validate(
        self,
        draft: TransactionDraft,
        transcript: str,
        clarification: Optional[ClarificationRecord] = None,
    ) -> ValidatorVerdict:
        h = draft_hash(draft.model_dump(mode="json", exclude_none=True))

        allow_unvalidated = os.environ.get(
            "DCTA_ALLOW_UNVALIDATED", ""
        ).strip().lower() == "true"

        if allow_unvalidated:
            return ValidatorVerdict(
                verdict="pass",
                discrepancies=[],
                reason="unvalidated (dev only)",
                draft_hash=h,
            )

        return ValidatorVerdict(
            verdict="freeze",
            discrepancies=[],
            reason="validator_not_configured",
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


def get_validator(
    *,
    client: Optional[ChatClient] = None,
    directory: Optional[PayeeDirectory] = None,
    env_path: Optional[str] = None,
) -> Any:
    """Return a validator based on the ``VALIDATOR_BACKEND`` env var.

    When ``VALIDATOR_BACKEND=llm``, returns an :class:`LlmValidator`
    configured with the validator ADP application settings.  The ADP
    client uses the ``VALIDATOR_APPKEY`` (never the parser's), the
    shared endpoint, and the shared timeout.

    Otherwise returns :class:`UnconfiguredValidator`.

    Args:
        client: Optional pre-built ChatClient (for testing).  When
            provided, the factory does not load settings or build an
            ADP client.
        directory: Optional PayeeDirectory.  Defaults to
            :class:`FixtureDirectory`.
        env_path: Optional path to a .env file for
            :func:`load_llm_settings`.
    """
    backend = os.environ.get("VALIDATOR_BACKEND", "").strip().lower()

    if backend != "llm":
        return UnconfiguredValidator()

    # Lazy import to avoid importing the LLM client stack when the
    # validator is not configured.
    from backend.validator.llm_validator import LlmValidator

    if client is not None:
        return LlmValidator(
            client=client,
            directory=directory or FixtureDirectory(),
        )

    # Load settings and build the ADP client for the validator app.
    settings = load_llm_settings(env_path=env_path)

    from backend.llm.adp_client import AdpChatClient

    adp_client = AdpChatClient(
        app_key=settings.validator_app_key,
        endpoint=settings.adp_endpoint,
        timeout_seconds=settings.adp_timeout_seconds,
    )

    return LlmValidator(
        client=adp_client,
        directory=directory or FixtureDirectory(),
    )
