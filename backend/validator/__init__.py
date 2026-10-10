"""
Validation Agent (Member 1, AI layer).

The validator is **read-only**: it compares the raw transcript to the
compiled draft (beneficiary, amount, asset, source account).  It has no
tools, no ledger access, and no write access (CONTEXT.md constraint 4,
CONTRACT.md §6).

**Design rule:** the model never sees the draft.  It independently
extracts what the user asked for from the transcript, and **code**
compares that extraction with the draft.  The model never edits,
repairs or suggests changes to a draft.
"""
