import hashlib
import hmac

from app.core.logging import redact
from app.core.security import verify_github_signature


def _sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_valid_signature() -> None:
    assert verify_github_signature("s", b"{}", _sig("s", b"{}"))


def test_invalid_missing_and_malformed() -> None:
    assert not verify_github_signature("s", b"{}", _sig("other", b"{}"))
    assert not verify_github_signature("s", b"{}", None)
    assert not verify_github_signature("s", b"{}", "sha1=abc")
    assert not verify_github_signature("", b"{}", _sig("", b"{}"))


def test_redaction() -> None:
    assert "ghp_" not in redact("token ghp_abcdefghijklmnopqrstuvwxyz0123")
    assert "PRIVATE" not in redact(
        "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----"
    )
