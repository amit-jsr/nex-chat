import asyncio
import logging
import uuid
from typing import Coroutine

from ..db import async_session
from .facts import maybe_extract_facts
from .summarizer import maybe_compress
from .titles import maybe_generate_title

logger = logging.getLogger(__name__)

# asyncio.create_task() does not itself keep the task alive — an unreferenced task can be
# garbage-collected mid-run. Holding a strong reference here (and dropping it on completion)
# is required, not optional, since nothing else in the request lifecycle references these tasks.
_background_tasks: set[asyncio.Task] = set()


def fire_and_forget(coro: Coroutine) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def run_post_response_maintenance(session_id: uuid.UUID, user_id: str) -> None:
    """Runs after the assistant response is already persisted — must never raise past
    itself, and one concern failing must never block the other."""
    try:
        async with async_session() as db:
            await maybe_compress(db, session_id)
    except Exception:
        logger.exception("summarization failed for session %s", session_id)

    try:
        async with async_session() as db:
            await maybe_extract_facts(db, session_id, user_id)
    except Exception:
        logger.exception("fact extraction failed for session %s", session_id)

    try:
        async with async_session() as db:
            await maybe_generate_title(db, session_id)
    except Exception:
        logger.exception("title generation failed for session %s", session_id)
