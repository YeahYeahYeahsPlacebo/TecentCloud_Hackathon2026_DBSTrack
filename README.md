# Direct Conversational Transaction Agent (DCTA)

A Direct Conversational Transaction Agent built for the DBS track of the Tencent Cloud x AI Singapore hackathon. The agent lets a user describe a payment in natural language (voice or text), drafts a transaction, and then asks for an explicit cryptographic signature before anything is submitted to the ledger. All banking services — accounts, balances, transfers — are mocked. The AI's only job is to produce and refine a draft; it never executes, never touches the ledger, and never moves money on its own.

## Folder Ownership

| Folder | Owner |
|---|---|
| `backend/parser`, `backend/validator` | Member 1 (AI layer) |
| `frontend/` | Member 2 |
| `backend/gateway`, `backend/ledger`, `backend/audit`, `backend/policy` | Member 3 |

## How We Work

- Each member works on their own branch and opens pull requests to `main`.
- We merge to `main` at least once a day.
- Do not edit another member's folder without asking them first.
