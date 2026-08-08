from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .db import init_db
from .routers import chat, facts, memory, sessions


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="NxtChat API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Has-More"],
)

app.include_router(sessions.router)
app.include_router(chat.router)
app.include_router(memory.router)
app.include_router(facts.router)


@app.get("/health")
async def health():
    return {"status": "ok"}
