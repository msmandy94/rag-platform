FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/app/models_cache \
    SENTENCE_TRANSFORMERS_HOME=/app/models_cache

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install -e . --no-deps && pip install \
    "fastapi>=0.115" "uvicorn[standard]>=0.32" "pydantic>=2.9" "pydantic-settings>=2.6" \
    "asyncpg>=0.30" "qdrant-client>=1.12" "sentence-transformers>=3.3" \
    "groq>=0.13" "google-generativeai>=0.8" "pypdf>=5.1" "python-docx>=1.1" \
    "tenacity>=9.0" "typer>=0.13" "python-multipart>=0.0.20" "httpx>=0.28" "structlog>=24.4"

COPY app/ ./app/
COPY migrations/ ./migrations/

# Pre-download embedding model into the image so cold starts are fast
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-en-v1.5')"

# HF Spaces requires the app to listen on $PORT (default 7860).
# Worker runs as a background asyncio task inside the same process.
EXPOSE 7860
ENV PORT=7860
CMD ["python", "-m", "app.cli", "api"]
