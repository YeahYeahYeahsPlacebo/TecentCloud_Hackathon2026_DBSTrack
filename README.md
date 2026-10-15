# Direct Conversational Transaction Agent (DCTA)

A Direct Conversational Transaction Agent built for the DBS track of the Tencent Cloud x AI Singapore hackathon. The agent lets a user describe a payment in natural language (voice or text), drafts a transaction, and then asks for an explicit cryptographic signature before anything is submitted to the ledger. All banking services — accounts, balances, transfers — are mocked. The AI's only job is to produce and refine a draft; it never executes, never touches the ledger, and never moves money on its own.

## Folder Ownership

| Folder | Owner |
|---|---|
| `backend/parser`, `backend/validator` | Member 1 (AI layer) |
| `frontend/` | Member 2 |
| `backend/gateway`, `backend/ledger`, `backend/audit`, `backend/policy`, `backend/bank` | Member 3 |

## Architecture

[`docs/architecture.md`](docs/architecture.md) — component flow and trust
boundaries as Mermaid diagrams. Start there if you are new to the repo; the
one-sentence version is that the AI writes a draft and only a signature
writes to the ledger.

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
| `DCTA_NO_CLIENT=1` | No chat client. Free-text `/api/clarify` then reads answers by rule. |
| `DCTA_FAKE_CLIENT=1` | Scripted client, for local wiring only. |
| `DCTA_ANSWER_BACKEND` | `model` \| `rules` — how a free-text answer is read. Default: `model` when ADP keys are configured, `rules` otherwise. `model` without a client is a 500. |
| `DCTA_SERVE_FRONTEND=0` | API only, no static files. |
| `DCTA_PORT`, `DCTA_HOST` | Default `8000` / `127.0.0.1`. |
| `DCTA_RP_ID` | WebAuthn rpId. Default `localhost`. **Set this when deploying.** |
| `DCTA_ORIGIN` | Origin `clientDataJSON` must carry, port included. Default `http://localhost:8000`. |
| `DCTA_ALLOW_UNVALIDATED=true` | Dev only: lets an unconfigured validator return `pass`. The verdict says so. |

The stub parser and the unconfigured validator are the defaults on purpose:
a fresh clone runs with no API keys. It will draft, and it will refuse to
sign — which is the safest thing a half-configured server can do.

## Demo line

The one that uses the real models:

```bash
PARSER_BACKEND=llm VALIDATOR_BACKEND=llm python -m backend.gateway.server
```

Both flags are required. `PARSER_BACKEND=llm` alone drafts but freezes at
check 11 with `validator_frozen`; neither drafts from the stub transcript
set. Confirm what is actually loaded before you start:

```bash
curl -s localhost:8000/api/health
```

### With no keys

A fresh clone runs, drafts, clarifies, and executes:

```bash
DCTA_ALLOW_UNVALIDATED=true python -m backend.gateway.server
```

| | works? | why |
|---|---|---|
| One-shot draft → sign → execute | ✅ | stub parser, dev validator pass |
| Ambiguous payee → pick a candidate → execute | ✅ | candidate picks need the directory, not a model |
| Free-text clarification ("who is mum?", "how much?") | ✅ | read by rule — see below |

The free-text form has two ways to read an answer, and `GET /api/health`
says which one this server is using:

- `answer_backend: model` — `resolve_question` asks the model to pull one
  field out of the answer. Needs `PARSER_APPKEY`.
- `answer_backend: rules` — `resolve_answer_rules` reads it with the same
  code the stub parser already trusts (`spoken_amount`, `match_payees`),
  and **asks again** rather than guessing when the answer does not name a
  value. No model, no keys.

A server with no ADP keys picks `rules` by default, because the
alternative was a 500 on the one clarification form a demo needs. Set
`DCTA_ANSWER_BACKEND=model` to require the model and fail loud without
it.

```bash
python scripts/demo.py --transcript "Pay John two hundred dollars."
python scripts/demo.py --transcript "Send fifty dollars to mum."   # free-text: "John Lee"
```

`DCTA_ALLOW_UNVALIDATED=true` makes the validator return a pass whose reason
is literally `unvalidated (dev only)` — fine for a rehearsal, weak on stage.
For a real `pass` without keys you need `VALIDATOR_BACKEND=stub` (Member 1).

## `.env`

Copy the variables below into a `.env` at the repo root. They are read by
`backend/llm/config.py` (via python-dotenv) whenever `PARSER_BACKEND=llm` or
`VALIDATOR_BACKEND=llm` is set; a missing one raises an error naming the
variable, never its value.

| Variable | Required | Notes |
|---|---|---|
| `PARSER_APPKEY` | yes | AppKey of the parser ADP application |
| `VALIDATOR_APPKEY` | yes | AppKey of the validator ADP application |
| `PARSER_MODEL_LABEL` | yes | Human-readable label only — the model is chosen in the ADP console |
| `VALIDATOR_MODEL_LABEL` | yes | Must differ from `PARSER_MODEL_LABEL` |
| `ADP_ENDPOINT` | no | Defaults to `DEFAULT_ENDPOINT` in `backend/llm/adp_client.py` |
| `ADP_TIMEOUT_SECONDS` | no | Default `30` |

The two AppKeys must differ and the two model labels must differ — the
hackathon requires the parser and validator to run on different models, and
`load_llm_settings()` refuses to start if either pair is equal.

`.env` is gitignored. Nothing in the test suite reads it: `load_llm_settings()`
only touches the file when a caller passes `env_path` explicitly, and tests
never do.

## Tests

```bash
python -m pytest              # the whole suite
python -m pytest -q -rs       # quiet, with skip reasons
python -m pytest tests/test_policy.py -q
```

Everything is offline by default — no `.env`, no network, no ADP calls. The
suite passes on a fresh clone with no keys.

## Eval and red-team scripts

These are **not** run by pytest. They load real settings from `.env` and make
live ADP calls, so they need keys and they are slow.

```bash
python scripts/smoke_adp.py                 # first: does either app answer?
python scripts/eval_parser.py --delay 3     # transcripts -> draft accuracy table
python scripts/eval_validator.py --delay 3 --repeat 1
python scripts/redteam_ai_layer.py --delay 3 --out docs/redteam/ai_layer_report.md
```

`--delay` spaces out the ADP calls. The red-team run scores each case on
three axes (result, correctness, safety) and writes
[`docs/redteam/ai_layer_report.md`](docs/redteam/ai_layer_report.md). None of
them ever print an AppKey or a request body.

## Rehearsal

```bash
python scripts/demo.py                      # against a running server
python scripts/demo.py --transcript "Send fifty dollars to John Doe."
```

`scripts/demo.py` walks the whole story over HTTP and prints each step:
register → speak → clarify → challenge → sign → execute → audit. Then it
attacks its own transaction — signs a *different* draft with the same key and
replays it — and fails if the gateway accepts that. Exit 0 means the story
worked **and** the gate held. Run it before every demo; it is the fastest way
to find out that a refactor broke check 8.

## How We Work

- Each member works on their own branch and opens pull requests to `main`.
- We merge to `main` at least once a day.
- Do not edit another member's folder without asking them first.
