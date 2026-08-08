import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Summary


async def get_or_create_summary_row(db: AsyncSession, session_id: uuid.UUID) -> None:
    """Idempotent: ensures a summaries row exists so there's something to lock.
    Committed in its own short transaction, decoupled from the locking transaction."""
    stmt = insert(Summary).values(session_id=session_id).on_conflict_do_nothing(
        index_elements=[Summary.session_id]
    )
    await db.execute(stmt)
    await db.commit()


async def lock_summary_row(db: AsyncSession, session_id: uuid.UUID) -> Summary | None:
    """SELECT ... FOR UPDATE SKIP LOCKED within the caller's open transaction.
    Returns None if a concurrent compress job currently holds the lock (the row is
    guaranteed to exist already via get_or_create_summary_row, so None here
    unambiguously means 'lock contended', not 'row missing')."""
    result = await db.execute(
        select(Summary).where(Summary.session_id == session_id).with_for_update(skip_locked=True)
    )
    return result.scalar_one_or_none()
