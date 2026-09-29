# ADR 4: The model proposes, the application disposes

**Problem.** LLMs invent files, line numbers and code, and repository text can carry prompt injections.
**Choice.** Findings are untrusted input: path must be in the diff; lines are snapped to one hunk; the
evidence quote must exist verbatim in what the model was shown; replacements must parse and touch only added
lines; text is sanitised; low confidence is withheld; duplicates are merged; publication is pinned to the
reviewed head SHA. The model has no write or network tools, and output uses a JSON schema.
**Tradeoff.** Some valid-but-poorly-quoted findings are rejected; they are persisted with a reason so the
threshold and prompts can be tuned from evidence.
