"""Tests for the policy-document chunker (vectorstore/ingest.py).

Regression suite for a real bug found 2026-08-02: chunk_text's step-forward
logic (`max(cut - CHUNK_OVERLAP, start + 1)`) falls back to advancing `start`
by exactly one character whenever the nearest paragraph break is found
repeatedly within the same overlap window. On lumbar_mri_72148.md this
produced ~190 near-duplicate, one-character-shifted chunks near the end of
the document (down to single-character fragments like 'nts', 'ts', 's').

This was not just wasteful — once more than one policy document existed in
the same ChromaDB collection, the dense cluster of near-identical embeddings
from the degenerate chunks dominated nearest-neighbor search for every query,
regardless of the actual CPT code. Retrieval returned lumbar_mri_72148.md as
the top result for all 5 test CPTs, including CPT 29881 (which has its own,
correctly-indexed policy). Confirmed via a live --live eval run whose
criterion-evidence scoring collapsed to 2/12 before this was diagnosed.
"""

from pathlib import Path

from prior_auth_agent.vectorstore.ingest import chunk_text, CHUNK_SIZE


# ── the pathological case ──────────────────────────────────────────────────────


def test_chunk_text_does_not_crawl_one_character_at_a_time():
    """A paragraph break sitting early in a chunk window must not pin `cut`
    and stall `start`'s advance to +1 per iteration.

    Constructed to reproduce the exact failure mode: a short paragraph
    ending in a blank line, followed by a long run of text with no further
    paragraph break until EOF. The break at position ~50 falls inside every
    window as `start` creeps forward, so `cut` stays pinned there.
    """
    text = ("A" * 50) + "\n\n" + ("B" * 1000)
    chunks = chunk_text(text)

    # Before the fix: ~51 chunks, most of them 1-50 char near-duplicate
    # prefixes of the "A" run. After the fix: 2 chunks (the "A" paragraph,
    # then the "B" run).
    assert len(chunks) <= 5, (
        f"expected a small, bounded number of chunks; got {len(chunks)} — "
        f"the chunker is crawling forward one character at a time"
    )
    assert all(len(c) >= 10 for c in chunks), (
        "no chunk should be a near-empty fragment; "
        f"shortest chunk was {min(len(c) for c in chunks)} chars"
    )


def test_chunk_text_no_duplicate_chunks_from_one_char_crawl():
    """The one-char crawl produces many chunks that are substrings of each
    other (near-duplicates). None of the returned chunks should be a strict
    substring of another — that's the signature of the crawl, not legitimate
    overlap (legitimate overlap chunks share a boundary region but each also
    contains new content beyond it)."""
    text = ("A" * 50) + "\n\n" + ("B" * 1000)
    chunks = chunk_text(text)

    for i, a in enumerate(chunks):
        for j, b in enumerate(chunks):
            if i != j and a in b:
                raise AssertionError(
                    f"chunk {i} ({a!r}) is a strict substring of chunk {j} "
                    f"({b!r}) — signature of the one-character crawl bug"
                )


# ── tied to the real production document that exposed this ────────────────────


def test_chunk_text_on_real_lumbar_policy_stays_reasonable():
    """lumbar_mri_72148.md (~1.5KB) produced 203 chunks before the fix —
    almost all near-duplicate one-character-shifted fragments near EOF.
    A document this size should chunk into single digits."""
    policy_path = (
        Path(__file__).resolve().parents[1]
        / "data" / "policies" / "lumbar_mri_72148.md"
    )
    text = policy_path.read_text()
    chunks = chunk_text(text)

    assert len(chunks) < 20, (
        f"lumbar_mri_72148.md ({len(text)} chars) produced {len(chunks)} "
        f"chunks — expected well under 20 for a document this size"
    )
    assert all(len(c) >= 20 for c in chunks), (
        "no chunk from a real policy document should be a near-empty "
        f"fragment; shortest was {min(len(c) for c in chunks)} chars"
    )


# ── normal, well-formed chunking must still work ───────────────────────────────


def test_chunk_text_splits_long_document_at_chunk_size():
    """A long, well-formed multi-paragraph document should still split into
    multiple chunks of roughly CHUNK_SIZE, with real overlap between them —
    the fix must not break the intended behavior for normal documents."""
    paragraphs = [f"Paragraph {i}. " + ("word " * 100) for i in range(10)]
    text = "\n\n".join(paragraphs)
    assert len(text) > CHUNK_SIZE * 2  # ensure this actually needs multiple chunks

    chunks = chunk_text(text)

    assert len(chunks) >= 2
    # every chunk except conceivably the last should be a meaningful size,
    # not a tiny fragment
    for c in chunks[:-1]:
        assert len(c) > CHUNK_SIZE // 4


def test_chunk_text_short_document_single_chunk():
    """A document shorter than CHUNK_SIZE should come back as one chunk."""
    text = "A short policy document.\n\nWith two paragraphs."
    chunks = chunk_text(text)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_chunk_text_empty_document():
    assert chunk_text("") == []
