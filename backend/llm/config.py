"""
LLM configuration loader.

Reads environment variables via python-dotenv and returns a typed
settings object.  A missing variable raises an error naming the
variable, never a value.

CONTEXT.md constraint 4 requires the parser and validator to run on
different models.  The two AppKeys and two model labels must differ;
this module refuses to load if either pair is equal.

Note: the model labels (``PARSER_MODEL_LABEL`` and
``VALIDATOR_MODEL_LABEL``) are **declared, not verified**.  The actual
model is chosen in the ADP console for each application, so the code
cannot prove which model an app runs — the labels are human-readable
hints only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]


@dataclass(frozen=True)
class LlmSettings:
    """Loaded LLM settings.

    Attributes:
        parser_app_key: AppKey for the parser ADP application.
        validator_app_key: AppKey for the validator ADP application.
        parser_model_label: Human-readable label for the parser model.
        validator_model_label: Human-readable label for the validator model.
        adp_endpoint: The ADP Chat API endpoint URL.
        adp_timeout_seconds: Request timeout in seconds.
    """

    parser_app_key: str
    validator_app_key: str
    parser_model_label: str
    validator_model_label: str
    adp_endpoint: str
    adp_timeout_seconds: int


def _require(name: str) -> str:
    """Read a required environment variable.

    Raises a ``RuntimeError`` naming the variable if it is missing or
    empty.  The error message never contains the value.
    """
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_llm_settings(env_path: str | None = None) -> LlmSettings:
    """Load LLM settings from environment variables.

    Reads via python-dotenv **only** when ``env_path`` is passed
    explicitly.  Without ``env_path`` the caller's environment is used
    as-is — tests must never read the real ``.env`` file.

    Checks the six required variables: ``PARSER_APPKEY``,
    ``VALIDATOR_APPKEY``, ``PARSER_MODEL_LABEL``,
    ``VALIDATOR_MODEL_LABEL``, ``ADP_ENDPOINT`` (default: the
    documented endpoint), and ``ADP_TIMEOUT_SECONDS`` (default: 30).

    Raises:
        RuntimeError: If a required variable is missing, or if the two
            AppKeys are equal, or if the two model labels are equal.
    """
    if load_dotenv is not None and env_path is not None:
        load_dotenv(env_path)

    parser_app_key = _require("PARSER_APPKEY")
    validator_app_key = _require("VALIDATOR_APPKEY")
    parser_model_label = _require("PARSER_MODEL_LABEL")
    validator_model_label = _require("VALIDATOR_MODEL_LABEL")

    adp_endpoint = os.environ.get("ADP_ENDPOINT", "").strip()
    if not adp_endpoint:
        from backend.llm.adp_client import DEFAULT_ENDPOINT

        adp_endpoint = DEFAULT_ENDPOINT

    timeout_str = os.environ.get("ADP_TIMEOUT_SECONDS", "").strip()
    if not timeout_str:
        adp_timeout_seconds = 30
    else:
        adp_timeout_seconds = int(timeout_str)

    # ── Constraint 4: parser and validator must use different models ──
    if parser_app_key == validator_app_key:
        raise RuntimeError(
            "PARSER_APPKEY and VALIDATOR_APPKEY must differ "
            "(CONTEXT.md constraint 4: different models)"
        )
    if parser_model_label == validator_model_label:
        raise RuntimeError(
            "PARSER_MODEL_LABEL and VALIDATOR_MODEL_LABEL must differ "
            "(CONTEXT.md constraint 4: different models)"
        )

    return LlmSettings(
        parser_app_key=parser_app_key,
        validator_app_key=validator_app_key,
        parser_model_label=parser_model_label,
        validator_model_label=validator_model_label,
        adp_endpoint=adp_endpoint,
        adp_timeout_seconds=adp_timeout_seconds,
    )
