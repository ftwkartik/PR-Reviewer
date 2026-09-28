from dataclasses import dataclass
from datetime import datetime


@dataclass
class User:
    id: int
    email: str


@dataclass
class Session:
    """A login session. Expired sessions stay stored until the cleanup job removes them."""

    id: str
    user_id: int
    expires_at: datetime

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at <= now


@dataclass
class Item:
    id: int
    owner_id: int
    name: str
    price_cents: int
