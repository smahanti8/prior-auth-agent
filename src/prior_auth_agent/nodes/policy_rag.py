"""Policy RAG: retrieve applicable payer-policy chunks from ChromaDB."""

from ..state import PriorAuthState
from ..vectorstore.store import get_collection

TOP_K = 6


def policy_rag(state: PriorAuthState) -> PriorAuthState:
    collection = get_collection()
    cpt = state["cpt_code"]

    results = collection.query(
        query_texts=[f"Prior authorization medical necessity criteria for CPT {cpt}"],
        n_results=TOP_K,
    )

    chunks = [
        {"text": doc, "source": meta.get("source", "unknown"), "distance": dist}
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        )
    ]
    return {"policy_chunks": chunks}
