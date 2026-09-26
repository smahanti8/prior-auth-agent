"""MCP server exposing the payer policy corpus as read-only retrieval tools.

Four tools — all read-only, all include a citation field:
  search_policies      — semantic search over ChromaDB (primary entry point)
  get_policy_chunk     — fetch a chunk by stable ID + adjacent chunk IDs
  get_policy_document  — full document text (explicit opt-in; costs more context)
  get_criteria         — encoded CriterionSpecs for a CPT code

Deliberately excluded — see README § MCP Server for rationale:
  run_determination    — the pipeline is not invokable via MCP
  evaluate_predicate   — predicate evaluation against a FHIR bundle is a
                         determination step, not a retrieval step
  submit_bundle        — FHIR patient data cannot enter via MCP
  get_audit_log        — append-only log is operator access only

Run:  python -m prior_auth_agent.mcp_server
"""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import POLICY_DIR
from .policy.registry import load_by_cpt
from .policy.schema import PredicateNode
from .vectorstore.store import get_collection

mcp = FastMCP("prior-auth-policy")

# ── Chunk-ID helpers ──────────────────────────────────────────────────────────
# Public chunk ID:     "{source}#{chunk_index}"  e.g. "SURG-041-rotator-cuff.md#3"
# ChromaDB internal:  "{stem}-{index}"           e.g. "SURG-041-rotator-cuff-3"
# The public form is stable across re-ingestions; the internal form is derived.


def _chunk_id(source: str, chunk_index: int) -> str:
    return f"{source}#{chunk_index}"


def _chroma_id(source: str, chunk_index: int) -> str:
    stem = source.rsplit(".", 1)[0]
    return f"{stem}-{chunk_index}"


def _parse_chunk_id(chunk_id: str) -> tuple[str, int]:
    """Parse a public chunk ID → (source, chunk_index). Raises ValueError on bad format."""
    if "#" not in chunk_id:
        raise ValueError(
            f"Invalid chunk_id {chunk_id!r} — expected 'policy-filename.md#N'"
        )
    source, _, idx_str = chunk_id.rpartition("#")
    try:
        return source, int(idx_str)
    except ValueError:
        raise ValueError(
            f"Invalid chunk_id {chunk_id!r} — index part must be an integer"
        )


# ── Predicate rendering ───────────────────────────────────────────────────────


def _render_predicate(node: PredicateNode) -> str:
    """Deterministic human-readable rendering of a PredicateNode tree.

    No paraphrasing — each operator produces a canonical string form.
    Callers that need to evaluate the predicate should use predicate_tree
    (the raw model_dump) rather than parsing this string.
    """
    if node.all_of is not None:
        return "(" + " AND ".join(_render_predicate(c) for c in node.all_of) + ")"
    if node.any_of is not None:
        return "(" + " OR ".join(_render_predicate(c) for c in node.any_of) + ")"
    if node.resource_exists is not None:
        return f"resource_exists({node.resource_exists.resource_type})"
    if node.finding_present is not None:
        p = node.finding_present
        pos = "[" + ", ".join(f"'{t}'" for t in p.affirmative_terms) + "]"
        s = f"finding_present({p.resource_type}, affirmative={pos}"
        if p.contradicting_terms:
            neg = "[" + ", ".join(f"'{t}'" for t in p.contradicting_terms) + "]"
            s += f", contradicting={neg}"
        return s + ")"
    if node.field_documented is not None:
        p = node.field_documented
        return f"field_documented({p.resource_type}.{p.field_path})"
    if node.minimum_duration_months is not None:
        p = node.minimum_duration_months
        return (
            f"minimum_duration_months({p.resource_type}, "
            f"{p.start_path}→{p.end_path}, min={p.min_months}mo)"
        )
    return "unknown_operator"  # unreachable if PredicateNode's validator holds


# ── Section extraction ────────────────────────────────────────────────────────


def _extract_section(text: str) -> str:
    """Return the first markdown heading found in the chunk text, or 'unknown'."""
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return "unknown"


# ── ChromaDB helpers ──────────────────────────────────────────────────────────


def _get_doc_chunks(collection: Any, source: str) -> list[dict]:
    """Return all stored chunks for source, sorted by chunk index."""
    result = collection.get(
        where={"source": source},
        include=["metadatas"],
    )
    if not result["ids"]:
        return []
    chunks = [
        {"chroma_id": cid, "chunk_index": meta["chunk"]}
        for cid, meta in zip(result["ids"], result["metadatas"])
    ]
    return sorted(chunks, key=lambda c: c["chunk_index"])


# ── Tool implementations (dependency-injectable for tests) ────────────────────
#
# Each public function is a thin FastMCP wrapper around a private _impl function
# that accepts an injectable `collection` parameter. Tests call the _impl
# directly with a MockCollection — no ChromaDB process required.


def _search_policies(
    query: str,
    cpt_code: str | None,
    n_results: int,
    collection: Any = None,
) -> dict:
    n_results = max(1, min(n_results, 20))
    col = collection if collection is not None else get_collection()

    # CPT code enriches the query rather than acting as a strict where-filter.
    # ChromaDB's metadata filter operators do not support substring match, and
    # coupling this path to the criteria registry would turn a retrieval call
    # into a determination-adjacent operation. For CPT-exact lookup, use
    # get_criteria which does an authoritative encoded-registry lookup.
    effective_query = f"CPT {cpt_code}: {query}" if cpt_code else query

    results = col.query(
        query_texts=[effective_query],
        n_results=n_results,
        include=["documents", "metadatas", "distances"],
    )
    docs = results["documents"][0]
    metas = results["metadatas"][0]
    distances = results["distances"][0]

    items = []
    for text, meta, dist in zip(docs, metas, distances):
        source = meta["source"]
        chunk_index = meta["chunk"]
        cid = _chunk_id(source, chunk_index)
        items.append({
            "text": text,
            "source": source,
            "chunk_index": chunk_index,
            "distance": round(dist, 4),
            "chunk_id": cid,
            "citation": cid,
        })

    return {
        "results": items,
        "query": query,
        "cpt_code_filter": cpt_code,
        "retrieved_at": datetime.datetime.utcnow().isoformat() + "Z",
    }


def _get_policy_chunk(
    chunk_id: str,
    collection: Any = None,
) -> dict:
    source, chunk_index = _parse_chunk_id(chunk_id)
    col = collection if collection is not None else get_collection()
    chroma_id = _chroma_id(source, chunk_index)

    result = col.get(ids=[chroma_id], include=["documents", "metadatas"])
    if not result["ids"]:
        raise ValueError(
            f"Chunk {chunk_id!r} not found in policy corpus. "
            "Use search_policies to discover valid chunk IDs."
        )

    text = result["documents"][0]
    section = _extract_section(text)

    all_chunks = _get_doc_chunks(col, source)
    max_index = max((c["chunk_index"] for c in all_chunks), default=chunk_index)

    prev_id = _chunk_id(source, chunk_index - 1) if chunk_index > 0 else None
    next_id = _chunk_id(source, chunk_index + 1) if chunk_index < max_index else None

    return {
        "chunk_id": chunk_id,
        "text": text,
        "source": source,
        "section": section,
        "chunk_index": chunk_index,
        "prev_chunk_id": prev_id,
        "next_chunk_id": next_id,
        "citation": f"{source} § {section} #{chunk_index}",
    }


def _get_policy_document(
    policy_id: str,
    collection: Any = None,
    policy_dir: Path = POLICY_DIR,
) -> dict:
    doc_path = policy_dir / policy_id
    if not doc_path.exists():
        raise ValueError(
            f"Policy document {policy_id!r} not found. "
            "Use search_policies to discover valid policy IDs."
        )

    text = doc_path.read_text(encoding="utf-8")
    col = collection if collection is not None else get_collection()
    all_chunks = _get_doc_chunks(col, policy_id)
    chunk_ids = [_chunk_id(policy_id, c["chunk_index"]) for c in all_chunks]

    return {
        "policy_id": policy_id,
        "text": text,
        "word_count": len(text.split()),
        "chunk_count": len(chunk_ids),
        "chunk_ids": chunk_ids,
        "citation": policy_id,
    }


def _get_criteria(
    cpt_code: str,
    as_of_date: str | None,
    criteria_dir: Path | None = None,
) -> dict:
    if as_of_date is None:
        as_of_date = datetime.date.today().isoformat()

    kwargs: dict[str, Any] = {"cpt_code": cpt_code, "as_of_date": as_of_date}
    if criteria_dir is not None:
        kwargs["criteria_dir"] = criteria_dir

    specs = load_by_cpt(**kwargs)

    unencoded_note: str | None = None
    if not specs:
        unencoded_note = (
            f"No encoded criteria for CPT {cpt_code} as of {as_of_date}. "
            "Policy corpus is available — use search_policies for unstructured retrieval."
        )

    criteria = []
    for spec in specs:
        citation = (
            f"{spec.policy_ref.file} § {spec.policy_ref.section} "
            f"[{spec.criterion_id} v{spec.version}]"
        )
        criteria.append({
            "criterion_id": spec.criterion_id,
            "version": spec.version,
            "required": spec.required,
            "criterion_text": spec.criterion_text,
            "policy_ref": {
                "document": spec.policy_ref.file,
                "section": spec.policy_ref.section,
                "text_fragment": spec.policy_ref.text_fragment,
            },
            "predicate_summary": _render_predicate(spec.predicate),
            "predicate_tree": spec.predicate.model_dump(exclude_none=True),
            "citation": citation,
            "notes": spec.notes or None,
        })

    return {
        "cpt_code": cpt_code,
        "as_of_date": as_of_date,
        "criteria": criteria,
        "encoded_count": len(criteria),
        "unencoded_note": unencoded_note,
        "retrieved_at": datetime.datetime.utcnow().isoformat() + "Z",
    }


# ── MCP tool registrations ────────────────────────────────────────────────────


@mcp.tool()
def search_policies(
    query: str,
    cpt_code: str | None = None,
    n_results: int = 5,
) -> dict:
    """Semantic search over the payer policy corpus.

    Returns ranked text chunks with source citation. Every result includes
    a chunk_id that can be passed to get_policy_chunk to expand context.

    cpt_code enriches the query text but is not a strict filter — ChromaDB
    metadata does not carry CPT tags. For an authoritative CPT lookup, call
    get_criteria which uses the encoded-criteria registry.
    """
    return _search_policies(query, cpt_code, n_results)


@mcp.tool()
def get_policy_chunk(chunk_id: str) -> dict:
    """Fetch a specific policy chunk by its stable chunk ID.

    chunk_id is the 'chunk_id' field returned by search_policies,
    formatted as 'policy-filename.md#N'. Returns the chunk text,
    section heading, and adjacent chunk IDs for deliberate context
    expansion. Full document text is available via get_policy_document.
    """
    return _get_policy_chunk(chunk_id)


@mcp.tool()
def get_policy_document(policy_id: str) -> dict:
    """Fetch the full text of a payer policy document.

    policy_id is the 'source' field returned by search_policies
    (e.g. 'SURG-041-rotator-cuff.md'). Use this only when full-document
    context is genuinely required — prefer get_policy_chunk for
    retrieval-bounded use cases. Returns chunk_ids so callers can see
    what retrieval would have surfaced versus what the document contains.
    """
    return _get_policy_document(policy_id)


@mcp.tool()
def get_criteria(
    cpt_code: str,
    as_of_date: str | None = None,
) -> dict:
    """Return encoded prior-auth criteria for a CPT code as of a given date.

    Each criterion includes its policy reference, version, required flag,
    a human-readable predicate_summary, and the raw predicate_tree for
    programmatic use. as_of_date defaults to today (ISO format YYYY-MM-DD).

    Does NOT evaluate criteria against a patient chart — this is a policy
    retrieval tool, not a determination tool. unencoded_note is non-null
    when no YAML specs are encoded for the requested CPT code.
    """
    return _get_criteria(cpt_code, as_of_date)


if __name__ == "__main__":
    mcp.run()
