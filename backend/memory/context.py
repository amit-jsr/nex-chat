import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import ChatSession, Message, Summary
from .facts import retrieve_facts
from .tokens import estimate_tokens

PERSONA = "You are NxtChat, a helpful, concise assistant."


@dataclass
class FactDebugInfo:
    content: str
    category: str | None
    score: float


@dataclass
class ContextDebugInfo:
    summary_present: bool
    summary_content: str | None
    summary_version: int | None
    summary_tokens: int
    facts: list[FactDebugInfo]
    facts_tokens: int
    recent_included: int
    recent_dropped: int
    recent_tokens: int
    oversized_new_message: bool
    oldest_uncompressed_message_age_seconds: float | None


@dataclass
class ContextResult:
    system_prompt: str
    messages: list[dict]
    debug: ContextDebugInfo


async def get_uncompressed_messages(db: AsyncSession, session_id: uuid.UUID) -> list[Message]:
    result = await db.execute(
        select(Message)
        .where(
            Message.session_id == session_id,
            Message.compressed.is_(False),
            Message.superseded_by.is_(None),
        )
        .order_by(Message.created_at.asc())
    )
    return list(result.scalars().all())


async def get_summary(db: AsyncSession, session_id: uuid.UUID) -> Summary | None:
    result = await db.execute(select(Summary).where(Summary.session_id == session_id))
    summary = result.scalar_one_or_none()
    if summary is None or not summary.content:
        return None
    return summary


def fit_to_budget(messages: list[Message], budget: int) -> tuple[list[Message], int, int]:
    """Keep as many of the newest messages as fit in `budget` tokens; drop oldest first.
    Returns (kept_in_chronological_order, tokens_used, dropped_count)."""
    kept: list[Message] = []
    tokens_used = 0
    for message in reversed(messages):
        if tokens_used + message.token_count > budget:
            break
        kept.append(message)
        tokens_used += message.token_count
    kept.reverse()
    dropped = len(messages) - len(kept)
    return kept, tokens_used, dropped


def render_system_prompt(persona: str, facts: list, summary) -> str:
    parts = [persona]
    if summary is not None:
        parts.append(f"Conversation summary so far: {summary.content}")
    if facts:
        fact_lines = "\n".join(f"- {fact.content}" for fact in facts)
        parts.append(f"Known about the user:\n{fact_lines}")
    return "\n\n".join(parts)


async def build_context(
    db: AsyncSession,
    session: ChatSession,
    pending_message: Message | None = None,
    exclude_message_id: uuid.UUID | None = None,
) -> ContextResult:
    """Assembles the prompt for a chat turn under a fixed token budget.

    `pending_message` (the just-persisted new user message) is never subject to the
    recent-history budget — only prior messages compete for RECENT_BUDGET. This is what
    keeps an oversized single message from ever blocking the response: it's always sent,
    nothing needs truncating, and only history has to shrink to make room.

    `exclude_message_id` drops one specific message from the prompt — used by regenerate
    to omit the stale reply being replaced (which is still `superseded_by IS NULL` at the
    point context is built, since it isn't marked until the new reply exists).
    """
    all_uncompressed = await get_uncompressed_messages(db, session.id)
    prior = [
        m
        for m in all_uncompressed
        if (pending_message is None or m.id != pending_message.id) and m.id != exclude_message_id
    ]

    summary = await get_summary(db, session.id)

    query_text = pending_message.content if pending_message else (prior[-1].content if prior else None)
    facts = await retrieve_facts(session.user_id, query_text)

    recent, recent_tokens, dropped = fit_to_budget(prior, settings.recent_budget)

    system_prompt = render_system_prompt(PERSONA, facts, summary)
    messages = [{"role": m.role, "content": m.content} for m in recent]
    if pending_message is not None:
        messages.append({"role": pending_message.role, "content": pending_message.content})

    oldest_age = None
    if all_uncompressed:
        oldest = all_uncompressed[0].created_at
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        oldest_age = (datetime.now(timezone.utc) - oldest).total_seconds()

    debug = ContextDebugInfo(
        summary_present=summary is not None,
        summary_content=summary.content if summary is not None else None,
        summary_version=summary.version if summary is not None else None,
        summary_tokens=summary.token_count if summary is not None else 0,
        facts=[FactDebugInfo(content=f.content, category=f.category, score=getattr(f, "score", 0.0)) for f in facts],
        facts_tokens=sum(estimate_tokens(f.content) for f in facts),
        recent_included=len(recent),
        recent_dropped=dropped,
        recent_tokens=recent_tokens,
        oversized_new_message=bool(pending_message and pending_message.token_count >= settings.recent_budget),
        oldest_uncompressed_message_age_seconds=oldest_age,
    )

    return ContextResult(system_prompt=system_prompt, messages=messages, debug=debug)
