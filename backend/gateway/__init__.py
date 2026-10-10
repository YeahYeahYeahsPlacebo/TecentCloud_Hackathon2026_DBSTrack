"""Gateway: the only place money moves.

- :mod:`backend.gateway.api` — every endpoint, wired together
- :mod:`backend.gateway.execute` — `/api/execute` and its twelve checks
- :mod:`backend.gateway.message` — `/api/message`, `/api/clarify`
- :mod:`backend.gateway.credential` — `/api/webauthn/challenge`, `/api/webauthn/register`
- :mod:`backend.gateway.webauthn` — ES256 verification
- :mod:`backend.gateway.reason_codes` — every rejection code
"""

from backend.gateway.api import GatewayAPI
from backend.gateway.execute import CHECKS, ExecuteGateway
from backend.gateway.reason_codes import ALL_REASON_CODES, EXECUTE_REASON_CODES

__all__ = [
    "ALL_REASON_CODES",
    "CHECKS",
    "EXECUTE_REASON_CODES",
    "ExecuteGateway",
    "GatewayAPI",
]
