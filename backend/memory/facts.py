import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from langchain_core.prompts import ChatPromptTemplate
from langchain_postgres import PGVector
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..llm import get_embeddings, get_utility_model
from ..models import Message
from .guardrails import sensitive_fact_confidence_floor

EXTRACTION_SYSTEM_PROMPT = "You extract durable facts about a user from a conversation."

EXTRACTION_PROMPT = """Extract durable facts about the user from these messages.
Durable = still true weeks from now (preferences, background, projects, constraints).
NOT transient state (e.g. "user is debugging X today").

MESSAGES:
{chunk}

Return no facts if nothing qualifies."""

CONTRADICTION_SYSTEM_PROMPT = "You compare two stated facts about the same user and classify their relationship."

CONTRADICTION_PROMPT = """EXISTING FACT: {existing}
NEW FACT: {new}

Are these the same fact restated, a contradiction (the new one supersedes the old), or genuinely distinct facts?"""


class ExtractedFact(BaseModel):
    content: str
    category: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)


class ExtractedFacts(BaseModel):
    facts: list[ExtractedFact]


class ContradictionVerdict(BaseModel):
    verdict: Literal["same", "contradiction", "distinct"]


# Structured-output chains (prompt | model.with_structured_output(schema)) replace the old
# manual json.loads()-of-a-hand-formatted-JSON-instruction approach — the schema is enforced
# by the model call itself rather than hoped for via prompt wording and parsed defensively
# after the fact. Built lazily, same reasoning as summarizer.py's chains.
_extraction_chain = None
_contradiction_chain = None


def _get_extraction_chain():
    global _extraction_chain
    if _extraction_chain is None:
        prompt = ChatPromptTemplate.from_messages([("system", EXTRACTION_SYSTEM_PROMPT), ("user", EXTRACTION_PROMPT)])
        _extraction_chain = prompt | get_utility_model().with_structured_output(ExtractedFacts)
    return _extraction_chain


def _get_contradiction_chain():
    global _contradiction_chain
    if _contradiction_chain is None:
        prompt = ChatPromptTemplate.from_messages([("system", CONTRADICTION_SYSTEM_PROMPT), ("user", CONTRADICTION_PROMPT)])
        _contradiction_chain = prompt | get_utility_model().with_structured_output(ContradictionVerdict)
    return _contradiction_chain


_COLLECTION_NAME = "nexchat_facts"
_vectorstore: PGVector | None = None


def _get_vectorstore() -> PGVector:
    """Fact storage/retrieval now goes through LangChain's PGVector vectorstore instead of
    a hand-rolled `facts` table + raw cosine-distance SQL. Uses its own psycopg3-based
    engine (langchain-postgres's dependency, not this project's asyncpg one) — a separate
    connection pool to the same Postgres instance, which is fine; they don't share
    transactions and never need to."""
    global _vectorstore
    if _vectorstore is None:
        connection = settings.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
        _vectorstore = PGVector(
            embeddings=get_embeddings(),
            connection=connection,
            collection_name=_COLLECTION_NAME,
            embedding_length=settings.embedding_dim,
            async_mode=True,
            use_jsonb=True,
        )
    return _vectorstore


@dataclass
class RetrievedFact:
    """Adapter shape so context.py doesn't need to know facts now come back as LangChain
    Documents (`.page_content`/`.metadata`) rather than `models.Fact` ORM rows."""

    id: str
    content: str
    category: str | None
    score: float


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_messages(messages: list[Message]) -> str:
    return "\n".join(f"{m.role}: {m.content}" for m in messages)


async def _count_assistant_messages(db: AsyncSession, session_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.count()).select_from(Message).where(
            Message.session_id == session_id, Message.role == "assistant"
        )
    )
    return result.scalar_one()


async def _get_last_n_messages(db: AsyncSession, session_id: uuid.UUID, n: int) -> list[Message]:
    result = await db.execute(
        select(Message).where(Message.session_id == session_id).order_by(Message.created_at.desc()).limit(n)
    )
    messages = list(result.scalars().all())
    messages.reverse()
    return messages


async def _find_closest_active_fact(user_id: str, content: str) -> tuple[str, str, dict, float] | None:
    """Returns (id, content, metadata, similarity) for the closest *active*
    (non-superseded) fact for this user, or None. `active` is an explicit bool in
    metadata rather than checking for `superseded_by`'s absence — simple-equality
    metadata filters are what PGVector's filter dict reliably supports; "key is
    null/missing" is not worth relying on. Content comes back as the document's own
    `page_content`, not a metadata field — there is no "content" key in metadata."""
    store = _get_vectorstore()
    results = await store.asimilarity_search_with_score(
        content, k=1, filter={"user_id": user_id, "active": True}
    )
    if not results:
        return None
    doc, distance = results[0]
    return doc.id, doc.page_content, doc.metadata, 1 - distance


async def _mark_superseded(fact_id: str, metadata: dict, content: str, superseded_by: str) -> None:
    """No in-place metadata update in this vectorstore's interface — emulate one via
    delete + re-add under the same id, so the row's identity (and thus any external
    reference to it) is preserved. Never a hard delete-and-forget: the old fact's content
    and embedding survive, just flagged inactive, same "soft-delete, audit trail always
    available" philosophy the original hand-rolled `facts` table used."""
    store = _get_vectorstore()
    updated = {**metadata, "active": False, "superseded_by": superseded_by, "updated_at": _now_iso()}
    await store.adelete(ids=[fact_id])
    await store.aadd_texts([content], metadatas=[updated], ids=[fact_id])


async def write_fact_with_dedup(user_id: str, candidate: dict, source_session_id: uuid.UUID) -> None:
    floor = sensitive_fact_confidence_floor(candidate.get("category"))
    if candidate.get("confidence", 0.0) < floor:
        return

    content = candidate["content"]
    closest = await _find_closest_active_fact(user_id, content)

    if closest is not None:
        existing_id, existing_content, existing_metadata, similarity = closest
        if similarity >= settings.dedup_threshold:
            return  # duplicate, skip

        if settings.fact_llm_check_floor <= similarity < settings.dedup_threshold:
            result: ContradictionVerdict = await _get_contradiction_chain().ainvoke({
                "existing": existing_content,
                "new": content,
            })
            if result.verdict == "same":
                return
            if result.verdict == "contradiction":
                new_id = str(uuid.uuid4())
                store = _get_vectorstore()
                now = _now_iso()
                await store.aadd_texts(
                    [content],
                    metadatas=[{
                        "user_id": user_id,
                        "category": candidate.get("category"),
                        "confidence": candidate["confidence"],
                        "source_session_id": str(source_session_id),
                        "active": True,
                        "superseded_by": None,
                        "created_at": now,
                        "updated_at": now,
                    }],
                    ids=[new_id],
                )
                await _mark_superseded(existing_id, existing_metadata, existing_content, new_id)
                return
            # "distinct" falls through to plain insert below

    store = _get_vectorstore()
    now = _now_iso()
    await store.aadd_texts(
        [content],
        metadatas=[{
            "user_id": user_id,
            "category": candidate.get("category"),
            "confidence": candidate["confidence"],
            "source_session_id": str(source_session_id),
            "active": True,
            "superseded_by": None,
            "created_at": now,
            "updated_at": now,
        }],
        ids=[str(uuid.uuid4())],
    )


async def maybe_extract_facts(db: AsyncSession, session_id: uuid.UUID, user_id: str) -> None:
    assistant_count = await _count_assistant_messages(db, session_id)
    if assistant_count == 0 or assistant_count % 5 != 0:
        return

    recent = await _get_last_n_messages(db, session_id, n=10)
    if not recent:
        return

    result: ExtractedFacts = await _get_extraction_chain().ainvoke({"chunk": _format_messages(recent)})
    for candidate in result.facts:
        await write_fact_with_dedup(user_id, candidate.model_dump(), source_session_id=session_id)


async def retrieve_facts(user_id: str, query_text: str | None) -> list[RetrievedFact]:
    if not query_text:
        return []

    store = _get_vectorstore()
    results = await store.asimilarity_search_with_score(
        query_text, k=settings.fact_top_k, filter={"user_id": user_id, "active": True}
    )
    facts = []
    for doc, distance in results:
        similarity = 1 - distance
        if similarity >= settings.fact_sim_floor:
            facts.append(RetrievedFact(
                id=doc.id, content=doc.page_content, category=doc.metadata.get("category"), score=similarity
            ))
    return facts
