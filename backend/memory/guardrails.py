from openai import AsyncOpenAI

from ..config import settings

SENSITIVE_CATEGORIES = {"health", "financial", "legal"}

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        _client = AsyncOpenAI(api_key=settings.openai_api_key)
    return _client


async def check_moderation(text: str) -> bool:
    response = await _get_client().moderations.create(input=text)
    return response.results[0].flagged


def sensitive_fact_confidence_floor(category: str | None) -> float:
    if category in SENSITIVE_CATEGORIES:
        return max(settings.fact_confidence_floor, settings.sensitive_fact_confidence_floor)
    return settings.fact_confidence_floor
