import uuid

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..llm import get_utility_model
from ..models import Message
from .context import get_uncompressed_messages
from .locks import get_or_create_summary_row, lock_summary_row
from .tokens import estimate_tokens

_MERGE_SYSTEM = "You are a careful, concise conversation summarizer."
_MERGE_USER = """You maintain a running summary of a conversation.

CURRENT SUMMARY:
{summary}

NEW TURNS TO INCORPORATE:
{chunk}

Rewrite the summary to include the new turns. Rules:
- Preserve: names, numbers, dates, decisions made, user preferences, and any open questions or unfinished threads.
- Drop: pleasantries, resolved back-and-forth, redundant detail.
- Write in third person ("The user asked...", "The assistant suggested...").
- Maximum {budget} tokens. If over, compress oldest material hardest.

Return only the summary."""

_REGEN_SYSTEM = "You are a careful, concise conversation summarizer."
_REGEN_USER = """You are writing a summary of a conversation from scratch, given its full history so far.

FULL HISTORY:
{chunk}

Write a summary. Rules:
- Preserve: names, numbers, dates, decisions made, user preferences, and any open questions or unfinished threads.
- Drop: pleasantries, resolved back-and-forth, redundant detail.
- Write in third person.
- Maximum {budget} tokens.

Return only the summary."""

# Real LCEL chains (prompt | model | parser) rather than calling the model with a
# manually-formatted string — this is the actual "chain" piece of the LangChain migration,
# distinct from summarizer.py's own trigger/watermark/lock orchestration around it, which
# stays hand-rolled since no LangChain memory class supports "keep last N verbatim +
# concurrency-safe regen-every-N-versions" semantics. Built lazily (not at module import
# time) since get_utility_model() requires OPENAI_API_KEY to already be set — matching
# every other lazy-singleton in llm.py, so importing this module never requires a key.
_merge_chain = None
_regen_chain = None


def _get_merge_chain():
    global _merge_chain
    if _merge_chain is None:
        _merge_chain = (
            ChatPromptTemplate.from_messages([("system", _MERGE_SYSTEM), ("user", _MERGE_USER)])
            | get_utility_model()
            | StrOutputParser()
        )
    return _merge_chain


def _get_regen_chain():
    global _regen_chain
    if _regen_chain is None:
        _regen_chain = (
            ChatPromptTemplate.from_messages([("system", _REGEN_SYSTEM), ("user", _REGEN_USER)])
            | get_utility_model()
            | StrOutputParser()
        )
    return _regen_chain


def _format_messages(messages: list[Message]) -> str:
    return "\n".join(f"{m.role}: {m.content}" for m in messages)


async def get_all_compressed_messages(db: AsyncSession, session_id: uuid.UUID) -> list[Message]:
    result = await db.execute(
        select(Message)
        .where(Message.session_id == session_id, Message.compressed.is_(True))
        .order_by(Message.created_at.asc())
    )
    return list(result.scalars().all())


async def maybe_compress(db: AsyncSession, session_id: uuid.UUID) -> None:
    await get_or_create_summary_row(db, session_id)

    async with db.begin():
        summary_row = await lock_summary_row(db, session_id)
        if summary_row is None:
            return  # a concurrent compress job holds the lock; next turn retries

        uncompressed = await get_uncompressed_messages(db, session_id)
        total_tokens = sum(m.token_count for m in uncompressed)
        if total_tokens <= settings.compress_trigger * settings.recent_budget:
            return

        to_fold = uncompressed[: -settings.keep_verbatim] if settings.keep_verbatim else uncompressed
        if not to_fold:
            return
        chunk = to_fold[: settings.compress_chunk_size]

        next_version = summary_row.version + 1
        if next_version % settings.regen_every_n_versions == 0:
            already_compressed = await get_all_compressed_messages(db, session_id)
            new_content = await _get_regen_chain().ainvoke({
                "chunk": _format_messages(already_compressed + chunk),
                "budget": settings.summary_budget,
            })
        else:
            new_content = await _get_merge_chain().ainvoke({
                "summary": summary_row.content or "(none yet)",
                "chunk": _format_messages(chunk),
                "budget": settings.summary_budget,
            })

        summary_row.content = new_content
        summary_row.covers_until_message_id = chunk[-1].id
        summary_row.token_count = estimate_tokens(new_content)
        summary_row.version = next_version
        for message in chunk:
            message.compressed = True
    # transaction commits here, releasing the row lock atomically with the write
