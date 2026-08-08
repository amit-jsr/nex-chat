import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import ChatSession, Message
from ..schemas import MessageFeedbackUpdate, MessageOut, SessionCreate, SessionOut, SessionUpdate

router = APIRouter(prefix="/sessions", tags=["sessions"])


async def get_owned_session(
    session_id: uuid.UUID, user_id: str, db: AsyncSession
) -> ChatSession:
    """Scoped lookup: cross-user access 404s rather than leaking that the id exists."""
    result = await db.execute(
        select(ChatSession).where(ChatSession.id == session_id, ChatSession.user_id == user_id)
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.post("", response_model=SessionOut)
async def create_session(
    body: SessionCreate,
    user_id: str = Header(..., alias="X-User-Id"),
    db: AsyncSession = Depends(get_db),
):
    session = ChatSession(user_id=user_id, title=body.title)
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


@router.get("", response_model=list[SessionOut])
async def list_sessions(
    response: Response,
    user_id: str = Header(..., alias="X-User-Id"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ChatSession)
        .where(ChatSession.user_id == user_id)
        .order_by(ChatSession.updated_at.desc())
        .limit(limit + 1)
        .offset(offset)
    )
    rows = result.scalars().all()
    response.headers["X-Has-More"] = "true" if len(rows) > limit else "false"
    return rows[:limit]


@router.patch("/{session_id}", response_model=SessionOut)
async def rename_session(
    session_id: uuid.UUID,
    body: SessionUpdate,
    user_id: str = Header(..., alias="X-User-Id"),
    db: AsyncSession = Depends(get_db),
):
    session = await get_owned_session(session_id, user_id, db)
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="Title can't be empty")
    session.title = title
    await db.commit()
    await db.refresh(session)
    return session


@router.delete("/{session_id}", status_code=204)
async def delete_session(
    session_id: uuid.UUID,
    user_id: str = Header(..., alias="X-User-Id"),
    db: AsyncSession = Depends(get_db),
):
    """Messages and the summary cascade at the DB level (ON DELETE CASCADE); facts extracted
    from this session survive with source_session_id set to NULL (ON DELETE SET NULL) — same
    cascade behavior already verified via schema inspection, this is the first endpoint to
    actually exercise it. Doesn't special-case an in-flight stream on this session (rare
    window, and the background persist would just fail its insert harmlessly on the now-gone
    session_id) — not worth the added complexity for a first pass."""
    session = await get_owned_session(session_id, user_id, db)
    await db.delete(session)
    await db.commit()


@router.get("/{session_id}/messages", response_model=list[MessageOut])
async def get_messages(
    session_id: uuid.UUID,
    response: Response,
    user_id: str = Header(..., alias="X-User-Id"),
    limit: int = Query(50, ge=1, le=200),
    before: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Cursor pagination rather than offset: a chat thread always wants "the N messages
    right before X", and offset/limit shifts under you as new messages arrive. `before` is
    scoped to this session explicitly (not a bare `db.get` by id) so a cursor from another
    session can't be used to probe timestamps here. Returned oldest-first, like the
    unpaginated version, so the client can keep just prepending pages as-is.
    """
    await get_owned_session(session_id, user_id, db)

    query = select(Message).where(Message.session_id == session_id, Message.superseded_by.is_(None))
    if before is not None:
        cursor = await db.execute(
            select(Message.created_at).where(Message.id == before, Message.session_id == session_id)
        )
        cursor_created_at = cursor.scalar_one_or_none()
        if cursor_created_at is not None:
            query = query.where(Message.created_at < cursor_created_at)

    result = await db.execute(query.order_by(Message.created_at.desc()).limit(limit + 1))
    rows = result.scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    rows.reverse()
    response.headers["X-Has-More"] = "true" if has_more else "false"
    return rows


@router.patch("/{session_id}/messages/{message_id}/feedback", response_model=MessageOut)
async def set_message_feedback(
    session_id: uuid.UUID,
    message_id: uuid.UUID,
    body: MessageFeedbackUpdate,
    user_id: str = Header(..., alias="X-User-Id"),
    db: AsyncSession = Depends(get_db),
):
    """Feedback is a rating on the assistant's *reply*, so the frontend enforces role ==
    "assistant" — nothing here depends on that, but there's no product reason to rate your
    own messages. Idempotent: sending the currently-set value or null both just overwrite."""
    await get_owned_session(session_id, user_id, db)
    result = await db.execute(
        select(Message).where(Message.id == message_id, Message.session_id == session_id)
    )
    message = result.scalar_one_or_none()
    if message is None:
        raise HTTPException(status_code=404, detail="Message not found")
    message.feedback = body.feedback
    await db.commit()
    await db.refresh(message)
    return message
