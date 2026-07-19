"""ChromaDB setup: persistent client + payer-policy collection."""

from functools import lru_cache

import chromadb

from ..config import CHROMA_DIR, POLICY_COLLECTION


@lru_cache(maxsize=1)
def get_client() -> chromadb.ClientAPI:
    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(CHROMA_DIR))


def get_collection() -> chromadb.Collection:
    # Uses Chroma's default embedding function (all-MiniLM-L6-v2, local).
    # Swap via the embedding_function parameter if you need a hosted embedder.
    return get_client().get_or_create_collection(
        name=POLICY_COLLECTION,
        metadata={"hnsw:space": "cosine"},
    )
