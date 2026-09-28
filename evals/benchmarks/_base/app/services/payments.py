from app.db import transaction


class InsufficientFunds(Exception):
    pass


def transfer(db, from_id: int, to_id: int, amount_cents: int) -> None:
    """Move money atomically: both updates commit together or not at all."""
    if amount_cents <= 0:
        raise ValueError("amount must be positive")
    with transaction(db):
        balance = db.query("SELECT balance FROM accounts WHERE id = ?", (from_id,))[0][0]
        if balance < amount_cents:
            raise InsufficientFunds(from_id)
        db.execute("UPDATE accounts SET balance = balance - ? WHERE id = ?", (amount_cents, from_id))
        db.execute("UPDATE accounts SET balance = balance + ? WHERE id = ?", (amount_cents, to_id))
