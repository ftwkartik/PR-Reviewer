# Demo shop

A small FastAPI service. Sessions are validated by `SessionManager`; money movement goes through
`app.services.payments.transfer`, which must stay atomic.
