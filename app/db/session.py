"""
app/db/session.py

Phase 1 upgrade:
  - Migrated from sync SQLAlchemy (psycopg2) to async SQLAlchemy (asyncpg)
  - Explicit pool configuration (pool_size, max_overflow)
  - get_db() in deps.py now yields AsyncSession instead of Session

Why async SQLAlchemy?
  The sync engine blocks a thread for every DB operation. Under load, you exhaust
  the thread pool and requests queue up. The async engine uses Python's event loop
  instead — one thread handles thousands of concurrent awaiting DB calls.
  asyncpg (already in requirements.txt) is the async Postgres driver that makes
  this possible. We just never wired it up until now.

Connection pool explained:
  pool_size=10        → 10 persistent connections kept alive (ready to use instantly)
  max_overflow=20     → up to 20 extra connections allowed under peak load
  pool_timeout=30     → if all connections are busy, wait max 30s before raising error
  pool_recycle=1800   → recycle connections every 30 min (prevents stale connections
                        from being killed by Postgres/Neon timeout)
  pool_pre_ping=True  → before using a connection, send SELECT 1 to verify it's alive
"""
import os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.core.config import settings

# ── Build the async DB URL ─────────────────────────────────────────────────────
# asyncpg requires "postgresql+asyncpg://" prefix instead of "postgresql://"
# We handle both the DATABASE_URL env var (Railway/Neon) and individual fields.

DB_URL = os.getenv("DATABASE_URL", settings.db_url)

# Fix legacy "postgres://" prefix (Heroku/old Render style)
if DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)

# Swap sync driver prefix for async driver
# "postgresql://" → "postgresql+asyncpg://"
if DB_URL.startswith("postgresql://") and "+asyncpg" not in DB_URL:
    DB_URL = DB_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

# ── Async engine ───────────────────────────────────────────────────────────────
engine = create_async_engine(
    DB_URL,
    echo=False,                # Set True temporarily to see SQL in logs (dev only)
    pool_size=10,              # Persistent connections kept alive
    max_overflow=20,           # Extra connections allowed under peak load
    pool_timeout=30,           # Seconds to wait if pool is full before raising error
    pool_recycle=1800,         # Recycle connections every 30 min (prevents stale conns)
    pool_pre_ping=True,        # Verify connection alive before using it
)

# ── Async session factory ──────────────────────────────────────────────────────
# expire_on_commit=False is important for async:
# After commit(), SQLAlchemy normally expires all attributes so they're re-fetched
# on next access. In async, that re-fetch would require another await — and if
# you're outside the session context, it silently fails. expire_on_commit=False
# keeps the values you already fetched available without needing another query.
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)