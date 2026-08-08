import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class SessionCreate(BaseModel):
    title: str | None = None


class SessionUpdate(BaseModel):
    title: str


class SessionOut(BaseModel):
    id: uuid.UUID
    user_id: str
    title: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MessageOut(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    token_count: int
    truncated: bool
    feedback: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatRequest(BaseModel):
    message: str


class MessageFeedbackUpdate(BaseModel):
    feedback: Literal["like", "dislike"] | None


class MemoryFactOut(BaseModel):
    content: str
    category: str | None
    score: float


class BudgetSlot(BaseModel):
    used_tokens: int
    limit_tokens: int


class MemoryDebugOut(BaseModel):
    summary: str | None
    summary_version: int | None
    uncompressed_message_count: int
    dropped_message_count: int
    oldest_uncompressed_message_age_seconds: float | None
    oversized_new_message: bool
    budget: dict[str, BudgetSlot]
    injected_facts: list[MemoryFactOut]


class FactOut(BaseModel):
    id: uuid.UUID
    content: str
    category: str | None
    confidence: float
    source_session_id: uuid.UUID | None
    superseded_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
