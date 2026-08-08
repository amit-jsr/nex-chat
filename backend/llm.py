import asyncio
from typing import AsyncIterator

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from .config import settings

_chat_model: ChatOpenAI | None = None
_utility_model: ChatOpenAI | None = None
_utility_model_json: ChatOpenAI | None = None
_embeddings: OpenAIEmbeddings | None = None


def _require_key() -> str:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    return settings.openai_api_key


def _get_chat_model() -> ChatOpenAI:
    global _chat_model
    if _chat_model is None:
        _chat_model = ChatOpenAI(
            api_key=_require_key(),
            model=settings.openai_model,
            max_tokens=settings.max_response_tokens,
            streaming=True,
        )
    return _chat_model


def _get_utility_model(*, json_mode: bool) -> ChatOpenAI:
    """Separate cached instances for plain vs. JSON-mode, rather than rebuilding a
    ChatOpenAI (and its underlying client) on every call — `.bind()` returns a new
    RunnableBinding cheaply, but the base model itself is worth reusing."""
    global _utility_model, _utility_model_json
    if _utility_model is None:
        _utility_model = ChatOpenAI(api_key=_require_key(), model=settings.utility_model)
    if json_mode:
        global _utility_model_json
        if _utility_model_json is None:
            _utility_model_json = _utility_model.bind(response_format={"type": "json_object"})
        return _utility_model_json
    return _utility_model


def get_utility_model() -> ChatOpenAI:
    """Public — for building real LCEL chains (`prompt | model | parser`) elsewhere, e.g.
    the summarization chains in memory/summarizer.py, rather than only going through the
    string-in-string-out complete_utility() wrapper."""
    return _get_utility_model(json_mode=False)


def get_embeddings() -> OpenAIEmbeddings:
    """Public (unlike the other _get_* helpers here) — memory/facts.py's PGVector
    instance needs the actual Embeddings object, not just an embed-one-string function."""
    global _embeddings
    if _embeddings is None:
        _embeddings = OpenAIEmbeddings(api_key=_require_key(), model=settings.embedding_model)
    return _embeddings


async def stream_chat(
    system: str, messages: list[dict], cancel_event: asyncio.Event | None = None
) -> AsyncIterator[str]:
    """Yields text deltas from a chat completion via LangChain's ChatOpenAI.

    If `cancel_event` gets set mid-stream, explicitly closes the underlying async
    generator (`aclose()`) rather than just `break`-ing out of the loop — `astream()`
    wraps the OpenAI SDK's own streaming HTTP response, and an abandoned-but-not-closed
    async generator isn't guaranteed to release that connection (and stop billed
    generation) promptly on its own. `aclose()` runs the generator's internal cleanup
    (which closes the underlying stream) synchronously, matching the raw-SDK version's
    explicit `stream.close()` this replaced.
    """
    model = _get_chat_model()
    lc_messages: list[BaseMessage] = [SystemMessage(content=system)]
    for m in messages:
        # `messages` only ever carries "user"/"assistant" roles here — the system prompt
        # is always the separate `system` param above, never part of this list.
        lc_messages.append(HumanMessage(content=m["content"]) if m["role"] == "user" else AIMessage(content=m["content"]))
    stream = model.astream(lc_messages)
    try:
        async for chunk in stream:
            if cancel_event is not None and cancel_event.is_set():
                break
            if chunk.content:
                yield chunk.content
    finally:
        await stream.aclose()


async def complete_utility(system: str, user: str, *, json_mode: bool = False) -> str:
    """Single-shot completion on the cheap utility model — summarizer, fact extractor,
    contradiction-checker. Kept separate from stream_chat, which always uses the main
    chat model."""
    model = _get_utility_model(json_mode=json_mode)
    response = await model.ainvoke([SystemMessage(content=system), HumanMessage(content=user)])
    return response.content or ""
