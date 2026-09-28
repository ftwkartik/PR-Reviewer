from fastapi import APIRouter, HTTPException

from app.services.items import items_with_owner_email, list_items

router = APIRouter()


@router.get("/items")
def get_items(limit: int = 20, offset: int = 0):
    items = list_items(DB, limit, offset)
    return [{"id": i.id, "name": i.name, "owner_id": i.owner_id, "price_cents": i.price_cents} for i in items]


@router.get("/items/with-owner")
def get_items_with_owner(limit: int = 20):
    return items_with_owner_email(DB, limit)


@router.get("/items/{item_id}")
def get_item(item_id: int):
    item = DB.get("Item", item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    return {"id": item.id, "name": item.name, "owner_id": item.owner_id}


DB = None
