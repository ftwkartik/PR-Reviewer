from datetime import datetime, timedelta, timezone

from app.auth.session import SessionManager
from app.auth.tokens import issue_token
from app.models import Session


class FakeDB:
    def __init__(self, session):
        self.session = session

    def get(self, model, pk):
        return self.session


def test_expired_session_is_rejected():
    expired = Session("s1", 1, datetime.now(timezone.utc) - timedelta(seconds=1))
    manager = SessionManager(FakeDB(expired))
    assert manager.verify_token(issue_token("s1")) is None


def test_valid_session_is_returned():
    fresh = Session("s1", 1, datetime.now(timezone.utc) + timedelta(hours=1))
    assert SessionManager(FakeDB(fresh)).verify_token(issue_token("s1")) is fresh
