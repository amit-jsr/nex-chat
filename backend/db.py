from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings
from .models import Base

engine = create_async_engine(settings.database_url, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        # The `vector` type must exist before create_all's CREATE TABLE facts (... vector ...) runs.
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        # No migration tool (Alembic) yet — create_all only creates missing tables, not columns
        # added to existing ones, so new columns need an explicit idempotent ALTER here.
        await conn.execute(
            text("ALTER TABLE messages ADD COLUMN IF NOT EXISTS truncated BOOLEAN NOT NULL DEFAULT false")
        )
        await conn.execute(
            text(
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS superseded_by UUID "
                "REFERENCES messages(id) ON DELETE SET NULL"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS feedback TEXT "
                "CHECK (feedback IN ('like', 'dislike'))"
            )
        )


async def get_db() -> AsyncSession:
    async with async_session() as session:
        yield session
