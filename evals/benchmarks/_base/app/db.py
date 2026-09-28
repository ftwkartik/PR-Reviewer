from contextlib import contextmanager


class Database:
    """Tiny DB facade used by services."""

    def get(self, model, pk):
        raise NotImplementedError

    def query(self, sql, params=()):
        raise NotImplementedError

    def execute(self, sql, params=()):
        raise NotImplementedError

    def commit(self):
        raise NotImplementedError

    def rollback(self):
        raise NotImplementedError


@contextmanager
def transaction(db):
    """Commit on success, roll back on any error."""
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
