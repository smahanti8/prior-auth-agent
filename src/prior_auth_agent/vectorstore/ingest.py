"""Index payer policy documents from data/policies/ into ChromaDB.

Usage: python -m prior_auth_agent.vectorstore.ingest
"""

from pathlib import Path

from ..config import POLICY_DIR
from .store import get_collection

CHUNK_SIZE = 1200
CHUNK_OVERLAP = 200


def chunk_text(text: str) -> list[str]:
    """Paragraph-aware fixed-size chunking with overlap."""
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + CHUNK_SIZE
        # try to break at a paragraph boundary
        cut = text.rfind("\n\n", start, end)
        if cut <= start:
            cut = end
        chunks.append(text[start:cut].strip())
        start = max(cut - CHUNK_OVERLAP, start + 1)
        if cut >= len(text):
            break
    return [c for c in chunks if c]


def ingest(policy_dir: Path = POLICY_DIR) -> int:
    collection = get_collection()
    total = 0

    for path in sorted(policy_dir.glob("*")):
        if path.suffix.lower() not in {".md", ".txt"}:
            continue
        text = path.read_text()
        chunks = chunk_text(text)
        if not chunks:
            continue
        collection.upsert(
            ids=[f"{path.stem}-{i}" for i in range(len(chunks))],
            documents=chunks,
            metadatas=[{"source": path.name, "chunk": i} for i in range(len(chunks))],
        )
        print(f"indexed {path.name}: {len(chunks)} chunks")
        total += len(chunks)

    if total == 0:
        print(f"no .md/.txt policy documents found in {policy_dir}")
    return total


if __name__ == "__main__":
    ingest()
