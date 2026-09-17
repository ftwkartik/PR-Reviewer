"""A small FastAPI-flavoured repo used by retrieval tests and (later) the evaluation harness."""

SESSION_BASE = '''"""Session management."""
from app.auth.tokens import decode_token
from app.models.session import Session


class SessionManager:
    """Validates sessions."""

    def __init__(self, repo):
        self.repo = repo

    async def verify_token(self, raw):
        claims = decode_token(raw)
        session = await self.repo.get(claims["sid"])
        if session is None:
            return None
        return session
'''

SESSION_HEAD = SESSION_BASE.replace(
    "        if session is None:\n            return None\n        return session\n",
    "        if session is None:\n            return None\n        session.touch()\n        return session\n",
)

FILES: dict[str, str] = {
    "app/auth/session.py": SESSION_BASE,
    "app/auth/tokens.py": '''import jwt


def decode_token(raw):
    """Decode and verify a signed token."""
    return jwt.decode(raw, "k", algorithms=["HS256"])


def decode_token_legacy(raw):
    """Old decoder kept for migration."""
    return {"sid": raw}
''',
    "app/models/session.py": '''from datetime import datetime


class Session:
    """Stored session. Expired sessions remain until cleanup runs."""

    id: str
    expires_at: datetime

    def touch(self):
        self.expires_at = datetime.now()
''',
    "app/api/routes.py": '''from app.auth.session import SessionManager

manager = SessionManager(None)


async def current_user(token):
    session = await manager.verify_token(token)
    return session
''',
    "app/jobs/cleanup.py": '''def cleanup_expired_sessions(repo):
    """Delete sessions whose expires_at is in the past."""
    return repo.delete_where("expires_at < now()")
''',
    "app/billing/invoices.py": '''def verify_invoice_signature(payload):
    """Verify the signature of an invoice payload and validate the payment token."""
    return payload.get("sig") is not None
''',
    "tests/test_sessions.py": '''from app.auth.session import SessionManager


async def test_expired_session_rejected():
    manager = SessionManager(None)
    assert await manager.verify_token("expired") is None
''',
    "README.md": "# Demo\n\n## Conventions\nAll auth changes need a regression test.\n",
    "CONTRIBUTING.md": "# Contributing\n\nRun pytest before opening a PR.\n",
    "config/settings.yaml": "session_ttl: 3600\ntoken_algorithm: HS256\n",
}
