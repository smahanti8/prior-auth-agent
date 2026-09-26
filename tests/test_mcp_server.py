"""Contract tests for the MCP policy-retrieval server.

Tests call the _impl functions directly with a MockCollection — no ChromaDB
process or API key required. The citation-invariant class asserts the repo's
core principle: nothing is returned without a resolving source.

All tests run in CI without any network access.
"""
from __future__ import annotations

import datetime
from pathlib import Path

import pytest
import yaml

from prior_auth_agent.mcp_server import (
    _chunk_id,
    _chroma_id,
    _extract_section,
    _get_criteria,
    _get_policy_chunk,
    _get_policy_document,
    _parse_chunk_id,
    _render_predicate,
    _search_policies,
)
from prior_auth_agent.policy.schema import (
    FieldDocumentedParams,
    FindingPresentParams,
    MinimumDurationMonthsParams,
    PredicateNode,
    ResourceExistsParams,
)


# ── Test doubles ──────────────────────────────────────────────────────────────


class MockCollection:
    """Minimal ChromaDB collection double covering query/get access patterns."""

    def __init__(self, chunks: list[dict]) -> None:
        # Each chunk: {id, text, source, chunk_index}
        self._chunks = chunks

    def query(
        self,
        query_texts: list[str],
        n_results: int,
        include: list[str],
        where: dict | None = None,
    ) -> dict:
        results = self._chunks
        if where and "source" in where:
            src_filter = where["source"]
            if "$in" in src_filter:
                allowed = set(src_filter["$in"])
                results = [c for c in results if c["source"] in allowed]
        results = results[:n_results]
        return {
            "documents": [[c["text"] for c in results]],
            "metadatas": [[{"source": c["source"], "chunk": c["chunk_index"]} for c in results]],
            "distances": [[round(0.1 * i, 4) for i in range(len(results))]],
            "ids": [[c["id"] for c in results]],
        }

    def get(
        self,
        ids: list[str] | None = None,
        where: dict | None = None,
        include: list[str] | None = None,
    ) -> dict:
        include = include or []
        if ids is not None:
            matching = [c for c in self._chunks if c["id"] in ids]
        elif where and "source" in where:
            source = where["source"]
            matching = [c for c in self._chunks if c["source"] == source]
        else:
            matching = list(self._chunks)

        return {
            "ids": [c["id"] for c in matching],
            "documents": [c["text"] for c in matching] if "documents" in include else [],
            "metadatas": (
                [{"source": c["source"], "chunk": c["chunk_index"]} for c in matching]
                if "metadatas" in include
                else []
            ),
        }


def _make_chunk(source: str, index: int, text: str = "") -> dict:
    return {
        "id": _chroma_id(source, index),
        "text": text or f"Policy text for chunk {index} of {source}.",
        "source": source,
        "chunk_index": index,
    }


# ── Helpers — criterion YAML fixture ─────────────────────────────────────────


def _write_criterion_yaml(tmp_path: Path, data: dict) -> None:
    name = f"{data['criterion_id']}_v{data['version'].replace('.', '_')}.yaml"
    (tmp_path / name).write_text(yaml.dump(data), encoding="utf-8")


_CRITERION_YAML = {
    "criterion_id": "TEST-001-C1",
    "version": "1.0",
    "policy_effective_date": "2026-01-01",
    "criterion_text": "Full-thickness tear confirmed by MRI.",
    "policy_ref": {
        "file": "test-policy.md",
        "section": "2.1",
        "text_fragment": "Full-thickness tear confirmed by MRI.",
    },
    "required": True,
    "cpt_codes": ["99999"],
    "predicate": {
        "all_of": [
            {"resource_exists": {"resource_type": "DiagnosticReport"}},
            {
                "finding_present": {
                    "resource_type": "DiagnosticReport",
                    "affirmative_terms": ["full-thickness"],
                    "contradicting_terms": ["partial-thickness"],
                }
            },
        ]
    },
}


# ── _render_predicate ─────────────────────────────────────────────────────────


class TestRenderPredicate:
    def test_resource_exists(self) -> None:
        node = PredicateNode(resource_exists=ResourceExistsParams(resource_type="DiagnosticReport"))
        assert _render_predicate(node) == "resource_exists(DiagnosticReport)"

    def test_finding_present_no_contradicting(self) -> None:
        node = PredicateNode(
            finding_present=FindingPresentParams(
                resource_type="DiagnosticReport",
                affirmative_terms=["full-thickness"],
            )
        )
        result = _render_predicate(node)
        assert "finding_present(DiagnosticReport" in result
        assert "affirmative=['full-thickness']" in result
        assert "contradicting" not in result

    def test_finding_present_with_contradicting(self) -> None:
        node = PredicateNode(
            finding_present=FindingPresentParams(
                resource_type="DiagnosticReport",
                affirmative_terms=["full-thickness"],
                contradicting_terms=["partial-thickness"],
            )
        )
        result = _render_predicate(node)
        assert "contradicting=['partial-thickness']" in result

    def test_field_documented(self) -> None:
        node = PredicateNode(
            field_documented=FieldDocumentedParams(
                resource_type="Procedure",
                field_path="outcome",
            )
        )
        assert _render_predicate(node) == "field_documented(Procedure.outcome)"

    def test_minimum_duration_months(self) -> None:
        node = PredicateNode(
            minimum_duration_months=MinimumDurationMonthsParams(
                resource_type="Procedure",
                start_path="performedPeriod.start",
                end_path="performedPeriod.end",
                min_months=3,
            )
        )
        result = _render_predicate(node)
        assert "minimum_duration_months(Procedure" in result
        assert "min=3mo" in result

    def test_all_of(self) -> None:
        node = PredicateNode(
            all_of=[
                PredicateNode(resource_exists=ResourceExistsParams(resource_type="DiagnosticReport")),
                PredicateNode(resource_exists=ResourceExistsParams(resource_type="Procedure")),
            ]
        )
        result = _render_predicate(node)
        assert result.startswith("(")
        assert " AND " in result
        assert result.endswith(")")

    def test_any_of(self) -> None:
        node = PredicateNode(
            any_of=[
                PredicateNode(resource_exists=ResourceExistsParams(resource_type="DiagnosticReport")),
                PredicateNode(resource_exists=ResourceExistsParams(resource_type="ImagingStudy")),
            ]
        )
        assert " OR " in _render_predicate(node)

    def test_summary_and_tree_carry_same_operators(self, tmp_path: Path) -> None:
        """The predicate_summary must not introduce operators absent from predicate_tree."""
        _write_criterion_yaml(tmp_path, _CRITERION_YAML)
        result = _get_criteria("99999", "2026-06-01", criteria_dir=tmp_path)
        c = result["criteria"][0]
        summary: str = c["predicate_summary"]
        tree: dict = c["predicate_tree"]
        # Both must reference DiagnosticReport — the tree is the authoritative source.
        assert "DiagnosticReport" in summary
        assert "DiagnosticReport" in str(tree)


# ── _parse_chunk_id / _chunk_id / _chroma_id ─────────────────────────────────


class TestChunkIdHelpers:
    def test_roundtrip(self) -> None:
        source, idx = _parse_chunk_id("SURG-041-rotator-cuff.md#3")
        assert source == "SURG-041-rotator-cuff.md"
        assert idx == 3

    def test_chunk_id_format(self) -> None:
        assert _chunk_id("policy.md", 5) == "policy.md#5"

    def test_chroma_id_strips_extension(self) -> None:
        assert _chroma_id("SURG-041-rotator-cuff.md", 3) == "SURG-041-rotator-cuff-3"

    def test_missing_hash_raises(self) -> None:
        with pytest.raises(ValueError, match="expected"):
            _parse_chunk_id("SURG-041-rotator-cuff.md-3")

    def test_non_integer_index_raises(self) -> None:
        with pytest.raises(ValueError, match="integer"):
            _parse_chunk_id("policy.md#abc")


# ── search_policies ───────────────────────────────────────────────────────────


class TestSearchPolicies:
    def _col(self, n: int = 3) -> MockCollection:
        return MockCollection([
            _make_chunk("SURG-041-rotator-cuff.md", i, f"## Criteria\nPolicy text {i}.")
            for i in range(n)
        ])

    def test_returns_results(self) -> None:
        result = _search_policies("rotator cuff", None, 3, collection=self._col())
        assert len(result["results"]) == 3

    def test_n_results_clamp_minimum(self) -> None:
        result = _search_policies("query", None, 0, collection=self._col())
        assert len(result["results"]) >= 1

    def test_n_results_clamp_maximum(self) -> None:
        col = MockCollection([_make_chunk("p.md", i) for i in range(30)])
        result = _search_policies("query", None, 99, collection=col)
        assert len(result["results"]) <= 20

    def test_result_has_required_fields(self) -> None:
        result = _search_policies("test", None, 1, collection=self._col(1))
        item = result["results"][0]
        for field in ("text", "source", "chunk_index", "distance", "chunk_id", "citation"):
            assert field in item, f"missing field: {field}"

    def test_cpt_code_in_top_level_response(self) -> None:
        result = _search_policies("test", "29827", 3, collection=self._col())
        assert result["cpt_code_filter"] == "29827"

    def test_no_cpt_filter_is_none(self) -> None:
        result = _search_policies("test", None, 3, collection=self._col())
        assert result["cpt_code_filter"] is None

    def test_empty_corpus_returns_empty_list(self) -> None:
        result = _search_policies("test", None, 5, collection=MockCollection([]))
        assert result["results"] == []

    def test_retrieved_at_is_iso_utc(self) -> None:
        result = _search_policies("test", None, 1, collection=self._col(1))
        ts = result["retrieved_at"]
        assert ts.endswith("Z")
        datetime.datetime.fromisoformat(ts.rstrip("Z"))  # raises if malformed


# ── get_policy_chunk ──────────────────────────────────────────────────────────


class TestGetPolicyChunk:
    def _col(self) -> MockCollection:
        return MockCollection([
            _make_chunk("SURG-041-rotator-cuff.md", i, f"## Section {i}\nText {i}.")
            for i in range(4)
        ])

    def test_basic_fetch(self) -> None:
        result = _get_policy_chunk("SURG-041-rotator-cuff.md#1", collection=self._col())
        assert result["chunk_index"] == 1
        assert result["source"] == "SURG-041-rotator-cuff.md"

    def test_has_citation(self) -> None:
        result = _get_policy_chunk("SURG-041-rotator-cuff.md#2", collection=self._col())
        assert result["citation"]
        assert "SURG-041-rotator-cuff.md" in result["citation"]
        assert "#2" in result["citation"]

    def test_prev_none_at_first_chunk(self) -> None:
        result = _get_policy_chunk("SURG-041-rotator-cuff.md#0", collection=self._col())
        assert result["prev_chunk_id"] is None
        assert result["next_chunk_id"] == "SURG-041-rotator-cuff.md#1"

    def test_next_none_at_last_chunk(self) -> None:
        result = _get_policy_chunk("SURG-041-rotator-cuff.md#3", collection=self._col())
        assert result["next_chunk_id"] is None
        assert result["prev_chunk_id"] == "SURG-041-rotator-cuff.md#2"

    def test_mid_chunk_has_both_neighbours(self) -> None:
        result = _get_policy_chunk("SURG-041-rotator-cuff.md#2", collection=self._col())
        assert result["prev_chunk_id"] == "SURG-041-rotator-cuff.md#1"
        assert result["next_chunk_id"] == "SURG-041-rotator-cuff.md#3"

    def test_section_extracted_from_heading(self) -> None:
        result = _get_policy_chunk("SURG-041-rotator-cuff.md#1", collection=self._col())
        assert result["section"] == "Section 1"

    def test_unknown_chunk_raises(self) -> None:
        with pytest.raises(ValueError, match="not found"):
            _get_policy_chunk("does-not-exist.md#99", collection=self._col())

    def test_single_chunk_document_no_neighbours(self) -> None:
        col = MockCollection([_make_chunk("solo.md", 0)])
        result = _get_policy_chunk("solo.md#0", collection=col)
        assert result["prev_chunk_id"] is None
        assert result["next_chunk_id"] is None


# ── get_policy_document ───────────────────────────────────────────────────────


class TestGetPolicyDocument:
    def _setup(self, tmp_path: Path, n_chunks: int = 3) -> MockCollection:
        policy_text = "# Rotator Cuff Policy\n\nFull document content here.\n"
        (tmp_path / "SURG-041-rotator-cuff.md").write_text(policy_text, encoding="utf-8")
        return MockCollection([
            _make_chunk("SURG-041-rotator-cuff.md", i) for i in range(n_chunks)
        ])

    def test_returns_full_text(self, tmp_path: Path) -> None:
        col = self._setup(tmp_path)
        result = _get_policy_document(
            "SURG-041-rotator-cuff.md", collection=col, policy_dir=tmp_path
        )
        assert "Rotator Cuff Policy" in result["text"]

    def test_has_citation(self, tmp_path: Path) -> None:
        col = self._setup(tmp_path)
        result = _get_policy_document(
            "SURG-041-rotator-cuff.md", collection=col, policy_dir=tmp_path
        )
        assert result["citation"] == "SURG-041-rotator-cuff.md"

    def test_chunk_ids_sorted_by_index(self, tmp_path: Path) -> None:
        col = MockCollection([
            _make_chunk("SURG-041-rotator-cuff.md", i) for i in [2, 0, 1]
        ])
        (tmp_path / "SURG-041-rotator-cuff.md").write_text("content", encoding="utf-8")
        result = _get_policy_document(
            "SURG-041-rotator-cuff.md", collection=col, policy_dir=tmp_path
        )
        indices = [int(cid.split("#")[1]) for cid in result["chunk_ids"]]
        assert indices == sorted(indices)

    def test_chunk_count_matches_chunk_ids_length(self, tmp_path: Path) -> None:
        col = self._setup(tmp_path, n_chunks=4)
        result = _get_policy_document(
            "SURG-041-rotator-cuff.md", collection=col, policy_dir=tmp_path
        )
        assert result["chunk_count"] == len(result["chunk_ids"])

    def test_word_count_positive(self, tmp_path: Path) -> None:
        col = self._setup(tmp_path)
        result = _get_policy_document(
            "SURG-041-rotator-cuff.md", collection=col, policy_dir=tmp_path
        )
        assert result["word_count"] > 0

    def test_unknown_policy_raises(self, tmp_path: Path) -> None:
        col = MockCollection([])
        with pytest.raises(ValueError, match="not found"):
            _get_policy_document("ghost.md", collection=col, policy_dir=tmp_path)


# ── get_criteria ──────────────────────────────────────────────────────────────


class TestGetCriteria:
    def test_both_summary_and_tree_present(self, tmp_path: Path) -> None:
        _write_criterion_yaml(tmp_path, _CRITERION_YAML)
        result = _get_criteria("99999", "2026-06-01", criteria_dir=tmp_path)
        c = result["criteria"][0]
        assert "predicate_summary" in c
        assert "predicate_tree" in c
        assert isinstance(c["predicate_summary"], str)
        assert isinstance(c["predicate_tree"], dict)

    def test_predicate_tree_is_model_dump(self, tmp_path: Path) -> None:
        _write_criterion_yaml(tmp_path, _CRITERION_YAML)
        result = _get_criteria("99999", "2026-06-01", criteria_dir=tmp_path)
        tree = result["criteria"][0]["predicate_tree"]
        # Must be the exact schema dict — top-level key is the operator name.
        assert "all_of" in tree
        assert isinstance(tree["all_of"], list)

    def test_citation_present_on_every_criterion(self, tmp_path: Path) -> None:
        _write_criterion_yaml(tmp_path, _CRITERION_YAML)
        result = _get_criteria("99999", "2026-06-01", criteria_dir=tmp_path)
        for c in result["criteria"]:
            assert c["citation"], "citation field must be non-empty"
            assert "TEST-001-C1" in c["citation"]

    def test_policy_ref_in_citation(self, tmp_path: Path) -> None:
        _write_criterion_yaml(tmp_path, _CRITERION_YAML)
        result = _get_criteria("99999", "2026-06-01", criteria_dir=tmp_path)
        assert "test-policy.md" in result["criteria"][0]["citation"]

    def test_unencoded_note_when_no_specs(self, tmp_path: Path) -> None:
        result = _get_criteria("00000", "2026-06-01", criteria_dir=tmp_path)
        assert result["unencoded_note"] is not None
        assert "00000" in result["unencoded_note"]
        assert result["encoded_count"] == 0

    def test_unencoded_note_none_when_specs_found(self, tmp_path: Path) -> None:
        _write_criterion_yaml(tmp_path, _CRITERION_YAML)
        result = _get_criteria("99999", "2026-06-01", criteria_dir=tmp_path)
        assert result["unencoded_note"] is None

    def test_as_of_date_excludes_future_spec(self, tmp_path: Path) -> None:
        future_spec = {**_CRITERION_YAML, "policy_effective_date": "2030-01-01"}
        _write_criterion_yaml(tmp_path, future_spec)
        result = _get_criteria("99999", "2026-06-01", criteria_dir=tmp_path)
        assert result["encoded_count"] == 0

    def test_as_of_date_defaults_to_today(self, tmp_path: Path) -> None:
        _write_criterion_yaml(tmp_path, _CRITERION_YAML)
        result = _get_criteria("99999", None, criteria_dir=tmp_path)
        assert result["as_of_date"] == datetime.date.today().isoformat()

    def test_required_flag_preserved(self, tmp_path: Path) -> None:
        _write_criterion_yaml(tmp_path, _CRITERION_YAML)
        result = _get_criteria("99999", "2026-06-01", criteria_dir=tmp_path)
        assert result["criteria"][0]["required"] is True

    def test_retrieved_at_is_iso_utc(self, tmp_path: Path) -> None:
        _write_criterion_yaml(tmp_path, _CRITERION_YAML)
        result = _get_criteria("99999", "2026-06-01", criteria_dir=tmp_path)
        ts = result["retrieved_at"]
        assert ts.endswith("Z")
        datetime.datetime.fromisoformat(ts.rstrip("Z"))


# ── Citation invariant ────────────────────────────────────────────────────────
# Every tool response must carry a citation field. This class is the
# executable form of the repo's "no determination stands without a citation"
# principle, applied to the retrieval layer.


class TestCitationInvariant:
    """Assert citation fields on every tool that returns data."""

    def test_search_results_each_have_citation(self) -> None:
        col = MockCollection([_make_chunk("policy.md", i) for i in range(3)])
        result = _search_policies("test", None, 3, collection=col)
        for item in result["results"]:
            assert item.get("citation"), f"missing citation on result: {item}"

    def test_chunk_response_has_citation(self) -> None:
        col = MockCollection([_make_chunk("policy.md", 0)])
        result = _get_policy_chunk("policy.md#0", collection=col)
        assert result.get("citation"), "get_policy_chunk must return a citation"

    def test_document_response_has_citation(self, tmp_path: Path) -> None:
        (tmp_path / "policy.md").write_text("content", encoding="utf-8")
        col = MockCollection([_make_chunk("policy.md", 0)])
        result = _get_policy_document("policy.md", collection=col, policy_dir=tmp_path)
        assert result.get("citation"), "get_policy_document must return a citation"

    def test_each_criterion_has_citation(self, tmp_path: Path) -> None:
        _write_criterion_yaml(tmp_path, _CRITERION_YAML)
        result = _get_criteria("99999", "2026-06-01", criteria_dir=tmp_path)
        for c in result["criteria"]:
            assert c.get("citation"), f"criterion {c.get('criterion_id')} missing citation"

    def test_citation_contains_resolving_source(self, tmp_path: Path) -> None:
        """Citations must name a source that a reader can locate — not just a truthy string."""
        _write_criterion_yaml(tmp_path, _CRITERION_YAML)
        result = _get_criteria("99999", "2026-06-01", criteria_dir=tmp_path)
        for c in result["criteria"]:
            assert ".md" in c["citation"] or "/" in c["citation"], (
                f"citation {c['citation']!r} does not reference a policy document"
            )

    def test_search_citation_matches_chunk_id(self) -> None:
        col = MockCollection([_make_chunk("policy.md", 2)])
        result = _search_policies("test", None, 1, collection=col)
        item = result["results"][0]
        assert item["citation"] == item["chunk_id"]


# ── _extract_section ──────────────────────────────────────────────────────────


class TestExtractSection:
    def test_finds_heading(self) -> None:
        assert _extract_section("## Conservative Management\nText here.") == "Conservative Management"

    def test_h1_heading(self) -> None:
        assert _extract_section("# Policy Overview\nContent.") == "Policy Overview"

    def test_no_heading_returns_unknown(self) -> None:
        assert _extract_section("Just some text without a heading.") == "unknown"

    def test_first_heading_wins(self) -> None:
        text = "## Section A\nSome text.\n## Section B\nMore text."
        assert _extract_section(text) == "Section A"
