"""Retrieval evaluation metric types and computation.

Three metrics:
  recall@k          fraction of required passages found in top-k results
  MRR               mean reciprocal rank across required passages
  rank_inversion    an irrelevant chunk ranks above any required passage

All metrics are per-case and aggregated at suite level. known_gap cases
(policies not yet in the corpus) are excluded from suite aggregation but
included in the report so failures are documented rather than hidden.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PassageResult:
    passage_id: str
    text_fragment: str
    source: str
    criterion_id: str | None
    required: bool
    found_at_rank: int | None       # 1-indexed; None = not in top-k
    found_in_corpus: bool | None    # None = corpus scan not run
    chunking_failure: bool | None   # None = corpus scan not run
    labeling_error: bool | None     # None = corpus scan not run


@dataclass
class CaseResult:
    case_id: str
    cpt_code: str
    query: str
    k: int
    known_gap: bool
    retrieved_chunks: list[dict]    # [{text, source, distance, rank}]
    passage_results: list[PassageResult]
    expected_wrong_source: str | None = None  # for known-gap cases

    @property
    def required_passages(self) -> list[PassageResult]:
        return [p for p in self.passage_results if p.required]

    def recall_at(self, k: int) -> float:
        required = self.required_passages
        if not required:
            return 0.0  # 0 required passages and known_gap=True → not counted
        found = sum(1 for p in required if p.found_at_rank is not None and p.found_at_rank <= k)
        return found / len(required)

    @property
    def mrr(self) -> float:
        """Mean reciprocal rank over required passages."""
        required = self.required_passages
        if not required:
            return 0.0
        return sum(
            1.0 / p.found_at_rank if p.found_at_rank is not None else 0.0
            for p in required
        ) / len(required)

    @property
    def has_rank_inversion(self) -> bool:
        """True when an irrelevant chunk ranks above the best-ranked required passage.

        Rank inversion means retrieval is doing work — surfacing a correct passage —
        but burying it below noise. This is distinct from recall failure (passage absent).
        """
        correct_ranks = [
            p.found_at_rank for p in self.required_passages if p.found_at_rank is not None
        ]
        if not correct_ranks:
            return False
        best_correct = min(correct_ranks)
        if best_correct == 1:
            return False
        required_fragments = {p.text_fragment for p in self.required_passages}
        for chunk in self.retrieved_chunks:
            if chunk["rank"] < best_correct:
                if not any(frag in chunk["text"] for frag in required_fragments):
                    return True
        return False

    @property
    def wrong_source_confirmed(self) -> bool | None:
        """For known-gap cases: did the top result come from the expected wrong source?"""
        if not self.expected_wrong_source or not self.retrieved_chunks:
            return None
        return self.retrieved_chunks[0]["source"] == self.expected_wrong_source


@dataclass
class SuiteResult:
    k: int
    case_results: list[CaseResult]

    @property
    def scored_cases(self) -> list[CaseResult]:
        """Cases counted in aggregate metrics (excludes known_gap cases)."""
        return [c for c in self.case_results if not c.known_gap]

    @property
    def gap_cases(self) -> list[CaseResult]:
        return [c for c in self.case_results if c.known_gap]

    def _mean(self, values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    @property
    def mean_recall_at_1(self) -> float:
        return self._mean([c.recall_at(1) for c in self.scored_cases])

    @property
    def mean_recall_at_3(self) -> float:
        return self._mean([c.recall_at(3) for c in self.scored_cases])

    @property
    def mean_recall_at_k(self) -> float:
        return self._mean([c.recall_at(self.k) for c in self.scored_cases])

    @property
    def mean_mrr(self) -> float:
        return self._mean([c.mrr for c in self.scored_cases])

    @property
    def rank_inversion_rate(self) -> float:
        if not self.scored_cases:
            return 0.0
        return sum(1 for c in self.scored_cases if c.has_rank_inversion) / len(self.scored_cases)

    @property
    def chunking_failure_rate(self) -> float:
        """Fraction of not-found required passages attributable to chunk boundary splits.

        A chunking failure is when the text_fragment exists verbatim in the raw policy
        file but does not appear in any stored chunk — the ingestor split it at a boundary.
        Identifying this separates 'embedding model can't find it' from 'it was never
        stored in a retrievable form'.
        """
        all_failures = [
            p
            for c in self.scored_cases
            for p in c.required_passages
            if p.found_at_rank is None
        ]
        if not all_failures:
            return 0.0
        chunking = sum(1 for p in all_failures if p.chunking_failure is True)
        return chunking / len(all_failures)
