# Direct Conversational Transaction Agent (DCTA)

A Direct Conversational Transaction Agent built for the DBS track of the Tencent Cloud x AI Singapore hackathon. The agent lets a user describe a payment in natural language (voice or text), drafts a transaction, and then asks for an explicit cryptographic signature before anything is submitted to the ledger. All banking services — accounts, balances, transfers — are mocked. The AI's only job is to produce and refine a draft; it never executes, never touches the ledger, and never moves money on its own.

## Folder Ownership

| Folder | Owner |
|---|---|
| `backend/parser`, `backend/validator`, `backend/llm` | Member 1 (AI layer) |
| `frontend/` | Member 2 |
| `backend/gateway`, `backend/ledger`, `backend/audit`, `backend/policy`, `backend/bank` | Member 3 |

## Run it

```bash
pip install -r requirements.txt
python -m backend.gateway.server      # or: uvicorn backend.gateway.server:app --reload
```

Then open <http://127.0.0.1:8000/>. The frontend is served from the same
origin, because `frontend/app.js` calls `fetch("/api/message")` with a
relative path — a different origin fails on CORS before anything else.

`GET /api/health` reports what the server is actually running (parser,
validator, bank, draft count). Read it before demoing.

| Environment variable | Effect |
|---|---|
| `PARSER_BACKEND=llm` | Real LLM parser. Unset: the stub. |
| `VALIDATOR_BACKEND=llm` | Real validator. Unset: **freezes every draft** with `validator_not_configured`. |
| `DCTA_AUDIT_PATH=…` | Append-only audit file. Unset: in memory, lost on restart. |
| `DCTA_LEDGER_PATH=…` | Ledger file. Unset: in memory. |
| `DCTA_NO_BANK=1` | Run with no money moving at all. |
| `DCTA_SERVE_FRONTEND=0` | API only, no static files. |
| `DCTA_PORT`, `DCTA_HOST` | Default `8000` / `127.0.0.1`. |

The stub parser and the unconfigured validator are the defaults on purpose:
a fresh clone runs with no API keys. It will draft, and it will refuse to
sign — which is the safest thing a half-configured server can do.

## How We Work

- Each member works on their own branch and opens pull requests to `main`.
- We merge to `main` at least once a day.
- Do not edit another member's folder without asking them first.
