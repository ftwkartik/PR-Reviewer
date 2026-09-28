def cleanup_expired_sessions(db) -> int:
    """Delete sessions whose expires_at is in the past. Runs every 10 minutes."""
    cur = db.execute("DELETE FROM sessions WHERE expires_at < now()")
    return cur.rowcount
