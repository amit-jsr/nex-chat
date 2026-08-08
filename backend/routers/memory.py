import uuid

from fastapi import APIRouter, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..memory.context import build_context
from ..schemas import BudgetSlot, MemoryDebugOut, MemoryFactOut
from .sessions import get_owned_session

router = APIRouter(prefix="/sessions", tags=["memory"])


@router.get("/{session_id}/memory", response_model=MemoryDebugOut)
async def get_memory(
    session_id: uuid.UUID,
    user_id: str = Header(..., alias="X-User-Id"),
    db: AsyncSession = Depends(get_db),
):
    session = await get_owned_session(session_id, user_id, db)
    result = await build_context(db, session, pending_message=None)
    debug = result.debug

    return MemoryDebugOut(
        summary=debug.summary_content,
        summary_version=debug.summary_version,
        uncompressed_message_count=debug.recent_included + debug.recent_dropped,
        dropped_message_count=debug.recent_dropped,
        oldest_uncompressed_message_age_seconds=debug.oldest_uncompressed_message_age_seconds,
        oversized_new_message=debug.oversized_new_message,
        budget={
            "system": BudgetSlot(used_tokens=0, limit_tokens=settings.system_prompt_budget),
            "facts": BudgetSlot(used_tokens=debug.facts_tokens, limit_tokens=settings.facts_budget),
            "summary": BudgetSlot(used_tokens=debug.summary_tokens, limit_tokens=settings.summary_budget),
            "recent": BudgetSlot(used_tokens=debug.recent_tokens, limit_tokens=settings.recent_budget),
        },
        injected_facts=[
            MemoryFactOut(content=f.content, category=f.category, score=f.score) for f in debug.facts
        ],
    )
