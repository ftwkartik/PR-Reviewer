def estimate_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token). Budgeting needs consistency, not precision."""
    return max(1, len(text) // 4) if text else 0
