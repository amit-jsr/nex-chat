import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..schemas import FactOut

router = APIRouter(tags=["facts"])

# Facts now live in LangChain's own tables (langchain_pg_embedding/langchain_pg_collection,
# see memory/facts.py) rather than a hand-rolled `facts` table — but "list everything active
# for this user" is a plain filtered listing, not a similarity search, and PGVector's own
# interface has no method for that (every search method wants a query vector). Raw SQL
# against the underlying tables is the correct tool here, not a fake vector search.
_LIST_FACTS_SQL = text("""
    SELECT e.id, e.document, e.cmetadata
    FROM langchain_pg_embedding e
    JOIN langchain_pg_collection c ON c.uuid = e.collection_id
    WHERE c.name = 'nxtchat_facts'
      AND e.cmetadata ->> 'user_id' = :user_id
      AND e.cmetadata ->> 'active' = 'true'
    ORDER BY e.cmetadata ->> 'created_at' DESC
""")

_DELETE_FACT_SQL = text("""
    DELETE FROM langchain_pg_embedding e
    USING langchain_pg_collection c
    WHERE c.uuid = e.collection_id
      AND c.name = 'nxtchat_facts'
      AND e.id = :fact_id
      AND e.cmetadata ->> 'user_id' = :user_id
    RETURNING e.id
""")


@router.get("/users/{user_id}/facts", response_model=list[FactOut])
async def list_facts(
    user_id: str,
    caller_id: str = Header(..., alias="X-User-Id"),
    db: AsyncSession = Depends(get_db),
):
    if user_id != caller_id:
        raise HTTPException(status_code=404, detail="Not found")
    result = await db.execute(_LIST_FACTS_SQL, {"user_id": user_id})
    return [
        FactOut(
            id=row.id,
            content=row.document,
            category=row.cmetadata.get("category"),
            confidence=row.cmetadata.get("confidence", 0.0),
            source_session_id=row.cmetadata.get("source_session_id"),
            superseded_by=row.cmetadata.get("superseded_by"),
            created_at=row.cmetadata["created_at"],
            updated_at=row.cmetadata["updated_at"],
        )
        for row in result.all()
    ]


@router.delete("/facts/{fact_id}", status_code=204)
async def delete_fact(
    fact_id: uuid.UUID,
    user_id: str = Header(..., alias="X-User-Id"),
    db: AsyncSession = Depends(get_db),
):
    """Same 404-not-403 ownership pattern as get_owned_session — scoped by user_id in the
    query itself (not a separate lookup-then-check), so a cross-user delete just deletes
    nothing rather than confirming the id exists."""
    result = await db.execute(_DELETE_FACT_SQL, {"fact_id": str(fact_id), "user_id": user_id})
    deleted = result.first()
    await db.commit()
    if deleted is None:
        raise HTTPException(status_code=404, detail="Fact not found")
