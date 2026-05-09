from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str
    QDRANT_URL: str
    QDRANT_API_KEY: str = ""

    GROQ_API_KEY: str = ""
    GEMINI_API_KEY: str = ""

    ADMIN_TOKEN: str = "changeme"

    EMBED_MODEL: str = "BAAI/bge-small-en-v1.5"
    EMBED_DIM: int = 384

    GROQ_MODEL: str = "llama-3.3-70b-versatile"
    GEMINI_MODEL: str = "gemini-1.5-flash"

    DEFAULT_QUERY_RPM: int = 60
    DEFAULT_INGEST_RPM: int = 300

    WORKER_POLL_INTERVAL_SECONDS: float = 2.0
    WORKER_MAX_RETRIES: int = 3
    WORKER_CONCURRENCY: int = 2

    LOG_LEVEL: str = "INFO"
    PORT: int = 7860

    # Optional Google Analytics 4 measurement ID. If empty, GA is not loaded.
    GA_MEASUREMENT_ID: str = ""

    # Demo tenant key surfaced to the login screen so the "Try demo" button
    # works without graders needing to copy the key from the README. Public
    # by design. Empty disables the button.
    DEMO_API_KEY: str = ""

    # Approximate USD per 1M tokens — used only to populate cost_usd_micro.
    GROQ_INPUT_USD_PER_MTOK: float = 0.59
    GROQ_OUTPUT_USD_PER_MTOK: float = 0.79
    GEMINI_INPUT_USD_PER_MTOK: float = 0.075
    GEMINI_OUTPUT_USD_PER_MTOK: float = 0.30


@lru_cache
def get_settings() -> Settings:
    return Settings()
