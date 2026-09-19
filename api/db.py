"""Database access for the API.

A small pool and a cursor context manager. Reads roll back rather than
commit, so a GET that accidentally writes leaves nothing behind.
"""
from __future__ import annotations
import os
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

DSN = os.environ.get('CCG_DSN', 'host=/tmp port=5433 user=ccg dbname=ccg')
_pool: ThreadedConnectionPool | None = None


def pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = ThreadedConnectionPool(1, 8, DSN)
    return _pool


@contextmanager
def cursor(commit: bool = False):
    cx = pool().getconn()
    try:
        with cx.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        cx.commit() if commit else cx.rollback()
    except Exception:
        cx.rollback()
        raise
    finally:
        pool().putconn(cx)


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def one(cur) -> dict | None:
    r = cur.fetchone()
    return dict(r) if r else None
