"""Local embeddings via sentence-transformers (BGE-small).

Loaded once at app startup. Inference is sync; we run in a thread to avoid
blocking the event loop. For prod we'd swap in a remote embedding service
(Voyage / OpenAI / Cohere) with the same interface — see TRADEOFFS.md.
"""
import asyncio
from typing import Iterable

from sentence_transformers import SentenceTransformer

from app.config import get_settings

_model: SentenceTransformer | None = None


def load_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(get_settings().EMBED_MODEL)
    return _model


async def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    model = load_model()
    # normalize_embeddings=True so cosine = dot, matches Qdrant cosine config.
    vectors = await asyncio.to_thread(
        model.encode,
        texts,
        normalize_embeddings=True,
        show_progress_bar=False,
        convert_to_numpy=True,
    )
    return [v.tolist() for v in vectors]


async def embed_query(text: str) -> list[float]:
    # BGE recommends a query prefix for retrieval — improves recall noticeably.
    prefixed = f"Represent this sentence for searching relevant passages: {text}"
    out = await embed_texts([prefixed])
    return out[0]
