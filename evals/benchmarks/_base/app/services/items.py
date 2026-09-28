from app.models import Item, User
from app.settings import MAX_PAGE_SIZE


def list_items(db, limit: int = 20, offset: int = 0) -> list[Item]:
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    rows = db.query(
        "SELECT id, owner_id, name, price_cents FROM items LIMIT ? OFFSET ?", (limit, offset)
    )
    return [Item(*r) for r in rows]


def items_with_owner_email(db, limit: int = 20) -> list[dict]:
    """Single joined query so the listing costs one round trip."""
    rows = db.query(
        "SELECT i.id, i.name, u.email FROM items i JOIN users u ON u.id = i.owner_id LIMIT ?",
        (limit,),
    )
    return [{"id": r[0], "name": r[1], "owner_email": r[2]} for r in rows]


def get_owner(db, item: Item) -> User:
    return db.get(User, item.owner_id)
