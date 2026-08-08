from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=Path(__file__).parent / ".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://nxtchat:nxtchat@localhost:5432/nxtchat"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o"
    max_response_tokens: int = 2048

    # Phase 2 — budgeted context window
    recent_budget: int = 8000
    summary_budget: int = 1500
    system_prompt_budget: int = 500
    facts_budget: int = 500

    # Phase 3 — rolling summarization
    compress_trigger: float = 0.7
    keep_verbatim: int = 8
    compress_chunk_size: int = 15
    regen_every_n_versions: int = 5
    utility_model: str = "gpt-4o-mini"

    # Phase 4 — fact memory
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    fact_top_k: int = 5
    fact_sim_floor: float = 0.35
    dedup_threshold: float = 0.9
    fact_llm_check_floor: float = 0.75
    fact_confidence_floor: float = 0.5

    # Phase 5 — rate limiting (guards the endpoints that call the main chat model)
    rate_limit_max_requests: int = 10
    rate_limit_window_seconds: int = 300


settings = Settings()
