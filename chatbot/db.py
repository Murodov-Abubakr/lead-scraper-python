#!/usr/bin/env python3
"""
Database connection helper — uses pg8000 (pure Python, no native deps).
Falls back gracefully when DATABASE_URL is not set (local dev).
"""
import os, ssl
from contextlib import contextmanager
from urllib.parse import urlparse

DATABASE_URL: str | None = os.getenv("DATABASE_URL")


def _parse_url(url) -> dict:
    if isinstance(url, (bytes, bytearray)):
        url = url.decode("utf-8")
    url = url.strip()
    p = urlparse(url)
    return {
        "host":     p.hostname,
        "port":     p.port or 5432,
        "database": p.path.lstrip("/"),
        "user":     p.username,
        "password": p.password,
    }


@contextmanager
def get_db():
    import pg8000.dbapi as pg
    url = os.getenv("DATABASE_URL") or DATABASE_URL
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode    = ssl.CERT_NONE
    conn = pg.connect(**_parse_url(url), ssl_context=ssl_ctx)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
