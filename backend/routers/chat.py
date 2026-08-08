import asyncio
import time
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from ..config import settings
from ..db import async_session, get_db
from ..llm import stream_chat
from ..memory.context import build_context
from ..memory.tasks import fire_and_forget, run_post_response_maintenance
from ..memory.tokens import estimate_tokens
from ..models import Message
from ..schemas import ChatRequest
from .sessions import get_owned_session

router = APIRouter(prefix="/sessions", tags=["chat"])

# One in-flight stream per session at a time in practice (the UI disables sending while
# streaming) — keyed by session_id so POST .../chat/cancel can find the right stream to stop.
_cancel_events: dict[uuid.UUID, asyncio.Event] = {}

# Fixed-window rate limit, keyed by X-User-Id, on the two endpoints that call the main chat
# model. In-memory like `_cancel_events` above — no auth/Redis in this project, and this is
# single-process uvicorn, so a plain dict is sufficient (resets on restart, which is fine
# for a local/demo deployment; wouldn't survive multiple worker processes if that changes).
_rate_limit_windows: dict[str, tuple[float, int]] = {}  # user_id -> (window_start, count)


def _check_rate_limit(user_id: str) -> None:
    now = time.monotonic()
    window_start, count = _rate_limit_windows.get(user_id, (now, 0))
    if now - window_start >= settings.rate_limit_window_seconds:
        window_start, count = now, 0
    count += 1
    _rate_limit_windows[user_id] = (window_start, count)
    if count > settings.rate_limit_max_requests:
        retry_after = round(settings.rate_limit_window_seconds - (now - window_start))
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded — try again in {retry_after}s",
        )


@router.post("/{session_id}/chat/cancel", status_code=204)
async def cancel_chat(
    session_id: uuid.UUID,
    user_id: str = Header(..., alias="X-User-Id"),
    db: AsyncSession = Depends(get_db),
):
    await get_owned_session(session_id, user_id, db)
    event = _cancel_events.get(session_id)
    if event is not None:
        event.set()


@router.post("/{session_id}/chat")
async def chat(
    session_id: uuid.UUID,
    body: ChatRequest,
    user_id: str = Header(..., alias="X-User-Id"),
    db: AsyncSession = Depends(get_db),
):
    _check_rate_limit(user_id)
    session = await get_owned_session(session_id, user_id, db)

    # Registered before any I/O (context building calls OpenAI's embedding API and can take a
    # while) so a cancel sent early can't race ahead of this dict entry existing.
    cancel_event = asyncio.Event()
    _cancel_events[session_id] = cancel_event

    user_message = Message(
        session_id=session.id,
        role="user",
        content=body.message,
        token_count=estimate_tokens(body.message),
    )
    db.add(user_message)
    await db.commit()

    context = await build_context(db, session, pending_message=user_message)

    async def event_stream():
        full_text = ""
        failed = False
        try:
            if not cancel_event.is_set():
                async for delta in stream_chat(context.system_prompt, context.messages, cancel_event):
                    full_text += delta
                    yield f"data: {delta}\n\n"
        except Exception as exc:
            failed = True
            yield f"event: error\ndata: {exc}\n\n"
        finally:
            cancelled = cancel_event.is_set()
            if _cancel_events.get(session_id) is cancel_event:
                _cancel_events.pop(session_id, None)
            # Persist whatever was generated even if the client disconnected, the call failed
            # partway, or it was cancelled — the request-scoped `db` may already be torn down
            # by then, so use a fresh session rather than relying on it.
            if full_text:
                new_message_id = uuid.uuid4()
                async with async_session() as write_db:
                    write_db.add(
                        Message(
                            id=new_message_id,
                            session_id=session.id,
                            role="assistant",
                            content=full_text,
                            token_count=estimate_tokens(full_text),
                            truncated=cancelled,
                        )
                    )
                    await write_db.commit()
                fire_and_forget(run_post_response_maintenance(session.id, session.user_id))
                # Streamed deltas never otherwise reveal the persisted row's id — the frontend
                # needs it to attach like/dislike feedback to this exact message afterward.
                yield f"event: message_id\ndata: {new_message_id}\n\n"
            if cancelled:
                yield "event: cancelled\ndata: {}\n\n"
            elif not failed:
                yield "event: done\ndata: {}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/{session_id}/regenerate")
async def regenerate(
    session_id: uuid.UUID,
    user_id: str = Header(..., alias="X-User-Id"),
    db: AsyncSession = Depends(get_db),
):
    """Replaces the trailing assistant reply with a freshly generated one, rather than
    appending a new turn. The stale reply is kept (never hard-deleted, matching how the
    rest of this app treats message history) and linked via `superseded_by` — the same
    pattern `Fact.superseded_by` already uses for contradiction handling — so it drops out
    of both the prompt (`context.py`) and the UI thread (`GET .../messages`) without losing
    the row for debugging.
    """
    _check_rate_limit(user_id)
    session = await get_owned_session(session_id, user_id, db)

    result = await db.execute(
        select(Message)
        .where(Message.session_id == session_id, Message.superseded_by.is_(None))
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    last_message = result.scalar_one_or_none()
    if last_message is None or last_message.role != "assistant":
        raise HTTPException(status_code=400, detail="No assistant reply to regenerate")
    old_message_id = last_message.id

    # Same registration-before-any-I/O ordering as chat() — see the comment there.
    cancel_event = asyncio.Event()
    _cancel_events[session_id] = cancel_event

    context = await build_context(db, session, exclude_message_id=old_message_id)

    async def event_stream():
        full_text = ""
        failed = False
        try:
            if not cancel_event.is_set():
                async for delta in stream_chat(context.system_prompt, context.messages, cancel_event):
                    full_text += delta
                    yield f"data: {delta}\n\n"
        except Exception as exc:
            failed = True
            yield f"event: error\ndata: {exc}\n\n"
        finally:
            cancelled = cancel_event.is_set()
            if _cancel_events.get(session_id) is cancel_event:
                _cancel_events.pop(session_id, None)
            if full_text:
                new_message_id = uuid.uuid4()
                async with async_session() as write_db:
                    write_db.add(
                        Message(
                            id=new_message_id,
                            session_id=session.id,
                            role="assistant",
                            content=full_text,
                            token_count=estimate_tokens(full_text),
                            truncated=cancelled,
                        )
                    )
                    old = await write_db.get(Message, old_message_id)
                    old.superseded_by = new_message_id
                    await write_db.commit()
                fire_and_forget(run_post_response_maintenance(session.id, session.user_id))
                yield f"event: message_id\ndata: {new_message_id}\n\n"
            if cancelled:
                yield "event: cancelled\ndata: {}\n\n"
            elif not failed:
                yield "event: done\ndata: {}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
