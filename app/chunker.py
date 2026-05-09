"""Recursive character-based chunker.

Simple, predictable, fast. For semantic chunking we'd swap in a sentence-window
or proposition-based strategy — see TRADEOFFS.md for the recall trade-off.
"""
from dataclasses import dataclass

DEFAULT_CHUNK_SIZE = 800
DEFAULT_OVERLAP = 100
SEPARATORS = ["\n\n", "\n", ". ", " ", ""]


@dataclass
class Chunk:
    index: int
    text: str


def chunk_text(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> list[Chunk]:
    if not text.strip():
        return []
    pieces = _recursive_split(text, chunk_size)
    chunks = _merge_with_overlap(pieces, chunk_size, overlap)
    return [Chunk(index=i, text=c) for i, c in enumerate(chunks) if c.strip()]


def _recursive_split(text: str, chunk_size: int) -> list[str]:
    if len(text) <= chunk_size:
        return [text]
    for sep in SEPARATORS:
        if sep == "":
            return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]
        if sep in text:
            parts = text.split(sep)
            out: list[str] = []
            for p in parts:
                if len(p) <= chunk_size:
                    out.append(p)
                else:
                    out.extend(_recursive_split(p, chunk_size))
            return [p + sep for p in out if p]
    return [text]


def _merge_with_overlap(pieces: list[str], chunk_size: int, overlap: int) -> list[str]:
    out: list[str] = []
    cur = ""
    for p in pieces:
        if len(cur) + len(p) <= chunk_size:
            cur += p
        else:
            if cur:
                out.append(cur)
            # carry overlap from end of previous chunk
            tail = cur[-overlap:] if overlap and cur else ""
            cur = tail + p
    if cur:
        out.append(cur)
    return out
