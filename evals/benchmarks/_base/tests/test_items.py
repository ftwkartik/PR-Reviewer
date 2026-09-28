from app.api.items import get_items
import app.api.items as items_module


class FakeDB:
    def query(self, sql, params=()):
        return [(1, 7, "widget", 500)]


def test_items_response_shape():
    items_module.DB = FakeDB()
    assert get_items() == [{"id": 1, "name": "widget", "owner_id": 7, "price_cents": 500}]
