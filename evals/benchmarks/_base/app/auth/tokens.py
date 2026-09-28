import jwt

from app.settings import TOKEN_ALGORITHM

SECRET = "change-me"


def decode_token(raw: str) -> dict:
    """Decode and verify a signed token. Raises jwt.InvalidTokenError when invalid."""
    return jwt.decode(raw, SECRET, algorithms=[TOKEN_ALGORITHM])


def issue_token(session_id: str) -> str:
    return jwt.encode({"sid": session_id}, SECRET, algorithm=TOKEN_ALGORITHM)
