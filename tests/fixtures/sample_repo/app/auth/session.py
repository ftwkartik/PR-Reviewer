"""Session management."""

from datetime import UTC, datetime

from ..models.session import Session
from . import tokens
from .tokens import decode_token

SESSION_TTL = 3600


class SessionManager(BaseManager):
    """Validates and refreshes sessions."""

    ttl = SESSION_TTL

    def __init__(self, repo):
        self.repo = repo

    @staticmethod
    def now():
        return datetime.now(UTC)

    async def verify_token(self, raw: str) -> Session | None:
        claims = decode_token(raw)
        session = await self.repo.get(claims["sid"])
        if session is None:
            return None
        return session


@router.get("/me")
async def current_user(token: str):
    """Return the current user."""
    return await tokens.lookup(token)


if __name__ == "__main__":
    print("demo")
