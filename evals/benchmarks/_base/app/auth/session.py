from datetime import datetime, timezone

from app.auth.tokens import decode_token
from app.models import Session


class SessionManager:
    """Validates sessions presented by clients."""

    def __init__(self, db):
        self.db = db

    def verify_token(self, raw: str) -> Session | None:
        claims = decode_token(raw)
        session = self.db.get(Session, claims["sid"])
        if session is None:
            return None
        if session.is_expired(datetime.now(timezone.utc)):
            return None
        return session
