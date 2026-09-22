"""Startup schema statements run one worker at a time (2026-09-22: two uvicorn
workers booting against one managed Postgres deadlocked on the same DDL).
Every store's schema goes through db.apply_schema, which takes one advisory
lock in a transaction; the knowledge schema holds the same lock for its
try-and-fall-back sequence."""
import asyncio
import re
from pathlib import Path

from app import db
from app.knowledge import schema as kb_schema

ROOT = Path(__file__).resolve().parents[1]


class _Conn:
    def __init__(self, log):
        self.log = log
    async def execute(self, sql, *args):
        self.log.append(sql.strip().split("(")[0][:36] if "advisory" in sql else "DDL:" + sql.strip()[:20])
        return "OK"
    async def fetchval(self, sql, *args):
        return "vector"
    def transaction(self):
        log = self.log
        class _Tx:
            async def __aenter__(self):
                log.append("BEGIN")
            async def __aexit__(self, *a):
                log.append("COMMIT")
        return _Tx()


class _Pool:
    def __init__(self):
        self.log = []
    def acquire(self):
        conn = _Conn(self.log)
        class _Acq:
            async def __aenter__(self):
                return conn
            async def __aexit__(self, *a):
                return False
        return _Acq()


def test_apply_schema_takes_the_lock_in_a_transaction_before_the_ddl():
    pool = _Pool()
    asyncio.run(db.apply_schema(pool, "CREATE TABLE IF NOT EXISTS t (id INT)"))
    assert pool.log == ["BEGIN", "SELECT pg_advisory_xact_lock", "DDL:CREATE TABLE IF NOT ", "COMMIT"]


def test_the_knowledge_schema_holds_the_session_lock_around_its_sequence():
    pool = _Pool()
    assert asyncio.run(kb_schema.ensure_schema(pool, 8)) is True
    assert pool.log[0] == "SELECT pg_advisory_lock" and pool.log[-1] == "SELECT pg_advisory_unlock"
    assert all(entry.startswith("DDL:") for entry in pool.log[1:-1])


def test_every_startup_schema_site_goes_through_the_lock():
    """No store may run its CREATE statements on a bare connection again."""
    offenders = []
    for path in list((ROOT / "app").rglob("*.py")):
        text = path.read_text()
        if path.name == "db.py":
            continue
        if re.search(r"conn\.execute\((_TABLE_SQL|_PLANS_TABLE_SQL)\)", text):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
