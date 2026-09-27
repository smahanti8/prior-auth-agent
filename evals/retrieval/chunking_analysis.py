"""Classify retrieval failures: chunking boundary split vs ranking failure vs labeling error.

A passage not found in top-k has one of three causes:

  chunking_failure  The text_fragment exists verbatim in the raw policy file but
                    appears in no single stored chunk. The ingestor split it at a
                    paragraph boundary during ingestion. Fixing this requires
                    adjusting CHUNK_SIZE, CHUNK_OVERLAP, or splitting strategy.

  ranking_failure   The text_fragment is in a stored chunk, but the embedding
                    model did not surface that chunk in top-k for this query.
                    Fixing this requires query enrichment, a different embedding
                    model, or increasing TOP_K.

  labeling_error    The text_fragment is not in the raw policy file verbatim.
                    The labeled case is wrong (policy text was edited, or the
                    fragment was misquoted). Fix the label, not the pipeline.

  no_policy         The raw policy file does not exist in the corpus. The
                    procedure has no indexed policy document. This is a known
                    gap, not a pipeline failure.

The classification matters because the remediation is different for each cause.
Conflating them leads to wrong fixes — e.g., increasing TOP_K when the real
problem is a chunk boundary split.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from prior_auth_agent.config import POLICY_DIR


_CAUSE_CHUNKING = "chunking_failure"
_CAUSE_RANKING = "ranking_failure"
_CAUSE_LABELING = "labeling_error"
_CAUSE_NO_POLICY = "no_policy_in_corpus"


def classify_failure(
    text_fragment: str,
    source: str,
    collection: Any,
    policy_dir: Path = POLICY_DIR,
) -> dict:
    """Classify why a required passage was not found in top-k results.

    Returns:
      {
        "cause":             one of the _CAUSE_* constants above,
        "in_raw_file":       bool | None (None if file doesn't exist),
        "in_any_chunk":      bool,
        "chunking_failure":  bool,
        "ranking_failure":   bool,
        "labeling_error":    bool,
        "no_policy":         bool,
        "diagnosis":         str  (human-readable explanation),
      }
    """
    doc_path = policy_dir / source

    # ── Check raw policy file ──────────────────────────────────────────────────
    if not doc_path.exists():
        return {
            "cause": _CAUSE_NO_POLICY,
            "in_raw_file": None,
            "in_any_chunk": False,
            "chunking_failure": False,
            "ranking_failure": False,
            "labeling_error": False,
            "no_policy": True,
            "diagnosis": (
                f"No policy file found at {doc_path}. "
                f"CPT {source!r} has not been indexed into the corpus. "
                "Add the policy document to data/policies/ and re-ingest."
            ),
        }

    raw_text = doc_path.read_text(encoding="utf-8")
    in_raw_file = text_fragment in raw_text

    # ── Check every stored chunk for this source ───────────────────────────────
    result = collection.get(
        where={"source": source},
        include=["documents"],
    )
    in_any_chunk = any(
        text_fragment in chunk_text
        for chunk_text in result.get("documents", [])
    )

    # ── Classify ───────────────────────────────────────────────────────────────
    if not in_raw_file:
        return {
            "cause": _CAUSE_LABELING,
            "in_raw_file": False,
            "in_any_chunk": in_any_chunk,
            "chunking_failure": False,
            "ranking_failure": False,
            "labeling_error": True,
            "no_policy": False,
            "diagnosis": (
                f"Labeling error — text_fragment not found verbatim in {source}. "
                "The policy file may have been updated, or the fragment was misquoted. "
                "Fix the labeled case, not the pipeline."
            ),
        }

    if not in_any_chunk:
        return {
            "cause": _CAUSE_CHUNKING,
            "in_raw_file": True,
            "in_any_chunk": False,
            "chunking_failure": True,
            "ranking_failure": False,
            "labeling_error": False,
            "no_policy": False,
            "diagnosis": (
                f"Chunking failure — fragment exists in {source} but was split "
                "across a chunk boundary during ingestion. "
                "Consider reducing CHUNK_SIZE, increasing CHUNK_OVERLAP, or "
                "implementing section-aware splitting to keep policy criteria intact."
            ),
        }

    return {
        "cause": _CAUSE_RANKING,
        "in_raw_file": True,
        "in_any_chunk": True,
        "chunking_failure": False,
        "ranking_failure": True,
        "labeling_error": False,
        "no_policy": False,
        "diagnosis": (
            f"Ranking failure — fragment exists in a {source} chunk but was not "
            f"surfaced in top-k. The embedding model did not rank it highly for this query. "
            "Consider: query enrichment, a higher-capacity embedding model, or increasing TOP_K."
        ),
    }
