import uuid

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..llm import get_utility_model
from ..models import ChatSession, Message

_TITLE_SYSTEM = "You are a concise conversation-title generator."
_TITLE_USER = """Generate a short, specific title for this conversation, based on the exchange below.

USER: {user_message}
ASSISTANT: {assistant_message}

Rules:
- 3-6 words.
- No quotes, no trailing punctuation.
- Describe the topic, not generic phrases like "Chat with AI".

Return only the title."""

_title_chain = None


def _get_title_chain():
    global _title_chain
    if _title_chain is None:
        prompt = ChatPromptTemplate.from_messages([("system", _TITLE_SYSTEM), ("user", _TITLE_USER)])
        _title_chain = prompt | get_utility_model() | StrOutputParser()
    return _title_chain


async def maybe_generate_title(db: AsyncSession, session_id: uuid.UUID) -> None:
    """Runs once per session, right after the first turn — a cheap-model call replaces
    the frontend's truncated first-message placeholder with a real title. Never fires
    again after that: this only triggers when the session has exactly 2 messages."""
    result = await db.execute(
        select(Message).where(Message.session_id == session_id).order_by(Message.created_at.asc())
    )
    messages = list(result.scalars().all())
    if len(messages) != 2:
        return

    user_message, assistant_message = messages
    title = await _get_title_chain().ainvoke({
        "user_message": user_message.content,
        "assistant_message": assistant_message.content,
    })
    title = title.strip().strip('"')
    if not title:
        return

    session = await db.get(ChatSession, session_id)
    session.title = title
    await db.commit()
