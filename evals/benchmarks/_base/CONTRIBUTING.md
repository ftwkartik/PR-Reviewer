# Contributing

- Every change to authentication or payments needs a regression test.
- Database writes that touch more than one row must run inside `app.db.transaction`.
- Request handlers must not block the event loop; use async clients or run blocking work in a thread.
- Public response fields are part of the API contract: do not rename or remove them without a migration plan.
